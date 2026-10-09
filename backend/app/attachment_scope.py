"""Conservative document-purpose checks, independent of evidence grounding.

Archive/notice/package names are deliberately not used as evidence of current
procurement. Filename hints always require parsing before any exclusion. Keep
every source in the import's audit ledger even when its extraction is disabled.
"""

from __future__ import annotations

import re
import unicodedata
from pathlib import PurePosixPath
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from app.schemas import ItemCandidate, ParticipantCandidate

AttachmentScope = Literal[
    "current_procurement", "current_evaluation", "historical_qualification", "mixed", "unknown",
]

# Generic 业绩, 项目名称, 采购包 and 项目编号 are not purpose headings. A
# supplier's historical table often carries the *current* tender's cover labels.
_HISTORY = re.compile(
    r"(?:供应商|投标人)?(?:同类|类似)项目(?:实施情况|业绩)(?:一览表|汇总表|表)"
    r"|(?:案例|业绩|历史合同|历史项目)(?:一览表|汇总表|清单)"
    r"|(?:历史|过往|已完成|已承接)(?:项目)?业绩"
    r"|(?:类似|同类)项目业绩(?:证明|材料)"
)
_CURRENT_ITEMS = re.compile(
    r"(?:本次|本项目)?(?:采购|供货|货物|需求)(?:清单|明细表)"
    r"|(?:分项|货物|产品|服务)报价(?:明细表|明细|表)"
    r"|(?:主要)?(?:中标|成交|投标)标的(?:名称|信息|明细)"
    r"|(?:本次|本项目)(?:采购内容|采购标的|供货内容)"
)
_EVALUATION = re.compile(
    r"(?:评审|评分|评标|开标)(?:结果|汇总表|一览表|情况表|得分)"
    r"|(?:资格性?|符合性)审查(?:结果|汇总表|一览表)"
    r"|(?:本次|本项目)(?:投标人|响应供应商|资格审查|符合性审查)"
)
_QUALIFICATION_TITLE = re.compile(
    r"^(?:附件\d*[:：]?)?(?:营业执照|资质证书|资格证明材料|资格证明文件|信用记录证明)"
)
_FILENAME_HISTORY = re.compile(r"业绩|案例一览|同类项目实施情况|历史合同|历史项目")
_FILENAME_CURRENT = re.compile(
    r"采购清单|供货清单|货物清单|分项报价|报价明细|中标标的|成交标的|招标文件|采购需求"
)
_FILENAME_EVALUATION = re.compile(r"评审|评分|评标|开标|投标人名单|资格审查结果")
_CONTRACT_FIELDS = re.compile(r"合同签订(?:日期|时间)|合同复印|合同扫描|验收(?:日期|时间)|项目单位地址")
_REQUEST_LANGUAGE = re.compile(r"(?:应|须|需|要求|请)(?:当|要)?(?:提供|提交|附|填写)|格式见|详见|评分标准")
# PDF parsers prefix extracted pages without preserving all line boundaries.
# Accept only their known leading marker; keep it in evidence offset accounting.
_PDF_PAGE_MARKER = r"\[page:[1-9]\d*(?:/(?:pdfplumber|ocr))?\]"
_HEADING_PREFIX = re.compile(
    rf"^(?:{_PDF_PAGE_MARKER})?"
    r"[第\d一二三四五六七八九十章节附件附表()（）.、:：\-]*$"
)
_OTHER_SECTION = re.compile(r"^[^\d]{2,35}(?:参数|说明|清单|信息|内容|报价|明细|结果|一览表|要求)$")
_SECTION_PROSE = re.compile(r"[，,。；;、]|相应|等内容|应当|须提供|及")
_PRODUCT_HEADER = re.compile(r"品目名称|货物名称|产品名称|商品名称|标的名称|采购标的|名称")
_ITEM_FIELDS = re.compile(r"品牌|规格型号|规格|型号|数量|单价|总价")


class AttachmentScopeDecision(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    source_file: str
    scope: AttachmentScope = "unknown"
    extract_items: bool = True
    extract_participants: bool = True
    extract_metadata: bool = True
    requires_parse: bool = False
    reason_codes: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)


def _compact(value: str) -> str:
    return "".join(unicodedata.normalize("NFKC", value).split()).casefold()


def _leaf_stem(filename: str) -> str:
    # Parent ZIPs frequently describe tender requirements, not their leaf's use.
    return _compact(PurePosixPath(filename.replace("\\", "/").split("!/")[-1]).stem)


