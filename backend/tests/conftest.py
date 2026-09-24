import json

import httpx
import pytest

from app.config import settings


@pytest.fixture(autouse=True)
def isolated_local_settings(tmp_path, monkeypatch):
    """Unit tests must never load a user's saved key or send paid model requests."""
    monkeypatch.setattr(settings, "database_path", str(tmp_path / "isolated.db"))
    for key in ("model_base_url", "model_api_key", "model_name"):
        monkeypatch.setattr(settings, key, "")


@pytest.fixture
def stub_model_stream(monkeypatch):
    """Patch ``httpx.stream`` with a fake SSE completion; nothing leaves the process.

    The payload is split across chunks and interleaved with a ``reasoning_content``
    delta, mirroring what the configured gateway actually returns.
    """

    def install(content: str, *, usage: dict | None = None, chunk_size: int = 37) -> list[dict]:
        frames = [
            json.dumps({"choices": [{"delta": {"role": "assistant"}}]}),
            json.dumps({"choices": [{"delta": {"reasoning_content": "先读原文，再逐条抽取。"}}]}),
        ]
        frames += [
            json.dumps({"choices": [{"delta": {"content": content[index:index + chunk_size]}}]})
            for index in range(0, len(content), chunk_size)
        ]
        if usage is not None:
            frames.append(json.dumps({"choices": [], "usage": usage}))
        frames.append("[DONE]")

        class _Response:
            status_code = 200

            def raise_for_status(self) -> None:
                return None

            def iter_lines(self):
                return iter(f"data: {frame}" for frame in frames)

        class _Stream:
            def __enter__(self) -> _Response:
                return _Response()

            def __exit__(self, *exc: object) -> bool:
                return False

        calls: list[dict] = []

        def fake_stream(method: str, url: str, **kwargs: object) -> _Stream:
            calls.append({"method": method, "url": url, **kwargs})
            return _Stream()

        monkeypatch.setattr(httpx, "stream", fake_stream)
        return calls

    return install

