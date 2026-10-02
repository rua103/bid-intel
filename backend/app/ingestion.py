from __future__ import annotations

import hashlib
import re
import time
import unicodedata
from pathlib import PurePosixPath

from app.config import Settings, effective_settings
from app.model_adapter import extract_unstructured_items
from app.package_codes import (
    DEFAULT_PACKAGE_CODE,
    has_explicit_named_package_evidence,
    is_named_package_code,
    normalize_package_code,
)
from app.parsers import (
    SourceDocument,
    expand_uploads,
    extract_metadata,
    parse_document,
    parse_document_with_participants,
)
from app.schemas import (
    BatchImportResult,
    BatchNoticeSummary,
    ImportResult,
    ItemCandidate,
    NoticeMetadata,
    ParticipantCandidate,
)
from app.storage import save_import

_DEFAULT_PARSE_DOCUMENT = parse_document


def _merge_model_items(
    rules: list[ItemCandidate], modeled: list[ItemCandidate], warnings: list[str],
) -> list[ItemCandidate]:
    """Merge model candidates into rule rows without manufacturing duplicates.

    The table parser owns row structure, package evidence, and numeric validation. The
    model owns semantic labels (category/brand/model). A model row is appended only when
    no rule row with the same product/package could be its source; an ambiguous match is
    retained by the rule row and reported for manual review.
    """

    # Pydantic validates normal construction, but model_copy(update=...) deliberately
    # skips validators. Normalize again at this boundary so matching, package fill,
    # and model-only additions all use the same canonical package ids.
    rules = [
        row.model_copy(update={"package_code": normalize_package_code(row.package_code)})
        for row in rules
    ]
    normalized_modeled: list[ItemCandidate] = []
    for row in modeled:
        package_code = normalize_package_code(row.package_code)
        if (
            is_named_package_code(package_code)
            and (
                not has_explicit_named_package_evidence(package_code, row.source_evidence)
                or (row.category and "".join(row.category.split()).casefold()
                    == "".join(package_code.split()).casefold())
            )
        ):
            warnings.append(f"模型名称型包号缺少标签证据，保留 default：{row.product_name}")
            package_code = DEFAULT_PACKAGE_CODE
        normalized_modeled.append(row.model_copy(update={"package_code": package_code}))
    modeled = normalized_modeled

    def key(value: object) -> str:
        return "".join(unicodedata.normalize("NFKC", str(value or "")).split()).casefold()

    def package_key(value: object) -> str:
        return key(normalize_package_code(str(value or "")))

    def present(value: object) -> bool:
        return value is not None and (not isinstance(value, str) or bool(key(value)))

    def same_source_product(row: ItemCandidate, candidate: ItemCandidate) -> bool:
        row_name = key(row.product_name)
        candidate_name = key(candidate.product_name)
        return bool(
            row.source_file == candidate.source_file
            # A missing rule name may be filled by a named model row, but a
            # nameless model row must never match every table row.
            and candidate_name
            and (not row_name or row_name == candidate_name)
        )

    def package_relation(row: ItemCandidate, candidate: ItemCandidate) -> str | None:
        row_package, model_package = package_key(row.package_code), package_key(candidate.package_code)
        if row_package != "default" and model_package != "default":
            return "exact" if row_package == model_package else None
        return "missing"

    def numbers_compatible(row: ItemCandidate, candidate: ItemCandidate) -> bool:
        return all(
            getattr(row, field) is None or getattr(candidate, field) is None
            or getattr(row, field) == getattr(candidate, field)
            for field in ("quantity", "unit_price", "total_price")
        )

    def compatible_values(left: ItemCandidate, right: ItemCandidate) -> bool:
        return all(
            getattr(left, field) is None or getattr(right, field) is None
            or getattr(left, field) == getattr(right, field)
            for field in ("quantity", "unit_price", "total_price")
        )

    def merge_duplicate_models(rows: list[ItemCandidate]) -> list[ItemCandidate]:
        """Collapse only exact repeated model rows.

        Same names and compatible numbers do not identify a physical procurement row;
        official notices may contain multiple identical purchases. Require the same
        package, the same evidence excerpt (including both excerpts being absent),
        and equality of every extracted value. Sparse/complementary rows remain
        separate.
        """
        output: list[ItemCandidate] = []
        all_fields = ("package_code", "product_name", "category", "brand", "model",
                      "quantity", "quantity_unit", "unit_price", "total_price")
        for candidate in rows:
            duplicate = None
            for index, prior in enumerate(output):
                if not same_source_product(prior, candidate):
                    continue
                prior_package = package_key(prior.package_code)
                candidate_package = package_key(candidate.package_code)
                if prior_package != candidate_package:
                    continue
                prior_evidence = key(prior.source_evidence)
                candidate_evidence = key(candidate.source_evidence)
                if prior_evidence != candidate_evidence:
                    continue
                values_equal = all(
                    ((key(getattr(prior, field)) == key(getattr(candidate, field)))
                     if isinstance(getattr(prior, field), str)
                     else getattr(prior, field) == getattr(candidate, field))
                    for field in all_fields
                )
                if values_equal:
                    duplicate = index
                    break
                conflicts = [
                    field for field in all_fields
                    if present(getattr(prior, field)) and present(getattr(candidate, field))
                    and ((key(getattr(prior, field)) != key(getattr(candidate, field)))
                         if isinstance(getattr(prior, field), str)
                         else getattr(prior, field) != getattr(candidate, field))
                ]
                if conflicts:
                    warnings.append(
                        f"模型重复候选字段冲突，保留两行并保留证据："
                        f"{candidate.product_name} / {','.join(conflicts)}"
                    )
            if duplicate is None:
                output.append(candidate)
                continue
            prior = output[duplicate]
            evidence = "\n".join(dict.fromkeys(filter(None, (
                prior.source_evidence, candidate.source_evidence))))
            changes = {"source_evidence": evidence} if evidence else {}
            output[duplicate] = prior.model_copy(update=changes)
            warnings.append(f"模型重复候选合并 2→1：{candidate.product_name}")
        return output

    modeled = merge_duplicate_models(modeled)
    result = list(rules)
    unmatched_rules = set(range(len(rules)))
    unmatched_models = set(range(len(modeled)))
    pairs: dict[int, int] = {}
    def _maximum_matching(edges: dict[int, list[int]]) -> dict[int, int]:
        """Return a deterministic maximum cardinality model→rule matching."""
        owners: dict[int, int] = {}

        def visit(model_index: int, seen: set[int]) -> bool:
            for rule_index in edges.get(model_index, ()):
                if rule_index in seen:
                    continue
                seen.add(rule_index)
                owner = owners.get(rule_index)
                if owner is None or visit(owner, seen):
                    owners[rule_index] = model_index
                    return True
            return False

        for model_index in sorted(edges):
            visit(model_index, set())
        return {model_index: rule_index for rule_index, model_index in owners.items()}

    def _matching_size(edges: dict[int, list[int]]) -> int:
        return len(_maximum_matching(edges))

    def _pair_forced_edges(edges: dict[int, list[int]]) -> dict[int, int]:
        """Pair only assignments present in every maximum matching.

        A plain greedy join can pair one model row with an arbitrary duplicate
        table row. Conversely, accepting only degree-one rows misses forced
        assignments such as ``m1→{r1,r2}, m2→{r1}``. We compute a maximum
        cardinality matching, then retain an edge only when removing it lowers
        that cardinality. Ambiguous cycles therefore stay unmatched and are
        handled by the warning/extra-candidate policy below.
        """
        if not edges:
            return {}
        maximum = _maximum_matching(edges)
        target_size = len(maximum)
        forced: dict[int, int] = {}
        for model_index, rule_index in maximum.items():
            alternatives = {
                candidate: [rule for rule in rows if not (
                    candidate == model_index and rule == rule_index
                )]
                for candidate, rows in edges.items()
            }
            if _matching_size(alternatives) < target_size:
                forced[model_index] = rule_index
        return forced

    # Known equal packages have priority over a missing package. Re-evaluate the
    # graph after every forced assignment so a global one-to-one match is found
    # without choosing an arbitrary edge from an ambiguous cycle.
    for exact_package in (True, False):
        while unmatched_models and unmatched_rules:
            edges: dict[int, list[int]] = {}
            for m in sorted(unmatched_models):
                candidate = modeled[m]
                matches = []
                for r in sorted(unmatched_rules):
                    row = rules[r]
                    known_equal = (package_key(row.package_code) != "default"
                                   and package_key(row.package_code) == package_key(candidate.package_code))
                    missing = "default" in (package_key(row.package_code), package_key(candidate.package_code))
                    package_matches = known_equal if exact_package else missing
                    if not same_source_product(row, candidate) or not package_matches:
                        continue
                    # With a missing package, conflicting amounts cannot identify
                    # the same row. Known packages can retain a flagged field conflict.
                    if exact_package or numbers_compatible(row, candidate):
                        matches.append(r)
                if len(matches) > 1:
                    compatible = [r for r in matches if numbers_compatible(rules[r], candidate)]
                    matches = compatible or matches
                if matches:
                    edges[m] = matches
            forced = _pair_forced_edges(edges)
            if not forced:
                break
            for m, r in forced.items():
                if m not in unmatched_models or r not in unmatched_rules:
                    continue
                pairs[m] = r
                unmatched_rules.remove(r)
                unmatched_models.remove(m)

    for m, candidate in enumerate(modeled):
        if m not in pairs:
            related = any(
                same_source_product(row, candidate)
                and (
                    package_relation(row, candidate) == "exact"
                    or (
                        package_relation(row, candidate) == "missing"
                        and (
                            package_key(candidate.package_code) == "default"
                            or compatible_values(row, candidate)
                        )
                    )
                )
                for row in rules
            )
            if related:
                warnings.append(f"模型行无法唯一对齐表格，保留规则行待核验：{candidate.product_name}")
                continue
            if not key(candidate.product_name):
                warnings.append("模型行缺少标的名称，已丢弃无法核验候选")
                continue
            warnings.append(f"模型额外候选保留，规则表格无对应行：{candidate.product_name}")
            result.append(candidate)
            continue
        index = pairs[m]
        row = rules[index]
        semantic_fields = ("product_name", "category", "brand", "model", "quantity_unit")
        numeric_fields = ("quantity", "unit_price", "total_price")
        changes = {
            field: getattr(candidate, field)
            for field in semantic_fields
            if present(getattr(candidate, field))
        }
        changes.update({
            field: getattr(candidate, field)
            for field in numeric_fields
            if getattr(row, field) is None and getattr(candidate, field) is not None
        })
        if row.package_code == "default" and candidate.package_code != "default":
            changes["package_code"] = normalize_package_code(candidate.package_code)
        semantic_conflicts = [field for field in semantic_fields
                              if present(getattr(row, field)) and present(getattr(candidate, field))
                              and key(getattr(row, field)) != key(getattr(candidate, field))]
        numeric_conflicts = [field for field in numeric_fields if getattr(row, field) is not None
                             and getattr(candidate, field) is not None
                             and getattr(row, field) != getattr(candidate, field)]
        if semantic_conflicts or numeric_conflicts:
            conflict_fields = semantic_conflicts + numeric_conflicts
            warnings.append(
                f"表格与模型字段不一致，模型语义/规则数值优先待核验："
                f"{candidate.product_name} / {','.join(conflict_fields)}"
            )
        changes.update(extraction_method="hybrid_source_verified",
                       source_evidence="\n".join(dict.fromkeys(filter(None, (
                           row.source_evidence, candidate.source_evidence)))))
        result[index] = row.model_copy(update=changes)
    return result


