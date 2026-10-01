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
    assert merged[0].package_code == "1"
    assert merged[0].category == "物理治疗、康复及体育治疗仪器设备"  # Model owns semantic labels.
    assert merged[0].brand == "迈步"
    assert merged[0].model == "MB-100"
    assert merged[0].extraction_method == "hybrid_source_verified"
    assert warnings == [
        "表格与模型字段不一致，模型语义/规则数值优先待核验：便携式 电动起立床 / category"
    ]


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

    assert merged == rule_items
    assert warnings == ["模型行无法唯一对齐表格，保留规则行待核验：电脑"]


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
        ("1", 100), ("2", 200),
    }
    assert len(merged) == 2


def test_hybrid_keeps_ambiguous_model_packages_and_warns():
    def item(code):
        return ItemCandidate(package_code=code, product_name="打印机",
                             source_file="a.html", source_location="row")

    warnings = []
    merged = _merge_model_items([item("default")], [item("包1"), item("包2")], warnings)
    assert [row.package_code for row in merged] == ["default"]
    assert len(warnings) == 2


def test_hybrid_prioritizes_known_packages_and_never_merges_model_rows_together():
    def item(code, **extra):
        return ItemCandidate(package_code=code, product_name="打印机",
                             source_file="a.html", source_location="row", **extra)

    modeled = [item("包2", brand="乙"), item("包1", brand="甲")]
    merged = _merge_model_items([item("default"), item("包1")], modeled, [])
    assert {(row.package_code, row.brand) for row in merged} == {("1", "甲"), ("2", "乙")}
    assert _merge_model_items([], modeled, []) == modeled


def test_hybrid_same_name_rows_match_by_package_one_to_one():
    def item(code, brand):
        return ItemCandidate(package_code=code, product_name="打印机", brand=brand,
                             source_file="notice.html", source_location=code)

    rules = [item("包1", "规则甲"), item("包2", "规则乙")]
    modeled = [item("包2", "模型乙"), item("包1", "模型甲")]
    merged = _merge_model_items(rules, modeled, [])

    assert [(row.package_code, row.brand) for row in merged] == [("1", "模型甲"), ("2", "模型乙")]


def test_hybrid_model_duplicate_is_collapsed_but_new_model_item_is_kept():
    rule = ItemCandidate(package_code="包1", product_name="打印机", source_file="notice.html",
                         source_location="table:1", source_evidence="表格证据")
    duplicate_a = ItemCandidate(package_code="包1", product_name="打印机", brand="甲",
                                source_file="notice.html", source_location="model:1",
                                source_evidence="模型证据一")
    duplicate_b = duplicate_a.model_copy(update={"source_location": "model:2",
                                                   "source_evidence": "模型证据一"})
    extra = ItemCandidate(package_code="包1", product_name="扫描仪", brand="乙",
                          source_file="notice.html", source_location="model:3")
    warnings = []
    merged = _merge_model_items([rule], [duplicate_a, duplicate_b, extra], warnings)

    assert len(merged) == 2
    assert [(row.product_name, row.brand) for row in merged] == [("打印机", "甲"), ("扫描仪", "乙")]
    assert "模型证据一" in (merged[0].source_evidence or "")
    assert (merged[0].source_evidence or "").count("模型证据一") == 1
    assert any("模型重复候选合并" in warning for warning in warnings)


def test_hybrid_collapses_exact_model_duplicates_without_evidence():
    first = ItemCandidate(package_code="包1", product_name="打印机",
                          source_file="notice.html", source_location="model:1")
    second = first.model_copy(update={"source_location": "model:2"})
    warnings = []

    merged = _merge_model_items([], [first, second], warnings)

    assert merged == [first]
    assert any("模型重复候选合并" in warning for warning in warnings)


def test_hybrid_model_extra_candidate_is_kept_with_warning():
    modeled = ItemCandidate(package_code="包1", product_name="扫描仪", brand="乙",
                            source_file="notice.html", source_location="model:1",
                            source_evidence="扫描仪报价")
    warnings = []

    merged = _merge_model_items([], [modeled], warnings)

    assert merged == [modeled]
    assert any("模型额外候选保留" in warning for warning in warnings)


def test_hybrid_keeps_generic_and_explicit_model_package_without_full_identity():
    generic = ItemCandidate(package_code="default", product_name="打印机", brand="甲",
                            source_file="notice.html", source_location="model:1",
                            source_evidence="打印机 包1 报价")
    explicit = generic.model_copy(update={"package_code": "合同包1", "source_location": "model:2"})

    warnings = []
    merged = _merge_model_items([], [generic, explicit], warnings)

    assert len(merged) == 2
    assert [row.package_code for row in merged] == ["default", "1"]
    assert not any("字段冲突" in warning for warning in warnings)


