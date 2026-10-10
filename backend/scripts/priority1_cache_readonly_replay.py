"""Replay GAP priority 1 using only the 2026-10-09 local response/parse caches."""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import tempfile
from collections import Counter
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from app import archive_files, ingestion
from app.cache_only_replay import (
    CacheMiss,
    block_model_transports,
    load_model_cache,
    load_parse_cache,
)
from app.config import Settings

MODES = ("model", "hybrid")
ROUTES = ("rules", "hybrid", "model")


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _inventory_sha256(directory: Path) -> tuple[int, str]:
    files = sorted(path for path in directory.glob("*.json") if path.is_file())
    digest = hashlib.sha256()
    for path in files:
        digest.update(path.name.encode("utf-8"))
        digest.update(b"\0")
        digest.update(_sha256_file(path).encode("ascii"))
        digest.update(b"\n")
    return len(files), digest.hexdigest()


def _write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def _warning_category(value: str) -> str:
    if "历史业绩/资格材料" in value:
        return "historical_scope_exclusion"
    if "附件用途混合" in value:
        return "mixed_scope_review"
    if "附件用途不明" in value or "用途不明" in value:
        return "unknown_scope_review"
    if "跨文件" in value:
        return "cross_file_reconciliation"
    if "模型" in value:
        return "cached_model_warning"
    if any(token in value for token in ("PDF", "解析", "OCR", "表格", "附件容器")):
        return "parse_or_attachment_warning"
    if "规则基线" in value:
        return "rules_baseline_notice"
    return "other_warning"


def _package_codes(items, participants=()) -> list[str]:
    values = {
        str(row.package_code)
        for row in [*items, *participants]
        if getattr(row, "package_code", None)
    }
    return sorted(values, key=lambda value: (value.casefold(), value))


def _package_hash(values: list[str]) -> str:
    material = "\n".join(values).encode("utf-8")
    return hashlib.sha256(material).hexdigest()


def _load_manifest(manifest_path: Path) -> dict[str, dict[str, str]]:
    with manifest_path.open("r", encoding="utf-8-sig", newline="") as stream:
        rows = list(csv.DictReader(stream))
    result = {}
    for row in rows:
        notice_id = row.get("notice_id", "").strip()
        if not notice_id or notice_id in result:
            raise ValueError("sample manifest has a blank or duplicate notice id")
        result[notice_id] = row
    return result


