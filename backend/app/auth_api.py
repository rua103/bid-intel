from __future__ import annotations

import hmac

from fastapi import APIRouter, HTTPException, Request, Response
from pydantic import BaseModel, Field

from app.auth import COOKIE_NAME, auth_is_configured, create_session, request_username
from app.config import settings

router = APIRouter(prefix="/api/v1/auth", tags=["authentication"])


class LoginPayload(BaseModel):
    username: str = Field(min_length=1, max_length=100)
    password: str = Field(min_length=1, max_length=512)


@router.get("/me")
def current_session(request: Request):
    if not settings.auth_enabled:
        return {"authenticated": True, "auth_enabled": False, "username": None}
    if not auth_is_configured():
        raise HTTPException(status_code=503, detail="登录未配置，请设置 AUTH_USERNAME、AUTH_PASSWORD 和 AUTH_SECRET_KEY")
    username = request_username(request)
    return {"authenticated": username is not None, "auth_enabled": True, "username": username}


@router.post("/login")
def login(payload: LoginPayload, response: Response):
    if not settings.auth_enabled:
        return {"authenticated": True, "auth_enabled": False, "username": None}
    if not auth_is_configured():
        raise HTTPException(status_code=503, detail="登录未配置，请检查后端环境变量")
    valid_username = hmac.compare_digest(payload.username, settings.auth_username)
    valid_password = hmac.compare_digest(payload.password, settings.auth_password)
    if not (valid_username and valid_password):
        raise HTTPException(status_code=401, detail="用户名或密码错误")
    response.set_cookie(
        COOKIE_NAME,
        create_session(payload.username),
        max_age=settings.auth_session_hours * 3600,
        httponly=True,
        secure=settings.auth_cookie_secure,
        samesite="strict",
        path="/",
    )
    return {"authenticated": True, "auth_enabled": True, "username": payload.username}


@router.post("/logout")
def logout(response: Response):
    response.delete_cookie(COOKIE_NAME, path="/", httponly=True, secure=settings.auth_cookie_secure, samesite="strict")
    return {"authenticated": False}
