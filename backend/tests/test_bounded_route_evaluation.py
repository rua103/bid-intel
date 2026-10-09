from __future__ import annotations

import argparse
import csv
import hashlib
import json
import threading
import time
from concurrent.futures import Future, ThreadPoolExecutor
from concurrent.futures.process import BrokenProcessPool
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import Mock

import pytest

from app import bounded_route_evaluation as bounded
from app import gold_route_evaluation as legacy
from app.bounded_runtime import DeploymentLimits
from app.config import Settings


@contextmanager
def _synthetic_sse(delay=0.1):
    state = {"active": 0, "maximum": 0, "requests": [], "fail": False}
    lock = threading.Lock()

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_args):
            pass

        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            with lock:
                state["active"] += 1
                state["maximum"] = max(state["maximum"], state["active"])
                state["requests"].append(body)
            try:
                time.sleep(delay)
                if state["fail"]:
                    self.send_response(429)
                    self.end_headers()
                    return
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.end_headers()
                response = json.dumps({"metadata": {}, "items": [], "participants": []})
                frames = [
                    {"choices": [{"delta": {"content": response}}]},
                    {"choices": [{"delta": {}, "finish_reason": "stop"}]},
                    {"choices": [], "usage": {"prompt_tokens": 11, "completion_tokens": 3}},
                ]
                for frame in frames:
                    self.wfile.write(f"data: {json.dumps(frame)}\n\n".encode())
                self.wfile.write(b"data: [DONE]\n\n")
                self.wfile.flush()
            finally:
                with lock:
                    state["active"] -= 1

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}/v1", state
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def _synthetic_inputs(tmp_path, count=3):
    source = tmp_path / "source"
    source.mkdir()
    records = []
    for index in range(count):
        content = f"<html><body>项目名称：合成项目 {index}。采购公告正文 {index}。</body></html>".encode()
        name = f"notice-{index}.html"
        (source / name).write_bytes(content)
        notice_id = hashlib.sha256(content).hexdigest()[:20]
        records.append((notice_id, name))
    gold = tmp_path / "gold.reviewed.json"
    gold.write_text(json.dumps({
        "schema_version": "1.0",
        "status": "reviewed",
        "notices": [{"notice_id": notice_id, "packages": []} for notice_id, _ in records],
    }), encoding="utf-8")
    manifest = tmp_path / "manifest.csv"
    with manifest.open("w", encoding="utf-8", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=[
            "notice_id", "official_file", "assignment", "categories", "source_files",
        ])
        writer.writeheader()
        for notice_id, name in records:
            writer.writerow({
                "notice_id": notice_id, "official_file": name,
                "assignment": "pilot:A", "categories": "synthetic", "source_files": name,
            })
    return gold, manifest, source


def test_concurrency_requires_positive_integer_before_io(tmp_path):
    with pytest.raises(ValueError, match="positive integer"):
        bounded.run_evaluation(
            gold_path=tmp_path / "missing-gold.json",
            manifest_path=tmp_path / "missing-manifest.csv",
            source_root=tmp_path / "missing-sources",
            output_dir=tmp_path / "out",
            concurrency=0,
        )
    for value in (0, -1, "2", True, 1.5):
        with pytest.raises(ValueError, match="positive integer"):
            bounded.run_evaluation(
                gold_path=tmp_path / "missing-gold.json",
                manifest_path=tmp_path / "missing-manifest.csv",
                source_root=tmp_path / "missing-sources",
                output_dir=tmp_path / "out",
                concurrency=value,
            )


@pytest.mark.parametrize("value", ["0", "-1", "2.5", "two"])
def test_cli_positive_integer_rejects_invalid_concurrency(value):
    with pytest.raises(argparse.ArgumentTypeError):
        bounded._positive_integer(value)