def _purpose_headings(text: str) -> list[tuple[int, int, str]]:
    """Map structural headings to compact-text offsets without matching prose.

    Historical/current-evaluation exclusion requires a line-start title. An
    unfamiliar section title ends inheritance from a prior historical section.
    Actual goods headers also protect tables lacking a recognized section title.
    """
    headings: list[tuple[int, int, str]] = []
    offset = 0
    normalized = unicodedata.normalize("NFKC", text)
    if re.match(rf"^\s*{_PDF_PAGE_MARKER}", normalized):
        # Recover page boundaries in the parser's flattened PDF text, retaining
        # marker characters so compact-text evidence offsets remain unchanged.
        normalized = re.sub(rf"(?<!\S)(?={_PDF_PAGE_MARKER})", "\n", normalized)
    for raw_line in normalized.splitlines():
        line = _compact(raw_line)
        matches: list[tuple[int, int, str]] = []
        for pattern, scope in ((_HISTORY, "history"), (_CURRENT_ITEMS, "current"),
                               (_EVALUATION, "evaluation")):
            for match in pattern.finditer(line):
                prefix = line[:match.start()]
                if scope != "current" and not _HEADING_PREFIX.fullmatch(prefix):
                    continue
                if scope == "history" and _REQUEST_LANGUAGE.search(prefix):
                    continue
                matches.append((offset + match.start(), offset + match.end(), scope))
        fields = set(_ITEM_FIELDS.findall(line))
        if (_PRODUCT_HEADER.search(line) and len(fields) >= 2
                and fields.intersection({"品牌", "规格型号", "规格", "型号"})):
            # Mark the whole header line, not the first product row.
            matches.append((offset, offset + len(line), "current"))
        if (not matches and "\t" not in raw_line
                and not re.search(r"项目名称|采购单位名称|序号", line)
                and not _SECTION_PROSE.search(line)
                and _OTHER_SECTION.fullmatch(line)):
            matches.append((offset, offset + len(line), "unknown"))
        headings.extend(matches)
        offset += len(line)
    return sorted(headings)


def classify_attachment_scope(filename: str, text: str | None = None) -> AttachmentScopeDecision:
    """Classify a leaf before/after parsing without guessing current package ids.

    ``text=None`` is a pre-parse hint, never permission to discard a file.
    ``extract_participants`` concerns current participation, not names of clients
    in a historical contract. Evaluation metadata is disabled because score tables
    do not establish current project amounts. Mixed/unknown sources require
    candidate-level checks.
    """
    name = _leaf_stem(filename)
    history_hint = bool(_FILENAME_HISTORY.search(name))
    if text is None:
        return AttachmentScopeDecision(
            source_file=filename, requires_parse=True,
            reason_codes=["filename_history_hint"] if history_hint else ["purpose_requires_text"],
        )
    body = _compact(text)
    if not body:
        return AttachmentScopeDecision(
            source_file=filename, reason_codes=["empty_or_unreadable_text"],
            warnings=[f"{filename}: 附件用途不明（正文为空），保留来源与候选待核验"],
        )
    headings = _purpose_headings(text)
    history = any(scope == "history" for _, _, scope in headings)
    # A generic 业绩 filename needs corroborating historical contract fields.
    contract_history = history_hint and bool(_CONTRACT_FIELDS.search(body))
    qualification = bool(_QUALIFICATION_TITLE.search(body))
    current = any(scope == "current" for _, _, scope in headings)
    current_hint = bool(_FILENAME_CURRENT.search(name))
    evaluation = any(scope == "evaluation" for _, _, scope in headings)
    unknown_sections = any(scope == "unknown" for _, _, scope in headings)
    evaluation_hint = bool(_FILENAME_EVALUATION.search(name))
    historical = history or contract_history or qualification
    reasons = []
    if history:
        reasons.append("historical_table_heading")
    if contract_history:
        reasons.append("history_filename_and_contract_fields")
    if qualification:
        reasons.append("qualification_document_heading")
    if current:
        reasons.append("current_item_section")
    if evaluation:
        reasons.append("current_evaluation_section")
    # Filename conflicts trigger preservation, not a winner-takes-all decision.
    if historical and (current or current_hint or evaluation or evaluation_hint or unknown_sections):
        return AttachmentScopeDecision(
            source_file=filename, scope="mixed", extract_metadata=False,
            reason_codes=reasons + ["mixed_purpose"],
            warnings=[f"{filename}: 附件用途混合，当前清单及主体逐条核验；禁用项目元数据通道"],
        )
    if historical:
        return AttachmentScopeDecision(
            source_file=filename, scope="historical_qualification", extract_items=False,
            extract_participants=False, extract_metadata=False, reason_codes=reasons,
            warnings=[
                f"{filename}: 历史业绩/资格材料，保留审计来源；"
                "不抽取本次采购标的、参与主体或项目元数据（" + ",".join(reasons) + "）",
            ],
        )
    if current:
        return AttachmentScopeDecision(
            source_file=filename, scope="current_procurement", reason_codes=reasons,
        )
    if evaluation:
        return AttachmentScopeDecision(
            source_file=filename, scope="current_evaluation", extract_items=False,
            extract_metadata=False,
            reason_codes=reasons,
            warnings=[f"{filename}: 当前评审/投标证据，保留当前参与主体；不抽取评分行或项目元数据"],
        )
    return AttachmentScopeDecision(
        source_file=filename, reason_codes=["purpose_not_confirmed"],
        warnings=[f"{filename}: 附件用途不明，保留候选；证据出现不等于属于本次采购，需核验"],
    )


