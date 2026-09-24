from __future__ import annotations

import re
import time
from pathlib import PurePosixPath

from app.config import effective_settings
from app.model_adapter import extract_unstructured_items
from app.parsers import SourceDocument, expand_uploads, extract_metadata, parse_document
from app.schemas import (
    BatchImportResult,
    BatchNoticeSummary,
    ImportResult,
    ItemCandidate,
    NoticeMetadata,
    ParticipantCandidate,
)
from app.storage import save_import


def _deduplicate(items: list[ItemCandidate]) -> list[ItemCandidate]:
    output: list[ItemCandidate] = []
    seen: set[tuple[str | None, ...]] = set()
    for item in items:
        key = tuple(
            (value or "").strip().casefold()
            for value in (
                item.product_name,
                item.category,
                item.brand,
                item.model,
                str(item.quantity) if item.quantity is not None else None,
                str(item.unit_price) if item.unit_price is not None else None,
                str(item.total_price) if item.total_price is not None else None,
            )
        )
        if key in seen:
            continue
        seen.add(key)
        output.append(item)
    return output


def _deduplicate_participants(
    participants: list[ParticipantCandidate], warnings: list[str]
) -> list[ParticipantCandidate]:
    result: dict[str, ParticipantCandidate] = {}
    for participant in participants:
        key = "".join(participant.organization_name.split()).casefold()
        prior = result.get(key)
        if prior is None or prior.outcome == "unknown" and participant.outcome != "unknown":
            result[key] = participant
        elif participant.outcome == "unknown":
            continue
        elif prior.outcome != participant.outcome and "unknown" not in {
            prior.outcome,
            participant.outcome,
        }:
            result[key] = prior.model_copy(update={"outcome": "unknown", "award_amount": None})
            warnings.append(f"主体身份结果冲突，已暂记为未知：{participant.organization_name}")
        elif prior.award_amount is None and participant.award_amount is not None:
            result[key] = prior.model_copy(update={"award_amount": participant.award_amount})
    return list(result.values())


def _normalized_stem(value: str) -> str:
    stem = PurePosixPath(value.replace("\\", "/")).stem.casefold()
    previous = None
    while previous != stem:
        previous = stem
        stem = re.sub(r"(?:[_\-\s]*(?:附件材料|附件资料|附件|attachments?|annex|files?))+$", "", stem)
    return re.sub(r"[^\w\u4e00-\u9fff]+", "", stem)


def _path_stem_candidates(filename: str) -> set[str]:
    candidates: set[str] = set()
    normalized = filename.replace("\\", "/")
    for layer in normalized.split("!/"):
        for component in layer.split("/"):
            key = _normalized_stem(component)
            if key:
                candidates.add(key)
    return candidates


def group_notice_documents(
    documents: list[SourceDocument],
) -> tuple[list[list[SourceDocument]], list[str]]:
    html_docs = [
        document
        for document in documents
        if document.filename.lower().endswith((".html", ".htm"))
    ]
    if not html_docs:
        raise ValueError("批量导入需要至少一份 HTML 公告用于识别公告分组")
    groups: list[list[SourceDocument]] = [[document] for document in html_docs]
    html_keys = [_path_stem_candidates(document.filename) for document in html_docs]
    shared_keys = set.intersection(*html_keys) if len(html_keys) > 1 else set()
    orphans: list[str] = []
    for document in documents:
        if document in html_docs:
            continue
        candidate_keys = _path_stem_candidates(document.filename) - shared_keys
        matches = [
            index
            for index, keys in enumerate(html_keys)
            if candidate_keys.intersection(keys - shared_keys)
        ]
        if len(matches) == 1:
            groups[matches[0]].append(document)
        elif not matches and len(html_docs) == 1:
            groups[0].append(document)
        else:
            orphans.append(document.filename)
    return groups, orphans


