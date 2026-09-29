from app.ingestion import _deduplicate
from app.schemas import ItemCandidate


def item(source="notice.html", **changes):
    values = {
        "package_code": "包1",
        "product_name": "打印机",
        "model": "X1",
        "quantity": 1,
        "unit_price": 100,
        "source_file": source,
        "source_location": "table:1/row:1",
        "source_evidence": "包1 | 打印机 | X1 | 1 | 100",
        "extraction_method": "table_header_mapping",
    }
    values.update(changes)
    return ItemCandidate(**values)


def test_same_source_parser_replay_at_same_row_with_identical_evidence_is_collapsed():
    warnings = []
    rows = _deduplicate(
        [item(source_location="table:1/row:1"), item(source_location="table:1/row:1")],
        warnings,
    )

    assert len(rows) == 1
    assert rows[0].extraction_method == "same_source_replay_verified"
    assert any("同源重复候选合并" in warning for warning in warnings)


def test_identical_same_source_rows_at_different_locations_remain_separate():
    rows = _deduplicate([
        item(source_location="table:1/row:1"),
        item(source_location="table:1/row:2"),
    ])

    assert len(rows) == 2


def test_conflicting_candidates_for_same_source_row_are_preserved_and_warned():
    warnings = []
    rows = _deduplicate([
        item(source_location="table:1/row:1"),
        item(source_location="table:1/row:1", quantity=2),
    ], warnings)

    assert len(rows) == 2
    assert any("同一来源行候选字段冲突" in warning for warning in warnings)


def test_same_source_model_rows_with_identical_evidence_are_not_collapsed():
    rows = [item(
        source_location="model_text_evidence",
        source_evidence="打印机 X1 数量1 单价100",
        extraction_method="qwen_deepseek_structured",
    ) for _ in range(2)]

    assert len(_deduplicate(rows)) == 2


def test_same_source_model_rows_with_distinct_evidence_preserve_order():
    rows = [item(
        source_location="model_text_evidence",
        source_evidence=f"打印机 X1 数量1 单价100 第{index}行",
        extraction_method="qwen_deepseek_structured",
    ) for index in (1, 2)]

    assert _deduplicate(rows) == rows
