from __future__ import annotations

import re
import time
import unicodedata
from pathlib import PurePosixPath

from app.config import Settings, effective_settings
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


def _merge_model_items(
    rules: list[ItemCandidate], modeled: list[ItemCandidate], warnings: list[str],
) -> list[ItemCandidate]:
    """Align original rule/model rows once; never consume an appended model row."""
    result = list(rules)

    def key(value: str | None) -> str:
        return "".join(unicodedata.normalize("NFKC", value or "").split()).casefold()

    def same_item(row: ItemCandidate, candidate: ItemCandidate) -> bool:
        return bool(
            row.source_file == candidate.source_file
            and key(row.product_name)
            and key(row.product_name) == key(candidate.product_name)
            and (not row.model or not candidate.model or key(row.model) == key(candidate.model))
        )

    def numbers_compatible(row: ItemCandidate, candidate: ItemCandidate) -> bool:
        return all(
            getattr(row, field) is None or getattr(candidate, field) is None
            or getattr(row, field) == getattr(candidate, field)
            for field in ("quantity", "unit_price", "total_price")
        )

    unmatched_rules = set(range(len(rules)))
    unmatched_models = set(range(len(modeled)))
    pairs: dict[int, int] = {}
    # Known equal packages have priority over a missing package. A pair must be
    # unique in both directions so the outcome cannot depend on model row order.
    for exact_package in (True, False):
        edges: dict[int, list[int]] = {}
        for m in sorted(unmatched_models):
            candidate = modeled[m]
            matches = []
            for r in sorted(unmatched_rules):
                row = rules[r]
                known_equal = (row.package_code != "default"
                               and key(row.package_code) == key(candidate.package_code))
                missing = "default" in (row.package_code, candidate.package_code)
                package_matches = known_equal if exact_package else missing
                if not same_item(row, candidate) or not package_matches:
                    continue
                # With a missing package, conflicting amounts cannot identify
                # the same row. Known packages can retain a flagged field conflict.
                if exact_package or numbers_compatible(row, candidate):
                    matches.append(r)
            if len(matches) > 1:
                compatible = [r for r in matches if numbers_compatible(rules[r], candidate)]
                matches = compatible or matches
            edges[m] = matches
        for m, matches in edges.items():
            if len(matches) != 1:
                continue
            r = matches[0]
            if sum(r in possible for possible in edges.values()) != 1:
                continue
            pairs[m] = r
            unmatched_rules.remove(r)
            unmatched_models.remove(m)

    fields = ("product_name", "category", "brand", "model", "quantity", "quantity_unit",
              "unit_price", "total_price")
    for m, candidate in enumerate(modeled):
        if m not in pairs:
            if any(same_item(row, candidate) and (
                key(row.package_code) == key(candidate.package_code)
                or "default" in (row.package_code, candidate.package_code)
            ) for row in rules):
                warnings.append(f"模型行无法唯一对齐表格，请核验：{candidate.product_name}")
            result.append(candidate)
            continue
        index = pairs[m]
        row = rules[index]
        changes = {field: getattr(candidate, field) for field in fields
                   if getattr(row, field) is None and getattr(candidate, field) is not None}
        if row.package_code == "default" and candidate.package_code != "default":
            changes["package_code"] = candidate.package_code
        conflicts = [field for field in fields if getattr(row, field) is not None
                     and getattr(candidate, field) is not None
                     and (key(getattr(row, field)) if isinstance(getattr(row, field), str)
                          else str(getattr(row, field)).casefold())
                     != (key(getattr(candidate, field)) if isinstance(getattr(candidate, field), str)
                         else str(getattr(candidate, field)).casefold())]
        if conflicts:
            warnings.append(f"表格与模型字段不一致，保留表格值待核验：{candidate.product_name} / {','.join(conflicts)}")
        changes.update(extraction_method="hybrid_source_verified",
                       source_evidence="\n".join(dict.fromkeys(filter(None, (
                           row.source_evidence, candidate.source_evidence)))))
        result[index] = row.model_copy(update=changes)
    return result


