import pytest

from app.config import settings


@pytest.fixture(autouse=True)
def isolated_local_settings(tmp_path, monkeypatch):
    """Unit tests must never load a user's saved key or send paid model requests."""
    monkeypatch.setattr(settings, "database_path", str(tmp_path / "isolated.db"))
    for key in ("model_base_url", "model_api_key", "model_name"):
        monkeypatch.setattr(settings, key, "")

