import json

import httpx
from fastapi.testclient import TestClient

from app.config import settings
from app.main import app


def test_model_config_roundtrip_masks_key(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "database_path", str(tmp_path / "cfg.db"))
    with TestClient(app) as client:
        put = client.put(
            "/api/v1/model-config",
            json={
                "model_base_url": "https://model.example/v1",
                "model_api_key": "sk-secret-12345678",
                "model_name": "qwen-plus",
            },
        )
        assert put.status_code == 200, put.text
        body = put.json()
        assert body["configured"] is True
        assert body["api_key_configured"] is True
        assert "sk-secret" not in body["model_api_key_masked"]
        assert body["model_api_key_masked"].endswith("5678")

        get = client.get("/api/v1/model-config").json()
        assert get["model_name"] == "qwen-plus"
        assert get["api_key_configured"] is True
        assert "sk-secret-12345678" not in json.dumps(get)


def test_model_config_blank_key_preserves_existing(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "database_path", str(tmp_path / "cfg2.db"))
    with TestClient(app) as client:
        client.put(
            "/api/v1/model-config",
            json={
                "model_base_url": "https://m/v1",
                "model_api_key": "sk-abcdef",
                "model_name": "qwen-x",
            },
        )
        updated = client.put(
            "/api/v1/model-config",
            json={
                "model_base_url": "https://m/v1",
                "model_api_key": "",
                "model_name": "deepseek-chat",
            },
        ).json()
        assert updated["configured"] is True
        assert updated["model_name"] == "deepseek-chat"
        assert updated["api_key_configured"] is True


def test_model_config_rejects_noncompliant_model(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "database_path", str(tmp_path / "cfg3.db"))
    with TestClient(app) as client:
        result = client.post(
            "/api/v1/model-config/test",
            json={
                "model_base_url": "https://m/v1",
                "model_api_key": "k",
                "model_name": "gpt-4",
            },
        ).json()
        assert result["ok"] is False
        assert "qwen" in result["message"] or "deepseek" in result["message"]


def test_model_config_test_pings_endpoint(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "database_path", str(tmp_path / "cfg4.db"))
    calls = []

    def fake_post(url, **kwargs):
        calls.append({"url": url, **kwargs})
        request = httpx.Request("POST", url)
        return httpx.Response(200, json={"choices": [{"message": {"content": "pong"}}]}, request=request)

    monkeypatch.setattr(httpx, "post", fake_post)
    with TestClient(app) as client:
        result = client.post(
            "/api/v1/model-config/test",
            json={
                "model_base_url": "https://m/v1",
                "model_api_key": "k",
                "model_name": "qwen-plus",
            },
        ).json()
        assert result["ok"] is True
    assert calls[0]["json"]["response_format"] == {"type": "json_object"}
    assert "JSON" in calls[0]["json"]["messages"][0]["content"]
    assert "JSON" in calls[0]["json"]["messages"][1]["content"]
    assert calls[0]["json"]["chat_template_kwargs"] == {"enable_thinking": False}
    assert "thinking" not in calls[0]["json"]


def test_deepseek_config_probe_uses_documented_thinking_field(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "database_path", str(tmp_path / "deepseek-cfg.db"))
    calls = []

    def fake_post(url, **kwargs):
        calls.append({"url": url, **kwargs})
        request = httpx.Request("POST", url)
        return httpx.Response(200, json={"choices": [{"message": {"content": "pong"}}]}, request=request)

    monkeypatch.setattr(httpx, "post", fake_post)
    with TestClient(app) as client:
        result = client.post(
            "/api/v1/model-config/test",
            json={
                "model_base_url": "https://api.deepseek.com",
                "model_api_key": "local-test-key",
                "model_name": "deepseek-flash",
            },
        ).json()
    assert result["ok"] is True
    assert calls[0]["url"] == "https://api.deepseek.com/chat/completions"
    assert calls[0]["json"]["model"] == "deepseek-flash"
    assert calls[0]["json"]["thinking"] == {"type": "disabled"}
    assert calls[0]["json"]["response_format"] == {"type": "json_object"}
    assert "JSON" in calls[0]["json"]["messages"][0]["content"]
    assert "JSON" in calls[0]["json"]["messages"][1]["content"]
    assert "chat_template_kwargs" not in calls[0]["json"]


def test_model_config_probe_reports_rejected_compatibility_parameters(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "database_path", str(tmp_path / "cfg5.db"))

    def fake_post(url, **kwargs):
        request = httpx.Request("POST", url)
        return httpx.Response(400, text="unsupported chat_template_kwargs", request=request)

    monkeypatch.setattr(httpx, "post", fake_post)
    with TestClient(app) as client:
        result = client.post(
            "/api/v1/model-config/test",
            json={
                "model_base_url": "https://m/v1",
                "model_api_key": "k",
                "model_name": "qwen-plus",
            },
        ).json()
    assert result["ok"] is False
    assert "HTTP 400" in result["message"]
    assert "请求参数兼容性" in result["message"]


def test_deepseek_probe_does_not_blame_thinking_for_unspecified_400(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "database_path", str(tmp_path / "deepseek-cfg-400.db"))

    def fake_post(url, **kwargs):
        request = httpx.Request("POST", url)
        return httpx.Response(400, text="unsupported field", request=request)

    monkeypatch.setattr(httpx, "post", fake_post)
    with TestClient(app) as client:
        result = client.post(
            "/api/v1/model-config/test",
            json={
                "model_base_url": "https://api.deepseek.com",
                "model_api_key": "local-test-key",
                "model_name": "deepseek-flash",
            },
        ).json()
    assert result["ok"] is False
    assert "HTTP 400" in result["message"]
    assert "thinking 参数" not in result["message"]


def test_model_config_probe_explains_json_mode_prompt_requirement(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "database_path", str(tmp_path / "json-prompt.db"))

    def fake_post(url, **kwargs):
        request = httpx.Request("POST", url)
        return httpx.Response(
            400,
            json={"error": {"message": "Prompt must contain the word 'json' in some form."}},
            request=request,
        )

    monkeypatch.setattr(httpx, "post", fake_post)
    with TestClient(app) as client:
        result = client.post(
            "/api/v1/model-config/test",
            json={
                "model_base_url": "https://api.deepseek.com",
                "model_api_key": "local-test-key",
                "model_name": "deepseek-flash",
            },
        ).json()
    assert result["ok"] is False
    assert "提示词明确包含 JSON" in result["message"]


def test_model_config_probe_reports_account_balance_for_402(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "database_path", str(tmp_path / "balance.db"))

    def fake_post(url, **kwargs):
        request = httpx.Request("POST", url)
        return httpx.Response(402, text="payment required", request=request)

    monkeypatch.setattr(httpx, "post", fake_post)
    with TestClient(app) as client:
        result = client.post(
            "/api/v1/model-config/test",
            json={
                "model_base_url": "https://api.deepseek.com",
                "model_api_key": "local-test-key",
                "model_name": "deepseek-flash",
            },
        ).json()
    assert result["ok"] is False
    assert "余额或调用额度不足" in result["message"]
