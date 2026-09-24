from __future__ import annotations

from contextlib import asynccontextmanager
from typing import Annotated

from fastapi import FastAPI, File, HTTPException, Query, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from starlette.concurrency import run_in_threadpool

from app import analytics
from app.config import (
    effective_settings,
    load_model_config,
    mask_api_key,
    save_model_config,
    settings,
)
from app.evaluation_api import router as evaluation_router
from app.graph import sqlite_graph
from app.ingestion import import_batch, import_notice
from app.model_adapter import test_model_connection
from app.parsers import SourceDocument
from app.schemas import (
    BatchImportResult,
    ModelConfigPayload,
    ModelConfigResponse,
    ModelTestResponse,
    OrganizationSelection,
)
from app.storage import count_notices, initialize, search_items


@asynccontextmanager
async def lifespan(_: FastAPI):
    initialize(settings.resolved_database_path)
    yield


app = FastAPI(
    title="招采数据智能分析引擎",
    version="0.1.0",
    description="赛题五：公告/附件解析、标的物候选提取与关系分析 API。",
    lifespan=lifespan,
)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:5173", "http://127.0.0.1:5173"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)
app.include_router(evaluation_router)


@app.get("/api/v1/graph")
def get_graph(limit: int = Query(default=50, ge=1, le=200)):
    return sqlite_graph(settings.resolved_database_path, limit=limit)


@app.get("/api/v1/health")
def health() -> dict[str, str | int]:
    return {"status": "ok", "notices_imported": count_notices(settings.resolved_database_path)}


def _model_config_response(base_url: str, api_key: str, name: str) -> ModelConfigResponse:
    return ModelConfigResponse(
        model_base_url=base_url,
        model_name=name,
        model_api_key_masked=mask_api_key(api_key),
        api_key_configured=bool(api_key),
        configured=bool(base_url and api_key and name),
    )


@app.get("/api/v1/model-config", response_model=ModelConfigResponse)
def get_model_config():
    effective = effective_settings()
    return _model_config_response(
        effective.model_base_url, effective.model_api_key, effective.model_name
    )


@app.put("/api/v1/model-config", response_model=ModelConfigResponse)
def set_model_config(payload: ModelConfigPayload):
    base_url = payload.model_base_url.strip()
    name = payload.model_name.strip()
    api_key = payload.model_api_key.strip()
    if not api_key:
        stored = load_model_config()
        api_key = stored.get("model_api_key", "") or settings.model_api_key
    save_model_config(base_url, api_key, name)
    return _model_config_response(base_url, api_key, name)


@app.post("/api/v1/model-config/test", response_model=ModelTestResponse)
def test_model_config(payload: ModelConfigPayload):
    base_url = payload.model_base_url.strip()
    name = payload.model_name.strip()
    api_key = payload.model_api_key.strip()
    if not api_key:
        stored = load_model_config()
        api_key = stored.get("model_api_key", "") or settings.model_api_key
    ok, message = test_model_connection(base_url, api_key, name)
    return ModelTestResponse(ok=ok, message=message)


async def _read_uploads(files: list[UploadFile], max_upload_mb: int) -> list[SourceDocument]:
    max_bytes = max_upload_mb * 1024 * 1024
    source_documents: list[SourceDocument] = []
    total = 0
    for upload in files:
        content = await upload.read(max_bytes + 1)
        total += len(content)
        if len(content) > max_bytes or total > max_bytes:
            raise HTTPException(
                status_code=413, detail=f"上传总大小不能超过 {max_upload_mb} MB"
            )
        source_documents.append(SourceDocument(upload.filename or "unnamed", content))
    return source_documents


@app.post("/api/v1/notices/import")
async def import_uploaded_notice(files: Annotated[list[UploadFile], File()]):
    source_documents = await _read_uploads(files, settings.max_upload_mb)
    try:
        return await run_in_threadpool(import_notice, source_documents, settings.resolved_database_path)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.post("/api/v1/notices/import-batch", response_model=BatchImportResult)
async def import_uploaded_batch(files: Annotated[list[UploadFile], File()]):
    source_documents = await _read_uploads(files, settings.max_batch_upload_mb)
    try:
        return await run_in_threadpool(import_batch, source_documents, settings.resolved_database_path)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.get("/api/v1/items")
def get_items(
    query: str | None = None,
    category: str | None = None,
    brand: str | None = None,
    model: str | None = None,
    limit: int = Query(default=100, ge=1, le=500),
) -> list[dict]:
    return search_items(
        settings.resolved_database_path,
        query=query,
        category=category,
        brand=brand,
        model=model,
        limit=limit,
    )


@app.get("/api/v1/organizations")
def get_organizations(query: str | None = None, limit: int = Query(default=100, ge=1, le=500)):
    return analytics.list_organizations(settings.resolved_database_path, query=query, limit=limit)


@app.get("/api/v1/analytics/buyers/{buyer_id}/awardees")
def get_buyer_awardees(buyer_id: int):
    return analytics.buyer_awardees(settings.resolved_database_path, buyer_id)


@app.get("/api/v1/analytics/buyers/{buyer_id}/bidders")
def get_buyer_bidders(
    buyer_id: int,
    include_winners: bool = True,
    top: int = Query(default=5, ge=1, le=100),
):
    return analytics.buyer_bidders(
        settings.resolved_database_path, buyer_id, include_winners=include_winners, top=top
    )


@app.get("/api/v1/analytics/suppliers/{supplier_id}/co-bidders")
def get_supplier_co_bidders(supplier_id: int, top: int = Query(default=5, ge=1, le=100)):
    return analytics.supplier_co_bidders(settings.resolved_database_path, supplier_id, top=top)


@app.post("/api/v1/analytics/common-buyers")
def get_common_award_buyers(selection: OrganizationSelection):
    try:
        return analytics.common_award_buyers(
            settings.resolved_database_path, selection.organization_ids
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@app.post("/api/v1/analytics/common-projects")
def get_common_bid_projects(selection: OrganizationSelection):
    try:
        return analytics.common_bid_packages(
            settings.resolved_database_path, selection.organization_ids
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
