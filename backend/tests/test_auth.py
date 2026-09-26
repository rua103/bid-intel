from fastapi.testclient import TestClient

from app.config import settings
from app.main import app


def enable_review_login(monkeypatch):
    monkeypatch.setattr(settings, "auth_enabled", True)
    monkeypatch.setattr(settings, "auth_username", "reviewer")
    monkeypatch.setattr(settings, "auth_password", "a-test-only-password")
    monkeypatch.setattr(settings, "auth_secret_key", "a-test-only-secret-key-with-enough-entropy")


def test_reviewer_login_protects_api_and_logout_clears_session(monkeypatch):
    enable_review_login(monkeypatch)
    with TestClient(app) as client:
        assert client.get("/api/v1/auth/me").json() == {
            "authenticated": False, "auth_enabled": True, "username": None,
        }
        assert client.get("/api/v1/parser-capabilities").status_code == 401
        assert client.get("/api/v1/health").status_code == 200
        bad = client.post("/api/v1/auth/login", json={"username": "reviewer", "password": "wrong"})
        assert bad.status_code == 401

        response = client.post("/api/v1/auth/login", json={
            "username": "reviewer", "password": "a-test-only-password",
        })
        assert response.status_code == 200
        cookie = response.cookies.get("bid_intel_session")
        assert cookie
        assert client.cookies.get("bid_intel_session") == cookie
        capabilities = client.get("/api/v1/parser-capabilities")
        assert capabilities.status_code == 200
        assert client.get("/api/v1/auth/me").json()["username"] == "reviewer"

        assert client.post("/api/v1/auth/logout").status_code == 200
        assert client.get("/api/v1/parser-capabilities").status_code == 401


def test_auth_enabled_without_credentials_fails_closed(monkeypatch):
    monkeypatch.setattr(settings, "auth_enabled", True)
    monkeypatch.setattr(settings, "auth_username", "reviewer")
    monkeypatch.setattr(settings, "auth_password", "")
    monkeypatch.setattr(settings, "auth_secret_key", "")
    with TestClient(app) as client:
        assert client.post("/api/v1/auth/login", json={
            "username": "reviewer", "password": "anything",
        }).status_code == 503
        assert client.get("/api/v1/parser-capabilities").status_code == 503


def test_auth_disabled_keeps_existing_local_developer_flow(monkeypatch):
    monkeypatch.setattr(settings, "auth_enabled", False)
    with TestClient(app) as client:
        status = client.get("/api/v1/auth/me")
        assert status.json() == {"authenticated": True, "auth_enabled": False, "username": None}
        assert client.get("/api/v1/parser-capabilities").status_code == 200
