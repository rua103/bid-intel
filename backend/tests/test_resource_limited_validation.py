import importlib.util
import json
import subprocess
from pathlib import Path

import httpx
import pytest


@pytest.fixture
def validator():
    path = Path(__file__).resolve().parents[2] / "scripts/resource_limited_validation.py"
    spec = importlib.util.spec_from_file_location("resource_validation", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_timeout_evidence_is_json_serializable(validator, monkeypatch, tmp_path):
    def timeout(*args, **kwargs):
        raise subprocess.TimeoutExpired(["test"], 1, output=b"partial output", stderr=b"failure")

    monkeypatch.setattr(validator.subprocess, "run", timeout)
    record = validator.run("bounded command", ["test"], tmp_path, timeout=1)
    assert record["status"] == "timeout"
    assert record["stdout_tail"] == "partial output"
    json.dumps(record)


@pytest.mark.parametrize("has_evidence", [True, False])
def test_api_validation_checks_notice_detail(validator, monkeypatch, has_evidence):
    paths = []

    def respond(request):
        path = request.url.path
        paths.append(path)
        payload = []
        if path == "/api/v1/health":
            payload = {"notices_imported": 3}
        elif path == "/api/v1/datasets":
            payload = [{"id": "synthetic", "name": "离线演示样例"}]
        elif path == "/api/v1/organizations":
            payload = [
                {"id": 1, "canonical_name": "示例公共服务中心"},
                {"id": 2, "canonical_name": "示例打印科技"},
            ]
        elif path == "/api/v1/items":
            payload = [{"notice_id": 7}]
        elif path == "/api/v1/notices/7":
            assert request.headers["X-Dataset-ID"] == "synthetic"
            payload = {"items": [{"source_evidence": "合成证据" if has_evidence else None,
                                  "source_location": "table:1"}]}
        return httpx.Response(200, json=payload)

    original_client = httpx.Client
    monkeypatch.setattr(httpx, "Client", lambda **kwargs: original_client(
        **kwargs, transport=httpx.MockTransport(respond),
    ))
    record = validator.validate_demo_api("http://synthetic.test")
    assert "/api/v1/notices/7" in paths
    assert record["status"] == ("passed" if has_evidence else "failed")
    assert record["job_pause_resume_verified"] is False
