from __future__ import annotations

import json
import multiprocessing
import time
from pathlib import Path

import pytest
from filelock import FileLock


def _cache_worker(cache_root: str, counter_path: str, result_path: str) -> None:
    from app import ingestion, model_adapter
    from app.bounded_runtime import DeploymentLimits, use_runtime
    from app.config import Settings
    from app.job_cache import instrumented_notice_cache
    from app.parsers import SourceDocument
    from app.schemas import NoticeMetadata

    def fake_parse(document, **_options):
        with FileLock(counter_path + ".parse.lock"):
            count = int(Path(counter_path + ".parse").read_text() or "0") if Path(
                counter_path + ".parse"
            ).exists() else 0
            Path(counter_path + ".parse").write_text(str(count + 1))
        return document.content.decode(), [], [], []

    def fake_model(*, filename, text, settings, include_participants=False):
        model_adapter._stream_completion(
            settings.model_base_url + "/chat/completions", settings.model_api_key,
            {"model": settings.model_name, "input": text}, read_timeout=3,
            total_seconds=5,
        )
        return NoticeMetadata(), [], [], []

    def fake_stream(*_args, **_kwargs):
        _kwargs['on_request_started']()
        with FileLock(counter_path + ".request.lock"):
            count_path = Path(counter_path + ".request")
            count = int(count_path.read_text() or "0") if count_path.exists() else 0
            count_path.write_text(str(count + 1))
        return "{}", {
            "prompt_tokens": 5, "completion_tokens": 2,
            "prompt_tokens_details": {"cached_tokens": 1},
            "_finish_reason": "stop",
        }

    ingestion.parse_document_with_participants = fake_parse
    ingestion.extract_unstructured_items = fake_model
    model_adapter._stream_completion = fake_stream
    settings = Settings(
        model_base_url="http://mock.local/v1", model_api_key="fake-cache-key",
        model_name="qwen-cache-test",
    )
    with (
        use_runtime(DeploymentLimits(Path(cache_root).parent / "runtime")),
        instrumented_notice_cache(Path(cache_root), "hybrid", settings, {}) as counters,
    ):
        ingestion.parse_document_with_participants(SourceDocument("notice.html", b"same body"))
        ingestion.extract_unstructured_items(
            filename="notice.html", text="same body", settings=settings,
        )
        Path(result_path).write_text(json.dumps({
            "model_calls": counters["model_calls"],
            "historical_model_calls": counters["historical_model_calls"],
            "parse_cache_hits": counters["parse_cache_hits"],
            "parse_cache_misses": counters["parse_cache_misses"],
            "model_cache_hits": counters["model_cache_hits"],
            "model_cache_misses": counters["model_cache_misses"],
            "provider_kv_tokens": counters["provider_kv_tokens"],
        }))


def test_same_cache_key_is_computed_once_across_spawn_workers(tmp_path):
    context = multiprocessing.get_context("spawn")
    cache_root = tmp_path / "cache"
    counter_path = str(tmp_path / "counter")
    results = [tmp_path / "result-1.json", tmp_path / "result-2.json"]
    workers = [context.Process(
        target=_cache_worker,
        args=(str(cache_root), counter_path, str(result_path)),
    ) for result_path in results]
    for worker in workers:
        worker.start()
    for worker in workers:
        worker.join(timeout=30)
        assert worker.exitcode == 0

    assert Path(counter_path + ".request").read_text() == "1"
    payloads = [json.loads(path.read_text(encoding="utf-8")) for path in results]
    assert sorted(len(row["model_calls"]) for row in payloads) == [0, 1]
    assert sorted(len(row["historical_model_calls"]) for row in payloads) == [0, 1]
    assert sum(row["parse_cache_hits"] for row in payloads) == 1
    assert sum(row["parse_cache_misses"] for row in payloads) == 1
    assert sum(row["model_cache_hits"] for row in payloads) == 1
    assert sum(row["model_cache_misses"] for row in payloads) == 1
    assert sum(row["provider_kv_tokens"] for row in payloads) == 1


