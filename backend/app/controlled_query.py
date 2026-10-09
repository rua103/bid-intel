"""Controlled natural-language access to the five read-only analytics scenes.

This module deliberately has no SQL/Cypher input surface.  Model output is an
intent-shaped JSON document and is validated before dispatching to the existing
parameterized analytics backend.
"""
from __future__ import annotations

import json
import logging
import re
import sqlite3
from collections.abc import Callable
from datetime import date
from decimal import Decimal
from enum import StrEnum
from pathlib import Path
from typing import Annotated, Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from app import analytics_backend
from app.bounded_runtime import ModelAdmissionError
from app.config import effective_settings
from app.datasets import DatabasePath, resolve_database
from app.model_adapter import _stream_completion, apply_thinking_setting, async_stream_completion

logger = logging.getLogger(__name__)


class Scene(StrEnum):
    BUYER_AWARDEES = "buyer_awardees"
    BUYER_BIDDERS = "buyer_bidders"
    SUPPLIER_CO_BIDDERS = "supplier_co_bidders"
    COMMON_BUYERS = "common_buyers"
    COMMON_PROJECTS = "common_projects"


class QueryFilters(BaseModel):
    model_config = ConfigDict(extra="forbid")
    buyer_id: int | None = Field(default=None, ge=1)
    supplier_id: int | None = Field(default=None, ge=1)
    supplier_ids: list[Annotated[int, Field(ge=1)]] | None = Field(default=None, min_length=2, max_length=50)
    start_date: date | None = None
    end_date: date | None = None
    min_amount: Decimal | None = Field(default=None, ge=0)
    max_amount: Decimal | None = Field(default=None, ge=0)
    include_winners: bool = True
    top: int = Field(default=5, ge=1, le=100)

    @model_validator(mode="after")
    def valid_ranges(self) -> QueryFilters:
        if self.supplier_ids is not None and len(set(self.supplier_ids)) != len(self.supplier_ids):
            raise ValueError("supplier_ids must identify distinct organizations")
        if self.start_date and self.end_date and self.start_date > self.end_date:
            raise ValueError("start_date must be before end_date")
        if self.min_amount is not None and self.max_amount is not None and self.min_amount > self.max_amount:
            raise ValueError("min_amount must be <= max_amount")
        return self


class ControlledIntent(BaseModel):
    model_config = ConfigDict(extra="forbid")
    scene: Scene
    filters: QueryFilters = Field(default_factory=QueryFilters)
    confidence: float = Field(default=1.0, ge=0, le=1)
    needs_clarification: bool = False
    clarification_options: list[str] = Field(default_factory=list, max_length=10)

    @model_validator(mode="after")
    def scene_parameters(self) -> ControlledIntent:
        if self.needs_clarification:
            return self
        f = self.filters
        if self.scene in (Scene.BUYER_AWARDEES, Scene.BUYER_BIDDERS) and not f.buyer_id:
            raise ValueError("buyer_id is required")
        if self.scene == Scene.SUPPLIER_CO_BIDDERS and not f.supplier_id:
            raise ValueError("supplier_id is required")
        if self.scene in (Scene.COMMON_BUYERS, Scene.COMMON_PROJECTS) and not f.supplier_ids:
            raise ValueError("supplier_ids are required")
        return self


class ParseRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    question: str = Field(min_length=1, max_length=2000)
    mock_intent: dict[str, Any] | None = None


class ExecuteRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    request: ControlledIntent


CONTROLLED_QUERY_PROMPT = """你是政府采购关系分析的受控意图解析器。只能把用户问题映射到下面五个只读场景之一，不能生成 SQL、Cypher 或任何写操作。
场景：buyer_awardees（采购单位的中标供应商）、buyer_bidders（采购单位的投标主体）、supplier_co_bidders（供应商的共同竞标方）、common_buyers（多家供应商共同合作采购单位）、common_projects（多家供应商共同投标项目）。
只从主体目录中选择 buyer_id、supplier_id 或 supplier_ids；无法唯一确定主体时必须 needs_clarification=true，并给出 clarification_options。只返回 JSON 对象，不要 Markdown，不要解释文字。
JSON 结构：{"scene":"...","filters":{"buyer_id":1,"supplier_id":2,"supplier_ids":[2,3],"start_date":null,"end_date":null,"min_amount":null,"max_amount":null,"include_winners":true,"top":5},"confidence":0.0,"needs_clarification":false,"clarification_options":[]}
"""


