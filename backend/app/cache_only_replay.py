"""Fail-closed helpers for local replay of historical extraction responses.

This module is intentionally separate from production ingestion/cache code. A
missing or invalid cache entry raises ``CacheMiss`` (a ``BaseException`` so the
production model adapter cannot turn it into an ordinary extraction warning).
No helper in this module writes a cache entry or makes a network request.
"""

from __future__ import annotations

import hashlib
import json
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from pydantic import ValidationError


class CacheMiss(BaseException):
    """A requested cache entry was absent, corrupt, or structurally invalid."""


class ModelTransportBlocked(BaseException):
    """Raised if replay code reaches a model transport function."""


def _read_required_json(path: Path, kind: str) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError) as exc:
        # Do not leak the source checkout's absolute path into logs or reports.
        raise ValueError(f"{kind} cache miss or invalid entry: {path.name}") from exc
    if not isinstance(value, dict):
        raise TypeError(f"{kind} cache miss or invalid entry: {path.name}")
    return value


def load_parse_cache(
    cache_root: Path,
    *,
    filename: str,
    suffix: str,
    content: bytes,
    options: dict[str, Any],
    limits: dict[str, Any],
    counters: dict[str, int],
):
    """Read a historical route-evaluation parse entry without recomputation."""
    from app.schemas import ItemCandidate, ParticipantCandidate

    identity = {
        "version": 3,
        "suffix": suffix,
        "options": options,
        "limits": limits,
    }
    identity_bytes = json.dumps(identity, sort_keys=True).encode("utf-8")
    key = hashlib.sha256(identity_bytes + content).hexdigest()
    path = Path(cache_root) / "parse-cache" / f"{key}.json"
    try:
        value = _read_required_json(path, "parse")
    except (ValueError, TypeError) as exc:
        counters["parse_cache_misses"] += 1
        raise CacheMiss(f"parse cache miss or invalid entry for source: {filename}") from exc
    if (
        not isinstance(value.get("text"), str)
        or not isinstance(value.get("items"), list)
        or not isinstance(value.get("participants"), list)
        or not isinstance(value.get("warnings"), list)
        or not all(isinstance(warning, str) for warning in value["warnings"])
    ):
        counters["parse_cache_misses"] += 1
        raise CacheMiss(f"parse cache miss or invalid entry for source: {filename}")
    try:
        items = [
            ItemCandidate.model_validate(row).model_copy(update={"source_file": filename})
            for row in value["items"]
        ]
        participants = [
            ParticipantCandidate.model_validate(row).model_copy(update={"source_file": filename})
            for row in value["participants"]
        ]
    except (ValueError, TypeError, ValidationError) as exc:
        counters["parse_cache_misses"] += 1
        raise CacheMiss(f"parse cache miss or invalid entry for source: {filename}") from exc
    counters["parse_cache_hits"] += 1
    return value["text"], items, participants, list(value["warnings"])


def load_model_cache(
    cache_root: Path,
    *,
    mode: str,
    filename: str,
    text: str,
    settings,
    include_participants: bool,
    counters: dict[str, int],
):
    """Restore one cached model result; a miss never reaches the model adapter."""
    from app.schemas import ItemCandidate, NoticeMetadata, ParticipantCandidate

    identity = {
        "version": 1,
        "mode": mode,
        "filename": filename,
        "model": settings.model_name,
        "max_chars": settings.model_max_chars,
        "max_output_tokens": settings.model_max_output_tokens,
        "disable_thinking": settings.model_disable_thinking,
        "include_participants": include_participants,
    }
    encoded_identity = json.dumps(identity, sort_keys=True).encode("utf-8")
    key = hashlib.sha256(encoded_identity + text.encode("utf-8")).hexdigest()
    path = Path(cache_root) / "model-cache" / mode / f"{key}.json"
    try:
        value = _read_required_json(path, "model")
        if (
            not isinstance(value.get("metadata"), dict)
            or not isinstance(value.get("items"), list)
            or not isinstance(value.get("participants"), list)
            or not isinstance(value.get("warnings"), list)
            or not isinstance(value.get("model_calls"), list)
            or not all(isinstance(warning, str) for warning in value["warnings"])
        ):
            raise ValueError("invalid cached model result shape")
        metadata = NoticeMetadata.model_validate(value["metadata"])
        items = [
            ItemCandidate.model_validate(row).model_copy(update={"source_file": filename})
            for row in value["items"]
        ]
        participants = [
            ParticipantCandidate.model_validate(row).model_copy(update={"source_file": filename})
            for row in value["participants"]
        ]
    except (OSError, ValueError, TypeError, KeyError, ValidationError) as exc:
        counters["model_cache_misses"] += 1
        raise CacheMiss(f"model cache miss or invalid entry for source: {filename}") from exc
    counters["model_cache_hits"] += 1
    counters["historical_cached_calls"] += len(value["model_calls"])
    counters["cached_items"] += len(items)
    counters["cached_participants"] += len(participants)
    return metadata, items, participants, list(value["warnings"])


@contextmanager
def block_model_transports():
    """Hard-block both model adapter transports during a cache-only replay."""
    from app import model_adapter

    original_sync = model_adapter._stream_completion
    original_async = model_adapter.async_stream_completion
    calls = {"sync": 0, "async": 0}

    def blocked_sync(*_args, **_kwargs):
        calls["sync"] += 1
        raise ModelTransportBlocked("model transport reached during cache-only replay")

    async def blocked_async(*_args, **_kwargs):
        calls["async"] += 1
        raise ModelTransportBlocked("async model transport reached during cache-only replay")

    model_adapter._stream_completion = blocked_sync
    model_adapter.async_stream_completion = blocked_async
    try:
        yield calls
    finally:
        model_adapter._stream_completion = original_sync
        model_adapter.async_stream_completion = original_async
