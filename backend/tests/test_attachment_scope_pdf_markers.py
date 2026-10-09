import io

import pytest
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.cidfonts import UnicodeCIDFont
from reportlab.pdfgen.canvas import Canvas

from app.attachment_scope import (
    classify_attachment_scope,
    filter_attachment_items,
    filter_attachment_participants,
)
from app.parsers import SourceDocument, parse_document_with_participants
from app.schemas import ItemCandidate, ParticipantCandidate


def _item(filename, name, evidence):
    return ItemCandidate(
        source_file=filename, source_location="model_text_evidence",
        product_name=name, source_evidence=evidence,
    )


@pytest.mark.parametrize("marker", ["[page:1]", "[page:2/pdfplumber]", "[page:3/ocr]"])
def test_pdf_page_heading_excludes_history_without_losing_audit_warning(marker):
    filename = "archive.zip!/reference.pdf"
    text = f"{marker} 案例一览表\n旧设备项目 1套 历史客户"
    candidate = _item(filename, "旧设备项目", "旧设备项目 1套")
    participant = ParticipantCandidate(
        source_file=filename, source_location="model_text_evidence",
        organization_name="历史客户", source_evidence="历史客户",
    )
    decision = classify_attachment_scope(filename, text)
    assert decision.scope == "historical_qualification"
    assert not any((decision.extract_items, decision.extract_participants, decision.extract_metadata))
    kept, warnings = filter_attachment_items(filename, text, [candidate], decision)
    assert not kept
    assert any(filename in warning and "保留审计来源" in warning for warning in warnings)
    assert any("排除采购标的 1 条" in warning for warning in warnings)
    kept, warnings = filter_attachment_participants(filename, text, [participant], decision)
    assert not kept
    assert any(filename in warning and "排除参与主体 1 条" in warning for warning in warnings)


def test_generated_pdf_reaches_history_filter_through_current_parser():
    pdfmetrics.registerFont(UnicodeCIDFont("STSong-Light"))
    buffer = io.BytesIO()
    canvas = Canvas(buffer)
    canvas.setFont("STSong-Light", 12)
    canvas.drawString(30, 750, "案例一览表")
    canvas.drawString(30, 720, "旧设备项目 1套")
    canvas.save()
    filename = "reference.pdf"
    text, _, _, _ = parse_document_with_participants(SourceDocument(filename, buffer.getvalue()))
    assert text.startswith("[page:1]")
    assert "旧设备项目" in text
    candidate = _item(filename, "旧设备项目", "旧设备项目 1套")
    kept, warnings = filter_attachment_items(filename, text, [candidate])
    assert not kept
    assert any("排除采购标的 1 条" in warning for warning in warnings)


@pytest.mark.parametrize("history_first", [True, False])
def test_flattened_pdf_pages_keep_current_participants_and_exclude_history(history_first):
    pdfmetrics.registerFont(UnicodeCIDFont("STSong-Light"))
    buffer = io.BytesIO()
    canvas = Canvas(buffer)
    sections = [
        ("案例一览表", "旧设备项目 历史客户"),
        ("评审结果汇总表", "投标人甲公司 98分"),
    ]
    if not history_first:
        sections.reverse()
    for title, evidence in sections:
        canvas.setFont("STSong-Light", 12)
        canvas.drawString(30, 750, title)
        canvas.drawString(30, 720, evidence)
        canvas.showPage()
    canvas.save()
    filename = "reference.pdf"
    text, _, _, _ = parse_document_with_participants(SourceDocument(filename, buffer.getvalue()))
    assert len(text.splitlines()) == 1 and "[page:2]" in text
    rows = [ParticipantCandidate(
        source_file=filename, source_location="model_text_evidence",
        organization_name=name, source_evidence=evidence,
    ) for name, evidence in [
        ("历史客户", "旧设备项目 历史客户"), ("甲公司", "投标人甲公司 98分"),
    ]]
    decision = classify_attachment_scope(filename, text)
    assert decision.scope == "mixed"
    kept, warnings = filter_attachment_participants(filename, text, rows, decision)
    assert [row.organization_name for row in kept] == ["甲公司"]
    assert any("排除历史段落参与主体" in warning for warning in warnings)