def _deduplicate(items: list[ItemCandidate]) -> list[ItemCandidate]:
    output: list[ItemCandidate] = []
    seen: set[tuple[str | None, ...]] = set()
    for item in items:
        key = tuple(
            (value or "").strip().casefold()
            for value in (
                item.package_code,
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
    result: dict[tuple[str, str], ParticipantCandidate] = {}
    for participant in participants:
        key = (participant.package_code, "".join(participant.organization_name.split()).casefold())
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
    expanded: list[SourceDocument], warnings: list[str], database_path=None,
    *, extraction_mode: str | None = None, model_settings: Settings | None = None,
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
    model_settings = model_settings or effective_settings()
    mode = extraction_mode or model_settings.extraction_mode
    if mode not in {"rules", "model", "hybrid"}:
        raise ValueError("抽取模式必须是 rules、model 或 hybrid")
    model_is_configured = bool(
        model_settings.model_base_url and model_settings.model_api_key and model_settings.model_name
    )
    if mode == "model" and not model_is_configured:
        raise ValueError("纯模型模式需要先配置 Qwen/DeepSeek 接口")
    for document in expanded:
        parse_options = {}
        if model_settings.ocr_enabled:
            parse_options = {
                "ocr_enabled": True, "ocr_language": model_settings.ocr_language,
                "ocr_timeout_seconds": model_settings.ocr_timeout_seconds,
            }
        text, parsed_items, parse_warnings = parse_document(document, **parse_options)
        if mode == "model":
            parsed_items = []
        if text:
            if document == primary_document:
                texts.insert(0, text)
            else:
                texts.append(text)
        if text and model_is_configured and mode != "rules":
            include_participants = True
            parsed_metadata, model_items, model_participants, model_warnings = (
                extract_unstructured_items(
                    filename=document.filename,
                    text=text,
                    settings=model_settings,
                    include_participants=include_participants,
                )
            )
            if mode == "hybrid":
                parsed_items = _merge_model_items(parsed_items, model_items, parse_warnings)
            else:
                parsed_items = model_items
            participants.extend(model_participants)
            if document == primary_document:
                model_metadata = parsed_metadata
            parse_warnings.extend(model_warnings)
        items.extend(parsed_items)
        warnings.extend(parse_warnings)
    rule_metadata = {}
    if mode != "model":
        for text in texts:
            for field, value in extract_metadata(text).model_dump().items():
                if value is not None:
                    rule_metadata.setdefault(field, value)
    metadata = NoticeMetadata(**rule_metadata)
    metadata_values = {
        field: (getattr(metadata, field) if getattr(metadata, field) is not None
                else getattr(model_metadata, field))
        for field in NoticeMetadata.model_fields
    }
    metadata = NoticeMetadata(**metadata_values)
    if mode == "rules":
        warnings.append("规则基线：不调用模型；仅提取表格标的与明确标签元数据")
    elif not model_is_configured:
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
    return save_import(database_path, result) if database_path is not None else result


def extract_notice(
    files: list[SourceDocument], *, extraction_mode: str | None = None,
    model_settings: Settings | None = None,
) -> ImportResult:
    if not files:
        raise ValueError("至少上传一个公告或附件文件")
    expanded, warnings = expand_uploads(files)
    html_count = sum(
        document.filename.lower().endswith((".html", ".htm")) for document in expanded
    )
    if html_count > 1:
        raise ValueError("每次导入请对应一条公告；当前 ZIP 中检测到多份 HTML，请使用批量导入接口")
    return _ingest_expanded(
        expanded, warnings, extraction_mode=extraction_mode, model_settings=model_settings,
    )


def import_notice(
    files: list[SourceDocument], database_path, *, extraction_mode: str | None = None,
) -> ImportResult:
    result = extract_notice(files, extraction_mode=extraction_mode)
    return save_import(database_path, result)


def import_batch(
    files: list[SourceDocument], database_path, *, extraction_mode: str | None = None,
) -> BatchImportResult:
    if not files:
        raise ValueError("至少上传一个公告数据 ZIP")
    started = time.perf_counter()
    expanded, archive_warnings = expand_uploads(files)
    if not expanded:
        detail = '；'.join(archive_warnings) or '上传材料为空'
        raise ValueError(f'未找到可处理的公告或附件：{detail}')
    groups, orphans = group_notice_documents(expanded)
    summaries: list[BatchNoticeSummary] = []
    errors: list[str] = []
    for group in groups:
        try:
            imported = _ingest_expanded(
                group, list(archive_warnings), database_path, extraction_mode=extraction_mode,
            )
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
        warnings=archive_warnings + (
            [f"{len(orphans)} 个附件未能唯一匹配公告，未导入，请检查文件名或分批上传"]
            if orphans else []
        ),
        notices=summaries,
        errors=errors,
    )
