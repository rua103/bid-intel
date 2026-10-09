"""Offline contracts for the shared runtime and OpenAI-compatible SSE adapter.

Every HTTP exchange is served by a loopback-only synthetic server. Tests use fake
credentials and never import model-provider SDKs or contact external endpoints.
"""
from __future__ import annotations

import asyncio
import json
import multiprocessing
import os
import threading
import time
from concurrent.futures import ProcessPoolExecutor, ThreadPoolExecutor
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import httpx
import pytest
from filelock import FileLock

from app.bounded_runtime import (
    DeploymentLimiter,
    DeploymentLimits,
    RuntimeConfigurationError,
    SchedulingStopped,
    atomic_json,
    read_json,
    use_runtime,
)
from app.config import Settings
from app.model_adapter import (
    _model_failure_warning,
    apply_thinking_setting,
    extract_unstructured_items,
)


@pytest.fixture(autouse=True)
def loopback_requests_bypass_environment_proxies(monkeypatch):
    monkeypatch.setenv('NO_PROXY', '127.0.0.1,localhost')
    monkeypatch.setenv('no_proxy', '127.0.0.1,localhost')


class _SSEState:
    def __init__(self):
        self.lock = threading.Lock()
        self.active = 0
        self.maximum = 0
        self.requests: list[dict] = []


@contextmanager
def fake_sse_server():
    state = _SSEState()

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.0"

        def log_message(self, *_args):
            pass

        def do_POST(self):
            payload = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            with state.lock:
                state.active += 1
                state.maximum = max(state.maximum, state.active)
                state.requests.append({
                    "path": self.path,
                    "body": payload,
                    "authorization": self.headers.get("Authorization"),
                })
            try:
                if "/429" in self.path:
                    self.send_response(429)
                    self.end_headers()
                    self.wfile.write(b"fake quota response")
                    return
                if "/timeout" in self.path:
                    time.sleep(1.35)
                # Hold each request open long enough for callers to overlap.
                time.sleep(0.08)
                if "/truncated/" in self.path:
                    content, reason = '{"items": [', "length"
                elif "/invalid/" in self.path:
                    content, reason = "definitely not JSON", "stop"
                else:
                    content, reason = '{"items": [], "participants": []}', "stop"
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.end_headers()
                frames = [
                    {"choices": [{"delta": {"content": content}}]},
                    {"choices": [{"delta": {}, "finish_reason": reason}]},
                    # OpenAI-compatible streams can send usage after finish_reason.
                    {"choices": [], "usage": {"prompt_tokens": 17, "completion_tokens": 5}},
                ]
                for frame in frames:
                    self.wfile.write(f"data: {json.dumps(frame)}\n\n".encode())
                    self.wfile.flush()
                if "/cutoff" not in self.path:
                    self.wfile.write(b"data: [DONE]\n\n")
                    self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError):
                pass
            finally:
                with state.lock:
                    state.active -= 1

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}", state
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def _settings(base_url: str, model: str = "qwen-plus") -> Settings:
    return Settings(
        model_base_url=base_url,
        model_api_key="offline-test-key",
        model_name=model,
        model_timeout_seconds=1,
        model_stream_total_seconds=5,
    )


def _process_slot_worker(runtime_dir: str, result_path: str, rounds: int) -> None:
    """Top-level for Windows spawn; measure simultaneous process leases safely."""
    limits = DeploymentLimits(Path(runtime_dir), model_concurrency=2)
    limiter = DeploymentLimiter(limits)
    result = Path(result_path)
    lock_path = result.with_suffix(".lock")
    async def run_rounds():
        for _ in range(rounds):
            async with limiter.model_slot():
                with FileLock(str(lock_path)):
                    current = read_json(result)
                    current["active"] += 1
                    current["maximum"] = max(current["maximum"], current["active"])
                    atomic_json(result, current)
                await asyncio.sleep(0.07)
                with FileLock(str(lock_path)):
                    current = read_json(result)
                    current["active"] -= 1
                    atomic_json(result, current)

    asyncio.run(run_rounds())


def _crash_with_document_lease(runtime_dir: str, marker_path: str) -> None:
    limiter = DeploymentLimiter(DeploymentLimits(Path(runtime_dir)))
    with limiter.document_slot():
        atomic_json(Path(marker_path), {"admitted": True})
        os._exit(17)


def _http_process_worker(runtime_dir: str, endpoint: str) -> tuple[str, dict]:
    from app.model_adapter import async_stream_completion

    limiter = DeploymentLimiter(DeploymentLimits(Path(runtime_dir), model_concurrency=2))
    return asyncio.run(async_stream_completion(
        endpoint, "spawn-fake-key", {"model": "qwen-plus"}, read_timeout=2,
        total_seconds=15, limiter=limiter,
    ))


