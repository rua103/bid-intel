import json

import pytest

from app import ingestion
from app.config import Settings
from app.gold_route_evaluation import _hash_code
from app.parsers import SourceDocument
from app.schemas import ItemCandidate, NoticeMetadata, ParticipantCandidate


def _item(filename, name="打印机", **changes):
    return ItemCandidate(
        source_file=filename, source_location="table:1/row:1",
        product_name=name, **changes,
    )


def _settings():
    return Settings(
        _env_file=None, model_base_url="https://example.invalid/v1",
        model_api_key="mock", model_name="qwen-test", ocr_enabled=False,
    )


@pytest.mark.parametrize("mode", ["rules", "hybrid", "model"])
def test_history_cannot_create_items_participants_metadata_or_model_calls(monkeypatch, mode):
    calls = []
    current = _item("notice.html", source_evidence="打印机 2台", quantity=2)
    history_file = "archive.zip!/案例一览表.txt"
    past = _item(history_file, "历史服务", source_evidence="历史服务 100元")
    client = ParticipantCandidate(
        source_file=history_file, source_location="row:1", organization_name="历史客户",
        outcome="winner", source_evidence="历史服务 历史客户 100元",
    )

    def parse(document, **kwargs):
        if document.filename == "notice.html":
            return "项目名称：本次采购\n采购清单\n打印机 2台", [current], [], []
        return (
            "案例一览表\n项目名称：历史项目\n项目预算：999万元\n历史服务 历史客户 100元",
            [past], [client], ["原有解析告警"],
        )

    def model(**kwargs):
        calls.append(kwargs["filename"])
        assert kwargs["filename"] == "notice.html"
        return NoticeMetadata(project_name="本次采购"), [current], [], []

    monkeypatch.setattr(ingestion, "parse_document_with_participants", parse)
    monkeypatch.setattr(ingestion, "extract_unstructured_items", model)
    result = ingestion._ingest_expanded(
        [SourceDocument("notice.html", b"notice"), SourceDocument(history_file, b"history")],
        [], extraction_mode=mode, model_settings=_settings(),
    )
    assert [row.product_name for row in result.items] == ["打印机"]
    assert not result.participants
    assert result.metadata.project_name == "本次采购"
    assert result.metadata.project_budget is None
    assert history_file in result.source_files
    assert "原有解析告警" in result.warnings
    assert any("历史业绩" in warning for warning in result.warnings)
    assert calls == ([] if mode == "rules" else ["notice.html"])


def test_evaluation_attachment_keeps_bidders_but_not_score_items_or_budget(monkeypatch):
    filename = "评审结果.txt"
    bidder = ParticipantCandidate(
        source_file=filename, source_location="row:1", organization_name="本次投标公司",
        outcome="nonwinner", source_evidence="本次投标公司 92.5 2",
    )
    score = _item(filename, "本次投标公司", source_evidence="本次投标公司 92.5 2")

    def parse(document, **kwargs):
        if document.filename == "notice.html":
            return "项目名称：当前项目", [], [], []
        return "评审结果汇总表\n项目预算：999万元\n本次投标公司 92.5 2", [score], [bidder], []

    def model(**kwargs):
        if kwargs["filename"] == "notice.html":
            return NoticeMetadata(project_name="当前项目"), [], [], []
        return NoticeMetadata(project_budget=9990000), [score], [bidder], []

    monkeypatch.setattr(ingestion, "parse_document_with_participants", parse)
    monkeypatch.setattr(ingestion, "extract_unstructured_items", model)
    result = ingestion._ingest_expanded(
        [SourceDocument("notice.html", b"notice"), SourceDocument(filename, b"evaluation")],
        [], extraction_mode="hybrid", model_settings=_settings(),
    )
    assert not result.items
    assert [row.organization_name for row in result.participants] == ["本次投标公司"]
    assert result.metadata.project_budget is None


def test_production_reconciliation_does_not_bypass_missing_source_link(monkeypatch):
    def parse(document, **kwargs):
        return "打印机 X1 2台 单价100", [_item(
            document.filename, package_code="1", model="X1", quantity=2,
            unit_price=100, source_evidence="打印机 X1 2台 单价100",
        )], [], []

    monkeypatch.setattr(ingestion, "parse_document_with_participants", parse)
    result = ingestion._ingest_expanded(
        [SourceDocument("notice.html", b"notice"), SourceDocument("quote.txt", b"quote")],
        [], extraction_mode="rules", model_settings=_settings(),
    )
    assert len(result.items) == 2


