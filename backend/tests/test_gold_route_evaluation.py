from __future__ import annotations

from app import gold_route_evaluation, ingestion, model_adapter
from app.config import Settings
from app.model_adapter import model_call_budget


def test_cached_model_response_restores_api_usage_after_resume(tmp_path, monkeypatch):
    model_settings = Settings(
        model_base_url="https://example.invalid/v1",
        model_api_key="unit-test-key",
        model_name="qwen-test",
    )
    stream_calls = []

    def fake_stream(endpoint, api_key, body, *, read_timeout, total_seconds):
        stream_calls.append((endpoint, body["model"]))
        return '{"metadata": {}, "participants": [], "items": []}', {
            "prompt_tokens": 123,
            "completion_tokens": 17,
        }

    monkeypatch.setattr(model_adapter, "_stream_completion", fake_stream)

    def invoke():
        with (
            gold_route_evaluation._instrumented_ingestion(
                tmp_path, "hybrid", model_settings, {},
            ) as stats,
            model_call_budget(10) as usage,
        ):
            result = ingestion.extract_unstructured_items(
                filename="notice.html",
                text="采购项目公告正文",
                settings=model_settings,
                include_participants=True,
            )
        return result, stats, usage

    first_result, first_stats, first_usage = invoke()
    assert first_result[3] == []
    assert first_usage.requests == 1
    assert first_usage.prompt_tokens == 123
    assert first_stats["model_cache_misses"] == 1
    assert len(first_stats["model_calls"]) == 1

    second_result, second_stats, second_usage = invoke()
    assert second_result[3] == []
    assert second_usage.requests == 1
    assert second_usage.successful_responses == 1
    assert second_usage.prompt_tokens == 123
    assert second_usage.completion_tokens == 17
    assert second_stats["model_cache_hits"] == 1
    assert second_stats["model_calls"] == first_stats["model_calls"]
    assert len(stream_calls) == 1


def test_summary_counts_elapsed_time_and_usage_for_incomplete_notices():
    record = {"notice_id": "notice-1"}
    telemetry = {
        "elapsed_seconds": 3.5,
        "status": "incomplete",
        "notice_id": "notice-1",
        "model_calls": [{"transport": "success", "prompt_tokens": 30,
                          "completion_tokens": 9}],
        "api_usage": {"requests": 1, "successful_responses": 1,
                       "prompt_tokens": 30, "completion_tokens": 9,
                       "token_usage_reported_requests": 1},
    }
    progress = {
        "status": "incomplete",
        "identity": {
            "scope": "html",
            "gold_sha256": "gold-hash",
            "manifest_sha256": "manifest-hash",
            "model_name": "qwen-test",
        },
        "completed": {mode: {"notice-1": telemetry} for mode in gold_route_evaluation.MODES},
    }

    summary = gold_route_evaluation._summary(progress, [record], {})

    for mode in gold_route_evaluation.MODES:
        assert summary["modes"][mode]["completed_notices"] == 0
        assert summary["modes"][mode]["failed_or_incomplete_notices"] == 1
        assert summary["modes"][mode]["elapsed_seconds_sum"] == 3.5
        assert summary["modes"][mode]["requests"] == 1
        assert summary["modes"][mode]["prompt_tokens"] == 30


def test_summary_counts_retry_usage_but_deduplicates_cached_calls(tmp_path):
    record = {"notice_id": "notice-1"}
    call = {"elapsed_seconds": 2.0, "prompt_tokens": 30,
            "completion_tokens": 9, "transport": "success"}
    first_attempt = {
        "status": "incomplete", "notice_id": "notice-1", "elapsed_seconds": 3.0,
        "model_seconds": 2.0, "model_calls": [call],
        "api_usage": {"requests": 1, "successful_responses": 1},
    }
    final_attempt = {
        "status": "done", "notice_id": "notice-1", "elapsed_seconds": 1.0,
        "model_seconds": 0.0, "model_cache_hits": 1, "model_calls": [call],
        "api_usage": {"requests": 1, "successful_responses": 1},
    }
    progress = {
        "status": "completed",
        "identity": {
            "scope": "html", "gold_sha256": "gold-hash",
            "manifest_sha256": "manifest-hash", "model_name": "qwen-test",
        },
        "attempt_history": {mode: {"notice-1": [first_attempt]} for mode in gold_route_evaluation.MODES},
        "completed": {mode: {"notice-1": final_attempt} for mode in gold_route_evaluation.MODES},
    }

    summary = gold_route_evaluation._summary(progress, [record], {})

    assert summary["modes"]["model"]["attempt_count"] == 2
    assert summary["modes"]["model"]["elapsed_seconds_sum"] == 4.0
    assert summary["modes"]["model"]["requests"] == 1
    assert summary["modes"]["model"]["prompt_tokens"] == 30
    assert summary["modes"]["model"]["per_notice"][0]["requests"] == 1
    assert summary["modes"]["model"]["per_notice"][0]["attempts"] == 2

    summary_path = tmp_path / "summary.md"
    gold_route_evaluation._write_summary_markdown(summary_path, summary)
    assert "| model | 1/1 | N/A | N/A | 4.0s | 1.0s | 1 |" in summary_path.read_text(encoding="utf-8")


def test_distinct_requests_with_matching_telemetry_are_counted():
    telemetry = {"elapsed_seconds": 1.0, "prompt_tokens": 20,
                 "completion_tokens": 5, "transport": "success"}
    calls = gold_route_evaluation._unique_model_calls([
        {**telemetry, "call_id": "first"},
        {**telemetry, "call_id": "second"},
        {**telemetry, "call_id": "first"},
    ])
    assert len(calls) == 2


def test_ingestion_exception_keeps_api_telemetry(tmp_path, monkeypatch):
    source = tmp_path / "notice.html"
    source.write_text("采购公告", encoding="utf-8")
    model_settings = Settings(model_name="qwen-test")

    def fake_stream(*args, **kwargs):
        return "{}", {"prompt_tokens": 20, "completion_tokens": 5}

    def fail_after_call(*args, **kwargs):
        model_adapter._stream_completion("endpoint", "key", {},
                                         read_timeout=1, total_seconds=1)
        raise RuntimeError("parse stopped")

    monkeypatch.setattr(model_adapter, "_stream_completion", fake_stream)
    monkeypatch.setattr(gold_route_evaluation, "_ingest_expanded", fail_after_call)
    with model_call_budget(10):
        row = gold_route_evaluation._run_notice_mode(
            {"notice_id": "test-notice", "files": [{"name": source.name,
                                                      "path": str(source)}]},
            mode="model", scope="html", output_dir=tmp_path,
            model_settings=model_settings, limits={}, max_calls_per_notice=10,
        )
    assert row["status"] == "failed"
    assert row["errors"] == ["RuntimeError: parse stopped"]
    assert len(row["model_calls"]) == 1
    assert row["model_calls"][0]["prompt_tokens"] == 20