def _parser_limit_worker(
    cache_root: str, runtime_root: str, state_path: str, result_path: str,
    payload: bytes, document_workers: int, start_barrier,
) -> None:
    from app import ingestion
    from app.bounded_runtime import DeploymentLimits, use_runtime
    from app.config import Settings
    from app.job_cache import instrumented_notice_cache

    def fake_parse(document, **_options):
        state_file = Path(state_path)
        with FileLock(state_path + ".lock"):
            state = json.loads(state_file.read_text(encoding="utf-8"))
            state["active"] += 1
            state["peak"] = max(state["peak"], state["active"])
            state_file.write_text(json.dumps(state), encoding="utf-8")
        try:
            # Different cache keys must reach this CPU/convert segment together;
            # the deployment lease, rather than the per-key cache lock, bounds it.
            time.sleep(0.3)
            return document.content.decode(), [], [], []
        finally:
            with FileLock(state_path + ".lock"):
                state = json.loads(state_file.read_text(encoding="utf-8"))
                state["active"] -= 1
                state["completed"] += 1
                state_file.write_text(json.dumps(state), encoding="utf-8")

    ingestion.parse_document_with_participants = fake_parse
    limits = DeploymentLimits(Path(runtime_root), document_workers=document_workers)

    class LazyDocument:
        filename = "notice.html"

        @property
        def content(self):
            state_file = Path(state_path)
            with FileLock(state_path + ".lock"):
                state = json.loads(state_file.read_text(encoding="utf-8"))
                state["read_active"] += 1
                state["read_peak"] = max(state["read_peak"], state["read_active"])
                state_file.write_text(json.dumps(state), encoding="utf-8")
            try:
                time.sleep(0.3)
                return payload
            finally:
                with FileLock(state_path + ".lock"):
                    state = json.loads(state_file.read_text(encoding="utf-8"))
                    state["read_active"] -= 1
                    state["read_completed"] += 1
                    state_file.write_text(json.dumps(state), encoding="utf-8")

    with (
        use_runtime(limits),
        instrumented_notice_cache(
            Path(cache_root), "rules", Settings(), {},
        ) as counters,
    ):
        start_barrier.wait(timeout=20)
        text, _, _, _ = ingestion.parse_document_with_participants(
            LazyDocument(),
        )
        Path(result_path).write_text(json.dumps({
            "text": text,
            "parse_cache_misses": counters["parse_cache_misses"],
            "parse_cache_hits": counters["parse_cache_hits"],
        }), encoding="utf-8")


@pytest.mark.parametrize("document_workers", [1, 2])
def test_different_cache_keys_share_document_limit_across_spawn_workers(
    tmp_path, document_workers,
):
    context = multiprocessing.get_context("spawn")
    worker_count = 4
    start_barrier = context.Barrier(worker_count)
    state_path = tmp_path / "parser-state.json"
    state_path.write_text(json.dumps({
        "active": 0, "peak": 0, "completed": 0,
        "read_active": 0, "read_peak": 0, "read_completed": 0,
    }))
    results = [tmp_path / f"parser-{index}.json" for index in range(worker_count)]
    workers = [context.Process(
        target=_parser_limit_worker,
        args=(str(tmp_path / "cache"), str(tmp_path / "runtime"), str(state_path),
              str(results[index]), f"unique notice {index}".encode(),
              document_workers, start_barrier),
    ) for index in range(worker_count)]
    for worker in workers:
        worker.start()
    for worker in workers:
        worker.join(timeout=30)
        assert worker.exitcode == 0

    state = json.loads(state_path.read_text(encoding="utf-8"))
    assert state == {
        "active": 0, "peak": document_workers, "completed": worker_count,
        "read_active": 0, "read_peak": document_workers, "read_completed": worker_count,
    }
    payloads = [json.loads(path.read_text(encoding="utf-8")) for path in results]
    assert len({row["text"] for row in payloads}) == worker_count
    assert all(row["parse_cache_misses"] == 1 and row["parse_cache_hits"] == 0
               for row in payloads)