def _deduplicate(
    items: list[ItemCandidate], warnings: list[str] | None = None,
) -> list[ItemCandidate]:
    """Merge only unambiguous, mutually compatible rows from different files."""
    warnings = warnings if warnings is not None else []

    def key(value: str | None) -> str:
        return ''.join(unicodedata.normalize('NFKC', value or '').split()).casefold()

    def package(value: str) -> str:
        return key(normalize_package_code(value))

    fields = ('category', 'brand', 'model', 'quantity', 'quantity_unit',
              'unit_price', 'total_price')

    def same_source_exact_duplicate(left: ItemCandidate, right: ItemCandidate) -> bool:
        """Recognize duplicate parser output for the same physical table row.

        Same-file rows at different locations are distinct procurement lines even
        when every extracted field is equal. Model evidence does not contain a
        reliable row address, so model candidates are never collapsed here.
        """
        if left.source_file != right.source_file:
            return False
        row_address = r'(?:^|/)table:\d+/row:\d+$'
        if (left.source_location != right.source_location
                or not re.search(row_address, left.source_location)):
            return False
        if not left.source_evidence or left.source_evidence != right.source_evidence:
            return False
        if left.extraction_method != right.extraction_method:
            return False
        if (left.extraction_method.casefold().startswith(('qwen', 'deepseek', 'model'))
                or 'model' in left.source_location.casefold()
                or 'model' in right.source_location.casefold()):
            return False
        values = ('package_code', 'product_name', 'category', 'brand', 'model',
                  'quantity', 'quantity_unit', 'unit_price', 'total_price')
        return all(
            (key(normalize_package_code(getattr(left, field)))
             == key(normalize_package_code(getattr(right, field))))
            if field == 'package_code' else getattr(left, field) == getattr(right, field)
            for field in values
        )

    def source_row_conflicts(left: ItemCandidate, right: ItemCandidate) -> list[str]:
        row_address = r'(?:^|/)table:\d+/row:\d+$'
        if (left.source_file != right.source_file
                or left.source_location != right.source_location
                or not re.search(row_address, left.source_location)):
            return []
        fields_to_compare = ('package_code', 'product_name', *fields)
        return [field for field in fields_to_compare
                if getattr(left, field) != getattr(right, field)]

    edges: list[set[int]] = [set() for _ in items]
    for i, left in enumerate(items):
        for j in range(i + 1, len(items)):
            right = items[j]
            if left.source_file == right.source_file:
                if same_source_exact_duplicate(left, right):
                    edges[i].add(j)
                    edges[j].add(i)
                else:
                    conflicts = source_row_conflicts(left, right)
                    if conflicts:
                        warnings.append(
                            f'同一来源行候选字段冲突，保留待核验：'
                            f'{left.product_name or right.product_name or "未命名标的"} / '
                            + ','.join(conflicts)
                        )
                continue
            if not key(left.product_name) or key(left.product_name) != key(right.product_name):
                continue
            packages = (package(left.package_code), package(right.package_code))
            if packages[0] != packages[1] and 'default' not in packages:
                continue
            shared = set()
            conflicts = []
            for field in fields:
                a, b = getattr(left, field), getattr(right, field)
                if a is None or b is None:
                    continue
                equal = key(a) == key(b) if isinstance(a, str) else a == b
                if equal:
                    shared.add(field)
                else:
                    conflicts.append(field)
            # A name alone or two sparse requirement rows cannot identify a
            # duplicate. Decimal comparisons naturally treat 1 and 1.0 equally.
            strong = (bool(shared & {'unit_price', 'total_price'})
                      and bool(shared & {'quantity', 'model'})) or (
                          {'model', 'quantity'} <= shared)
            if conflicts:
                warnings.append(f'跨文件同名候选字段冲突，保留待核验：{left.product_name} / '
                                + ','.join(conflicts))
            elif strong:
                edges[i].add(j)
                edges[j].add(i)

    output: list[ItemCandidate] = []
    visited: set[int] = set()
    for start in range(len(items)):
        if start in visited:
            continue
        component, pending = set(), [start]
        while pending:
            index = pending.pop()
            if index in component:
                continue
            component.add(index)
            pending.extend(edges[index] - component)
        visited.update(component)
        indices = sorted(component)
        if len(component) == 1:
            output.append(items[start])
            continue
        # Requiring a clique prevents a missing-package row from bridging two
        # packages, and prevents one attachment row consuming two source rows.
        if any(component - {index} != edges[index] for index in component):
            output.extend(items[index] for index in indices)
            warnings.append(f'跨文件候选无法唯一对齐，保留待核验：{items[start].product_name}')
            continue
        merged = items[start]
        evidence = []
        for index in indices:
            row = items[index]
            changes = {field: getattr(row, field) for field in fields
                       if getattr(merged, field) is None and getattr(row, field) is not None}
            if merged.package_code == 'default' and row.package_code != 'default':
                changes['package_code'] = row.package_code
            merged = merged.model_copy(update=changes)
            evidence.append(f'[{row.source_file} @ {row.source_location}]\n'
                            + (row.source_evidence or ''))
        same_source = len({items[index].source_file for index in indices}) == 1
        output.append(merged.model_copy(update={
            'source_evidence': '\n'.join(evidence),
            'extraction_method': (
                'same_source_replay_verified' if same_source
                else 'cross_file_source_verified'
            ),
        }))
        relation = '同源重复候选' if same_source else '跨文件重复候选'
        warnings.append(f'{relation}合并 {len(indices)}→1（保留来源证据）：{merged.product_name}')
    return output