def test_coordinator_writes_unique_attempts_in_input_order_at_concurrency_one(
    tmp_path, monkeypatch,
):
    submitted = []

    class ImmediatePool:
        def __init__(self, *, max_workers, mp_context):
            assert max_workers == 1

        def submit(self, function, task):
            submitted.append(task)
            future = Future()
            future.set_result({
                "task_id": task["task_id"],
                "mode": task["mode"],
                "notice_id": task["record"]["notice_id"],
                "attempt": task["attempt"],
                "status": "done",
                "errors": [],
                "model_calls": [],
                "api_usage": {"requests": 0, "successful_responses": 0},
                "elapsed_seconds": 0.01,
            })
            return future

        def shutdown(self, *, wait, cancel_futures):
            assert wait is True

    monkeypatch.setattr(bounded, "ProcessPoolExecutor", ImmediatePool)
    limits = DeploymentLimits(tmp_path / "runtime", queue_capacity=1)
    progress = {
        "run_id": "run-1",
        "completed": {mode: {} for mode in bounded.MODES},
        "attempt_history": {mode: {} for mode in bounded.MODES},
        "tasks": {},
        "errors": [],
    }
    records = [{"notice_id": f"notice-{index}", "files": []} for index in range(3)]
    progress_path = tmp_path / "progress.json"
    progress_path.write_text("{}", encoding="utf-8")

    finished = bounded._run_mode_bounded(
        mode="rules",
        records=records,
        progress=progress,
        progress_path=progress_path,
        output_dir=tmp_path,
        scope="html",
        model_settings=Settings(model_name="qwen-test"),
        parser_limits={},
        max_calls_per_notice=5,
        concurrency=1,
        deployment_limits=limits,
        stop_path=tmp_path / "stop",
    )

    assert finished is True
    assert [task["record"]["notice_id"] for task in submitted] == [
        "notice-0", "notice-1", "notice-2",
    ]
    results = list(progress["completed"]["rules"].values())
    assert all(row["status"] == "done" for row in results)
    assert all({"mode", "notice_id", "attempt", "status", "errors",
                "model_calls", "api_usage", "task_id"} <= row.keys()
               for row in results)
    assert len(progress["tasks"]) == 3
    assert len(set(progress["tasks"])) == 3


def test_resume_attempt_is_archived_and_retry_number_increments():
    previous = {"attempt": 2, "status": "failed", "errors": ["HTTP 429"]}
    progress = {
        "completed": {"model": {"n1": previous}},
        "attempt_history": {"model": {"n1": [{"attempt": 1, "status": "failed"}]}},
    }

    assert bounded._attempt_number(progress, "model", "n1") == 3
    bounded._archive_previous(progress, "model", "n1", previous)
    assert progress["attempt_history"]["model"]["n1"][-1] == previous


def test_failed_evaluator_notice_requires_explicit_retry(tmp_path, monkeypatch):
    submitted = []

    class ImmediatePool:
        def __init__(self, *, max_workers, mp_context):
            assert max_workers == 1

        def submit(self, _function, task):
            submitted.append(task)
            future = Future()
            future.set_result({
                "task_id": task["task_id"], "mode": task["mode"],
                "notice_id": task["record"]["notice_id"], "attempt": task["attempt"],
                "status": "done", "errors": [], "model_calls": [],
                "api_usage": {"requests": 0, "successful_responses": 0},
                "elapsed_seconds": 0.01,
            })
            return future

        def shutdown(self, *, wait, cancel_futures):
            assert wait

    monkeypatch.setattr(bounded, "ProcessPoolExecutor", ImmediatePool)
    limits = DeploymentLimits(tmp_path / "runtime")
    prior = {
        "task_id": "old-task", "mode": "model", "notice_id": "n1",
        "attempt": 1, "status": "failed", "errors": ["HTTP 429"],
        "model_calls": [{"call_id": "old-request"}],
        "api_usage": {"requests": 1, "successful_responses": 0},
        "elapsed_seconds": 0.2,
    }
    progress = {
        "run_id": "run-retry", "completed": {mode: {} for mode in bounded.MODES},
        "attempt_history": {mode: {} for mode in bounded.MODES},
        "tasks": {"old-task": prior}, "errors": [],
    }
    progress["completed"]["model"]["n1"] = prior
    record = {"notice_id": "n1", "files": []}
    progress_path = tmp_path / "progress.json"
    progress_path.write_text("{}", encoding="utf-8")

    def run(retry_failed):
        return bounded._run_mode_bounded(
            mode="model", records=[record], progress=progress,
            progress_path=progress_path, output_dir=tmp_path, scope="html",
            model_settings=Settings(model_name="qwen-test"), parser_limits={},
            max_calls_per_notice=5, concurrency=1,
            deployment_limits=limits, stop_path=tmp_path / "stop",
            retry_failed=retry_failed,
        )

    assert run(retry_failed=False)
    assert submitted == []
    assert progress["completed"]["model"]["n1"] == prior
    assert run(retry_failed=True)
    assert submitted[0]["attempt"] == 2
    assert progress["attempt_history"]["model"]["n1"] == [prior]
    assert progress["completed"]["model"]["n1"]["status"] == "done"


