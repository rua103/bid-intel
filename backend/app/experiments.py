"""Reproducible extraction-mode comparisons over the development corpus.

This module reports extraction coverage and differences. Without a hand-labeled
gold set, its numbers are not accuracy, precision, or recall.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import tempfile
import time
from collections.abc import Iterable
from contextlib import suppress
from pathlib import Path
from typing import Any

from app.config import effective_settings
from app.ingestion import extract_notice
from app.model_adapter import model_call_budget
from app.parsers import SourceDocument
from app.schemas import ImportResult

VALID_MODES = ("rules", "model", "hybrid")


def _result_summary(result: ImportResult, *, elapsed_seconds: float) -> dict[str, Any]:
    return {
        "notice_id": result.notice_id,
        "source_files": result.source_files,
        "items_found": result.items_found,
        "participants_found": len(result.participants),
        "warnings": list(result.warnings),
        "elapsed_seconds": round(elapsed_seconds, 4),
        "item_fields": {
            field: sum(getattr(item, field) not in (None, "") for item in result.items)
            for field in ("product_name", "category", "brand", "model", "quantity", "unit_price", "total_price")
        },
        "result": result.model_dump(mode="json"),
    }


def compare_extraction_modes(
    notices: Iterable[list[SourceDocument]],
    *,
    modes: Iterable[str] = VALID_MODES,
    model_settings=None,
    max_model_calls: int = 6,
) -> dict[str, Any]:
    modes = tuple(dict.fromkeys(modes))
    invalid = [mode for mode in modes if mode not in VALID_MODES]
    if invalid:
        raise ValueError(f"Unsupported extraction mode(s): {', '.join(invalid)}")
    notices = list(notices)
    model_settings = model_settings or effective_settings()
    started = time.perf_counter()
    by_mode: dict[str, list[dict[str, Any]]] = {mode: [] for mode in modes}
    # Rules is deterministic; model and hybrid are bounded by one shared
    # context so a comparison cannot unexpectedly spend on every corpus item.
    with model_call_budget(max_model_calls) as usage:
        for mode in modes:
            for files in notices:
                item_started = time.perf_counter()
                result = extract_notice(files, extraction_mode=mode, model_settings=model_settings)
                by_mode[mode].append(_result_summary(result, elapsed_seconds=time.perf_counter() - item_started))
    mode_totals = {}
    for mode, rows in by_mode.items():
        mode_totals[mode] = {
            "notices": len(rows),
            "items_found": sum(row["items_found"] for row in rows),
            "participants_found": sum(row["participants_found"] for row in rows),
            "warnings": sum(len(row["warnings"]) for row in rows),
        }
    return {
        "schema_version": 1,
        "purpose": "development_mode_comparison_not_accuracy_evaluation",
        "modes": list(modes),
        "notice_count": len(notices),
        "model_name": model_settings.model_name if any(mode != "rules" for mode in modes) else None,
        "elapsed_seconds": round(time.perf_counter() - started, 4),
        "model_usage": {
            "requests": usage.requests,
            "successful_responses": usage.successful_responses,
            "prompt_tokens": usage.prompt_tokens,
            "completion_tokens": usage.completion_tokens,
            "max_calls": usage.max_calls,
            "exhausted": usage.exhausted,
        },
        "totals": mode_totals,
        "notices": by_mode,
    }


def load_manifest_notices(manifest_path: Path, *, limit: int = 3) -> list[list[SourceDocument]]:
    manifest_path = Path(manifest_path)
    payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    root = manifest_path.parent
    notices: list[list[SourceDocument]] = []
    for notice in payload.get("notices", [])[:limit]:
        files: list[SourceDocument] = []
        for file_info in notice.get("files", []):
            path = (root / file_info["path"]).resolve()
            if not path.is_relative_to(root.resolve()):
                raise ValueError("Corpus manifest path must stay within its directory")
            content = path.read_bytes()
            if hashlib.sha256(content).hexdigest() != file_info["sha256"]:
                raise ValueError(f"Corpus file hash mismatch: {path.name}")
            files.append(SourceDocument(path.name, content))
        if files:
            notices.append(files)
    return notices


def _write_report(path: Path, payload: str) -> None:
    """Replace ``path`` atomically so an interrupted write cannot leave a half report."""
    path.parent.mkdir(parents=True, exist_ok=True)
    handle, temp_name = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(handle, "w", encoding="utf-8", newline="\n") as stream:
            stream.write(payload + "\n")
        os.replace(temp_name, path)
    except BaseException:
        with suppress(OSError):
            os.unlink(temp_name)
        raise


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("manifest", type=Path)
    parser.add_argument("--limit", type=int, default=3)
    parser.add_argument("--max-model-calls", type=int, default=6)
    parser.add_argument("--mode", action="append", choices=VALID_MODES)
    parser.add_argument("--output", type=Path)
    parser.add_argument(
        "--force",
        action="store_true",
        help="覆盖已存在的 --output 文件；默认拒绝，以免上一次的对比结果被覆盖",
    )
    args = parser.parse_args()
    # Decide before spending anything: the comparison below is the expensive half
    # (real model calls, minutes of wall clock) and the write is the half that fails
    # predictably. Checking after the run throws away work that has already been paid
    # for -- the failure mode this guard exists to prevent.
    if args.output and args.output.exists() and not args.force:
        parser.error(
            f"输出文件已存在，已提前终止：{args.output}\n"
            "本次运行会真实调用模型并消耗额度，因此不在跑完之后才失败。"
            "需要覆盖请加 --force，或改用其他 --output 路径。"
        )
    notices = load_manifest_notices(args.manifest, limit=max(1, min(args.limit, 100)))
    result = compare_extraction_modes(
        notices,
        modes=args.mode or VALID_MODES,
        max_model_calls=max(0, args.max_model_calls),
    )
    output = json.dumps(result, ensure_ascii=False, indent=2)
    if args.output:
        _write_report(args.output, output)
    else:
        print(output)


if __name__ == "__main__":
    main()