@pytest.mark.parametrize("marker", ["[page:2/pdfplumber]", "[page:2/ocr]"])
def test_flattened_mixed_page_markers_preserve_candidate_offsets(marker):
    filename = "combined.pdf"
    text = f"[page:1] 案例一览表 旧设备项目 1套 {marker} 本次采购清单 打印机 2台"
    candidates = [_item(filename, "旧设备项目", "旧设备项目 1套"),
                  _item(filename, "打印机", "打印机 2台")]
    assert classify_attachment_scope(filename, text).scope == "mixed"
    kept, _ = filter_attachment_items(filename, text, candidates)
    assert [row.product_name for row in kept] == ["打印机"]


@pytest.mark.parametrize("marker", ["[page:1]", "[page:2/pdfplumber]", "[page:3/ocr]"])
def test_page_marked_history_submission_requirement_keeps_current_items(marker):
    filename = "attachment.pdf"
    text = f"{marker} 供应商须提供案例一览表。\n本次采购清单\n打印机 2台"
    candidate = _item(filename, "打印机", "打印机 2台")
    decision = classify_attachment_scope(filename, text)
    assert decision.scope == "current_procurement"
    assert filter_attachment_items(filename, text, [candidate])[0] == [candidate]


@pytest.mark.parametrize("marker", ["[page:1]", "[page:2/pdfplumber]", "[page:3/ocr]"])
def test_page_marked_current_evaluation_retains_participants(marker):
    filename = "attachment.pdf"
    text = f"{marker} 评审结果汇总表\n投标人甲公司 98分"
    participant = ParticipantCandidate(
        source_file=filename, source_location="model_text_evidence",
        organization_name="甲公司", source_evidence="投标人甲公司 98分",
    )
    decision = classify_attachment_scope(filename, text)
    assert decision.scope == "current_evaluation"
    assert filter_attachment_participants(filename, text, [participant])[0] == [participant]
    assert not decision.extract_items and not decision.extract_metadata


def test_marked_mixed_sections_keep_offsets_and_ambiguous_evidence():
    filename = "combined.pdf"
    text = (
        "[page:1] 案例一览表\n旧设备项目 1套\n共同项目 1套\n"
        "[page:2/pdfplumber] 本次采购清单\n打印机 2台\n共同项目 1套\n"
        "[page:3/ocr] 评审结果汇总表\n投标人甲公司 98分"
    )
    candidates = [
        _item(filename, "旧设备项目", "旧设备项目 1套"),
        _item(filename, "打印机", "打印机 2台"),
        _item(filename, "甲公司", "投标人甲公司 98分"),
        _item(filename, "重复证据", "共同项目 1套"),
        _item(filename, "跨页证据", "共同项目 1套 [page:2/pdfplumber] 本次采购清单"),
    ]
    decision = classify_attachment_scope(filename, text)
    assert decision.scope == "mixed"
    kept, warnings = filter_attachment_items(filename, text, candidates, decision)
    assert [row.product_name for row in kept] == ["打印机", "重复证据", "跨页证据"]
    assert sum("排除非本次采购清单候选" in warning for warning in warnings) == 2
    assert sum("用途待核验" in warning for warning in warnings) == 2


@pytest.mark.parametrize("prefix", [
    "[page:0]", "[page:x]", "[page:1/custom]", "[section:1]", "说明 [page:1]",
])
def test_unknown_marker_does_not_authorize_history_exclusion(prefix):
    filename = "attachment.pdf"
    text = f"{prefix} 案例一览表 旧设备项目 1套"
    candidate = _item(filename, "旧设备项目", "旧设备项目 1套")
    assert classify_attachment_scope(filename, text).scope == "unknown"
    assert filter_attachment_items(filename, text, [candidate])[0] == [candidate]