def test_summary_separates_local_history_from_fresh_requests():
    row = {
        "task_id": "t1", "mode": "hybrid", "notice_id": "n1", "attempt": 1,
        "status": "done", "elapsed_seconds": 0.5,
        "model_calls": [], "api_usage": {"requests": 0, "successful_responses": 0},
        "historical_cached_requests": 3, "provider_kv_tokens": 8,
    }
    progress = {
        "identity": {"scope": "html", "gold_sha256": "g", "manifest_sha256": "m",
                     "model_name": "qwen-test"},
        "status": "completed", "completed": {mode: {} for mode in bounded.MODES},
        "attempt_history": {mode: {} for mode in bounded.MODES},
    }
    progress["completed"]["hybrid"]["n1"] = row
    summary = legacy._summary(progress, [{"notice_id": "n1"}], {})
    assert summary["modes"]["hybrid"]["requests"] == 0
    assert summary["modes"]["hybrid"]["local_cache_restored_requests"] == 3
    assert summary["modes"]["hybrid"]["provider_kv_tokens"] == 8


def test_restart_recovers_incomplete_attempt_telemetry_before_retry(
    tmp_path, monkeypatch,
):
    from app.bounded_runtime import atomic_json, read_json

    gold_path, manifest_path, source_root = _synthetic_inputs(tmp_path, count=1)
    output_dir = tmp_path / "restarted-run"
    limits = DeploymentLimits(tmp_path / "runtime")
    model_settings = Settings(
        model_base_url="http://offline.invalid/v1", model_api_key="fake-restart-key",
        model_name="qwen-test",
    )
    monkeypatch.setattr(bounded, "effective_settings", lambda: model_settings)
    monkeypatch.setattr(bounded, "_run_mode_bounded", lambda **_kwargs: False)

    def resume():
        return bounded.run_evaluation(
            gold_path=gold_path, manifest_path=manifest_path,
            source_root=source_root, output_dir=output_dir,
            _deployment_limits=limits,
        )

    resume()
    progress_path = output_dir / "progress.json"
    progress = read_json(progress_path)
    notice_id = progress["identity"]["notice_ids"][0]
    stub = {
        "task_id": "interrupted-request-task", "mode": "hybrid",
        "notice_id": notice_id, "attempt": 1, "status": "running",
        "errors": [], "model_calls": [],
        "api_usage": {"requests": 0, "successful_responses": 0},
    }
    progress["completed"]["hybrid"][notice_id] = stub
    progress["tasks"][stub["task_id"]] = stub
    atomic_json(progress_path, progress)
    call = {
        "call_id": "sent-before-restart", "transport": "success",
        "elapsed_seconds": 0.75, "prompt_tokens": 13, "completion_tokens": 5,
        "provider_kv_tokens": 3,
    }
    atomic_json(output_dir / "task-results" / f"{stub['task_id']}.telemetry.json", {
        "model_calls": [call], "historical_model_calls": [{"call_id": "cached-old-call"}],
        "provider_kv_tokens": 3,
    })

    resumed_summary = resume()
    recovered = read_json(progress_path)
    recovered_row = recovered["completed"]["hybrid"][notice_id]
    assert recovered_row["model_calls"] == [call]
    assert recovered_row["api_usage"] == {
        "requests": 1, "successful_responses": 1,
        "prompt_tokens": 13, "completion_tokens": 5,
    }
    assert resumed_summary["modes"]["hybrid"]["requests"] == 1
    assert resumed_summary["modes"]["hybrid"]["prompt_tokens"] == 13
    assert resumed_summary["modes"]["hybrid"]["provider_kv_tokens"] == 3
    assert resumed_summary["modes"]["hybrid"]["local_cache_restored_requests"] == 1
    assert resumed_summary["modes"]["hybrid"]["elapsed_seconds_sum"] == 0.75

    bounded._archive_previous(recovered, "hybrid", notice_id, recovered_row)
    recovered["completed"]["hybrid"][notice_id] = {
        "attempt": 2, "status": "done", "notice_id": notice_id,
        "elapsed_seconds": 0.1, "model_calls": [],
        "api_usage": {"requests": 0, "successful_responses": 0},
        "historical_cached_requests": 1,
    }
    summary = legacy._summary(recovered, [{"notice_id": notice_id}], {})
    assert summary["modes"]["hybrid"]["requests"] == 1
    assert summary["modes"]["hybrid"]["attempt_count"] == 2
    assert recovered["attempt_history"]["hybrid"][notice_id][0]["status"] == "interrupted"
    assert "fake-restart-key" not in progress_path.read_text(encoding="utf-8")