INJECTION_RE = re.compile(r"(?:select\s+.+\s+from|match\s*\(|create\s+|delete\s+|drop\s+|update\s+|merge\s+|insert\s+|cypher|sql\b|\b写入|删除|修改|执行命令)", re.IGNORECASE | re.DOTALL)
_mock_parser: Callable[[str], dict[str, Any]] | None = None


def set_mock_parser(parser: Callable[[str], dict[str, Any]] | None) -> None:
    global _mock_parser
    _mock_parser = parser


def _safe_parse_json(value: Any) -> dict[str, Any]:
    if isinstance(value, str):
        value = json.loads(value)
    if not isinstance(value, dict):
        raise ValueError("模型返回必须是 JSON 对象")  # noqa: TRY004
    forbidden = {"sql", "cypher", "query", "statement", "raw_query"}
    if forbidden.intersection(value):
        raise ValueError("模型返回包含被禁止的查询字段")
    return value


def _organization_catalog(path: Path | None) -> list[dict[str, Any]]:
    if path is None:
        return []
    try:
        from app.storage import connect

        with connect(path) as connection:
            rows = connection.execute(
                "SELECT id, canonical_name FROM organizations ORDER BY canonical_name LIMIT 500"
            ).fetchall()
        return [dict(row) for row in rows]
    except (OSError, sqlite3.Error):
        return []


def _model_parse_intent(question: str, path: Path | None) -> dict[str, Any]:
    settings = effective_settings()
    if not (settings.model_base_url and settings.model_api_key and settings.model_name):
        raise RuntimeError("模型不可用，请改用手动选择场景和参数")
    if not settings.model_name.casefold().startswith(("qwen", "deepseek")):
        raise RuntimeError("当前模型不符合 Qwen/DeepSeek 配置要求，请改用手动选择场景和参数")
    endpoint = settings.model_base_url.rstrip("/")
    if not endpoint.endswith("/chat/completions"):
        endpoint += "/chat/completions"
    catalog = json.dumps(_organization_catalog(path), ensure_ascii=False)
    body: dict[str, Any] = {
        "model": settings.model_name,
        "temperature": 0,
        "max_tokens": max(256, min(settings.model_max_output_tokens, 1536)),
        "response_format": {"type": "json_object"},
        "stream": True,
        "stream_options": {"include_usage": True},
        "messages": [
            {"role": "system", "content": CONTROLLED_QUERY_PROMPT},
            {"role": "user", "content": f"主体目录：{catalog}\n用户问题：{question}"},
        ],
    }
    apply_thinking_setting(
        body, settings.model_name, disable_thinking=settings.model_disable_thinking,
    )
    try:
        content, usage = _stream_completion(
            endpoint,
            settings.model_api_key,
            body,
            read_timeout=settings.model_timeout_seconds,
            total_seconds=settings.model_stream_total_seconds,
        )
        if usage.get("_finish_reason") == "length" or not content.strip():
            raise RuntimeError("模型意图输出不完整")
        return _safe_parse_json(json.loads(content.strip()))
    except RuntimeError:
        raise
    except Exception as exc:
        logger.warning("controlled query model parse failed: %s", type(exc).__name__)
        raise RuntimeError("模型意图解析失败，请改用手动选择场景和参数") from exc


