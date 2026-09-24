"""Annotation seeds and evaluation endpoints; draft predictions never become gold silently."""

from __future__ import annotations

import hashlib
import json
from typing import Annotated, Literal

from fastapi import APIRouter, File, HTTPException, Query, UploadFile
from pydantic import ValidationError
from starlette.concurrency import run_in_threadpool

from app.evaluation import GoldDataset, evaluate_dataset, render_markdown
from app.ingestion import extract_notice, group_notice_documents
from app.parsers import SourceDocument, expand_uploads, parse_document
from app.schemas import ImportResult

router = APIRouter(prefix="/api/v1/evaluation", tags=["annotation-evaluation"])
FIELDS = ("product_name", "category", "brand", "model", "quantity", "unit_price", "total_price")


def result_notice(result: ImportResult, notice_id: str) -> dict:
    codes = sorted({row.package_code for row in [*result.items, *result.participants]}) or ["default"]
    packages = []
    for code in codes:
        people = [p for p in result.participants if p.package_code == code]
        packages.append({
            "package_id": code,
            "items": [{"item_id": f"item-{index + 1}",
                       **{key: row.model_dump(mode="json")[key] for key in FIELDS}}
                      for index, row in enumerate(result.items) if row.package_code == code],
            "buyer": {"entity_id": "buyer-1", "name": result.metadata.procurement_unit}
                     if result.metadata.procurement_unit else None,
            "winners": [{"entity_id": f"entity-{i + 1}", "name": p.organization_name,
                         "award_amount": p.model_dump(mode="json")["award_amount"]}
                        for i, p in enumerate(people) if p.outcome == "winner"],
            "bidders": [{"entity_id": f"entity-{i + 1}", "name": p.organization_name,
                         "outcome": p.outcome} for i, p in enumerate(people)],
        })
    return {"notice_id": notice_id, "packages": packages}


def annotation_seed(files: list[SourceDocument], mode: str = "rules") -> dict:
    expanded, warnings = expand_uploads(files)
    html = [doc for doc in expanded if doc.filename.lower().endswith((".html", ".htm"))]
    groups, orphans = group_notice_documents(expanded) if len(html) > 1 else ([expanded], [])
    notices, sources = [], []
    seen = set()
    for group in groups:
        if not group:
            continue
        primary = next((doc for doc in group if doc in html), group[0])
        notice_id = hashlib.sha256(primary.content).hexdigest()[:20]
        if notice_id in seen:
            warnings.append(f"跳过重复原文：{primary.filename}")
            continue
        seen.add(notice_id)
        result = extract_notice(group, extraction_mode=mode)
        notices.append(result_notice(result, notice_id))
        sources.append({"notice_id": notice_id, "files": result.source_files,
                        "text": "\n\n".join(
                            f"【{doc.filename}】\n{parse_document(doc)[0]}" for doc in group),
                        "evidence": [row.model_dump(mode="json") for row in result.items],
                        "warnings": result.warnings})
    if not notices:
        raise ValueError("没有找到可解析的公告")
    return {"gold": {"schema_version": "1.0", "status": "draft", "notices": notices},
            "predictions": {"schema_version": "1.0", "status": "predicted", "notices": notices},
            "sources": sources, "warnings": warnings, "orphan_files": orphans,
            "mode": mode}


@router.get("/schema")
def gold_schema():
    return GoldDataset.model_json_schema()


@router.post("/draft")
async def make_draft(
    files: Annotated[list[UploadFile], File()],
    mode: Literal["rules", "model", "hybrid"] = "rules",
):
    documents, size = [], 0
    for upload in files:
        content = await upload.read(50 * 1024 * 1024 + 1)
        size += len(content)
        if size > 50 * 1024 * 1024:
            raise HTTPException(status_code=413, detail="标注材料一次最多 50 MB，请分批处理")
        documents.append(SourceDocument(upload.filename or "unnamed", content))
    try:
        return await run_in_threadpool(annotation_seed, documents, mode)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.post("/run")
async def run_evaluation(
    gold: Annotated[UploadFile, File()], predictions: Annotated[UploadFile, File()],
    allow_draft: bool = Query(default=False),
):
    values = []
    for uploaded in (gold, predictions):
        content = await uploaded.read(10 * 1024 * 1024 + 1)
        if len(content) > 10 * 1024 * 1024:
            raise HTTPException(status_code=413, detail="评测 JSON 每份最多 10 MB")
        try:
            values.append(json.loads(content.decode("utf-8-sig")))
        except (ValueError, UnicodeError) as exc:
            raise HTTPException(status_code=422, detail="请上传 UTF-8 标注和预测 JSON") from exc
    try:
        report = await run_in_threadpool(evaluate_dataset, *values, allow_draft=allow_draft)
        return {"report": report.model_dump(mode="json"), "markdown": render_markdown(report)}
    except (ValueError, ValidationError) as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
