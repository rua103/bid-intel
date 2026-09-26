from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import json
import time

from fastapi import Request

from app.config import settings

COOKIE_NAME = "bid_intel_session"


def auth_is_configured() -> bool:
    return bool(
        settings.auth_enabled
        and settings.auth_username
        and len(settings.auth_password) >= 12
        and len(settings.auth_secret_key) >= 32
    )


def _b64encode(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _b64decode(value: str) -> bytes:
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))


def create_session(username: str) -> str:
    now = int(time.time())
    payload = _b64encode(json.dumps(
        {"sub": username, "iat": now, "exp": now + settings.auth_session_hours * 3600},
        separators=(",", ":"),
    ).encode("utf-8"))
    signature = hmac.new(settings.auth_secret_key.encode("utf-8"), payload.encode("ascii"), hashlib.sha256).digest()
    return f"{payload}.{_b64encode(signature)}"


def session_username(token: str | None) -> str | None:
    if not token or not auth_is_configured():
        return None
    try:
        payload, supplied_signature = token.split(".", 1)
        expected_signature = _b64encode(hmac.new(
            settings.auth_secret_key.encode("utf-8"), payload.encode("ascii"), hashlib.sha256
        ).digest())
        if not hmac.compare_digest(supplied_signature, expected_signature):
            return None
        data = json.loads(_b64decode(payload))
        if (
            data.get("sub") != settings.auth_username
            or not isinstance(data.get("exp"), int)
            or data["exp"] <= int(time.time())
        ):
            return None
        return data["sub"]
    except (ValueError, TypeError, AttributeError, binascii.Error, json.JSONDecodeError, UnicodeDecodeError):
        return None


def request_username(request: Request) -> str | None:
    return session_username(request.cookies.get(COOKIE_NAME))
