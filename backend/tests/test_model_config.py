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

    def fake_post(url, **kwargs):
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