def test_hybrid_package_promotion_does_not_merge_without_full_identity():
    generic = ItemCandidate(package_code="default", product_name="打印机", brand="甲",
                            source_file="notice.html", source_location="model:1",
                            source_evidence="正文中的打印机")
    explicit = generic.model_copy(update={"package_code": "合同包1",
                                          "source_location": "model:2",
                                          "source_evidence": "包1报价中的打印机"})

    merged = _merge_model_items([], [generic, explicit], [])

    assert len(merged) == 2
    assert [row.package_code for row in merged] == ["default", "1"]


def test_hybrid_model_rows_with_conflicting_semantics_remain_separate():
    first = ItemCandidate(package_code="包1", product_name="打印机", brand="甲",
                          source_file="notice.html", source_location="model:1",
                          source_evidence="同一段报价证据")
    second = first.model_copy(update={"brand": "乙", "source_location": "model:2"})
    warnings = []

    merged = _merge_model_items([], [first, second], warnings)

    assert len(merged) == 2
    assert [row.brand for row in merged] == ["甲", "乙"]
    assert all("同一段报价证据" in (row.source_evidence or "") for row in merged)
    assert any("字段冲突" in warning and "brand" in warning for warning in warnings)


def test_hybrid_fills_missing_rule_product_name_from_unique_model_row():
    rule = ItemCandidate(package_code="包1", product_name=None, quantity=2,
                         unit_price=100, source_file="notice.html", source_location="table:1")
    modeled = ItemCandidate(package_code="包1", product_name="打印机", quantity=2,
                            brand="甲", source_file="notice.html", source_location="model",
                            source_evidence="打印机 数量 2 单价 100")

    merged = _merge_model_items([rule], [modeled], [])

    assert len(merged) == 1
    assert merged[0].product_name == "打印机"
    assert merged[0].brand == "甲"


def test_hybrid_normalizes_package_from_unvalidated_model_copy_update():
    rule = ItemCandidate(package_code="default", product_name="打印机", quantity=1,
                         source_file="notice.html", source_location="table:1")
    modeled = ItemCandidate(package_code="default", product_name="打印机", quantity=1,
                            source_file="notice.html", source_location="model")
    modeled = modeled.model_copy(update={"package_code": "合同包二"})

    merged = _merge_model_items([rule], [modeled], [])

    assert len(merged) == 1
    assert merged[0].package_code == "2"


def test_hybrid_keeps_named_package_default_without_label_evidence():
    modeled = ItemCandidate(
        package_code="教学仪器", product_name="精密注塑成型机",
        source_file="notice.html", source_location="model",
        source_evidence="精密注塑成型机报价",
    )
    warnings: list[str] = []

    merged = _merge_model_items([], [modeled], warnings)

    assert [row.package_code for row in merged] == ["default"]
    assert any("名称型包号缺少标签证据" in warning for warning in warnings)


def test_hybrid_accepts_named_package_with_label_evidence():
    modeled = ItemCandidate(
        package_code="教学仪器", product_name="精密注塑成型机",
        source_file="notice.html", source_location="model",
        source_evidence="精密注塑成型机报价\n分包名称：教学仪器",
    )

    merged = _merge_model_items([], [modeled], [])

    assert [row.package_code for row in merged] == ["教学仪器"]


def test_hybrid_keeps_same_name_same_package_rows_with_distinct_evidence():
    def item(total, evidence, location):
        return ItemCandidate(package_code="包1", product_name="打印机", quantity=1,
                             total_price=total, unit_price=total, source_file="notice.html",
                             source_location=location, source_evidence=evidence)

    rules = [item(100, "表格第一行", "table:1/row:1"),
             item(200, "表格第二行", "table:1/row:2")]
    modeled = [item(200, "模型第二行", "model_text_evidence"),
               item(100, "模型第一行", "model_text_evidence")]
    merged = _merge_model_items(rules, modeled, [])

    assert len(merged) == 2
    assert [(row.total_price, row.source_evidence) for row in merged] == [
        (100, "表格第一行\n模型第一行"),
        (200, "表格第二行\n模型第二行"),
    ]


def test_hybrid_model_semantics_win_but_rule_numbers_are_validation_source():
    rule = ItemCandidate(package_code="包1", product_name="打印机", category="规则品目",
                         brand="规则品牌", model="规则型号", quantity=2, unit_price=100,
                         total_price=200, source_file="notice.html", source_location="table:1",
                         source_evidence="表格证据")
    modeled = ItemCandidate(package_code="包1", product_name="打印机", category="模型品目",
                             brand="模型品牌", model="模型型号", quantity=3, unit_price=110,
                             total_price=330, source_file="notice.html", source_location="model:1",
                             source_evidence="模型证据")
    warnings = []
    merged = _merge_model_items([rule], [modeled], warnings)

    assert len(merged) == 1
    row = merged[0]
    assert (row.category, row.brand, row.model) == ("模型品目", "模型品牌", "模型型号")
    assert (row.quantity, row.unit_price, row.total_price) == (2, 100, 200)
    assert "表格证据" in (row.source_evidence or "")
    assert "模型证据" in (row.source_evidence or "")
    assert any("quantity" in warning and "total_price" in warning for warning in warnings)


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

