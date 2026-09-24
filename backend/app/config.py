import json
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    database_path: str = ".data/bidintel.db"
    max_upload_mb: int = 50
    max_batch_upload_mb: int = 500
    model_base_url: str = ""
    model_api_key: str = ""
    model_name: str = ""
    model_max_chars: int = 12000
    model_timeout_seconds: int = 45
    extraction_mode: str = "hybrid"
    ocr_enabled: bool = False
    ocr_language: str = "chi_sim+eng"
    ocr_timeout_seconds: int = 30

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    @property
    def resolved_database_path(self) -> Path:
        path = Path(self.database_path)
        return path if path.is_absolute() else Path(__file__).resolve().parents[1] / path


settings = Settings()

MODEL_CONFIG_FILENAME = "model_config.json"


def model_config_path() -> Path:
    return settings.resolved_database_path.parent / MODEL_CONFIG_FILENAME


def load_model_config() -> dict[str, str]:
    path = model_config_path()
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return {
        key: str(value)
        for key, value in data.items()
        if key in {"model_base_url", "model_api_key", "model_name"}
        and isinstance(value, str)
    }


def save_model_config(model_base_url: str, model_api_key: str, model_name: str) -> None:
    payload = {
        "model_base_url": model_base_url,
        "model_api_key": model_api_key,
        "model_name": model_name,
    }
    path = model_config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def effective_settings() -> Settings:
    stored = load_model_config()
    return settings.model_copy(update={
        "model_base_url": stored.get("model_base_url") or settings.model_base_url,
        "model_api_key": stored.get("model_api_key") or settings.model_api_key,
        "model_name": stored.get("model_name") or settings.model_name,
    })


def mask_api_key(key: str) -> str:
    if not key:
        return ""
    if len(key) <= 4:
        return "****"
    return "****" + key[-4:]