def _evidence_scopes(text: str, evidence: str) -> set[str]:
    """Use every exact occurrence, never arbitrarily choose the first repeated row."""
    body = _compact(text)
    headings = _purpose_headings(text)
    scopes: set[str] = set()
    offset = 0
    while evidence:
        start = body.find(evidence, offset)
        if start < 0:
            break
        end = start + len(evidence)
        prior = [row for row in headings if row[0] <= start]
        scope = prior[-1][2] if prior else "unknown"
        scopes.add(scope)
        # Evidence spanning a boundary is ambiguous, so preserve the current row.
        scopes.update(row[2] for row in headings if start < row[0] < end)
        offset = start + 1
    return scopes


def filter_attachment_items(
    filename: str, text: str, items: list[ItemCandidate],
    decision: AttachmentScopeDecision | None = None,
) -> tuple[list[ItemCandidate], list[str]]:
    """Apply purpose restrictions to rule and model rows, preserving originals.

    This does not validate whether product/brand/package values are grounded.
    Missing/unlocatable evidence stays pending with a warning; the independent
    grounding layer must reject fabricated values. No candidates are rewritten.
    """
    decision = decision or classify_attachment_scope(filename, text)
    if decision.source_file != filename or decision.requires_parse:
        raise ValueError("附件用途判定必须来自同一文件的解析后正文")
    if any(item.source_file != filename for item in items):
        raise ValueError("采购标的来源与附件用途判定不一致")
    warnings = list(decision.warnings)
    if not decision.extract_items:
        if items:
            warnings.append(
                f"{filename}: 附件用途排除采购标的 {len(items)} 条（{decision.scope}），保留审计来源",
            )
        return [], warnings
    if decision.scope != "mixed":
        return list(items), warnings
    kept: list[ItemCandidate] = []
    for item in items:
        evidence = _compact(item.source_evidence or "")
        scopes = _evidence_scopes(text, evidence)
        if scopes and scopes.issubset({"history", "evaluation"}):
            warnings.append(
                f"{filename}: 排除非本次采购清单候选 {item.product_name or '(无名称)'}"
                f"（{'/'.join(sorted(scopes))}段落；原文存在仍不代表本次采购）",
            )
            continue
        kept.append(item)
        if scopes != {"current"}:
            warnings.append(
                f"{filename}: 混合附件候选用途待核验，保留 {item.product_name or '(无名称)'}"
                f"（{'/'.join(sorted(scopes)) or '证据缺失或无法定位'}）",
            )
    return kept, list(dict.fromkeys(warnings))


def filter_attachment_participants(
    filename: str, text: str, participants: list[ParticipantCandidate],
    decision: AttachmentScopeDecision | None = None,
) -> tuple[list[ParticipantCandidate], list[str]]:
    """Exclude historical clients/suppliers while retaining current score rows.

    This remains independent of participant grounding and winner-status checks.
    Unclear/missing evidence is retained with an explicit review warning.
    """
    decision = decision or classify_attachment_scope(filename, text)
    if decision.source_file != filename or decision.requires_parse:
        raise ValueError("附件用途判定必须来自同一文件的解析后正文")
    if any(row.source_file != filename for row in participants):
        raise ValueError("参与主体来源与附件用途判定不一致")
    warnings = list(decision.warnings)
    if not decision.extract_participants:
        if participants:
            warnings.append(
                f"{filename}: 附件用途排除参与主体 {len(participants)} 条"
                f"（{decision.scope}），保留审计来源",
            )
        return [], warnings
    if decision.scope != "mixed":
        return list(participants), warnings
    kept: list[ParticipantCandidate] = []
    for participant in participants:
        scopes = _evidence_scopes(text, _compact(participant.source_evidence or ""))
        if scopes == {"history"}:
            warnings.append(
                f"{filename}: 排除历史段落参与主体 {participant.organization_name}，保留审计来源",
            )
            continue
        kept.append(participant)
        if not scopes or not scopes.issubset({"current", "evaluation"}):
            warnings.append(
                f"{filename}: 混合附件主体用途待核验，保留 {participant.organization_name}"
                f"（{'/'.join(sorted(scopes)) or '证据缺失或无法定位'}）",
            )
    return kept, list(dict.fromkeys(warnings))