def test_deployment_limits_are_separate_and_configuration_is_fixed(tmp_path):
    limits = DeploymentLimits(tmp_path / "runtime", document_workers=1,
                              model_concurrency=2, queue_capacity=3)
    limiter = DeploymentLimiter(limits)
    document = limiter.try_acquire("document")
    first = limiter.try_acquire("model")
    second = limiter.try_acquire("model")
    queued = limiter.try_acquire("queue")
    assert document is not None
    assert first is not None and second is not None
    assert limiter.try_acquire("model") is None
    first.release()
    second.release()
    document.release()
    assert queued is not None
    queued.release()
    with pytest.raises(RuntimeConfigurationError):
        DeploymentLimiter(DeploymentLimits(tmp_path / "runtime", model_concurrency=3))


def test_filesystem_model_leases_bound_multiple_spawned_processes(tmp_path):
    runtime_dir = tmp_path / "runtime"
    limits = DeploymentLimits(runtime_dir, model_concurrency=2)
    DeploymentLimiter(limits)
    result = tmp_path / "measure.json"
    atomic_json(result, {"active": 0, "maximum": 0})
    # Explicit spawn exercises the Windows process model as well as Linux.
    context = multiprocessing.get_context("spawn")
    with ProcessPoolExecutor(max_workers=4, mp_context=context) as pool:
        futures = [pool.submit(_process_slot_worker, str(runtime_dir), str(result), 3)
                   for _ in range(4)]
        for future in futures:
            future.result(timeout=20)
    assert read_json(result) == {"active": 0, "maximum": 2}


def test_worker_crash_releases_document_lease_without_manual_cleanup(tmp_path):
    runtime_dir, marker = tmp_path / "runtime", tmp_path / "marker.json"
    limiter = DeploymentLimiter(DeploymentLimits(runtime_dir))
    process = multiprocessing.get_context("spawn").Process(
        target=_crash_with_document_lease, args=(str(runtime_dir), str(marker)),
    )
    process.start()
    process.join(timeout=10)
    assert process.exitcode == 17
    assert read_json(marker) == {"admitted": True}
    # OS locks disappear even when the worker never executes a finally block.
    with limiter.document_slot(timeout=1):
        pass


def test_actual_http_requests_across_processes_share_the_model_limit(tmp_path):
    runtime_dir = tmp_path / "runtime"
    DeploymentLimiter(DeploymentLimits(runtime_dir, model_concurrency=2))
    with (
        fake_sse_server() as (base_url, state),
        ProcessPoolExecutor(
            max_workers=4, mp_context=multiprocessing.get_context("spawn")
        ) as pool,
    ):
        futures = [pool.submit(_http_process_worker, str(runtime_dir),
                               base_url + "/openai/v1/chat/completions") for _ in range(4)]
        results = [future.result(timeout=30) for future in futures]
    assert len(results) == len(state.requests) == 4
    assert state.maximum <= 2
    assert all(result[1]["prompt_tokens"] == 17 for result in results)


def test_async_sse_requests_overlap_under_one_shared_deployment_quota(tmp_path):
    from app import model_adapter

    with fake_sse_server() as (base_url, state):
        limiter = DeploymentLimiter(DeploymentLimits(tmp_path / "runtime", model_concurrency=2))

        async def run_many():
            async def one(index):
                started = time.perf_counter()
                result = await model_adapter.async_stream_completion(
                    f"{base_url}/openai/v1/chat/completions", "fake-key",
                    {"model": "qwen-plus", "index": index}, read_timeout=2,
                    total_seconds=12, limiter=limiter,
                )
                return result, time.perf_counter() - started

            started = time.perf_counter()
            results = await asyncio.gather(*(one(index) for index in range(6)))
            return results, time.perf_counter() - started

        results, wall_seconds = asyncio.run(run_many())
    assert len(results) == 6
    assert all(value[0][1]["prompt_tokens"] == 17 for value in results)
    assert all(value[0][1]["completion_tokens"] == 5 for value in results)
    assert all(value[0][1]["_finish_reason"] == "stop" for value in results)
    assert state.maximum == 2
    assert wall_seconds < 10
    mean_latency = sum(value[1] for value in results) / len(results)
    print(
        f"Synthetic SSE: requests={len(results)} peak_in_flight={state.maximum} "
        f"wall_seconds={wall_seconds:.3f} throughput={len(results) / wall_seconds:.2f}/s "
        f"mean_request_seconds={mean_latency:.3f}"
    )


