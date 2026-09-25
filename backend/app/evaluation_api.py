"""Annotation seeds and evaluation endpoints; draft predictions never become gold silently."""

from __future__ import annotations

import hashlib
import json
from decimal import Decimal
from typing import Annotated, Literal

from fastapi import APIRouter, File, HTTPException, Query, UploadFile
from pydantic import ValidationError
from starlette.concurrency import run_in_threadpool

from app.evaluation import (
    ITEM_FIELDS,
    GoldDataset,
    PredictionDataset,
    evaluate_dataset,
    normalize_name,
    normalize_number,
    normalized_field,
    render_markdown,
)
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


def gold_skeleton(predicted_notice: dict) -> dict:
    """Create a blank annotation scaffold without copying extracted values into gold."""
    return {
        "notice_id": predicted_notice["notice_id"],
        "packages": [
            {
                "package_id": package["package_id"],
                "items": [],
                "buyer": None,
                "winners": [],
                "bidders": [],
            }
            for package in predicted_notice["packages"]
        ],
    }


def has_gold_annotations(gold: GoldDataset) -> bool:
    return any(
        package.items or package.buyer is not None or package.winners or package.bidders
        for notice in gold.notices
        for package in notice.packages
    )


def same_evaluation_content(gold: GoldDataset, predictions: PredictionDataset) -> bool:
    """Compare scored content independent of row IDs and JSON list ordering."""
    def signature(value: object) -> str:
        # 2, 2.0 and 2.000 are the same value, regardless of JSON serialization.
        return repr(value.normalize() if isinstance(value, Decimal) else value)

    def item_signature(item: object) -> tuple:
        return tuple(
            (field, signature(normalized_field(field, getattr(item, field))))
            for field in ITEM_FIELDS
        )

    def dataset_signature(dataset: GoldDataset | PredictionDataset) -> tuple:
        notices = []
        for notice in dataset.notices:
            packages = []
            for package in notice.packages:
                buyer = normalize_name(package.buyer.name) if package.buyer else None
                items = tuple(sorted((item_signature(item) for item in package.items), key=repr))
                winners = tuple(sorted(
                    (
                        normalize_name(person.name),
                        signature(normalize_number(person.award_amount, monetary=True)),
                    )
                    for person in package.winners
                ))
                bidders = tuple(sorted(
                    (normalize_name(person.name), person.outcome)
                    for person in package.bidders
                ))
                packages.append((package.package_id, buyer, items, winners, bidders))
            notices.append((notice.notice_id, tuple(sorted(packages, key=repr))))
        return tuple(sorted(notices, key=repr))

    return dataset_signature(gold) == dataset_signature(predictions)


def annotation_seed(files: list[SourceDocument], mode: str = "rules") -> dict:
    expanded, warnings = expand_uploads(files)
    html = [doc for doc in expanded if doc.filename.lower().endswith((".html", ".htm"))]
    groups, orphans = group_notice_documents(expanded) if len(html) > 1 else ([expanded], [])
    gold_notices, predicted_notices, sources = [], [], []
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
        predicted_notice = result_notice(result, notice_id)
        predicted_notices.append(predicted_notice)
        gold_notices.append(gold_skeleton(predicted_notice))
        sources.append({"notice_id": notice_id, "files": result.source_files,
                        "text": "\n\n".join(
                            f"【{doc.filename}】\n{parse_document(doc)[0]}" for doc in group),
                        "evidence": [row.model_dump(mode="json") for row in result.items],
                        "warnings": result.warnings})
    if not predicted_notices:
        raise ValueError("没有找到可解析的公告")
    return {"gold": {"schema_version": "1.0", "status": "draft", "notices": gold_notices},
            "predictions": {"schema_version": "1.0", "status": "predicted",
                            "notices": predicted_notices},
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
    allow_identical_gold: bool = Query(default=False),
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
        gold_dataset = GoldDataset.model_validate(values[0])
        prediction_dataset = PredictionDataset.model_validate(values[1])
        if not has_gold_annotations(gold_dataset):
            raise ValueError("gold 中没有人工标注记录；请先对照原文录入标的或主体后再评测")
        identical = same_evaluation_content(gold_dataset, prediction_dataset)
        if not allow_identical_gold and identical:
            raise ValueError("gold 与 predictions 内容完全一致；如已独立对照原文复核，请显式确认后重试")
        report = await run_in_threadpool(
            evaluate_dataset, gold_dataset, prediction_dataset, allow_draft=allow_draft
        )
        if identical:
            report.warnings.append("gold 与预测内容完全一致；调用方已显式声明独立核验，系统无法代替人工证明。")
        return {"report": report.model_dump(mode="json"), "markdown": render_markdown(report)}
    except (ValueError, ValidationError) as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