def _validate_inputs(input_root: Path, run_dir: Path, progress: dict[str, Any]):
    identity = progress["identity"]
    manifest_path = input_root / "sample_manifest.csv"
    old_gold_path = input_root / "gold.reviewed.json"
    v2_gold_path = (
        input_root / "revisions" / "20261009-source-verified-v2" / "gold.reviewed.v2.json"
    )
    ledger_path = v2_gold_path.with_name("gold-revision-ledger.json")
    manifest = _load_manifest(manifest_path)
    if _sha256_file(manifest_path) != identity["manifest_sha256"]:
        raise ValueError("sample manifest hash differs from the frozen 2026-10-09 run")
    if _sha256_file(old_gold_path) != identity["gold_sha256"]:
        raise ValueError("old Gold hash differs from the frozen 2026-10-09 run")

    source_records = identity["sources"]
    if set(source_records) != set(identity["notice_ids"]):
        raise ValueError("source records do not cover the run's frozen notice ids")
    if set(source_records) != set(manifest):
        raise ValueError("sample manifest and frozen source record notice ids differ")
    source_file_count = 0
    for notice_id, files in source_records.items():
        if not files:
            raise ValueError(f"{notice_id}: no source files in frozen run identity")
        for source in files:
            path = Path(source["path"]).resolve(strict=True)
            if not path.is_file() or _sha256_file(path) != source["sha256"]:
                raise ValueError(f"{notice_id}: frozen source file missing or hash changed")
            source_file_count += 1

    def dataset_counts(path: Path) -> tuple[int, int, int]:
        value = json.loads(path.read_text(encoding="utf-8-sig"))
        notices = value.get("notices", [])
        return (
            len(notices),
            sum(len(n.get("packages", [])) for n in notices),
            sum(
                len(package.get("items", []))
                for notice in notices
                for package in notice.get("packages", [])
            ),
        )

    old_gold_counts = dataset_counts(old_gold_path)
    v2_gold_counts = dataset_counts(v2_gold_path)
    if old_gold_counts[0] != 24 or v2_gold_counts[0] != 24:
        raise ValueError("expected the same 24-notice old Gold and v2 scope")

    prediction_paths = {mode: run_dir / f"predictions-{mode}.json" for mode in ROUTES}
    evaluation_paths = {mode: run_dir / f"evaluation-{mode}.json" for mode in ROUTES}
    for path in [*prediction_paths.values(), *evaluation_paths.values()]:
        if not path.is_file():
            raise ValueError(f"required historical file is missing: {path.name}")

    old_ids = {
        row["notice_id"]
        for row in json.loads(old_gold_path.read_text(encoding="utf-8-sig"))["notices"]
    }
    v2_ids = {
        row["notice_id"]
        for row in json.loads(v2_gold_path.read_text(encoding="utf-8-sig"))["notices"]
    }
    if old_ids != v2_ids or old_ids != set(identity["notice_ids"]):
        raise ValueError("old Gold, Gold v2, and frozen run notice IDs differ")
    prediction_counts = {}
    for mode, path in prediction_paths.items():
        data = json.loads(path.read_text(encoding="utf-8-sig"))
        notice_ids = {row["notice_id"] for row in data.get("notices", [])}
        if notice_ids != old_ids or len(notice_ids) != len(data.get("notices", [])):
            raise ValueError(f"{path.name} is incomplete or has duplicate notice ids")
        prediction_counts[mode] = dataset_counts(path)

    key_files = {
        "manifest": manifest_path,
        "old_gold": old_gold_path,
        "gold_v2": v2_gold_path,
        "gold_v2_ledger": ledger_path,
        "progress": run_dir / "progress.json",
        "old_gap_report": run_dir / "gap-route-report.json",
        "old_summary": run_dir / "summary.json",
        **{f"prediction_{mode}": path for mode, path in prediction_paths.items()},
        **{f"old_evaluation_{mode}": path for mode, path in evaluation_paths.items()},
    }
    hashes = {name: _sha256_file(path) for name, path in key_files.items()}
    for mode in MODES:
        count, digest = _inventory_sha256(
            run_dir / "cache" / "model-cache" / mode,
        )
        hashes[f"{mode}_model_cache_inventory"] = digest
        hashes[f"{mode}_model_cache_file_count"] = count
    parse_count, parse_digest = _inventory_sha256(run_dir / "cache" / "parse-cache")
    hashes["parse_cache_inventory"] = parse_digest
    hashes["parse_cache_file_count"] = parse_count
    return {
        "identity": identity,
        "manifest": manifest,
        "source_records": source_records,
        "source_file_count": source_file_count,
        "old_gold_counts": old_gold_counts,
        "v2_gold_counts": v2_gold_counts,
        "prediction_counts": prediction_counts,
        "hashes_before": hashes,
        "prediction_paths": prediction_paths,
        "evaluation_paths": evaluation_paths,
        "old_gold_path": old_gold_path,
        "v2_gold_path": v2_gold_path,
        "manifest_path": manifest_path,
        "ledger_path": ledger_path,
    }


def _settings_from_identity(identity: dict[str, Any]) -> Settings:
    return Settings(
        _env_file=None,
        model_base_url="http://cache-only.invalid/v1",
        model_api_key="CACHE_ONLY_DO_NOT_USE",
        model_name=identity["model_name"],
        model_max_chars=identity["model_max_chars"],
        model_max_output_tokens=identity["model_max_output_tokens"],
        model_disable_thinking=identity["model_disable_thinking"],
        ocr_enabled=identity["ocr_enabled"],
        ocr_language=identity["ocr_language"],
        ocr_timeout_seconds=identity["ocr_timeout_seconds"],
        **identity["limits"],
    )


