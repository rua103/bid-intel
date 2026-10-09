"""Concurrent-safe parse and model-response cache instrumentation.

Cache entries are disposable optimizations. Their keys include source/config/code
identity, and readers validate complete payloads before treating them as hits.
"""
from __future__ import annotations

import hashlib
import json
import os
import tempfile
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any
from uuid import uuid4

from filelock import FileLock
from pydantic import ValidationError


def _sha256(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _file_sha256(path: Path) -> str:
    try:
        with path.open('rb') as stream:
            return hashlib.file_digest(stream, 'sha256').hexdigest()
    except OSError:
        return 'unavailable'


def _atomic_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, pending = tempfile.mkstemp(prefix=f'.{path.name}.', suffix='.pending', dir=path.parent)
    try:
        with os.fdopen(fd, 'w', encoding='utf-8', newline='\n') as stream:
            json.dump(value, stream, ensure_ascii=False, indent=2, allow_nan=False)
            stream.write('\n')
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(pending, path)
        if os.name != 'nt':
            directory = os.open(path.parent, os.O_RDONLY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
    finally:
        Path(pending).unlink(missing_ok=True)


def _read_cache(path: Path, validate) -> Any | None:
    if not path.is_file():
        return None
    try:
        value = json.loads(path.read_text(encoding='utf-8'))
        return value if validate(value) else None
    except (OSError, ValueError, TypeError, KeyError, ValidationError):
        return None


def _is_model_cache(value: Any) -> bool:
    if not isinstance(value, dict):
        return False
    if not all(isinstance(value.get(name), dict) for name in ('metadata',)):
        return False
    if not all(isinstance(value.get(name), list) for name in ('items', 'participants', 'warnings', 'model_calls')):
        return False
    from app.schemas import ItemCandidate, NoticeMetadata, ParticipantCandidate

    NoticeMetadata.model_validate(value['metadata'])
    for item in value['items']:
        ItemCandidate.model_validate(item)
    for person in value['participants']:
        ParticipantCandidate.model_validate(person)
    return all(isinstance(row, dict) for row in value['model_calls'])


def _is_parse_cache(value: Any) -> bool:
    if not isinstance(value, dict) or not isinstance(value.get('text'), str):
        return False
    if not all(isinstance(value.get(name), list)
               for name in ('items', 'participants', 'warnings')):
        return False
    from app.schemas import ItemCandidate, ParticipantCandidate

    for item in value['items']:
        ItemCandidate.model_validate(item)
    for person in value['participants']:
        ParticipantCandidate.model_validate(person)
    return all(isinstance(warning, str) for warning in value['warnings'])


def _safe_counter_write(path: Path | None, counters: dict[str, Any]) -> None:
    if path is not None:
        # Telemetry deliberately excludes inputs, prompts, endpoint credentials and
        # generated content. This receipt survives a worker dying after an API call.
        _atomic_json(path, {
            'model_calls': counters['model_calls'],
            'historical_model_calls': counters['historical_model_calls'],
            'provider_kv_tokens': counters['provider_kv_tokens'],
        })


@contextmanager
def instrumented_notice_cache(
    cache_root: Path,
    mode: str,
    model_settings,
    limits: dict[str, Any],
    stop_path: Path | None = None,
    telemetry_path: Path | None = None,
    cache_parsing: bool = True,
):
    """Patch ingestion locally while yielding per-attempt cache/API counters.

    ``model_calls`` contains only HTTP requests sent during this attempt. Responses
    restored from disk are counted under ``historical_model_calls`` instead.
    """
    from app import ingestion, model_adapter
    from app.bounded_runtime import SchedulingStopped, current_runtime
    from app.parsers import SourceDocument
    from app.parsers import __file__ as parser_module_path
    from app.schemas import (
        ItemCandidate,
        NoticeMetadata,
        ParticipantCandidate,
    )

    cache_root = Path(cache_root)
    parse_root = cache_root / 'parse-cache'
    response_root = cache_root / 'model-cache' / mode
    parse_root.mkdir(parents=True, exist_ok=True)
    response_root.mkdir(parents=True, exist_ok=True)
    original_parse = ingestion.parse_document_with_participants
    original_model = ingestion.extract_unstructured_items
    original_stream = model_adapter._stream_completion
    counters: dict[str, Any] = {
        'parse_cache_hits': 0, 'parse_cache_misses': 0, 'parse_seconds': 0.0,
        'model_cache_hits': 0, 'model_cache_misses': 0, 'model_seconds': 0.0,
        'model_calls': [], 'historical_model_calls': [], 'provider_kv_tokens': 0,
    }
    parser_code = (_file_sha256(Path(ingestion.__file__))
                   + _file_sha256(Path(parser_module_path)))
    extractor_code = _file_sha256(Path(model_adapter.__file__))

    def save_telemetry():
        _safe_counter_write(Path(telemetry_path) if telemetry_path is not None else None, counters)

    def cached_parse(document, **options):
        limiter, runtime_stop = current_runtime()
        with limiter.document_slot(stop_path=stop_path or runtime_stop):
            return parse_under_lease(document, options)

    def parse_under_lease(document, options):
        content = document.content
        suffix = Path(document.filename).suffix.lower()
        identity = {
            'version': 4, 'parser_sha256': parser_code, 'suffix': suffix,
            'options': options, 'limits': limits,
        }
        key = _sha256(json.dumps(identity, sort_keys=True, ensure_ascii=False,
                                 default=str).encode() + b'\0' + content)
        path = parse_root / f'{key}.json'
        with FileLock(str(path.with_suffix('.lock')), timeout=1200):
            stored = _read_cache(path, _is_parse_cache)
            if stored is None:
                counters['parse_cache_misses'] += 1
                if stop_path is not None and Path(stop_path).exists():
                    raise SchedulingStopped('用户请求暂停；停止新的文档解析')
                started = time.perf_counter()
                text, items, participants, warnings = original_parse(
                    SourceDocument('document' + suffix, content), **options,
                )
                counters['parse_seconds'] += time.perf_counter() - started
                stored = {
                    'text': text,
                    'items': [row.model_dump(mode='json') for row in items],
                    'participants': [row.model_dump(mode='json') for row in participants],
                    'warnings': warnings,
                }
                if _is_parse_cache(stored) and not any(
                    any(marker in warning for marker in ('失败', '超时', '未安装', '缺少'))
                    for warning in warnings
                ):
                    _atomic_json(path, stored)
            else:
                counters['parse_cache_hits'] += 1
        items = [ItemCandidate.model_validate(row).model_copy(
            update={'source_file': document.filename}) for row in stored['items']]
        participants = [ParticipantCandidate.model_validate(row).model_copy(
            update={'source_file': document.filename}) for row in stored['participants']]
        warnings = [warning.replace('document' + suffix, document.filename)
                    for warning in stored['warnings']]
        return stored['text'], items, participants, warnings

    def measured_stream(*args, **kwargs):
        started = time.perf_counter()
        request = {
            'call_id': uuid4().hex,
            'elapsed_seconds': None,
            'prompt_tokens': None,
            'completion_tokens': None,
            'provider_kv_tokens': 0,
            'transport': 'failed',
        }
        def request_started():
            counters['model_calls'].append(request)
            save_telemetry()

        try:
            content, usage = original_stream(*args, **kwargs, on_request_started=request_started)
            request['transport'] = 'success'
            for field in ('prompt_tokens', 'completion_tokens'):
                value = usage.get(field)
                if isinstance(value, (int, float)) and not isinstance(value, bool):
                    request[field] = int(value)
            details = usage.get('prompt_tokens_details')
            cached_tokens = 0
            if isinstance(details, dict):
                cached_tokens = details.get('cached_tokens', details.get('cache_read_input_tokens', 0))
            else:
                details = {}
            detail_count = (
                int(cached_tokens)
                if isinstance(cached_tokens, (int, float)) and not isinstance(cached_tokens, bool)
                else 0
            )
            provider_count = usage.get('prompt_cache_hit_tokens')
            provider_count = (
                int(provider_count)
                if isinstance(provider_count, (int, float))
                and not isinstance(provider_count, bool) else 0
            )
            cached_tokens = max(detail_count, provider_count)
            if cached_tokens:
                request['provider_kv_tokens'] = cached_tokens
                counters['provider_kv_tokens'] += cached_tokens
            return content, usage
        except Exception as exc:
            if isinstance(exc, SchedulingStopped):
                # Admission was rejected before a transport started; do not
                # report this as a request or consume the notice API budget.
                usage = model_adapter._usage.get()
                if usage is not None and usage.requests:
                    usage.requests -= 1
            else:
                request['error_type'] = type(exc).__name__
            raise
        finally:
            elapsed = time.perf_counter() - started
            if request in counters['model_calls']:
                request['elapsed_seconds'] = round(elapsed, 4)
                counters['model_seconds'] += elapsed
            save_telemetry()

    def cached_model(*, filename, text, settings, include_participants=False):
        # Never persist a credential. Hash it into a cache identity so changing
        # accounts cannot reuse another account's responses.
        identity = {
            'version': 2, 'extractor_sha256': extractor_code, 'mode': mode,
            'filename': filename,
            'model': settings.model_name,
            'endpoint_sha256': _sha256(settings.model_base_url.rstrip('/').encode()),
            'api_key_sha256': _sha256(settings.model_api_key.encode()),
            'max_chars': settings.model_max_chars,
            'max_output_tokens': settings.model_max_output_tokens,
            'timeout_seconds': settings.model_timeout_seconds,
            'stream_total_seconds': settings.model_stream_total_seconds,
            'disable_thinking': settings.model_disable_thinking,
            'include_participants': include_participants,
        }
        key = _sha256(json.dumps(identity, sort_keys=True, ensure_ascii=False).encode()
                      + b'\0' + text.encode('utf-8'))
        path = response_root / f'{key}.json'
        with FileLock(str(path.with_suffix('.lock')), timeout=1200):
            cached = _read_cache(path, _is_model_cache)
            if cached is not None:
                counters['model_cache_hits'] += 1
                counters['historical_model_calls'].extend(cached['model_calls'])
                return (
                    NoticeMetadata.model_validate(cached['metadata']),
                    [ItemCandidate.model_validate(row) for row in cached['items']],
                    [ParticipantCandidate.model_validate(row) for row in cached['participants']],
                    list(cached['warnings']),
                )
            counters['model_cache_misses'] += 1
            call_index = len(counters['model_calls'])
            result = original_model(filename=filename, text=text, settings=settings,
                                    include_participants=include_participants)
            new_calls = counters['model_calls'][call_index:]
            if not any(marker in warning for warning in result[3] for marker in (
                '模型抽取失败', '模型返回空内容', '实验模型调用额度已用完',
                '未配置模型', '模型名称不符合赛题要求',
            )):
                _atomic_json(path, {
                    'metadata': result[0].model_dump(mode='json'),
                    'items': [row.model_dump(mode='json') for row in result[1]],
                    'participants': [row.model_dump(mode='json') for row in result[2]],
                    'warnings': result[3],
                    'model_calls': new_calls,
                })
            return result

    if cache_parsing:
        ingestion.parse_document_with_participants = cached_parse
    ingestion.extract_unstructured_items = cached_model
    model_adapter._stream_completion = measured_stream
    try:
        yield counters
    finally:
        if cache_parsing:
            ingestion.parse_document_with_participants = original_parse
        ingestion.extract_unstructured_items = original_model
        model_adapter._stream_completion = original_stream
        save_telemetry()