def test_health_api_is_responsive_while_async_model_io_waits(tmp_path, monkeypatch):
    from httpx import ASGITransport, AsyncClient

    from app import model_adapter
    from app.config import settings
    from app.main import app
    from app.storage import initialize

    monkeypatch.setattr(settings, "database_path", str(tmp_path / "health.db"))
    initialize(settings.resolved_database_path)

    async def exercise(base_url, state):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            await client.get("/api/v1/health")
            pending_model = asyncio.create_task(
                model_adapter.async_stream_completion(
                    f"{base_url}/timeout", "fake-key", {}, read_timeout=2, total_seconds=5,
                )
            )
            # Measure during a request actually waiting for the local SSE
            # response, after HTTP client creation and socket admission.
            for _ in range(100):
                with state.lock:
                    active = state.active
                if active:
                    break
                await asyncio.sleep(0.02)
            else:
                raise AssertionError("local model request never entered flight")
            started = time.perf_counter()
            health = await client.get("/api/v1/health")
            health_seconds = time.perf_counter() - started
            await pending_model
            return health, health_seconds

    with fake_sse_server() as (base_url, state):
        response, elapsed = asyncio.run(exercise(base_url, state))
    assert response.status_code == 200
    assert response.json()["status"] == "ok"
    assert elapsed < 0.5
    print(f"Synthetic health: request_seconds={elapsed:.6f}")


def test_waiting_model_request_is_not_counted_when_stopped_before_transport(tmp_path, monkeypatch):
    from app import ingestion, model_adapter
    from app.job_cache import instrumented_notice_cache

    limits = DeploymentLimits(tmp_path / 'runtime')
    limiter = DeploymentLimiter(limits)
    permits = [limiter.try_acquire('model'), limiter.try_acquire('model')]
    stop_path = tmp_path / 'stop'
    telemetry = tmp_path / 'telemetry.json'
    entered = threading.Event()
    counters_seen = []

    def forbidden_transport(*args, **kwargs):
        pytest.fail('a request waiting for admission must not reach HTTP')

    monkeypatch.setattr(model_adapter.httpx, 'stream', forbidden_transport)

    def waiting_request():
        with (
            use_runtime(limits, stop_path=stop_path),
            instrumented_notice_cache(
                tmp_path / 'cache', 'model', _settings('http://127.0.0.1:1'), {},
                stop_path=stop_path, telemetry_path=telemetry,
            ) as counters,
            model_adapter.model_call_budget(1) as usage,
        ):
            counters_seen.append(counters)
            entered.set()
            try:
                ingestion.extract_unstructured_items(
                    filename='synthetic.html', text='synthetic',
                    settings=_settings('http://127.0.0.1:1'),
                )
            except SchedulingStopped:
                return usage.requests
            raise AssertionError('the stopped request must raise SchedulingStopped')

    try:
        with ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(waiting_request)
            assert entered.wait(5)
            time.sleep(0.15)
            assert counters_seen[0]['model_calls'] == []
            stop_path.touch()
            assert future.result(timeout=5) == 0
        assert read_json(telemetry)['model_calls'] == []
    finally:
        for permit in permits:
            permit.release()


def test_async_adapter_preserves_sse_usage_and_openai_compatible_endpoint_configs():
    with fake_sse_server() as (base_url, state):
        for path, model in (("qwen/v1", "qwen-plus"), ("deepseek/v1", "deepseek-chat")):
            body = {
                "model": model,
                "stream": True,
                "stream_options": {"include_usage": True},
                "response_format": {"type": "json_object"},
            }
            apply_thinking_setting(body, model, disable_thinking=True)
            content, usage = asyncio.run(
                __import__("app.model_adapter", fromlist=["async_stream_completion"])
                .async_stream_completion(
                    f"{base_url}/{path}/chat/completions", "provider-key",
                    body, read_timeout=2, total_seconds=4,
                )
            )
            assert json.loads(content)["items"] == []
            assert usage["prompt_tokens"] == 17 and usage["completion_tokens"] == 5
            assert usage["_finish_reason"] == "stop"
    qwen, deepseek = (row["body"] for row in state.requests)
    assert qwen["chat_template_kwargs"] == {"enable_thinking": False}
    assert "thinking" not in qwen
    assert deepseek["thinking"] == {"type": "disabled"}
    assert "chat_template_kwargs" not in deepseek
    assert qwen["response_format"] == deepseek["response_format"]
    assert qwen["model"] != deepseek["model"]
    assert all(row["authorization"] == "Bearer provider-key" for row in state.requests)