@contextmanager
def _instrument_cache_only(mode: str, cache_root: Path, settings: Settings, limits: dict[str, Any]):
    stats: dict[str, Any] = {
        "parse_cache_hits": 0,
        "parse_cache_misses": 0,
        "model_cache_hits": 0,
        "model_cache_misses": 0,
        "historical_cached_calls": 0,
        "cached_items": 0,
        "cached_participants": 0,
        "scope_decisions": Counter(),
        "scope_filter_item_input": 0,
        "scope_filter_item_output": 0,
        "scope_filter_participant_input": 0,
        "scope_filter_participant_output": 0,
        "pre_fusion_items": 0,
        "post_fusion_items": 0,
        "pre_fusion_packages": set(),
        "post_fusion_packages": set(),
    }
    original_parse = ingestion.parse_document_with_participants
    original_model = ingestion.extract_unstructured_items
    original_classify = ingestion.classify_attachment_scope
    original_filter_items = ingestion.filter_attachment_items
    original_filter_participants = ingestion.filter_attachment_participants
    original_reconcile = ingestion.reconcile_candidates

    def cached_parse(document, **options):
        return load_parse_cache(
            cache_root,
            filename=document.filename,
            suffix=Path(document.filename).suffix,
            content=document.content,
            options=options,
            limits=limits,
            counters=stats,
        )

    def cached_model(*, filename, text, settings: Settings, include_participants=False):
        return load_model_cache(
            cache_root,
            mode=mode,
            filename=filename,
            text=text,
            settings=settings,
            include_participants=include_participants,
            counters=stats,
        )

    def tracked_classify(filename, text=None):
        decision = original_classify(filename, text)
        if text is not None:
            stats["scope_decisions"][decision.scope] += 1
        return decision

    def tracked_filter_items(filename, text, items, decision=None):
        stats["scope_filter_item_input"] += len(items)
        kept, warnings = original_filter_items(filename, text, items, decision)
        stats["scope_filter_item_output"] += len(kept)
        return kept, warnings

    def tracked_filter_participants(filename, text, participants, decision=None):
        stats["scope_filter_participant_input"] += len(participants)
        kept, warnings = original_filter_participants(filename, text, participants, decision)
        stats["scope_filter_participant_output"] += len(kept)
        return kept, warnings

    def tracked_reconcile(items, warnings=None, **kwargs):
        stats["pre_fusion_items"] += len(items)
        stats["pre_fusion_packages"].update(row.package_code for row in items if row.package_code)
        reconciled = original_reconcile(items, warnings, **kwargs)
        stats["post_fusion_items"] += len(reconciled)
        stats["post_fusion_packages"].update(
            row.package_code for row in reconciled if row.package_code
        )
        return reconciled

    ingestion.parse_document_with_participants = cached_parse
    ingestion.extract_unstructured_items = cached_model
    ingestion.classify_attachment_scope = tracked_classify
    ingestion.filter_attachment_items = tracked_filter_items
    ingestion.filter_attachment_participants = tracked_filter_participants
    ingestion.reconcile_candidates = tracked_reconcile
    try:
        yield stats
    finally:
        ingestion.parse_document_with_participants = original_parse
        ingestion.extract_unstructured_items = original_model
        ingestion.classify_attachment_scope = original_classify
        ingestion.filter_attachment_items = original_filter_items
        ingestion.filter_attachment_participants = original_filter_participants
        ingestion.reconcile_candidates = original_reconcile


def _plain_stats(stats: dict[str, Any]) -> dict[str, Any]:
    pre = sorted(stats["pre_fusion_packages"], key=lambda value: (value.casefold(), value))
    post = sorted(stats["post_fusion_packages"], key=lambda value: (value.casefold(), value))
    return {
        key: value
        for key, value in stats.items()
        if key not in {"pre_fusion_packages", "post_fusion_packages", "scope_decisions"}
    } | {
        "scope_decisions": dict(sorted(stats["scope_decisions"].items())),
        "pre_fusion_package_codes": pre,
        "post_fusion_package_codes": post,
        "pre_fusion_package_set_sha256": _package_hash(pre),
        "post_fusion_package_set_sha256": _package_hash(post),
    }