def test_broken_evaluator_child_is_interrupted_and_keeps_receipted_sibling(
    tmp_path,
):
    task = {
        "task_id": "dead-child", "mode": "hybrid", "record": {"notice_id": "n1"},
        "attempt": 1, "model_settings": {"model_api_key": "fake-key"},
        "output_dir": str(tmp_path),
    }
    progress = {"completed": {"hybrid": {}}, "tasks": {}, "errors": []}
    progress_path = tmp_path / "progress.json"
    progress_path.write_text("{}", encoding="utf-8")
    future = Future()
    future.set_exception(BrokenProcessPool("synthetic crash"))
    active = {future: (task, Mock(release=Mock()))}

    bounded._drain_futures(active, progress, progress_path)

    row = progress["completed"]["hybrid"]["n1"]
    assert row["status"] == "interrupted"
    assert row["errors"]
    assert "fake-key" not in progress_path.read_text(encoding="utf-8")

    receipt = dict(row, task_id="receipted-sibling", notice_id="n2", status="done")
    task2 = {**task, "task_id": "receipted-sibling", "record": {"notice_id": "n2"}}
    receipt_path = tmp_path / "task-results" / "receipted-sibling.json"
    from app.bounded_runtime import atomic_json

    atomic_json(receipt_path, receipt)
    receipt_future = Future()
    receipt_future.set_exception(BrokenProcessPool("pool broken after child wrote receipt"))
    active = {receipt_future: (task2, Mock(release=Mock()))}
    bounded._drain_futures(active, progress, progress_path)
    assert progress["completed"]["hybrid"]["n2"]["task_id"] == "receipted-sibling"


def test_model_key_is_never_written_to_identity(tmp_path):
    gold = tmp_path / "gold.json"
    manifest = tmp_path / "manifest.csv"
    gold.write_text("{}", encoding="utf-8")
    manifest.write_text("notice_id\n", encoding="utf-8")
    model_settings = Settings(
        model_base_url="http://localhost:9999/v1",
        model_api_key="synthetic-secret-never-persist",
        model_name="qwen-test",
    )
    identity = bounded._identity(
        gold_path=gold,
        manifest_path=manifest,
        records=[],
        scope="html",
        pilot_only=True,
        max_calls_per_notice=10,
        model_settings=model_settings,
        limits=DeploymentLimits(tmp_path / "runtime"),
        concurrency=1,
    )

    assert "synthetic-secret-never-persist" not in str(identity)
    assert identity["code_sha256"]


def test_html_evaluator_keeps_file_bytes_lazy_until_ingestion(tmp_path, monkeypatch):
    from app.archive_files import DiskDocument
    from app.schemas import ImportResult

    source = tmp_path / "notice.html"
    source.write_text("synthetic html", encoding="utf-8")
    seen = []

    def ingest(documents, _warnings, **_kwargs):
        assert len(documents) == 1
        assert isinstance(documents[0], DiskDocument)
        seen.append(documents[0].path)
        return ImportResult(
            notice_id=0, source_files=["notice.html"], items_found=0, items=[], metadata={},
        )

    def reject_eager_read(_path):
        raise AssertionError("HTML was read before the bounded cache/parse segment")

    monkeypatch.setattr(Path, "read_bytes", reject_eager_read)
    monkeypatch.setattr(legacy, "_ingest_expanded", ingest)
    row = bounded._run_notice_mode(
        {"notice_id": "n1", "files": [{"name": "notice.html", "path": str(source)}]},
        mode="rules", scope="html", output_dir=tmp_path / "run",
        model_settings=Settings(), limits={"job_max_member_mb": 1},
        max_calls_per_notice=1,
    )
    assert seen == [source]
    assert row["notice_id"] == "n1"
    assert row["status"] == "done"