def _ingest_expanded(
    expanded: list[SourceDocument], warnings: list[str], database_path
) -> ImportResult:
    if not expanded:
        raise ValueError("没有找到可解析的公告或附件")
    html_documents = [
        document for document in expanded if document.filename.lower().endswith((".html", ".htm"))
    ]
    if len(html_documents) > 1:
        raise ValueError("每次导入请对应一条公告；当前 ZIP 中检测到多份 HTML，请按公告分别上传")
    primary_document = html_documents[0] if html_documents else expanded[0]

    items: list[ItemCandidate] = []
    participants: list[ParticipantCandidate] = []
    texts: list[str] = []
    model_metadata = NoticeMetadata()
    model_settings = effective_settings()
    model_is_configured = bool(
        model_settings.model_base_url and model_settings.model_api_key and model_settings.model_name
    )
    for document in expanded:
        text, parsed_items, parse_warnings = parse_document(document)
        if text:
            texts.append(text)
        if text and model_is_configured and (document == primary_document or not parsed_items):
            include_participants = document == primary_document
            parsed_metadata, model_items, model_participants, model_warnings = (
                extract_unstructured_items(
                    filename=document.filename,
                    text=text,
                    settings=model_settings,
                    include_participants=include_participants,
                )
            )
            parsed_items.extend(model_items)
            participants.extend(model_participants)
            if include_participants:
                model_metadata = parsed_metadata
            parse_warnings.extend(model_warnings)
        items.extend(parsed_items)
        warnings.extend(parse_warnings)
    metadata = extract_metadata(" ".join(texts))
    metadata_values = {
        field: getattr(metadata, field) or getattr(model_metadata, field)
        for field in NoticeMetadata.model_fields
    }
    metadata = NoticeMetadata(**metadata_values)
    if not model_is_configured:
        warnings.append("未配置合规 Qwen/DeepSeek 模型；投标主体和非表格标的尚未自动抽取")
    deduplicated_items = _deduplicate(items)
    deduplicated_participants = _deduplicate_participants(participants, warnings)
    result = ImportResult(
        notice_id=0,
        source_files=[document.filename for document in expanded],
        items=deduplicated_items,
        items_found=len(deduplicated_items),
        metadata=metadata,
        participants=deduplicated_participants,
        warnings=list(dict.fromkeys(warnings)),
    )
    return save_import(database_path, result)


def import_notice(files: list[SourceDocument], database_path) -> ImportResult:
    if not files:
        raise ValueError("至少上传一个公告或附件文件")
    expanded, warnings = expand_uploads(files)
    html_count = sum(
        document.filename.lower().endswith((".html", ".htm")) for document in expanded
    )
    if html_count > 1:
        raise ValueError("每次导入请对应一条公告；当前 ZIP 中检测到多份 HTML，请使用批量导入接口")
    return _ingest_expanded(expanded, warnings, database_path)


def import_batch(files: list[SourceDocument], database_path) -> BatchImportResult:
    if not files:
        raise ValueError("至少上传一个公告数据 ZIP")
    started = time.perf_counter()
    expanded, archive_warnings = expand_uploads(files)
    groups, orphans = group_notice_documents(expanded)
    summaries: list[BatchNoticeSummary] = []
    errors: list[str] = []
    for group in groups:
        try:
            imported = _ingest_expanded(group, list(archive_warnings), database_path)
            summaries.append(
                BatchNoticeSummary(
                    notice_id=imported.notice_id,
                    source_files=imported.source_files,
                    items_found=imported.items_found,
                    participants_found=len(imported.participants),
                    warnings=imported.warnings,
                )
            )
        except ValueError as exc:
            errors.append(f"{group[0].filename}: {exc}")
    return BatchImportResult(
        notices_found=len(groups),
        notices_imported=len(summaries),
        total_items_found=sum(row.items_found for row in summaries),
        total_participants_found=sum(row.participants_found for row in summaries),
        elapsed_seconds=round(time.perf_counter() - started, 3),
        orphan_files=orphans,
        notices=summaries,
        errors=errors,
    )