def _run_route(
    *,
    mode: str,
    input_records: dict[str, Any],
    run_dir: Path,
    settings: Settings,
    output_dir: Path,
) -> dict[str, Any]:
    from app.archive_files import DiskDocument, expand_paths

    cache_root = run_dir / "cache"
    records = input_records["source_records"]
    target_ids = input_records["identity"]["notice_ids"]
    result_rows = []
    missing = []
    warning_categories = Counter()
    total_stats: dict[str, Any] = {
        "parse_cache_hits": 0,
        "parse_cache_misses": 0,
        "model_cache_hits": 0,
        "model_cache_misses": 0,
        "historical_cached_calls": 0,
        "cached_items": 0,
        "cached_participants": 0,
        "scope_filter_item_input": 0,
        "scope_filter_item_output": 0,
        "scope_filter_participant_input": 0,
        "scope_filter_participant_output": 0,
        "pre_fusion_items": 0,
        "post_fusion_items": 0,
        "pre_fusion_packages": set(),
        "post_fusion_packages": set(),
        "scope_decisions": Counter(),
    }
    stream_attempts = {"sync": 0, "async": 0}
    archive_files.settings = settings

    def accumulate(stats):
        for field in (
            "parse_cache_hits",
            "parse_cache_misses",
            "model_cache_hits",
            "model_cache_misses",
            "historical_cached_calls",
            "cached_items",
            "cached_participants",
            "scope_filter_item_input",
            "scope_filter_item_output",
            "scope_filter_participant_input",
            "scope_filter_participant_output",
            "pre_fusion_items",
            "post_fusion_items",
        ):
            total_stats[field] += stats[field]
        total_stats["scope_decisions"].update(stats["scope_decisions"])
        total_stats["pre_fusion_packages"].update(stats["pre_fusion_packages"])
        total_stats["post_fusion_packages"].update(stats["post_fusion_packages"])

    with block_model_transports() as blocked_calls:
        for notice_id in target_ids:
            stats = None
            source_documents = [
                DiskDocument(
                    filename=row["name"],
                    path=Path(row["path"]),
                    source_sha256=row["sha256"],
                    source_size=row.get("size"),
                )
                for row in records[notice_id]
            ]
            try:
                with tempfile.TemporaryDirectory(
                    prefix=".cache-only-expand-",
                    dir=output_dir,
                ) as temporary:
                    expanded, expansion_warnings = expand_paths(
                        source_documents,
                        Path(temporary),
                    )
                    with _instrument_cache_only(
                        mode, cache_root, settings, input_records["identity"]["limits"]
                    ) as stats:
                        result = ingestion._ingest_expanded(
                            expanded,
                            expansion_warnings,
                            extraction_mode=mode,
                            model_settings=settings,
                        )
                        accumulate(stats)
                        stats = None
                        package_codes = _package_codes(result.items, result.participants)
                        warning_categories.update(_warning_category(w) for w in result.warnings)
                        result_rows.append(
                            {
                                "notice_id": notice_id,
                                "status": "complete",
                                "final_item_candidates": len(result.items),
                                "final_participant_candidates": len(result.participants),
                                "package_codes": package_codes,
                                "warnings": {
                                    "total": len(result.warnings),
                                    "categories": dict(
                                        sorted(
                                            Counter(
                                                _warning_category(w) for w in result.warnings
                                            ).items()
                                        )
                                    ),
                                },
                            }
                        )
            except CacheMiss as exc:
                if stats is not None:
                    accumulate(stats)
                missing.append({"notice_id": notice_id, "stage": str(exc)})
                result_rows.append({"notice_id": notice_id, "status": "blocked_by_cache_miss"})
                continue
        stream_attempts = blocked_calls

    completed = sum(row["status"] == "complete" for row in result_rows)
    not_attempted = [
        notice_id
        for notice_id in target_ids
        if notice_id not in {row["notice_id"] for row in result_rows}
    ]
    blocked_notices = {
        row["notice_id"] for row in result_rows if row["status"] == "blocked_by_cache_miss"
    }
    incomplete_after_stop = [notice_id for notice_id in target_ids if notice_id in blocked_notices]
    stats_summary = _plain_stats(total_stats)
    final_items = sum(row.get("final_item_candidates", 0) for row in result_rows)
    final_participants = sum(row.get("final_participant_candidates", 0) for row in result_rows)
    final_package_codes = sorted(
        {code for row in result_rows for code in row.get("package_codes", [])},
        key=lambda value: (value.casefold(), value),
    )
    return {
        "route": mode,
        "status": "complete" if completed == len(target_ids) else "partial",
        "expected_notice_count": len(target_ids),
        "processed_notice_count": completed,
        "blocked_notice_ids": incomplete_after_stop,
        "not_attempted_notice_ids": not_attempted,
        "cache_misses": missing,
        "cache_counters": {
            "parse_hits": total_stats["parse_cache_hits"],
            "parse_misses": total_stats["parse_cache_misses"],
            "model_response_hits": total_stats["model_cache_hits"],
            "model_response_misses": total_stats["model_cache_misses"],
            "historical_http_calls_represented_in_hits": total_stats["historical_cached_calls"],
            "new_model_api_requests": 0,
        },
        "model_transport_interceptor": {
            "sync_attempts": stream_attempts["sync"],
            "async_attempts": stream_attempts["async"],
            "expected_attempts": 0,
        },
        "scope_and_fusion": stats_summary,
        "final_candidate_counts": {
            "items": final_items,
            "participants": final_participants,
        },
        "final_package_codes": final_package_codes,
        "final_package_set_sha256": _package_hash(final_package_codes),
        "warning_categories": dict(sorted(warning_categories.items())),
        "notices": result_rows,
    }