def test_corrupt_cache_is_a_miss_and_config_or_input_changes_isolate_entries(
    tmp_path, monkeypatch,
):
    from app import ingestion, model_adapter
    from app.config import Settings
    from app.job_cache import instrumented_notice_cache
    from app.parsers import SourceDocument
    from app.schemas import NoticeMetadata

    cache_root = tmp_path / "cache"
    requests = []
    parses = []

    def fake_parse(document, **_options):
        parses.append(document.content)
        return document.content.decode(), [], [], []

    def fake_model(*, filename, text, settings, include_participants=False):
        model_adapter._stream_completion(
            settings.model_base_url + "/chat/completions", settings.model_api_key,
            {"model": settings.model_name, "max_tokens": settings.model_max_output_tokens,
             "input": text}, read_timeout=3, total_seconds=5,
        )
        return NoticeMetadata(), [], [], []

    def fake_stream(*_args, **_kwargs):
        _kwargs['on_request_started']()
        requests.append(1)
        return "{}", {"prompt_tokens": 5, "completion_tokens": 2}

    monkeypatch.setattr(ingestion, "parse_document_with_participants", fake_parse)
    monkeypatch.setattr(ingestion, "extract_unstructured_items", fake_model)
    monkeypatch.setattr(model_adapter, "_stream_completion", fake_stream)

    base = Settings(
        model_base_url="http://mock-a.local/v1", model_api_key="key-a",
        model_name="qwen-cache-test", model_max_output_tokens=256,
    )

    def invoke(settings, text="same body", root=cache_root):
        with instrumented_notice_cache(root, "hybrid", settings, {}) as counters:
            ingestion.parse_document_with_participants(SourceDocument("notice.html", text.encode()))
            ingestion.extract_unstructured_items(
                filename="notice.html", text=text, settings=settings,
            )
            return dict(counters)

    first = invoke(base)
    hit = invoke(base)
    assert first["model_cache_misses"] == 1 and len(first["model_calls"]) == 1
    assert hit["model_cache_hits"] == 1 and not hit["model_calls"]
    assert len(hit["historical_model_calls"]) == 1

    for changed in (
        base.model_copy(update={"model_name": "qwen-other"}),
        base.model_copy(update={"model_base_url": "http://mock-b.local/v1"}),
        base.model_copy(update={"model_api_key": "key-b"}),
        base.model_copy(update={"model_max_output_tokens": 512}),
    ):
        assert invoke(changed)["model_cache_misses"] == 1
    assert invoke(base, text="different body")["model_cache_misses"] == 1
    assert len(requests) == 6

    corrupt_root = tmp_path / "corrupt-cache"
    assert invoke(base, root=corrupt_root)["model_cache_misses"] == 1
    model_entries = list((corrupt_root / "model-cache" / "hybrid").glob("*.json"))
    parse_entries = list((corrupt_root / "parse-cache").glob("*.json"))
    model_entries[0].write_text("{partial", encoding="utf-8")
    parse_entries[0].write_text("{partial", encoding="utf-8")
    recovered = invoke(base, root=corrupt_root)
    assert recovered["model_cache_misses"] == 1
    assert recovered["parse_cache_misses"] == 1
    assert len(requests) == 8
    assert parses.count(b"same body") == 3

    code_identity_before = len(list((cache_root / "model-cache" / "hybrid").glob("*.json")))
    monkeypatch.setattr(model_adapter, "__file__", str(tmp_path / "different-adapter.py"))
    assert invoke(base)["model_cache_misses"] == 1
    assert len(list((cache_root / "model-cache" / "hybrid").glob("*.json"))) == (
        code_identity_before + 1
    )