async def _model_parse_intent_async(question: str, path: Path | None) -> dict[str, Any]:
    """Use async network I/O on the Web event loop for the interactive query API."""
    settings = effective_settings()
    if not (settings.model_base_url and settings.model_api_key and settings.model_name):
        raise RuntimeError("模型不可用，请改用手动选择场景和参数")
    if not settings.model_name.casefold().startswith(("qwen", "deepseek")):
        raise RuntimeError("当前模型不符合 Qwen/DeepSeek 配置要求，请改用手动选择场景和参数")
    endpoint = settings.model_base_url.rstrip("/")
    if not endpoint.endswith("/chat/completions"):
        endpoint += "/chat/completions"
    catalog = json.dumps(_organization_catalog(path), ensure_ascii=False)
    body: dict[str, Any] = {
        "model": settings.model_name,
        "temperature": 0,
        "max_tokens": max(256, min(settings.model_max_output_tokens, 1536)),
        "response_format": {"type": "json_object"},
        "stream": True,
        "stream_options": {"include_usage": True},
        "messages": [
            {"role": "system", "content": CONTROLLED_QUERY_PROMPT},
            {"role": "user", "content": f"主体目录：{catalog}\n用户问题：{question}"},
        ],
    }
    apply_thinking_setting(
        body, settings.model_name, disable_thinking=settings.model_disable_thinking,
    )
    try:
        content, usage = await async_stream_completion(
            endpoint, settings.model_api_key, body,
            read_timeout=settings.model_timeout_seconds,
            total_seconds=settings.model_stream_total_seconds,
        )
        if usage.get("_finish_reason") == "length" or not content.strip():
            raise RuntimeError("模型意图输出不完整")
        return _safe_parse_json(json.loads(content.strip()))
    except RuntimeError:
        raise
    except Exception as exc:
        logger.warning("controlled query async model parse failed: %s", type(exc).__name__)
        raise RuntimeError("模型意图解析失败，请改用手动选择场景和参数") from exc


def parse_question(
    question: str,
    *,
    mock_intent: dict[str, Any] | None = None,
    database_path: Path | None = None,
) -> ControlledIntent:
    if INJECTION_RE.search(question):
        raise ValueError("仅支持五类只读分析场景，拒绝写请求或任意查询")
    raw: Any = mock_intent
    if raw is None:
        if _mock_parser is not None:
            raw = _mock_parser(question)
        else:
            raw = _model_parse_intent(question, database_path)
    if raw is None:
        raise RuntimeError("模型不可用，请改用手动选择场景和参数")
    try:
        return ControlledIntent.model_validate(_safe_parse_json(raw))
    except (ValidationError, ValueError, json.JSONDecodeError) as exc:
        logger.warning("controlled query intent rejected: %s", type(exc).__name__)
        raise ValueError("无法解析为受控查询，请补充场景和主体") from exc


def _source_summary(path: Path, intent: ControlledIntent) -> list[dict[str, Any]]:
    """Return notices participating in the approved analytics scene."""
    filters = intent.filters
    try:
        from app.storage import connect

        with connect(path) as conn:
            if intent.scene in (Scene.BUYER_AWARDEES, Scene.BUYER_BIDDERS):
                table = "awards" if intent.scene == Scene.BUYER_AWARDEES else "bid_participations"
                outcome = (
                    ""
                    if filters.include_winners or table == "awards"
                    else " AND b.outcome = 'nonwinner'"
                )
                alias = "a" if table == "awards" else "b"
                rows = conn.execute(
                    f"SELECT DISTINCT n.id, n.project_name, n.source_files_json "
                    f"FROM {table} {alias} JOIN packages p ON p.id = {alias}.package_id "
                    f"JOIN projects x ON x.id = p.project_id JOIN notices n ON n.id = x.notice_id "
                    f"WHERE x.buyer_organization_id = ?{outcome} ORDER BY n.id DESC LIMIT 100",
                    (filters.buyer_id,),
                ).fetchall()
            elif intent.scene == Scene.SUPPLIER_CO_BIDDERS:
                rows = conn.execute(
                    "SELECT DISTINCT n.id, n.project_name, n.source_files_json "
                    "FROM awards a JOIN packages p ON p.id = a.package_id "
                    "JOIN projects x ON x.id = p.project_id JOIN notices n ON n.id = x.notice_id "
                    "WHERE a.organization_id = ? ORDER BY n.id DESC LIMIT 100",
                    (filters.supplier_id,),
                ).fetchall()
            else:
                ids = filters.supplier_ids or []
                marks = ",".join("?" for _ in ids)
                table = "awards" if intent.scene == Scene.COMMON_BUYERS else "bid_participations"
                group = (
                    "x2.buyer_organization_id"
                    if intent.scene == Scene.COMMON_BUYERS
                    else "p2.id"
                )
                target = (
                    "x.buyer_organization_id"
                    if intent.scene == Scene.COMMON_BUYERS
                    else "p.id"
                )
                rows = conn.execute(
                    f"SELECT DISTINCT n.id, n.project_name, n.source_files_json "
                    f"FROM {table} r JOIN packages p ON p.id = r.package_id "
                    f"JOIN projects x ON x.id = p.project_id JOIN notices n ON n.id = x.notice_id "
                    f"WHERE r.organization_id IN ({marks}) AND {target} IN ("
                    f"SELECT {group} FROM {table} r2 "
                    f"JOIN packages p2 ON p2.id = r2.package_id "
                    f"JOIN projects x2 ON x2.id = p2.project_id "
                    f"WHERE r2.organization_id IN ({marks}) GROUP BY {group} "
                    f"HAVING COUNT(DISTINCT r2.organization_id) = ?) "
                    f"ORDER BY n.id DESC LIMIT 100",
                    [*ids, *ids, len(ids)],
                ).fetchall()
        return [dict(row) for row in rows]
    except (OSError, sqlite3.Error, TypeError, ValueError, json.JSONDecodeError):
        return []