def run(input_root: Path, output_dir: Path):
    input_root = input_root.resolve(strict=True)
    output_dir = output_dir.resolve()
    backend_root = Path(__file__).resolve().parents[1]
    repo_output_root = (backend_root / ".data").resolve()
    if not output_dir.is_relative_to(repo_output_root):
        raise ValueError("output must be under backend/.data in the isolated worktree")
    if output_dir.exists():
        raise FileExistsError("output directory must be new; refusing to reuse or overwrite")
    output_dir.mkdir(parents=True, exist_ok=False)

    run_dir = input_root / "evaluation-routes-qwen-20261009"
    progress = json.loads((run_dir / "progress.json").read_text(encoding="utf-8"))
    inputs = _validate_inputs(input_root, run_dir, progress)
    settings = _settings_from_identity(inputs["identity"])
    summary = {
        "kind": "cache-only extraction replay",
        "baseline_commit": "ef8b5958238061aad43c0337385a356d0e29d9ea",
        "coverage": {
            "notice_count": len(inputs["identity"]["notice_ids"]),
            "source_file_count": inputs["source_file_count"],
            "old_gold": {
                "notices": inputs["old_gold_counts"][0],
                "packages": inputs["old_gold_counts"][1],
                "items": inputs["old_gold_counts"][2],
            },
            "gold_v2": {
                "notices": inputs["v2_gold_counts"][0],
                "packages": inputs["v2_gold_counts"][1],
                "items": inputs["v2_gold_counts"][2],
            },
            "old_predictions": {
                route: {"notices": counts[0], "packages": counts[1], "items": counts[2]}
                for route, counts in inputs["prediction_counts"].items()
            },
        },
        "input_hashes_before": inputs["hashes_before"],
        "routes": {},
    }
    for mode in MODES:
        summary["routes"][mode] = _run_route(
            mode=mode,
            input_records=inputs,
            run_dir=run_dir,
            settings=settings,
            output_dir=output_dir,
        )

    after_hashes = _validate_inputs(input_root, run_dir, progress)["hashes_before"]
    summary["input_hashes_after"] = after_hashes
    summary["source_gold_prediction_old_report_and_cache_unchanged"] = (
        after_hashes == inputs["hashes_before"]
    )
    summary["complete_for_all_routes"] = all(
        route["status"] == "complete" for route in summary["routes"].values()
    )
    _write_json(output_dir / "cache-only-replay-summary.json", summary)
    return 0 if summary["complete_for_all_routes"] else 2


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input-root", required=True, type=Path)
    parser.add_argument("--output-dir", required=True, type=Path)
    args = parser.parse_args(argv)
    return run(args.input_root, args.output_dir)


if __name__ == "__main__":
    raise SystemExit(main())