def test_html_evaluator_rejects_oversize_file_before_reading(tmp_path, monkeypatch):
    source = tmp_path / "oversize.html"
    with source.open("wb") as stream:
        stream.truncate(1024**2 + 1)
    monkeypatch.setattr(Path, "read_bytes", Mock(side_effect=AssertionError("eager read")))
    with pytest.raises(ValueError, match="单文件超过后台任务限制"):
        bounded._run_notice_mode(
            {"notice_id": "n1", "files": [{"name": source.name, "path": str(source)}]},
            mode="rules", scope="html", output_dir=tmp_path / "run",
            model_settings=Settings(), limits={"job_max_member_mb": 1},
            max_calls_per_notice=1,
        )


@pytest.mark.parametrize('parameter', ['model_timeout_seconds', 'model_stream_total_seconds'])
def test_resume_rejects_changed_request_timeout_before_scheduling(tmp_path, monkeypatch, parameter):
    gold, manifest, source = _synthetic_inputs(tmp_path, count=1)
    model_settings = Settings(
        model_base_url='http://localhost:9999/v1', model_name='qwen-test',
        model_api_key='synthetic-no-request-key',
    )
    monkeypatch.setattr(bounded, 'effective_settings', lambda: model_settings)
    scheduler = Mock(return_value=False)
    monkeypatch.setattr(bounded, '_run_mode_bounded', scheduler)
    run_args = {
        'gold_path': gold, 'manifest_path': manifest, 'source_root': source,
        'output_dir': tmp_path / 'run',
        '_deployment_limits': DeploymentLimits(tmp_path / 'runtime'),
    }
    assert bounded.run_evaluation(**run_args)['status'] == 'paused'
    progress_before = (tmp_path / 'run' / 'progress.json').read_bytes()
    scheduler.reset_mock()
    setattr(model_settings, parameter, getattr(model_settings, parameter) + 1)

    with pytest.raises(ValueError, match='拒绝续跑'):
        bounded.run_evaluation(**run_args)

    scheduler.assert_not_called()
    assert (tmp_path / 'run' / 'progress.json').read_bytes() == progress_before


def test_parser_and_office_limits_are_frozen_into_spawned_task(tmp_path, monkeypatch):
    from app.config import settings as process_settings

    received = {}

    def fake_legacy(*_args, **kwargs):
        received.update(kwargs)
        from app import archive_files

        received["global_limits"] = {
            "job_max_member_mb": archive_files.settings.job_max_member_mb,
            "document_conversion_timeout_seconds": (
                process_settings.document_conversion_timeout_seconds
            ),
        }
        return {"status": "done", "notice_id": "n1", "errors": []}

    monkeypatch.setattr(bounded, "_run_notice_mode", fake_legacy)
    task = {
        "record": {"notice_id": "n1", "files": []}, "mode": "rules",
        "scope": "html", "output_dir": str(tmp_path),
        "model_settings": Settings(model_name="qwen-test").model_dump(),
        "limits": {"runtime_dir": str(tmp_path / "runtime"),
                   "document_workers": 1, "model_concurrency": 2,
                   "queue_capacity": 2},
        "parser_limits": {"job_max_member_mb": 17,
                          "document_conversion_timeout_seconds": 23,
                          "libreoffice_path": "", "job_max_expanded_mb": 30,
                          "job_max_archive_depth": 2, "job_max_archive_files": 5,
                          "pdf_max_pages": 10, "pdf_max_ocr_pages": 4,
                          "ocr_engine": "rapidocr"},
        "max_calls_per_notice": 5, "attempt": 1, "task_id": "freeze-check",
        "stop_path": str(tmp_path / "stop"),
    }

    bounded._worker_run_task(task)

    assert received["limits"]["job_max_member_mb"] == 17
    assert received["global_limits"] == {
        "job_max_member_mb": 17,
        "document_conversion_timeout_seconds": 23,
    }
    assert process_settings.document_conversion_timeout_seconds != 23


