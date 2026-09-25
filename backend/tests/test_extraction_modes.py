import json
import sys

import pytest

from app import experiments
from app.config import Settings
from app.ingestion import _deduplicate_participants, _merge_model_items, extract_notice
from app.parsers import SourceDocument
from app.schemas import ImportResult, ItemCandidate, NoticeMetadata, ParticipantCandidate
from app.storage import connect, save_import


def test_rules_never_calls_configured_model(monkeypatch):
    def forbidden(**kwargs):
        raise AssertionError("rules must not call model")
    monkeypatch.setattr("app.ingestion.extract_unstructured_items", forbidden)
    result = extract_notice([SourceDocument("a.txt", b"hello")], extraction_mode="rules",
                            model_settings=Settings(model_base_url="https://test.invalid",
                                                    model_api_key="test", model_name="deepseek-test"))
    assert result.items == []


def test_hybrid_merges_model_fields_into_same_rule_item_without_package_code():
    rule_item = ItemCandidate(
        package_code="default",
        product_name="便携式电动起立床",
        category="A02320800",
        source_file="notice.html",
        source_location="table:1/row:2",
    )
    model_item = ItemCandidate(
        package_code="合同包1",
        product_name="便携式 电动起立床",
        category="物理治疗、康复及体育治疗仪器设备",
        brand="迈步",
        model="MB-100",
        source_file="notice.html",
        source_location="model",
        source_evidence="便携式电动起立床，品牌迈步，型号 MB-100",
        extraction_method="qwen_deepseek_structured",
    )
    warnings: list[str] = []

    merged = _merge_model_items([rule_item], [model_item], warnings)

    assert len(merged) == 1
    assert merged[0].package_code == "合同包1"
    assert merged[0].category == "A02320800"  # Keep the table's source value on conflict.
    assert merged[0].brand == "迈步"
    assert merged[0].model == "MB-100"
    assert merged[0].extraction_method == "hybrid_source_verified"
    assert warnings == ["表格与模型字段不一致，保留表格值待核验：便携式 电动起立床 / category"]


def test_hybrid_does_not_merge_ambiguous_same_item_across_packages():
    rule_items = [
        ItemCandidate(package_code=package, product_name="电脑", source_file="notice.html",
                      source_location=f"table:1/row:{index}")
        for index, package in enumerate(("包1", "包2"), start=2)
    ]
    model_item = ItemCandidate(
        package_code="default", product_name="电脑", brand="品牌A", source_file="notice.html",
        source_location="model",
    )
    warnings: list[str] = []

    merged = _merge_model_items(rule_items, [model_item], warnings)

    assert merged == [*rule_items, model_item]
    assert warnings == ["模型行无法唯一对齐表格，请核验：电脑"]


@pytest.mark.parametrize("reverse", [False, True])
def test_hybrid_does_not_consume_one_rule_row_for_two_packages(reverse):
    def item(code, total):
        return ItemCandidate(package_code=code, product_name="打印机", total_price=total,
                             source_file="a.html", source_location="row")

    modeled = [item("合同包1", 100), item("合同包2", 200)]
    if reverse:
        modeled.reverse()
    merged = _merge_model_items([item("default", 100)], modeled, [])
    assert {(row.package_code, row.total_price) for row in merged} == {
        ("合同包1", 100), ("合同包2", 200),
    }
    assert len(merged) == 2


def test_hybrid_keeps_ambiguous_model_packages_and_warns():
    def item(code):
        return ItemCandidate(package_code=code, product_name="打印机",
                             source_file="a.html", source_location="row")

    warnings = []
    merged = _merge_model_items([item("default")], [item("包1"), item("包2")], warnings)
    assert [row.package_code for row in merged] == ["default", "包1", "包2"]
    assert warnings


def test_hybrid_prioritizes_known_packages_and_never_merges_model_rows_together():
    def item(code, **extra):
        return ItemCandidate(package_code=code, product_name="打印机",
                             source_file="a.html", source_location="row", **extra)

    modeled = [item("包2", brand="乙"), item("包1", brand="甲")]
    merged = _merge_model_items([item("default"), item("包1")], modeled, [])
    assert {(row.package_code, row.brand) for row in merged} == {("包1", "甲"), ("包2", "乙")}
    assert _merge_model_items([], modeled, []) == modeled


