"""Resumable, same-gold evaluation of rules, hybrid, and model extraction routes.

All scores are the repository's documented local proxy. This runner never writes
to the Gold file and keeps source material, responses, and predictions under .data.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import os
import statistics
import tempfile
import time
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from app.config import effective_settings, settings
from app.evaluation import GoldDataset, PredictionDataset, evaluate_dataset, render_markdown
from app.ingestion import _ingest_expanded
from app.model_adapter import model_call_budget
from app.parsers import SourceDocument
from app.schemas import ImportResult, ItemCandidate, NoticeMetadata, ParticipantCandidate

MODES = ("rules", "hybrid", "model")
ERROR_WARNING_MARKERS = (
    "模型抽取失败", "模型返回空内容", "实验模型调用额度已用完",
    "未配置模型", "模型名称不符合赛题要求",
)


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _atomic_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    encoded = json.dumps(value, ensure_ascii=False, indent=2) + "\n"
    handle, name = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(handle, "w", encoding="utf-8", newline="\n") as stream:
            stream.write(encoded)
        os.replace(name, path)
    except BaseException:
        try:
            os.unlink(name)
        except OSError:
            pass
        raise


def _read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def _hash_code() -> dict[str, str]:
    package = Path(__file__).resolve().parent
    return {
        name: _sha256_file(package / name)
        for name in (
            "gold_route_evaluation.py", "evaluation.py", "schemas.py", "config.py",
            "archive_files.py", "parsers.py", "ingestion.py", "model_adapter.py",
            "package_codes.py",
        )
    }


def _load_sources(
    gold: GoldDataset,
    manifest_path: Path,
    source_root: Path,
    *,
    pilot_only: bool,
    limit: int | None,
) -> list[dict[str, Any]]:
    source_root = source_root.resolve(strict=True)
    with manifest_path.open("r", encoding="utf-8-sig", newline="") as stream:
        rows = list(csv.DictReader(stream))
    by_id: dict[str, dict[str, str]] = {}
    for row in rows:
        notice_id = row.get("notice_id", "").strip()
        if not notice_id or notice_id in by_id:
            raise ValueError("样本清单含空 notice_id 或重复 notice_id")
        by_id[notice_id] = row
    gold_ids = {notice.notice_id for notice in gold.notices}
    if not gold_ids <= by_id.keys():
        raise ValueError(f"source manifest 缺少 Gold ID：{sorted(gold_ids - by_id.keys())}")
    selected = []
    for notice in gold.notices:
        row = by_id[notice.notice_id]
        if pilot_only and not row.get("assignment", "").startswith("pilot:"):
            continue
        names = [part.strip() for part in row.get("source_files", "").split(";") if part.strip()]
        html_names = [name for name in names if Path(name).suffix.lower() in {".html", ".htm"}]
        if len(html_names) != 1:
            raise ValueError(f"{notice.notice_id}: 必须且只能列出一个 HTML 源文件")
        files = []
        for name in names:
            path = (source_root / name).resolve(strict=True)
            if not path.is_relative_to(source_root) or not path.is_file():
                raise ValueError(f"来源路径越界或不是文件：{name}")
            files.append({"name": name, "path": str(path), "sha256": _sha256_file(path)})
        html = next(file for file in files if file["name"] == html_names[0])
        if html["sha256"][:20] != notice.notice_id:
            raise ValueError(f"{notice.notice_id}: HTML SHA-256 与 Gold notice_id 不匹配")
        selected.append({
            "notice_id": notice.notice_id,
            "official_file": row.get("official_file", ""),
            "assignment": row.get("assignment", ""),
            "files": files,
        })
    if limit is not None:
        if limit < 1:
            raise ValueError("--limit 必须大于 0")
        selected = selected[:limit]
    if not selected:
        raise ValueError("筛选后没有可评测公告")
    return selected


def _prediction_notice(result: ImportResult, notice_id: str) -> dict[str, Any]:
    codes = sorted({row.package_code for row in [*result.items, *result.participants]}) or ["default"]
    packages = []
    for code in codes:
        people = [row for row in result.participants if row.package_code == code]
        items = [row for row in result.items if row.package_code == code]
        packages.append({
            "package_id": code,
            "items": [{
                "item_id": f"item-{index + 1}",
                **{field: row.model_dump(mode="json").get(field)
                   for field in ("product_name", "category", "brand", "model", "quantity", "unit_price", "total_price")},
            } for index, row in enumerate(items)],
            "buyer": ({"entity_id": "buyer-1", "name": result.metadata.procurement_unit}
                      if result.metadata.procurement_unit else None),
            "winners": [{
                "entity_id": f"winner-{index + 1}", "name": person.organization_name,
                "award_amount": person.model_dump(mode="json").get("award_amount"),
            } for index, person in enumerate(people) if person.outcome == "winner"],
            "bidders": [{
                "entity_id": f"bidder-{index + 1}", "name": person.organization_name,
                "outcome": person.outcome,
            } for index, person in enumerate(people)],
        })
    return {"notice_id": notice_id, "packages": packages}


def _cache_key(*parts: str) -> str:
    return _sha256_bytes("\0".join(parts).encode("utf-8"))


@contextmanager
def _instrumented_ingestion(cache_root: Path, mode: str, model_settings, limits: dict[str, Any]):
    """Reuse successful parse/model work after interruption; account for each HTTP stream."""
    from app import ingestion, model_adapter

    original_parse = ingestion.parse_document_with_participants
    original_model = ingestion.extract_unstructured_items
    original_stream = model_adapter._stream_completion
    counters = {
        "parse_cache_hits": 0, "parse_cache_misses": 0, "parse_seconds": 0.0,
        "model_cache_hits": 0, "model_cache_misses": 0, "model_seconds": 0.0,
        "model_calls": [],
    }
    parse_cache = cache_root / "parse-cache"
    response_cache = cache_root / "model-cache" / mode
    parse_cache.mkdir(parents=True, exist_ok=True)
    response_cache.mkdir(parents=True, exist_ok=True)

    def cached_parse(document, **options):
        content = document.content
        suffix = Path(document.filename).suffix
        identity = json.dumps({"version": 3, "suffix": suffix, "options": options,
                              "limits": limits}, sort_keys=True)
        digest = _sha256_bytes(identity.encode() + content)
        path = parse_cache / f"{digest}.json"
        stored = None
        if path.is_file():
            try:
                stored = _read_json(path)
            except (OSError, ValueError):
                stored = None
        if stored is not None:
            counters["parse_cache_hits"] += 1
        else:
            counters["parse_cache_misses"] += 1
            started = time.perf_counter()
            text, items, participants, warnings = original_parse(
                SourceDocument("document" + suffix, content), **options,
            )
            counters["parse_seconds"] += time.perf_counter() - started
            stored = {"text": text,
                      "items": [row.model_dump(mode="json") for row in items],
                      "participants": [row.model_dump(mode="json") for row in participants],
                      "warnings": warnings}
            if not any(any(word in warning for word in ("失败", "超时", "未安装", "缺少"))
                       for warning in warnings):
                _atomic_json(path, stored)
        items = [ItemCandidate.model_validate(row).model_copy(update={"source_file": document.filename})
                 for row in stored["items"]]
        participants = [ParticipantCandidate.model_validate(row).model_copy(
            update={"source_file": document.filename}) for row in stored["participants"]]
        warnings = [warning.replace("document" + suffix, document.filename)
                    for warning in stored["warnings"]]
        return stored["text"], items, participants, warnings

    def measured_stream(*args, **kwargs):
        started = time.perf_counter()
        request = {"call_id": uuid.uuid4().hex, "elapsed_seconds": None, "prompt_tokens": None,
                   "completion_tokens": None, "transport": "failed"}
        try:
            content, token_usage = original_stream(*args, **kwargs)
            request["transport"] = "success"
            for field in ("prompt_tokens", "completion_tokens"):
                value = token_usage.get(field)
                if isinstance(value, (int, float)):
                    request[field] = int(value)
            return content, token_usage
        except Exception as exc:
            request["error_type"] = type(exc).__name__
            raise
        finally:
            elapsed = time.perf_counter() - started
            request["elapsed_seconds"] = round(elapsed, 4)
            counters["model_seconds"] += elapsed
            counters["model_calls"].append(request)

    def cached_model(*, filename, text, settings, include_participants=False):
        identity = json.dumps({
            "version": 1, "mode": mode, "filename": filename,
            "model": settings.model_name, "max_chars": settings.model_max_chars,
            "max_output_tokens": settings.model_max_output_tokens,
            "disable_thinking": settings.model_disable_thinking,
            "include_participants": include_participants,
        }, sort_keys=True)
        digest = _sha256_bytes(identity.encode() + text.encode("utf-8"))
        path = response_cache / f"{digest}.json"
        if path.is_file():
            try:
                cached = _read_json(path)
                counters["model_cache_hits"] += 1
                cached_calls = list(cached.get("model_calls", []))
                counters["model_calls"].extend(cached_calls)
                counters["model_seconds"] += sum(
                    float(call.get("elapsed_seconds") or 0) for call in cached_calls
                )
                # Treat already-paid calls as part of the per-notice budget and
                # recover their usage when a run resumes from a response cache.
                usage = model_adapter._usage.get()
                if usage is not None:
                    usage.requests += len(cached_calls)
                    usage.successful_responses += sum(
                        call.get("transport") == "success" for call in cached_calls
                    )
                    usage.prompt_tokens += sum(
                        int(call["prompt_tokens"])
                        for call in cached_calls if call.get("prompt_tokens") is not None
                    )
                    usage.completion_tokens += sum(
                        int(call["completion_tokens"])
                        for call in cached_calls if call.get("completion_tokens") is not None
                    )
                return (
                    NoticeMetadata.model_validate(cached["metadata"]),
                    [ItemCandidate.model_validate(row) for row in cached["items"]],
                    [ParticipantCandidate.model_validate(row) for row in cached["participants"]],
                    list(cached["warnings"]),
                )
            except (OSError, ValueError, ValidationError, KeyError):
                pass
        counters["model_cache_misses"] += 1
        call_index = len(counters["model_calls"])
        result = original_model(filename=filename, text=text, settings=settings,
                                include_participants=include_participants)
        if not any(marker in warning for warning in result[3] for marker in ERROR_WARNING_MARKERS):
            _atomic_json(path, {
                "metadata": result[0].model_dump(mode="json"),
                "items": [row.model_dump(mode="json") for row in result[1]],
                "participants": [row.model_dump(mode="json") for row in result[2]],
                "warnings": result[3],
                "model_calls": counters["model_calls"][call_index:],
            })
        return result

    ingestion.parse_document_with_participants = cached_parse
    ingestion.extract_unstructured_items = cached_model
    model_adapter._stream_completion = measured_stream
    try:
        yield counters
    finally:
        ingestion.parse_document_with_participants = original_parse
        ingestion.extract_unstructured_items = original_model
        model_adapter._stream_completion = original_stream


def _run_notice_mode(
    record: dict[str, Any], *, mode: str, scope: str, output_dir: Path,
    model_settings, limits: dict[str, Any], max_calls_per_notice: int,
) -> dict[str, Any]:
    from app.archive_files import DiskDocument, expand_paths

    selected_files = [file for file in record["files"] if scope == "attachments"
                      or Path(file["name"]).suffix.lower() in {".html", ".htm"}]
    started = time.perf_counter()
    expand_seconds = 0.0
    if scope == "html":
        documents = [SourceDocument(row["name"], Path(row["path"]).read_bytes())
                     for row in selected_files]
        expansion_warnings: list[str] = []
    else:
        with tempfile.TemporaryDirectory(prefix="gold-eval-expand-") as temporary:
            expand_started = time.perf_counter()
            documents, expansion_warnings = expand_paths(
                [DiskDocument(row["name"], Path(row["path"])) for row in selected_files],
                Path(temporary),
            )
            expand_seconds = time.perf_counter() - expand_started
            with (
                _instrumented_ingestion(output_dir / "cache", mode, model_settings, limits) as stats,
                model_call_budget(max_calls_per_notice) as usage,
            ):
                parse_started = time.perf_counter()
                try:
                    result = _ingest_expanded(
                        documents, expansion_warnings, extraction_mode=mode,
                        model_settings=model_settings,
                    )
                except Exception as exc:  # noqa: BLE001  # retain paid API calls on failed notices
                    return _failed_mode_result(record, exc, stats, usage, started)
                mode_elapsed = time.perf_counter() - parse_started
            return _mode_result(record, result, stats, usage, started, mode_elapsed,
                                expand_seconds, limits)
    with (
        _instrumented_ingestion(output_dir / "cache", mode, model_settings, limits) as stats,
        model_call_budget(max_calls_per_notice) as usage,
    ):
        parse_started = time.perf_counter()
        try:
            result = _ingest_expanded(documents, expansion_warnings,
                                      extraction_mode=mode, model_settings=model_settings)
        except Exception as exc:  # noqa: BLE001  # retain paid API calls on failed notices
            return _failed_mode_result(record, exc, stats, usage, started)
        mode_elapsed = time.perf_counter() - parse_started
    return _mode_result(record, result, stats, usage, started, mode_elapsed,
                        expand_seconds, limits)


def _failed_mode_result(record, exc, stats, usage, started):
    error = f"{type(exc).__name__}: {exc}"
    return {
        "status": "failed",
        "notice_id": record["notice_id"],
        "error": error,
        "errors": [error],
        "elapsed_seconds": round(time.perf_counter() - started, 4),
        "model_seconds": round(stats["model_seconds"], 4),
        "model_calls": stats["model_calls"],
        "api_usage": {
            "requests": usage.requests,
            "successful_responses": usage.successful_responses,
        },
    }


def _mode_result(record, result, stats, usage, started, mode_elapsed, expand_seconds, limits):
    prediction = _prediction_notice(result, record["notice_id"])
    errors = [warning for warning in result.warnings
              if any(marker in warning for marker in ERROR_WARNING_MARKERS)]
    if usage.exhausted:
        errors.append("单公告模型调用预算耗尽，结果不完整")
    return {
        "status": "incomplete" if errors else "done",
        "notice_id": record["notice_id"],
        "prediction": prediction,
        "items_found": result.items_found,
        "participants_found": len(result.participants),
        "warnings": result.warnings,
        "errors": list(dict.fromkeys(errors)),
        "elapsed_seconds": round(time.perf_counter() - started, 4),
        "ingestion_seconds": round(mode_elapsed, 4),
        "expansion_seconds": round(expand_seconds, 4),
        "parse_seconds": round(stats["parse_seconds"], 4),
        "parse_cache_hits": stats["parse_cache_hits"],
        "parse_cache_misses": stats["parse_cache_misses"],
        "model_seconds": round(stats["model_seconds"], 4),
        "model_cache_hits": stats["model_cache_hits"],
        "model_cache_misses": stats["model_cache_misses"],
        "model_calls": stats["model_calls"],
        "api_usage": {
            "requests": usage.requests,
            "successful_responses": usage.successful_responses,
            "prompt_tokens": (
                sum(call["prompt_tokens"] for call in stats["model_calls"]
                    if call.get("prompt_tokens") is not None)
                if any(call.get("prompt_tokens") is not None for call in stats["model_calls"])
                else None
            ),
            "completion_tokens": (
                sum(call["completion_tokens"] for call in stats["model_calls"]
                    if call.get("completion_tokens") is not None)
                if any(call.get("completion_tokens") is not None for call in stats["model_calls"])
                else None
            ),
            "budget": usage.max_calls,
            "exhausted": usage.exhausted,
            "token_usage_reported_requests": sum(
                call.get("prompt_tokens") is not None or call.get("completion_tokens") is not None
                for call in stats["model_calls"]
            ),
        },
        "mode_semantics": {
            "model": "model-derived items/metadata; parser-derived bidder tables remain enabled",
            "hybrid": "parser output plus evidence-checked model additions; parser values win conflicts",
            "rules": "parser/OCR only; no model requests",
        },
    }


def _score_mode(gold_data: dict, rows: list[dict], output_dir: Path, mode: str) -> dict[str, Any]:
    predicted = PredictionDataset.model_validate({
        "schema_version": "1.0", "status": "predicted",
        "notices": [row["prediction"] for row in rows],
    })
    gold_subset = GoldDataset.model_validate({
        **{key: value for key, value in gold_data.items() if key != "notices"},
        "notices": [notice for notice in gold_data["notices"]
                    if notice["notice_id"] in {row["notice_id"] for row in rows}],
    })
    report = evaluate_dataset(gold_subset, predicted)
    if report.missing_notice_ids or report.extra_notice_ids:
        raise ValueError(f"{mode}: Gold/Prediction ID mismatch")
    if report.gold_notice_count != len(rows):
        raise ValueError(f"{mode}: scored notice count differs from completed predictions")
    prediction_path = output_dir / f"predictions-{mode}.json"
    report_path = output_dir / f"evaluation-{mode}.json"
    _atomic_json(prediction_path, predicted.model_dump(mode="json"))
    _atomic_json(report_path, report.model_dump(mode="json"))
    markdown_path = output_dir / f"evaluation-{mode}.md"
    temporary = markdown_path.with_suffix(".md.tmp")
    temporary.write_text(render_markdown(report), encoding="utf-8")
    os.replace(temporary, markdown_path)
    return report.model_dump(mode="json")


def run_evaluation(
    *, gold_path: Path, manifest_path: Path, source_root: Path, output_dir: Path,
    scope: str = "html", pilot_only: bool = False, limit: int | None = None,
    max_calls_per_notice: int = 50,
) -> dict[str, Any]:
    gold_path, manifest_path = gold_path.resolve(strict=True), manifest_path.resolve(strict=True)
    gold_data = json.loads(gold_path.read_text(encoding="utf-8-sig"))
    gold = GoldDataset.model_validate(gold_data)
    if gold.status != "reviewed":
        raise ValueError("只允许使用 reviewed Gold 评测")
    if scope not in {"html", "attachments"}:
        raise ValueError("scope must be html or attachments")
    records = _load_sources(gold, manifest_path, source_root, pilot_only=pilot_only, limit=limit)
    model_settings = effective_settings().model_copy(update={"ocr_enabled": scope == "attachments"})
    if any(mode != "rules" for mode in MODES) and not (
        model_settings.model_base_url and model_settings.model_api_key and model_settings.model_name
    ):
        raise ValueError("hybrid/model 评测需要已配置模型接口")
    if not model_settings.model_name.casefold().startswith(("qwen", "deepseek")):
        raise ValueError("模型名需为 Qwen 或 DeepSeek 系列")
    if max_calls_per_notice < 1:
        raise ValueError("max_calls_per_notice 必须大于 0")

    # The attached-source comparison uses the same OCR bounds as the completed
    # official full import. HTML-only is unaffected by OCR settings.
    limits = {
        "job_max_expanded_mb": settings.job_max_expanded_mb,
        "job_max_member_mb": settings.job_max_member_mb,
        "job_max_archive_depth": settings.job_max_archive_depth,
        "job_max_archive_files": settings.job_max_archive_files,
        "pdf_max_pages": settings.pdf_max_pages,
        "pdf_max_ocr_pages": settings.pdf_max_ocr_pages,
        "ocr_engine": settings.ocr_engine,
    }
    gold_hash = _sha256_file(gold_path)
    model_secret_hash = _sha256_bytes(model_settings.model_api_key.encode())
    model_fingerprint = _sha256_bytes((
        model_settings.model_base_url + "\0" + model_settings.model_name + "\0" + model_secret_hash
    ).encode())
    identity = {
        "schema_version": 1, "scope": scope, "pilot_only": pilot_only,
        "gold_sha256": gold_hash, "manifest_sha256": _sha256_file(manifest_path),
        "notice_ids": [row["notice_id"] for row in records],
        "sources": {row["notice_id"]: row["files"] for row in records},
        "modes": list(MODES), "model_name": model_settings.model_name,
        "model_config_fingerprint": model_fingerprint,
        "model_max_chars": model_settings.model_max_chars,
        "model_max_output_tokens": model_settings.model_max_output_tokens,
        "model_disable_thinking": model_settings.model_disable_thinking,
        "ocr_enabled": model_settings.ocr_enabled,
        "ocr_language": model_settings.ocr_language,
        "ocr_timeout_seconds": model_settings.ocr_timeout_seconds,
        "max_calls_per_notice": max_calls_per_notice,
        "limits": limits, "code_sha256": _hash_code(),
    }
    output_dir = output_dir.resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    progress_path = output_dir / "progress.json"
    if progress_path.exists():
        progress = _read_json(progress_path)
        if progress.get("identity") != identity:
            raise ValueError("已有进度对应不同 Gold/source/config/code；拒绝续跑")
    else:
        if any(output_dir.iterdir()):
            raise ValueError("输出目录非空但没有 progress.json，为保护旧结果拒绝覆盖")
        progress = {
            "identity": identity,
            "started_at": time.time(),
            "status": "running",
            "completed": {mode: {} for mode in MODES},
            "attempt_history": {mode: {} for mode in MODES},
            "errors": [],
        }
        _atomic_json(progress_path, progress)

    for mode in MODES:
        completed = progress["completed"].setdefault(mode, {})
        for index, record in enumerate(records, start=1):
            existing = completed.get(record["notice_id"])
            if existing and existing.get("status") == "done":
                continue
            print(f"[{scope}/{mode}] {index}/{len(records)} {record['notice_id']} starting", flush=True)
            try:
                row = _run_notice_mode(
                    record, mode=mode, scope=scope, output_dir=output_dir,
                    model_settings=model_settings, limits=limits,
                    max_calls_per_notice=max_calls_per_notice,
                )
                if existing and "elapsed_seconds" in existing:
                    _archive_attempt(progress, mode, record["notice_id"], existing)
                completed[record["notice_id"]] = row
                progress["last_updated_at"] = time.time()
                _atomic_json(progress_path, progress)
                if row["status"] != "done":
                    print(f"[{scope}/{mode}] {record['notice_id']} INCOMPLETE: {row['errors']}", flush=True)
                else:
                    print(
                        f"[{scope}/{mode}] {record['notice_id']} done "
                        f"wall={row['elapsed_seconds']:.1f}s requests={row['api_usage']['requests']} "
                        f"tokens={row['api_usage']['prompt_tokens']}+{row['api_usage']['completion_tokens']}",
                        flush=True,
                    )
            except Exception as exc:  # noqa: BLE001  # isolate notices so the run can resume
                if existing and "elapsed_seconds" in existing:
                    _archive_attempt(progress, mode, record["notice_id"], existing)
                completed[record["notice_id"]] = {
                    "status": "failed", "notice_id": record["notice_id"],
                    "error": f"{type(exc).__name__}: {exc}",
                }
                progress.setdefault("errors", []).append({
                    "mode": mode, "notice_id": record["notice_id"],
                    "error": f"{type(exc).__name__}: {exc}",
                })
                _atomic_json(progress_path, progress)
                print(f"[{scope}/{mode}] {record['notice_id']} FAILED: {type(exc).__name__}: {exc}", flush=True)

    reports = {}
    all_complete = True
    for mode in MODES:
        rows = [progress["completed"].get(mode, {}).get(row["notice_id"]) for row in records]
        if any(row is None or row.get("status") != "done" for row in rows):
            all_complete = False
            continue
        reports[mode] = _score_mode(gold_data, rows, output_dir, mode)
    progress["reports"] = reports
    progress["status"] = "completed" if all_complete else "incomplete"
    progress["finished_at"] = time.time() if all_complete else None
    progress["last_updated_at"] = time.time()
    _atomic_json(progress_path, progress)
    summary = _summary(progress, records, reports)
    _atomic_json(output_dir / "summary.json", summary)
    _write_summary_markdown(output_dir / "summary.md", summary)
    return summary


def _archive_attempt(
    progress: dict[str, Any], mode: str, notice_id: str, entry: dict[str, Any],
) -> None:
    fields = (
        "status", "notice_id", "elapsed_seconds", "ingestion_seconds", "expansion_seconds",
        "parse_seconds", "parse_cache_hits", "parse_cache_misses", "model_seconds",
        "model_cache_hits", "model_cache_misses", "model_calls", "api_usage", "errors",
    )
    archive = {field: entry[field] for field in fields if field in entry}
    history = progress.setdefault("attempt_history", {}).setdefault(mode, {}).setdefault(notice_id, [])
    history.append(archive)


def _summary(progress: dict[str, Any], records: list[dict[str, Any]], reports: dict[str, Any]) -> dict[str, Any]:
    modes = {}
    for mode in MODES:
        entries = [progress.get("completed", {}).get(mode, {}).get(row["notice_id"])
                   for row in records]
        good = [entry for entry in entries if entry and entry.get("status") == "done"]
        observed = [entry for entry in entries if entry and "elapsed_seconds" in entry]
        attempts_by_notice = [
            [*progress.get("attempt_history", {}).get(mode, {}).get(record["notice_id"], []),
             *([entry] if entry and "elapsed_seconds" in entry else [])]
            for record, entry in zip(records, entries)
        ]
        attempts = [attempt for group in attempts_by_notice for attempt in group]
        calls = _unique_model_calls(
            call for entry in attempts for call in entry.get("model_calls", [])
        )
        elapsed = [entry["elapsed_seconds"] for entry in attempts]
        final_elapsed = [entry["elapsed_seconds"] for entry in observed]
        api = [entry.get("api_usage", {}) for entry in attempts]
        prompt = [row.get("prompt_tokens") for row in calls if row.get("prompt_tokens") is not None]
        completion = [row.get("completion_tokens") for row in calls if row.get("completion_tokens") is not None]
        modes[mode] = {
            "completed_notices": len(good),
            "failed_or_incomplete_notices": len(entries) - len(good),
            "elapsed_seconds_sum": round(sum(elapsed), 3),
            "elapsed_seconds_median": round(statistics.median(final_elapsed), 3) if final_elapsed else None,
            "elapsed_seconds_max": round(max(final_elapsed), 3) if final_elapsed else None,
            "attempt_count": len(attempts),
            "parse_seconds": round(sum(entry.get("parse_seconds", 0) for entry in attempts), 3),
            "model_seconds": round(
                sum(call.get("elapsed_seconds", 0) for call in calls)
                if calls else sum(entry.get("model_seconds", 0) for entry in attempts),
                3,
            ),
            "source_documents_parsed": sum(entry.get("parse_cache_hits", 0) + entry.get("parse_cache_misses", 0) for entry in attempts),
            "parse_cache_hits": sum(entry.get("parse_cache_hits", 0) for entry in attempts),
            "model_cache_hits": sum(entry.get("model_cache_hits", 0) for entry in attempts),
            "requests": len(calls) if calls else sum(row.get("requests", 0) for row in api),
            "successful_responses": (
                sum(call.get("transport") == "success" for call in calls)
                if calls else sum(row.get("successful_responses", 0) for row in api)
            ),
            "failed_transports": sum(call.get("transport") == "failed" for call in calls),
            "prompt_tokens": sum(prompt) if prompt else None,
            "completion_tokens": sum(completion) if completion else None,
            "token_usage_reported_requests": (
                sum(call.get("prompt_tokens") is not None or call.get("completion_tokens") is not None
                    for call in calls)
                if calls else sum(row.get("token_usage_reported_requests", 0) for row in api)
            ),
            "per_notice": [{
                "notice_id": entry.get("notice_id") if entry else records[index]["notice_id"],
                "status": entry.get("status") if entry else "pending",
                "elapsed_seconds": entry.get("elapsed_seconds") if entry else None,
                "attempts": len(attempts_by_notice[index]),
                "requests": len(_unique_model_calls(
                    call for attempt in attempts_by_notice[index]
                    for call in attempt.get("model_calls", [])
                )),
                "prompt_tokens": _sum_reported_tokens(attempts_by_notice[index], "prompt_tokens"),
                "completion_tokens": _sum_reported_tokens(attempts_by_notice[index], "completion_tokens"),
                "errors": entry.get("errors", [entry.get("error")] if entry.get("error") else []) if entry else [],
            } for index, entry in enumerate(entries)],
            "evaluation": reports.get(mode),
        }
    return {
        "schema_version": 1,
        "evaluation_kind": "same_gold_local_proxy_route_comparison",
        "status": progress.get("status"),
        "scope": progress["identity"]["scope"],
        "gold_sha256": progress["identity"]["gold_sha256"],
        "manifest_sha256": progress["identity"]["manifest_sha256"],
        "notice_count": len(records),
        "notice_ids": [row["notice_id"] for row in records],
        "model_name": progress["identity"]["model_name"],
        "disclaimer": "Scores use the repository's local open-extraction proxy, not official competition scoring. API tokens are not monetary cost; provider usage may be absent.",
        "mode_semantics": {
            "rules": "parser/OCR only; no model requests",
            "hybrid": "parser plus evidence-checked model additions; parser values win conflicts",
            "model": "model-derived items/metadata; parser-derived bidder tables remain enabled",
        },
        "modes": modes,
    }


def _sum_reported_tokens(attempts: list[dict[str, Any]], field: str) -> int | None:
    calls = _unique_model_calls(
        call for attempt in attempts for call in attempt.get("model_calls", [])
    )
    values = [call[field] for call in calls if call.get(field) is not None]
    return sum(values) if values else None


def _unique_model_calls(calls) -> list[dict[str, Any]]:
    unique = []
    seen = set()
    for call in calls:
        key = call.get("call_id") or json.dumps(call, sort_keys=True, separators=(",", ":"))
        if key not in seen:
            seen.add(key)
            unique.append(call)
    return unique


def _write_summary_markdown(path: Path, summary: dict[str, Any]) -> None:
    lines = [
        "# Rules / Hybrid / Model Gold 比较", "",
        f"状态：{summary['status']}；范围：{summary['scope']}；Gold 公告：{summary['notice_count']} 条。", "",
        "这是本地开放抽取代理分，不是官方竞赛分。API token 数不等于费用；接口可能不返回 usage。", "",
        "| 路线 | 完成 | 字段 Weighted | 完整记录 Weighted | 累计墙钟 | 中位单篇 | 请求 | 成功响应 | Prompt tokens | Completion tokens |",
        "|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for mode in MODES:
        row = summary["modes"][mode]
        report = row.get("evaluation") or {}
        field_score = report.get("field_micro", {}).get("weighted_score")
        record_score = report.get("records", {}).get("weighted_score")
        fmt = lambda value: "N/A" if value is None else f"{value:.4%}"
        lines.append(
            f"| {mode} | {row['completed_notices']}/{summary['notice_count']} | {fmt(field_score)} | "
            f"{fmt(record_score)} | {row['elapsed_seconds_sum']:.1f}s | "
            f"{row['elapsed_seconds_median']:.1f}s | "
            f"{row['requests']} | {row['successful_responses']} | "
            f"{row['prompt_tokens'] if row['prompt_tokens'] is not None else 'N/A'} | "
            f"{row['completion_tokens'] if row['completion_tokens'] is not None else 'N/A'} |"
        )
    lines += ["", "路线语义：", ""]
    for mode, meaning in summary["mode_semantics"].items():
        lines.append(f"- `{mode}`：{meaning}")
    lines += ["", "逐公告时延和 API 用量见 `summary.json`；完整预测、字段/实体评估报告分别在 `predictions-*.json` 与 `evaluation-*.json`。"]
    temporary = path.with_suffix(".md.tmp")
    temporary.write_text("\n".join(lines) + "\n", encoding="utf-8")
    os.replace(temporary, path)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--gold", type=Path, required=True)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--source-root", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--scope", choices=("html", "attachments"), default="html")
    parser.add_argument("--pilot-only", action="store_true")
    parser.add_argument("--limit", type=int)
    parser.add_argument("--max-calls-per-notice", type=int, default=50)
    args = parser.parse_args(argv)
    try:
        result = run_evaluation(
            gold_path=args.gold, manifest_path=args.manifest, source_root=args.source_root,
            output_dir=args.output_dir, scope=args.scope, pilot_only=args.pilot_only,
            limit=args.limit, max_calls_per_notice=args.max_calls_per_notice,
        )
    except (OSError, ValueError, ValidationError) as exc:
        print(f"Gold route evaluation failed: {exc}")
        return 2
    print(json.dumps({
        "status": result["status"], "scope": result["scope"],
        "notice_count": result["notice_count"], "output_dir": str(args.output_dir.resolve()),
    }, ensure_ascii=False))
    return 0 if result["status"] == "completed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
