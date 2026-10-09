"""Bounded, resumable same-Gold route comparison.

Routes remain sequential. Notices within a route run in isolated processes so the
legacy evaluator's temporary module patches never race between worker threads.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import multiprocessing
import time
import uuid
from concurrent.futures import FIRST_COMPLETED, CancelledError, ProcessPoolExecutor, wait
from concurrent.futures.process import BrokenProcessPool
from pathlib import Path
from typing import Any

from filelock import FileLock, Timeout
from pydantic import ValidationError

from app import gold_route_evaluation as legacy
from app.bounded_runtime import (
    DeploymentLimits,
    SchedulingStopped,
    atomic_json,
    from_settings,
    read_json,
)
from app.config import effective_settings, settings
from app.evaluation import GoldDataset

MODES = legacy.MODES


def _positive_integer(value: str) -> int:
    try:
        number = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("must be a positive integer") from exc
    if number < 1:
        raise argparse.ArgumentTypeError("must be a positive integer")
    return number


def _sha256_file(path: Path) -> str:
    return legacy._sha256_file(path)


def _safe_error(exc: BaseException, api_key: str = "") -> str:
    message = f"{type(exc).__name__}: {exc}"
    if api_key:
        message = message.replace(api_key, "[REDACTED]")
    return message[:2000]


def _attempt_number(progress: dict[str, Any], mode: str, notice_id: str) -> int:
    history = progress.get("attempt_history", {}).get(mode, {}).get(notice_id, [])
    current = progress.get("completed", {}).get(mode, {}).get(notice_id)
    return len(history) + (1 if current and current.get("attempt") else 0) + 1


def _archive_previous(progress: dict[str, Any], mode: str, notice_id: str, row: dict[str, Any]) -> None:
    archive = dict(row)
    if archive.get("status") == "running":
        archive["status"] = "interrupted"
        archive["errors"] = [*archive.get("errors", []), "运行中进程未完成；续跑时重新调度"]
        archive.setdefault("elapsed_seconds", 0.0)
    progress.setdefault("attempt_history", {}).setdefault(mode, {}).setdefault(notice_id, []).append(archive)


def _restore_task_telemetry(row: dict[str, Any], output_dir: Path) -> None:
    """Recover request receipts when the worker died before its final result."""
    task_id = row.get("task_id")
    if not task_id:
        return
    try:
        telemetry = read_json(output_dir / "task-results" / f"{task_id}.telemetry.json")
    except (OSError, ValueError, TypeError):
        return
    if not isinstance(telemetry, dict) or not isinstance(telemetry.get("model_calls"), list):
        return
    calls = []
    for value in telemetry["model_calls"]:
        if not isinstance(value, dict) or not isinstance(value.get("call_id"), str):
            continue
        call = {"call_id": value["call_id"], "transport": value.get("transport", "failed")}
        for name in ("elapsed_seconds", "prompt_tokens", "completion_tokens", "provider_kv_tokens"):
            number = value.get(name)
            valid = (isinstance(number, (int, float)) and not isinstance(number, bool)
                     and math.isfinite(number) and number >= 0)
            call[name] = number if valid else (None if name.endswith("tokens") else 0.0)
        if isinstance(value.get("error_type"), str):
            call["error_type"] = value["error_type"]
        calls.append(call)
    row["model_calls"] = calls
    row["api_usage"] = {
        "requests": len(calls),
        "successful_responses": sum(call["transport"] == "success" for call in calls),
        "prompt_tokens": sum(call["prompt_tokens"] for call in calls
                             if call["prompt_tokens"] is not None) or None,
        "completion_tokens": sum(call["completion_tokens"] for call in calls
                                 if call["completion_tokens"] is not None) or None,
    }
    row["model_seconds"] = round(sum(call["elapsed_seconds"] for call in calls), 4)
    row["elapsed_seconds"] = max(row.get("elapsed_seconds", 0.0), row["model_seconds"])
    row["provider_kv_tokens"] = sum(call["provider_kv_tokens"] or 0 for call in calls)
    historical = telemetry.get("historical_model_calls")
    row["historical_cached_requests"] = len(historical) if isinstance(historical, list) else 0


def _identity(
    *, gold_path: Path, manifest_path: Path, records: list[dict[str, Any]], scope: str,
    pilot_only: bool, max_calls_per_notice: int, model_settings, limits: DeploymentLimits,
    concurrency: int,
) -> dict[str, Any]:
    model_key_hash = hashlib.sha256(model_settings.model_api_key.encode()).hexdigest()
    endpoint_hash = hashlib.sha256(model_settings.model_base_url.encode()).hexdigest()
    code_hashes = legacy._hash_code()
    package_dir = Path(legacy.__file__).resolve().parent
    for filename in ("bounded_route_evaluation.py", "bounded_runtime.py", "job_cache.py"):
        path = package_dir / filename
        if path.is_file():
            code_hashes[filename] = _sha256_file(path)
    return {
        "schema_version": 1,
        "runner": "bounded_route_evaluation",
        "scope": scope,
        "pilot_only": pilot_only,
        "gold_sha256": _sha256_file(gold_path),
        "manifest_sha256": _sha256_file(manifest_path),
        "notice_ids": [row["notice_id"] for row in records],
        "sources": {row["notice_id"]: row["files"] for row in records},
        "modes": list(MODES),
        "model_name": model_settings.model_name,
        "model_config_fingerprint": hashlib.sha256(
            (model_settings.model_base_url + "\0" + model_settings.model_name + "\0"
             + model_key_hash).encode()
        ).hexdigest(),
        "model_base_url_sha256": endpoint_hash,
        "model_max_chars": model_settings.model_max_chars,
        "model_max_output_tokens": model_settings.model_max_output_tokens,
        "model_disable_thinking": model_settings.model_disable_thinking,
        "model_timeout_seconds": model_settings.model_timeout_seconds,
        "model_stream_total_seconds": model_settings.model_stream_total_seconds,
        "ocr_enabled": model_settings.ocr_enabled,
        "ocr_language": model_settings.ocr_language,
        "ocr_timeout_seconds": model_settings.ocr_timeout_seconds,
        "max_calls_per_notice": max_calls_per_notice,
        "limits": {
            "job_max_expanded_mb": settings.job_max_expanded_mb,
            "job_max_member_mb": settings.job_max_member_mb,
            "job_max_archive_depth": settings.job_max_archive_depth,
            "job_max_archive_files": settings.job_max_archive_files,
            "pdf_max_pages": settings.pdf_max_pages,
            "pdf_max_ocr_pages": settings.pdf_max_ocr_pages,
            "ocr_engine": settings.ocr_engine,
            "document_conversion_timeout_seconds": settings.document_conversion_timeout_seconds,
            "libreoffice_path": settings.libreoffice_path,
        },
        "bounded_limits": {**limits.identity(), "concurrency": concurrency},
        "code_sha256": code_hashes,
    }


def _worker_run_task(task: dict[str, Any]) -> dict[str, Any]:
    """Run one notice/mode in a process; all legacy patching stays process-local."""
    from app import archive_files
    from app.bounded_runtime import use_runtime
    from app.config import Settings
    from app.config import settings as process_settings
    from app.job_cache import instrumented_notice_cache

    record = task["record"]
    mode = task["mode"]
    model_settings = Settings.model_validate(task["model_settings"])
    limits = DeploymentLimits(**task["limits"])
    started = time.perf_counter()
    original_instrumenter = legacy._instrumented_ingestion
    original_expand = archive_files.expand_paths
    original_parser_settings = {
        name: getattr(process_settings, name) for name in task["parser_limits"]
    }
    instrumentation_stats = []
    try:
        # Spawned children load environment defaults. Apply the coordinator's
        # frozen parser limits so archive/OCR behavior matches the run identity.
        for name, value in task["parser_limits"].items():
            setattr(process_settings, name, value)
        with use_runtime(limits, stop_path=task["stop_path"]) as limiter:
            def bounded_expand(*args, **kwargs):
                with limiter.document_slot(stop_path=task["stop_path"]):
                    return original_expand(*args, **kwargs)

            archive_files.expand_paths = bounded_expand
            def instrumented(*args, **kwargs):
                from contextlib import contextmanager

                @contextmanager
                def capture():
                    with instrumented_notice_cache(
                        *args,
                        **kwargs,
                        stop_path=Path(task["stop_path"]),
                        telemetry_path=(
                            Path(task["output_dir"]) / "task-results"
                            / f"{task['task_id']}.telemetry.json"
                        ),
                    ) as counters:
                        instrumentation_stats.append(counters)
                        yield counters

                return capture()

            legacy._instrumented_ingestion = instrumented
            row = _run_notice_mode(
                record,
                mode=mode,
                scope=task["scope"],
                output_dir=Path(task["output_dir"]),
                model_settings=model_settings,
                limits=task["parser_limits"],
                max_calls_per_notice=task["max_calls_per_notice"],
            )
    finally:
        legacy._instrumented_ingestion = original_instrumenter
        archive_files.expand_paths = original_expand
        for name, value in original_parser_settings.items():
            setattr(process_settings, name, value)

    row["mode"] = mode
    row["notice_id"] = record["notice_id"]
    row["attempt"] = task["attempt"]
    row["task_id"] = task["task_id"]
    row.setdefault("errors", [row["error"]] if row.get("error") else [])
    row.setdefault("model_calls", [])
    row.setdefault("api_usage", {"requests": 0, "successful_responses": 0})
    counters = instrumentation_stats[-1] if instrumentation_stats else {}
    row["provider_kv_tokens"] = int(counters.get("provider_kv_tokens", 0))
    row["historical_cached_requests"] = len(counters.get("historical_model_calls", []))
    # Keep an explicit error count for summary consumers without retaining a key.
    row["errors"] = [_safe_error_message(error, model_settings.model_api_key)
                     for error in row["errors"]]
    if any("SchedulingStopped" in error for error in row["errors"]):
        row["status"] = "paused"
    row.setdefault("attempt_elapsed_seconds", round(time.perf_counter() - started, 4))
    # A completed process result is durable before it crosses the executor pipe.
    # If another child crashes and breaks the pool, the coordinator can recover
    # this result instead of marking a completed sibling attempt as lost.
    result_path = Path(task["output_dir"]) / "task-results" / f"{task['task_id']}.json"
    atomic_json(result_path, row)
    return row


def _run_notice_mode(
    record: dict[str, Any], *, mode: str, scope: str, output_dir: Path,
    model_settings, limits: dict[str, Any], max_calls_per_notice: int,
) -> dict[str, Any]:
    """Keep HTML bytes lazy until the cache/parser acquires its document lease."""
    if scope != "html":
        return legacy._run_notice_mode(
            record, mode=mode, scope=scope, output_dir=output_dir,
            model_settings=model_settings, limits=limits,
            max_calls_per_notice=max_calls_per_notice,
        )
    from app.archive_files import DiskDocument
    from app.model_adapter import model_call_budget

    documents = [DiskDocument(row["name"], Path(row["path"])) for row in record["files"]
                 if Path(row["name"]).suffix.lower() in {".html", ".htm"}]
    member_limit = limits.get("job_max_member_mb", model_settings.job_max_member_mb) * 1024**2
    for document in documents:
        if document.path.stat().st_size > member_limit:
            raise ValueError(f"{document.filename}: 单文件超过后台任务限制")
    started = time.perf_counter()
    with (
        legacy._instrumented_ingestion(output_dir / "cache", mode, model_settings, limits) as stats,
        model_call_budget(max_calls_per_notice) as usage,
    ):
        parse_started = time.perf_counter()
        try:
            result = legacy._ingest_expanded(
                documents, [], extraction_mode=mode, model_settings=model_settings,
            )
        except Exception as exc:  # noqa: BLE001 - preserve partial API telemetry
            return legacy._failed_mode_result(record, exc, stats, usage, started)
        mode_elapsed = time.perf_counter() - parse_started
    return legacy._mode_result(record, result, stats, usage, started, mode_elapsed, 0.0, limits)


def _safe_error_message(error: Any, api_key: str) -> str:
    message = str(error)
    return message.replace(api_key, "[REDACTED]") if api_key else message


def _task_failure(task: dict[str, Any], exc: BaseException) -> dict[str, Any]:
    error = _safe_error(exc, task.get("model_settings", {}).get("model_api_key", ""))
    return {
        "task_id": task["task_id"],
        "mode": task["mode"],
        "notice_id": task["record"]["notice_id"],
        "attempt": task["attempt"],
        "status": "paused" if isinstance(exc, SchedulingStopped) else "failed",
        "errors": [error],
        "error": error,
        "model_calls": [],
        "api_usage": {"requests": 0, "successful_responses": 0},
        "elapsed_seconds": 0.0,
    }


def _persist_result(progress: dict[str, Any], progress_path: Path, row: dict[str, Any]) -> None:
    mode, notice_id = row["mode"], row["notice_id"]
    progress.setdefault("tasks", {})[row["task_id"]] = row
    progress.setdefault("completed", {}).setdefault(mode, {})[notice_id] = row
    if row.get("errors"):
        progress.setdefault("errors", []).append({
            "task_id": row["task_id"], "mode": mode, "notice_id": notice_id,
            "attempt": row["attempt"], "errors": row["errors"],
        })
    progress["last_updated_at"] = time.time()
    atomic_json(progress_path, progress)


def _drain_futures(active, progress, progress_path, *, release=True) -> None:
    for future, (task, permit) in list(active.items()):
        if not future.done():
            continue
        if release:
            permit.release()
        try:
            row = future.result()
        except Exception as exc:  # noqa: BLE001 - preserve each worker failure independently
            result_path = Path(task["output_dir"]) / "task-results" / f"{task['task_id']}.json"
            try:
                row = read_json(result_path)
            except (OSError, ValueError):
                row = _task_failure(task, exc)
                if isinstance(exc, CancelledError | BrokenProcessPool):
                    row["status"] = "interrupted"
                _restore_task_telemetry(row, Path(task["output_dir"]))
        _persist_result(progress, progress_path, row)
        del active[future]


def _run_mode_bounded(
    *, mode: str, records: list[dict[str, Any]], progress: dict[str, Any],
    progress_path: Path, output_dir: Path, scope: str, model_settings,
    parser_limits: dict[str, Any], max_calls_per_notice: int, concurrency: int,
    deployment_limits: DeploymentLimits, stop_path: Path, retry_failed: bool = False,
) -> bool:
    """Return False when paused; only the coordinator writes progress.json."""
    from app.bounded_runtime import DeploymentLimiter

    completed = progress.setdefault("completed", {}).setdefault(mode, {})
    limiter = DeploymentLimiter(deployment_limits)
    process_pool = ProcessPoolExecutor(
        max_workers=concurrency,
        mp_context=multiprocessing.get_context("spawn"),
    )
    active = {}
    def should_schedule(record):
        prior = completed.get(record["notice_id"]) or {}
        if prior.get("status") == "done":
            return False
        return retry_failed or prior.get("status") not in {"failed", "incomplete"}

    pending = iter((index, record) for index, record in enumerate(records, 1)
                   if should_schedule(record))
    exhausted = False
    paused = False

    def schedule_one() -> bool:
        nonlocal exhausted
        if stop_path.exists():
            return False
        permit = None
        if stop_path.exists():
            return False
        permit = limiter.try_acquire("queue")
        if permit is None:
            return False
        try:
            index, record = next(pending)
        except StopIteration:
            permit.release()
            exhausted = True
            return False
        attempt = _attempt_number(progress, mode, record["notice_id"])
        previous = completed.get(record["notice_id"])
        if previous:
            if previous.get("status") == "running":
                _restore_task_telemetry(previous, output_dir)
            _archive_previous(progress, mode, record["notice_id"], previous)
        task_id = hashlib.sha256(
            f"{progress['run_id']}\0{mode}\0{record['notice_id']}\0{attempt}".encode()
        ).hexdigest()[:32]
        task = {
            "task_id": task_id,
            "record": record,
            "mode": mode,
            "scope": scope,
            "output_dir": str(output_dir),
            "model_settings": model_settings.model_dump(),
            "limits": {
                "runtime_dir": str(deployment_limits.runtime_dir),
                "document_workers": deployment_limits.document_workers,
                "model_concurrency": deployment_limits.model_concurrency,
                "queue_capacity": deployment_limits.queue_capacity,
            },
            "parser_limits": parser_limits,
            "max_calls_per_notice": max_calls_per_notice,
            "attempt": attempt,
            "stop_path": str(stop_path),
        }
        stub = {
            "task_id": task_id, "mode": mode, "notice_id": record["notice_id"],
            "attempt": attempt, "status": "running", "errors": [],
            "model_calls": [], "api_usage": {"requests": 0, "successful_responses": 0},
        }
        progress.setdefault("tasks", {})[task_id] = stub
        completed[record["notice_id"]] = stub
        progress["last_updated_at"] = time.time()
        atomic_json(progress_path, progress)
        try:
            future = process_pool.submit(_worker_run_task, task)
        except Exception as exc:
            permit.release()
            _persist_result(progress, progress_path, _task_failure(task, exc))
            raise
        active[future] = (task, permit)
        print(f"[{scope}/{mode}] {index}/{len(records)} {record['notice_id']} submitted", flush=True)
        return True

    try:
        while not exhausted or active:
            while len(active) < concurrency and not exhausted and not stop_path.exists():
                if not schedule_one():
                    break
            if active:
                done, _ = wait(active, timeout=0.1, return_when=FIRST_COMPLETED)
                if done:
                    _drain_futures(active, progress, progress_path)
            elif stop_path.exists():
                paused = True
                break
            elif exhausted:
                break
            else:
                # Another deployment owner may temporarily hold every queue
                # lease. Retry admission without blocking active local futures.
                time.sleep(0.05)
        if stop_path.exists():
            paused = True
    except KeyboardInterrupt:
        paused = True
        stop_path.touch(exist_ok=True)
    except BrokenProcessPool:
        paused = True
        stop_path.touch(exist_ok=True)
    finally:
        if paused:
            stop_path.touch(exist_ok=True)
            for future in active:
                future.cancel()
        process_pool.shutdown(wait=True, cancel_futures=paused)
        _drain_futures(active, progress, progress_path)
        for future, (_, permit) in list(active.items()):
            permit.release()
            del active[future]
    return not paused


def _parser_limits() -> dict[str, Any]:
    return {
        "job_max_expanded_mb": settings.job_max_expanded_mb,
        "job_max_member_mb": settings.job_max_member_mb,
        "job_max_archive_depth": settings.job_max_archive_depth,
        "job_max_archive_files": settings.job_max_archive_files,
        "pdf_max_pages": settings.pdf_max_pages,
        "pdf_max_ocr_pages": settings.pdf_max_ocr_pages,
        "ocr_engine": settings.ocr_engine,
        "document_conversion_timeout_seconds": settings.document_conversion_timeout_seconds,
        "libreoffice_path": settings.libreoffice_path,
    }


def run_evaluation(
    *, gold_path: Path, manifest_path: Path, source_root: Path, output_dir: Path,
    scope: str = "html", pilot_only: bool = False, limit: int | None = None,
    max_calls_per_notice: int = 50, notice_ids: set[str] | None = None,
    concurrency: int = 1, retry_failed: bool = False,
    _deployment_limits: DeploymentLimits | None = None,
) -> dict[str, Any]:
    if isinstance(concurrency, bool) or not isinstance(concurrency, int) or concurrency < 1:
        raise ValueError("concurrency must be a positive integer")
    gold_path = gold_path.resolve(strict=True)
    manifest_path = manifest_path.resolve(strict=True)
    gold_data = json.loads(gold_path.read_text(encoding="utf-8-sig"))
    gold = GoldDataset.model_validate(gold_data)
    if gold.status != "reviewed":
        raise ValueError("只允许使用 reviewed Gold 评测")
    if scope not in {"html", "attachments"}:
        raise ValueError("scope must be html or attachments")
    records = legacy._load_sources(
        gold, manifest_path, source_root, pilot_only=pilot_only, limit=limit,
        notice_ids=notice_ids,
    )
    model_settings = effective_settings().model_copy(update={"ocr_enabled": scope == "attachments"})
    if not (model_settings.model_base_url and model_settings.model_api_key and model_settings.model_name):
        raise ValueError("hybrid/model 评测需要已配置模型接口")
    if not model_settings.model_name.casefold().startswith(("qwen", "deepseek")):
        raise ValueError("模型名需为 Qwen 或 DeepSeek 系列")
    if max_calls_per_notice < 1:
        raise ValueError("max_calls_per_notice 必须大于 0")
    limits = _deployment_limits or from_settings(settings)
    output_dir = output_dir.resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    progress_path = output_dir / "progress.json"
    stop_path = output_dir / "stop"
    run_lock = FileLock(str(output_dir / "run.lock"), timeout=0)
    try:
        run_lock.acquire()
    except Timeout as exc:
        raise ValueError("该输出目录已有评测协调器运行") from exc
    try:
        stop_path.unlink(missing_ok=True)
        identity = _identity(
            gold_path=gold_path, manifest_path=manifest_path, records=records,
            scope=scope, pilot_only=pilot_only, max_calls_per_notice=max_calls_per_notice,
            model_settings=model_settings, limits=limits, concurrency=concurrency,
        )
        if progress_path.exists():
            progress = read_json(progress_path)
            if progress.get("identity") != identity:
                raise ValueError("已有进度对应不同 Gold/source/config/code/limits；拒绝续跑")
            # Recover worker receipts after the coordinator was interrupted
            # between a child fsync and a progress.json update.
            for mode in MODES:
                for row in list(progress.get("completed", {}).get(mode, {}).values()):
                    if row.get("status") != "running" or not row.get("task_id"):
                        continue
                    receipt_path = output_dir / "task-results" / f"{row['task_id']}.json"
                    try:
                        receipt = read_json(receipt_path)
                    except (OSError, ValueError):
                        _restore_task_telemetry(row, output_dir)
                        _persist_result(progress, progress_path, row)
                        continue
                    if (
                        receipt.get("task_id") == row["task_id"]
                        and receipt.get("mode") == mode
                        and receipt.get("notice_id") == row["notice_id"]
                        and receipt.get("attempt") == row["attempt"]
                    ):
                        _persist_result(progress, progress_path, receipt)
        else:
            if any(path.name != "run.lock" for path in output_dir.iterdir()):
                raise ValueError("输出目录非空但没有 progress.json，为保护旧结果拒绝覆盖")
            progress = {
                "identity": identity,
                "run_id": uuid.uuid4().hex,
                "started_at": time.time(),
                "status": "running",
                "completed": {mode: {} for mode in MODES},
                "attempt_history": {mode: {} for mode in MODES},
                "tasks": {},
                "errors": [],
            }
            atomic_json(progress_path, progress)

        paused = False
        progress["status"] = "running"
        progress["last_updated_at"] = time.time()
        atomic_json(progress_path, progress)
        parser_limits = _parser_limits()
        for mode in MODES:
            if not _run_mode_bounded(
                mode=mode, records=records, progress=progress, progress_path=progress_path,
                output_dir=output_dir, scope=scope, model_settings=model_settings,
                parser_limits=parser_limits, max_calls_per_notice=max_calls_per_notice,
                concurrency=concurrency, deployment_limits=limits, stop_path=stop_path,
                retry_failed=retry_failed,
            ):
                paused = True
                break

        reports = {}
        all_complete = True
        for mode in MODES:
            rows = [progress["completed"].get(mode, {}).get(row["notice_id"]) for row in records]
            if any(row is None or row.get("status") != "done" for row in rows):
                all_complete = False
                continue
            reports[mode] = legacy._score_mode(gold_data, rows, output_dir, mode)
        progress["reports"] = reports
        progress["status"] = "paused" if paused else ("completed" if all_complete else "incomplete")
        progress["finished_at"] = time.time() if all_complete and not paused else None
        progress["last_updated_at"] = time.time()
        atomic_json(progress_path, progress)
        summary = legacy._summary(progress, records, reports)
        summary["status"] = progress["status"]
        summary["concurrency"] = concurrency
        summary["queue_capacity"] = limits.queue_capacity
        summary["document_workers"] = limits.document_workers
        summary["model_concurrency"] = limits.model_concurrency
        atomic_json(output_dir / "summary.json", summary)
        legacy._write_summary_markdown(output_dir / "summary.md", summary)
        return summary
    finally:
        run_lock.release()


def main(argv: list[str] | None = None) -> int:
    if argv is None:
        import sys

        argv = sys.argv[1:]
    # This command is intentionally usable from another shell without needing
    # to restate the frozen Gold, model config, or worker options.
    if argv and argv[0] == "stop":
        stop_parser = argparse.ArgumentParser(description="Stop a running bounded evaluation")
        stop_parser.add_argument("--output-dir", type=Path, required=True)
        stop_args = stop_parser.parse_args(argv[1:])
        output_dir = stop_args.output_dir.resolve()
        if not (output_dir / "progress.json").is_file():
            print("run directory does not contain progress.json")
            return 2
        (output_dir / "stop").touch(exist_ok=True)
        return 0

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--gold", type=Path, required=True)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--source-root", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--scope", choices=("html", "attachments"), default="html")
    parser.add_argument("--pilot-only", action="store_true")
    parser.add_argument("--limit", type=_positive_integer)
    parser.add_argument("--notice-id", action="append", dest="notice_ids")
    parser.add_argument("--max-calls-per-notice", type=_positive_integer, default=50)
    parser.add_argument("--concurrency", type=_positive_integer, default=1)
    parser.add_argument(
        "--retry-failed", action="store_true",
        help="retry notices with failed/incomplete results; default resumes pending/interrupted only",
    )
    args = parser.parse_args(argv)
    try:
        result = run_evaluation(
            gold_path=args.gold, manifest_path=args.manifest, source_root=args.source_root,
            output_dir=args.output_dir, scope=args.scope, pilot_only=args.pilot_only,
            limit=args.limit, max_calls_per_notice=args.max_calls_per_notice,
            concurrency=args.concurrency,
            retry_failed=args.retry_failed,
            notice_ids={value.strip() for value in args.notice_ids if value.strip()}
            if args.notice_ids else None,
        )
    except (OSError, ValueError, ValidationError, json.JSONDecodeError) as exc:
        print(f"Bounded Gold route evaluation failed: {_safe_error(exc)}")
        return 2
    print(json.dumps({
        "status": result["status"], "scope": result["scope"],
        "notice_count": result["notice_count"], "output_dir": str(args.output_dir.resolve()),
    }, ensure_ascii=False))
    return 0 if result["status"] == "completed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