def _reference_attachment(filename: str) -> bool:
    # Parent archives often say 采购文件集 while containing actual quotations.
    # Classify only the leaf filename, and give explicit result documents priority.
    name = PurePosixPath(filename.replace('\\', '/')).stem
    if re.search(r'报价|成交|中标|评审|评标|开标', name):
        return False
    return bool(re.search(r'招标文件|磋商文件|谈判文件|采购文件|采购需求|响应文件格式|投标文件格式', name)
                or re.search(r'(?:空白|填写|填报|格式)模板', name))


def _unfilled_template(text: str, items: list[ItemCandidate]) -> bool:
    """Recognize explicit empty fields, never classify on a generic 模板 keyword."""
    compact = ''.join(unicodedata.normalize('NFKC', text).split())
    if re.search(r'投标人名称[:：](?:合计|备注|时间)', compact) and (
        compact.count('{供应商响应}') >= 2
    ):
        return True
    if items:
        return False
    return bool(
        ('(供应商名称)' in compact and '(项目名称)' in compact
         and re.search(r'供应商名称\(加盖公章\)[:：]日期[:：]$', compact))
        or re.search(r'供应商单位全称[:：]\(公章\)', compact)
        and re.search(r'项目编号为包号为', compact)
        or '此表为表样' in compact and re.search(r'供应商全称\(公章\)[:：]序号', compact)
    )


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
        detail = '；'.join(warnings) or '上传材料为空'
        raise ValueError(f'没有找到可解析的公告或附件：{detail}')
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
        if html_documents and document != primary_document and _reference_attachment(document.filename):
            warnings.append(f'{document.filename}: 参考采购材料，保留来源；不抽取成交标的、主体或元数据')
            continue
        parse_options = {}
        if model_settings.ocr_enabled:
            parse_options = {
                "ocr_enabled": True, "ocr_language": model_settings.ocr_language,
                "ocr_timeout_seconds": model_settings.ocr_timeout_seconds,
            }
        if parse_document is _DEFAULT_PARSE_DOCUMENT:
            text, parsed_items, parsed_participants, parse_warnings = (
                parse_document_with_participants(document, **parse_options)
            )
        else:
            # Preserve existing three-value parser test hooks during this API extension.
            text, parsed_items, parse_warnings = parse_document(document, **parse_options)
            parsed_participants = []
        if document != primary_document and _unfilled_template(text, parsed_items):
            warnings.extend(parse_warnings)
            warnings.append(f'{document.filename}: 检测到未填写模板占位，保留来源；不作为成交结果或送入模型')
            continue
        participants.extend(parsed_participants)
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
        warnings.append("规则基线：不调用模型；提取表格标的、明确投标主体及标签元数据")
    elif not model_is_configured:
        warnings.append("未配置合规 Qwen/DeepSeek 模型；非表格标的尚未自动抽取")
    deduplicated_items = _deduplicate(items, warnings)
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
    return save_import(
        database_path,
        result,
        source_hashes=[
            (document.filename,
             document.source_sha256 or hashlib.sha256(document.content).hexdigest(),
             document.source_size if document.source_size is not None else len(document.content))
            for document in expanded
        ],
    ) if database_path is not None else result


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
    if not files:
        raise ValueError("至少上传一个公告或附件文件")
    expanded, warnings = expand_uploads(files)
    html_count = sum(
        document.filename.lower().endswith((".html", ".htm")) for document in expanded
    )
    if html_count > 1:
        raise ValueError("每次导入请对应一条公告；当前 ZIP 中检测到多份 HTML，请使用批量导入接口")
    return _ingest_expanded(
        expanded, warnings, database_path, extraction_mode=extraction_mode,
    )


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