def test_429_is_not_retried_and_timeout_truncation_and_bad_json_are_reported():
    from app import model_adapter

    with fake_sse_server() as (base_url, state):
        with pytest.raises(httpx.HTTPStatusError) as caught:
            asyncio.run(model_adapter.async_stream_completion(
                f"{base_url}/429", "fake-key", {}, read_timeout=1, total_seconds=4,
            ))
        assert caught.value.response.status_code == 429
        assert len(state.requests) == 1

        with pytest.raises((httpx.TimeoutException, httpx.ReadError)):
            asyncio.run(model_adapter.async_stream_completion(
                f"{base_url}/timeout", "fake-key", {}, read_timeout=1, total_seconds=1,
            ))
        with pytest.raises(httpx.RemoteProtocolError):
            asyncio.run(model_adapter.async_stream_completion(
                f"{base_url}/cutoff", "fake-key", {}, read_timeout=2, total_seconds=4,
            ))

        for path, expected in (("truncated", "finish_reason=length"),
                               ("invalid", "非法 JSON")):
            _, _, _, warnings = extract_unstructured_items(
                filename=f"{path}.html", text="公告正文", settings=_settings(base_url + f"/{path}"),
            )
            assert warnings and expected in warnings[0]


def test_cancelled_stop_never_starts_request_and_cache_files_are_atomic(tmp_path):
    stop_path = tmp_path / "stop"
    stop_path.write_text("stop", encoding="utf-8")
    with fake_sse_server() as (base_url, state):
        limits = DeploymentLimits(tmp_path / "runtime")
        with use_runtime(limits, stop_path=stop_path), pytest.raises(SchedulingStopped):
            from app.model_adapter import async_stream_completion

            asyncio.run(async_stream_completion(
                base_url + "/openai/v1/chat/completions", "fake-key", {},
                read_timeout=1, total_seconds=2,
            ))
        assert state.requests == []

    cache = tmp_path / "cache.json"
    values = [{"writer": index, "payload": "x" * 1000} for index in range(8)]

    def write(value):
        atomic_json(cache, value)

    threads = [threading.Thread(target=write, args=(value,)) for value in values]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert read_json(cache) in values
    pending = list(tmp_path.glob("cache.json.*.pending"))
    assert pending == []
    cache.write_text("{partial", encoding="utf-8")
    with pytest.raises(json.JSONDecodeError):
        read_json(cache)
    atomic_json(cache, {"recovered": True})
    assert read_json(cache) == {"recovered": True}


def test_serial_adapter_results_and_model_policy_are_deterministic():
    with fake_sse_server() as (base_url, _):
        # Exercise the same sync entry point production extraction retains when
        # concurrency is one; no scheduler worker pool or live service is used.
        settings = _settings(base_url + "/openai/v1", "qwen-plus")
        first = extract_unstructured_items(
            filename="notice.html", text="项目名称：合成项目", settings=settings,
        )
        second = extract_unstructured_items(
            filename="notice.html", text="项目名称：合成项目", settings=settings,
        )
        disallowed = extract_unstructured_items(
            filename="notice.html", text="正文", settings=_settings(base_url, "gpt-test"),
        )
    assert first == second
    assert "Qwen/DeepSeek" in disallowed[3][0]


def test_request_configuration_identity_includes_endpoint_and_parameters():
    """Independent endpoint contracts must not share settings or response keys."""
    qwen = {
        "model": "qwen-plus",
        "endpoint": "http://127.0.0.1:8000/qwen/v1/chat/completions",
        "temperature": 0,
        "max_tokens": 512,
        "response_format": {"type": "json_object"},
        "thinking": {"chat_template_kwargs": {"enable_thinking": False}},
    }
    deepseek = {
        "model": "deepseek-chat",
        "endpoint": "http://127.0.0.1:8000/deepseek/v1/chat/completions",
        "temperature": 0,
        "max_tokens": 512,
        "response_format": {"type": "json_object"},
        "thinking": {"thinking": {"type": "disabled"}},
    }
    assert json.dumps(qwen, sort_keys=True) != json.dumps(deepseek, sort_keys=True)
    assert qwen["thinking"] != deepseek["thinking"]


def test_failure_warnings_do_not_include_provider_keys_or_raw_response():
    request = httpx.Request("POST", "http://127.0.0.1/private")
    response = httpx.Response(429, request=request, text="provider response contains fake-key")
    warning = _model_failure_warning(
        "notice.html", httpx.HTTPStatusError("secret fake-key", request=request, response=response),
        settings=_settings("http://127.0.0.1", "qwen-plus"),
    )
    assert "429" in warning
    assert "fake-key" not in warning