def execute_intent(path: Path, intent: ControlledIntent) -> dict[str, Any]:
    if intent.needs_clarification:
        return {"status": "clarification_required", "intent": intent.model_dump(mode="json"), "options": intent.clarification_options}
    f = intent.filters
    unsupported = [name for name in ("start_date", "end_date", "min_amount", "max_amount") if getattr(f, name) is not None]
    if unsupported:
        return {
            "status": "unsupported_filter",
            "scene": intent.scene.value,
            "filters": intent.filters.model_dump(mode="json"),
            "detail": "当前五类分析 API 尚未提供该过滤条件，请清除后再执行",
            "unsupported_filters": unsupported,
        }
    params: dict[str, Any] = {k: v for k, v in f.model_dump().items() if v is not None}
    result = analytics_backend.query_analytics(path, intent.scene.value, **params)
    return {
        "status": "ok",
        "scene": intent.scene.value,
        "filters": intent.filters.model_dump(mode="json"),
        "query_semantics": {
            Scene.BUYER_AWARDEES: "合作次数按中标项目去重，采购包数单列；中标金额按唯一中标记录汇总，品牌金额仅累计已披露标的总价",
            Scene.COMMON_BUYERS: "共同采购单位按所选供应商的合作采购单位取交集；项目数按各供应商分别去重，不要求共同项目或同包中标；金额按唯一中标记录汇总",
        }.get(intent.scene, "现有五类分析 API 的固定口径；自然语言仅用于意图解析，不构成证据"),
        "analytics_backend": result.backend,
        "analytics_fallback": result.fell_back,
        "payload": result.payload,
        "source_notices": _source_summary(path, intent),
    }


def router_for(database_dependency: Any = resolve_database) -> APIRouter:
    """Build routes without importing the application's global dependency graph."""
    router = APIRouter(prefix="/api/v1/controlled-query", tags=["controlled-query"])

    @router.post("/parse")
    async def parse_endpoint(payload: ParseRequest, database_path: DatabasePath):
        try:
            if INJECTION_RE.search(payload.question):
                raise ValueError("仅支持五类只读分析场景，拒绝写请求或任意查询")
            if payload.mock_intent is None and _mock_parser is None:
                raw = await _model_parse_intent_async(payload.question, database_path)
                intent = ControlledIntent.model_validate(raw)
            else:
                intent = parse_question(payload.question, mock_intent=payload.mock_intent,
                                        database_path=database_path)
        except RuntimeError as exc:
            if isinstance(exc, ModelAdmissionError):
                raise HTTPException(
                    status_code=429, detail=str(exc), headers={"Retry-After": "5"},
                ) from exc
            raise HTTPException(status_code=503, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        return {"status": "clarification_required" if intent.needs_clarification else "ready", "intent": intent.model_dump(mode="json")}

    @router.post("/execute")
    def execute_endpoint(
        payload: ExecuteRequest,
        database_path: DatabasePath,
    ):
        try:
            return execute_intent(database_path, payload.request)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    return router