def test_spawned_evaluator_matches_serial_and_passes_final_report_gate(
    tmp_path, monkeypatch,
):
    from app import route_evaluation_report

    gold_path, manifest_path, source_root = _synthetic_inputs(tmp_path, count=2)
    tuning_gold = tmp_path / "tuning.reviewed.json"
    tuning_gold.write_text(json.dumps({
        "schema_version": "1.0", "status": "reviewed",
        "notices": [{"notice_id": "independent-tuning-id", "packages": []}],
    }), encoding="utf-8")

    with _synthetic_sse(delay=0.35) as (base_url, state):
        model_settings = Settings(
            model_base_url=base_url, model_api_key="fake-evaluator-key",
            model_name="qwen-plus", model_max_output_tokens=256,
        )
        monkeypatch.setattr(bounded, "effective_settings", lambda: model_settings)
        monkeypatch.setattr(legacy, "effective_settings", lambda: model_settings)
        limits = DeploymentLimits(
            tmp_path / "shared-runtime", document_workers=1,
            model_concurrency=2, queue_capacity=4,
        )

        legacy_dir = tmp_path / "legacy-serial"
        legacy.run_evaluation(
            gold_path=gold_path, manifest_path=manifest_path,
            source_root=source_root, output_dir=legacy_dir,
            max_calls_per_notice=5,
        )
        bounded_serial_dir = tmp_path / "bounded-serial"
        bounded.run_evaluation(
            gold_path=gold_path, manifest_path=manifest_path,
            source_root=source_root, output_dir=bounded_serial_dir,
            max_calls_per_notice=5, concurrency=1,
            _deployment_limits=limits,
        )
        for mode in bounded.MODES:
            assert json.loads((legacy_dir / f"predictions-{mode}.json").read_text()) == json.loads(
                (bounded_serial_dir / f"predictions-{mode}.json").read_text()
            )

        parallel_dir = tmp_path / "bounded-parallel"
        summary = bounded.run_evaluation(
            gold_path=gold_path, manifest_path=manifest_path,
            source_root=source_root, output_dir=parallel_dir,
            max_calls_per_notice=5, concurrency=2,
            _deployment_limits=limits,
        )

    assert state["maximum"] == 2
    assert summary["status"] == "completed"
    assert summary["modes"]["hybrid"]["requests"] == 2
    assert summary["modes"]["model"]["requests"] == 2
    progress = json.loads((parallel_dir / "progress.json").read_text(encoding="utf-8"))
    for mode in bounded.MODES:
        rows = list(progress["completed"][mode].values())
        assert len(rows) == 2
        assert len({row["task_id"] for row in rows}) == 2
        assert all(row["status"] == "done" for row in rows)
        assert all({"mode", "notice_id", "attempt", "status", "errors",
                    "model_calls", "api_usage"} <= row.keys() for row in rows)

    final = route_evaluation_report.build_report(
        gold_path=gold_path, manifest_path=manifest_path,
        run_dir=parallel_dir, dataset_role="holdout", stage="final",
        tuning_gold_path=tuning_gold,
    )
    assert final["status"] == "completed"


def test_two_evaluation_runs_share_one_deployment_request_cap(tmp_path, monkeypatch):
    gold_path, manifest_path, source_root = _synthetic_inputs(tmp_path, count=3)
    with _synthetic_sse(delay=0.4) as (base_url, state):
        model_settings = Settings(
            model_base_url=base_url, model_api_key="fake-multi-run-key",
            model_name="qwen-plus", model_max_output_tokens=256,
        )
        monkeypatch.setattr(bounded, "effective_settings", lambda: model_settings)
        monkeypatch.setattr(legacy, "effective_settings", lambda: model_settings)
        limits = DeploymentLimits(
            tmp_path / "one-deployment-runtime", document_workers=1,
            model_concurrency=2, queue_capacity=8,
        )

        def run(name):
            return bounded.run_evaluation(
                gold_path=gold_path, manifest_path=manifest_path,
                source_root=source_root, output_dir=tmp_path / name,
                max_calls_per_notice=5, concurrency=2,
                _deployment_limits=limits,
            )

        with ThreadPoolExecutor(max_workers=2) as pool:
            summaries = list(pool.map(run, ("run-a", "run-b")))

    assert all(summary["status"] == "completed" for summary in summaries)
    assert state["maximum"] == 2
    assert len(state["requests"]) == 12