def test_frozen_evaluation_identity_covers_quality_modules():
    hashes = _hash_code()
    assert {"attachment_scope.py", "candidate_reconciliation.py"} <= hashes.keys()
    assert all(len(hashes[name]) == 64 for name in (
        "attachment_scope.py", "candidate_reconciliation.py",
    ))


@pytest.mark.parametrize("mode", ["rules", "hybrid", "model"])
def test_production_reconciles_independently_linked_sources_with_package_evidence(monkeypatch, mode):
    def contents(filename):
        package = "1" if filename == "notice.html" else "default"
        evidence = "包号：1 打印机 X1 2台 单价100" if package == "1" else "打印机 X1 2台 单价100"
        return (
            f"项目名称：办公采购\n项目编号：TEST-2026-1\n采购清单\n{evidence}",
            [_item(filename, package_code=package, model="X1", quantity=2,
                   unit_price=100, source_evidence=evidence)],
        )

    def parse(document, **kwargs):
        text, items = contents(document.filename)
        return text, items, [], []

    def model(**kwargs):
        _, items = contents(kwargs["filename"])
        return NoticeMetadata(project_name="办公采购", project_number="TEST-2026-1"), items, [], []

    monkeypatch.setattr(ingestion, "parse_document_with_participants", parse)
    monkeypatch.setattr(ingestion, "extract_unstructured_items", model)
    result = ingestion._ingest_expanded(
        [SourceDocument("notice.html", b"notice"), SourceDocument("quote.txt", b"quote")],
        [], extraction_mode=mode, model_settings=_settings(),
    )
    assert len(result.items) == 1
    row = result.items[0]
    assert (row.product_name, row.package_code, row.quantity, row.unit_price) == ("打印机", "1", 2, 100)
    provenance = json.loads(row.source_evidence)
    assert {origin["source_file"] for origin in provenance["sources"]} == {"notice.html", "quote.txt"}
    assert {origin["package_code"] for origin in provenance["sources"]} == {"1", "default"}


@pytest.mark.parametrize("mode", ["rules", "hybrid", "model"])
def test_mixed_history_cannot_supply_metadata_or_reconciliation_context(monkeypatch, mode):
    mixed_file, quote_file = "combined.txt", "quote-other.txt"
    evidence = "打印机 X1 2台 单价100"
    current = _item(mixed_file, package_code="1", model="X1", quantity=2,
                    unit_price=100, source_evidence=evidence)
    history = _item(mixed_file, "历史服务", source_evidence="历史服务 历史客户 99元")
    participant = ParticipantCandidate(
        source_file=mixed_file, source_location="row:2", organization_name="历史客户",
        outcome="winner", source_evidence="历史服务 历史客户 99元",
    )
    contents = {
        "notice.html": ("项目名称：当前项目", [], []),
        mixed_file: (
            (f"案例一览表\n项目预算：999万元\n包号：1\n见 {quote_file}\n"
             f"历史服务 历史客户 99元\n采购清单\n{evidence}"),
            [history, current], [participant],
        ),
        quote_file: (evidence, [_item(quote_file, model="X1", quantity=2,
                                    unit_price=100, source_evidence=evidence)], []),
    }

    def parse(document, **kwargs):
        text, items, participants = contents[document.filename]
        return text, list(items), list(participants), []

    def model(**kwargs):
        _, items, participants = contents[kwargs["filename"]]
        metadata = (NoticeMetadata(project_budget=9990000) if kwargs["filename"] == mixed_file
                    else NoticeMetadata(project_name="当前项目"))
        return metadata, list(items), list(participants), []

    monkeypatch.setattr(ingestion, "parse_document_with_participants", parse)
    monkeypatch.setattr(ingestion, "extract_unstructured_items", model)
    result = ingestion._ingest_expanded(
        [SourceDocument(filename, filename.encode()) for filename in contents],
        [], extraction_mode=mode, model_settings=_settings(),
    )
    assert len(result.items) == 2
    assert {row.package_code for row in result.items} == {"1", "default"}
    assert all(row.product_name == "打印机" for row in result.items)
    assert not result.participants
    assert result.metadata.project_budget is None
    assert any("用途混合" in warning for warning in result.warnings)