def test_hybrid_rule_item_and_model_winner_use_same_package_in_storage(tmp_path, monkeypatch):
    html = '''<table><tr><td>采购单位</td><td>某医院</td></tr></table>
    <table><tr><th>名称</th><th>数量</th><th>单价</th></tr>
    <tr><td>打印机</td><td>1</td><td>100</td></tr></table>'''.encode()
    item = ItemCandidate(package_code="合同包1", product_name="打印机", quantity=1,
                         unit_price=100, source_file="a.html", source_location="model")
    person = ParticipantCandidate(package_code="合同包1", organization_name="某供应商",
                                  outcome="winner", award_amount=100,
                                  source_file="a.html", source_location="model")
    monkeypatch.setattr("app.ingestion.extract_unstructured_items",
                        lambda **kwargs: (NoticeMetadata(), [item], [person], []))
    result = extract_notice([SourceDocument("a.html", html)], extraction_mode="hybrid",
                            model_settings=Settings(model_base_url="https://test.invalid",
                                                    model_api_key="stub", model_name="stub"))
    db = tmp_path / "aligned.db"
    save_import(db, result)
    with connect(db) as conn:
        assert conn.execute("SELECT count(*) FROM packages").fetchone()[0] == 1
        assert conn.execute("""SELECT count(*) FROM procurement_items i
                            JOIN awards a ON i.package_id = a.package_id""").fetchone()[0] == 1


def test_multiple_packages_and_consortium_do_not_duplicate_amounts(tmp_path):
    participants = [ParticipantCandidate(
        package_code=code, organization_name="甲乙联合体", consortium_members=["甲公司", "乙公司"],
        outcome="winner", award_amount=amount, source_file="a", source_location=code,
    ) for code, amount in [("包1", 100), ("包2", 200)]]
    assert len(_deduplicate_participants(participants, [])) == 2
    items = [ItemCandidate(package_code=code, product_name="电脑", source_file="a", source_location=code)
             for code in ["包1", "包2"]]
    db = tmp_path / "packages.db"
    save_import(db, ImportResult(notice_id=0, source_files=["a"], items_found=2, items=items,
                                 participants=participants,
                                 metadata=NoticeMetadata(announced_total_award=300)))
    with connect(db) as conn:
        assert conn.execute("SELECT count(*) FROM packages").fetchone()[0] == 2
        assert conn.execute("SELECT count(*) FROM awards").fetchone()[0] == 2
        assert conn.execute("SELECT sum(cast(award_amount as real)) FROM awards").fetchone()[0] == 300
        assert conn.execute("SELECT count(*) FROM organizations").fetchone()[0] == 1
        assert conn.execute("SELECT count(*) FROM packages WHERE package_award_total IS NULL").fetchone()[0] == 2


def test_report_refuses_to_overwrite_before_spending_model_calls(tmp_path, monkeypatch, capsys):
    """The guard must run first: it used to sit after the comparison, so a re-run paid
    for every model call and only then threw FileExistsError, discarding all results."""
    output = tmp_path / "report.json"
    output.write_text("previous run\n", encoding="utf-8")

    def forbidden(*args, **kwargs):
        raise AssertionError("must refuse before any model call")
    monkeypatch.setattr(experiments, "compare_extraction_modes", forbidden)
    # A manifest that does not exist: reaching the loader at all would raise
    # FileNotFoundError instead of the clean SystemExit the guard owes us.
    monkeypatch.setattr(
        sys, "argv",
        ["experiments", str(tmp_path / "missing.json"), "--output", str(output)],
    )

    with pytest.raises(SystemExit) as exit_info:
        experiments.main()

    assert exit_info.value.code == 2
    assert "已存在" in capsys.readouterr().err
    assert output.read_text(encoding="utf-8") == "previous run\n"


def test_force_swaps_in_the_report_only_once_it_is_complete(tmp_path, monkeypatch):
    output = tmp_path / "report.json"
    output.write_text("previous run\n", encoding="utf-8")
    during: dict[str, str] = {}

    def fake_compare(notices, **kwargs):
        # The previous report stays readable until the replacement is fully written.
        during["text"] = output.read_text(encoding="utf-8")
        return {"mode": "stub"}

    monkeypatch.setattr(experiments, "load_manifest_notices", lambda path, limit: [])
    monkeypatch.setattr(experiments, "compare_extraction_modes", fake_compare)
    monkeypatch.setattr(
        sys, "argv",
        ["experiments", "manifest.json", "--output", str(output), "--force"],
    )
    experiments.main()

    assert during["text"] == "previous run\n"
    assert json.loads(output.read_text(encoding="utf-8")) == {"mode": "stub"}
    assert [entry.name for entry in tmp_path.iterdir()] == ["report.json"]

