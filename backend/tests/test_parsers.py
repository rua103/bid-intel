from app.parsers import SourceDocument, expand_uploads, parse_document, parse_item_tables


def test_parse_challenge_style_item_table():
    rows = [
        [
            "序号",
            "品目名称",
            "采购标的",
            "品牌",
            "规格型号",
            "数量（单位）",
            "单价（元）",
            "总价（元）",
        ],
        [
            "1-2",
            "信息化设备",
            "自助综合机（多功能自助终端及系统配套设备）",
            "长城医疗",
            "MBS200K-3",
            "1.000(批)",
            "1,336,000.0000",
            "1,336,000.0000",
        ],
    ]
    result = parse_item_tables(rows, source_file="notice.html", table_index=1)
    assert len(result) == 1
    item = result[0]
    assert item.product_name.startswith("自助综合机")
    assert item.category == "信息化设备"
    assert item.brand == "长城医疗"
    assert item.model == "MBS200K-3"
    assert item.quantity == 1
    assert item.quantity_unit == "(批)"
    assert item.unit_price == 1336000
    assert item.total_price == 1336000
    assert "MBS200K-3" in item.source_evidence


def test_html_table_parser_preserves_source_row():
    html = """<html><body><table>
      <tr><th>品目名称</th><th>采购标的</th><th>品牌</th><th>规格型号</th><th>数量</th><th>单价(元)</th><th>总价(元)</th></tr>
      <tr><td>办公设备</td><td>打印机</td><td>Canon</td><td>GX7000</td><td>2(台)</td><td>4000</td><td>8000</td></tr>
    </table></body></html>""".encode()
    text, items, warnings = parse_document(SourceDocument("公告.html", html))
    assert "打印机" in text
    assert not warnings
    assert len(items) == 1
    assert items[0].source_location == "table:1/row:2"


def test_zip_expansion_stays_in_memory_and_skips_invalid_archive():
    import io
    import zipfile

    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("../unsafe.html", "<p>ok</p>")
    expanded, warnings = expand_uploads([SourceDocument("sample.zip", buffer.getvalue())])
    assert len(expanded) == 1
    assert "../unsafe.html" in expanded[0].filename
    assert not warnings


def test_metadata_keeps_budget_and_award_amount_separate():
    from app.parsers import extract_metadata

    metadata = extract_metadata(
        "项目名称：医院信息化采购项目 项目编号：ABC-2026 采购单位：某市中心医院 "
        "预算金额：2,000,000元 总中标/成交金额：1,500,000元"
    )
    assert metadata.project_name == "医院信息化采购项目"
    assert metadata.project_number == "ABC-2026"
    assert metadata.procurement_unit == "某市中心医院"
    assert metadata.project_budget == 2_000_000
    assert metadata.announced_total_award == 1_500_000


def test_repository_demo_notice_is_a_parseable_smoke_fixture():
    from pathlib import Path

    from app.parsers import extract_metadata

    fixture = Path(__file__).resolve().parents[2] / "examples" / "demo_notice.html"
    text, items, warnings = parse_document(SourceDocument(fixture.name, fixture.read_bytes()))
    metadata = extract_metadata(text)
    assert not warnings
    assert metadata.project_name == "办公设备采购演示项目"
    assert metadata.procurement_unit == "示例市公共服务中心"
    assert metadata.project_budget == 20_000
    assert metadata.announced_total_award == 18_000
    assert len(items) == 1
    assert items[0].product_name == "激光打印机"
