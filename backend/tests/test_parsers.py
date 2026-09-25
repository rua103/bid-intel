from decimal import Decimal

import pytest

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


def test_ccgp_standard_headers_map_amount_and_item_name_without_treating_item_number_as_category():
    rows = [
        ["序号", "品目号", "标项名称", "标的名称", "品牌（如有）", "规格型号", "数量", "单价(元)", "金额(元)"],
        ["1", "A02320800", "康复设备", "便携式电动起立床", "迈步", "MB-100", "2台", "1000", "2000"],
    ]
    items = parse_item_tables(rows, source_file="ccgp.html", table_index=1)

    assert len(items) == 1
    assert items[0].product_name == "便携式电动起立床"
    # 品目号 is a code, not a human-readable category name.
    assert items[0].category is None
    assert items[0].brand == "迈步"
    assert items[0].model == "MB-100"
    assert items[0].unit_price == 1000
    assert items[0].total_price == 2000


def test_rule_parser_skips_merged_heading_and_non_item_rows():
    from app.parsers import parse_item_tables

    rows = [
        ["序号 名称 品牌（如有） 规格型号 数量 单价"],
        ["三、中标情况 / 联系人：郑宁飞 / 收费标准：按规定收取"],
    ]
    assert parse_item_tables(rows, source_file="notice.html", table_index=1) == []

    rows = [
        ["序号", "标的名称", "品牌", "规格型号", "数量", "单价", "金额"],
        ["", "三、中标情况", "", "", "", "", ""],
        ["", "联系人：郑宁飞", "", "联系方式：0371-12345678", "", "", ""],
        ["", "收费标准：采购代理机构按照规定收费", "", "", "", "100", ""],
        ["1", "高温炉", "仪器品牌", "GF-1200", "1台", "12000", "12000"],
    ]
    items = parse_item_tables(rows, source_file="notice.html", table_index=1)
    assert [item.product_name for item in items] == ["高温炉"]


def test_ccgp_two_column_header_metadata_is_extracted_from_html():
    from app.parsers import extract_metadata

    html = """<html><body><table>
      <tr><td>采购项目名称</td><td>昌江县紧密县域医疗卫生强基工程设备采购项目</td></tr>
      <tr><td>采购单位</td><td>昌江黎族自治县医疗集团</td></tr>
      <tr><td>采购预算金额</td><td>￥1,500.25 万元（人民币）</td></tr>
      <tr><td>总中标金额</td><td>￥1472.608500 万元（人民币）</td></tr>
    </table></body></html>""".encode()

    text, _, warnings = parse_document(SourceDocument("ccgp.html", html))
    metadata = extract_metadata(text)

    assert not warnings
    assert metadata.project_name == "昌江县紧密县域医疗卫生强基工程设备采购项目"
    assert metadata.procurement_unit == "昌江黎族自治县医疗集团"
    assert metadata.project_budget == 15_002_500
    assert metadata.announced_total_award == 14_726_085


def test_repeated_headers_are_skipped_even_after_column_order_changes():
    rows = [
        ["名称", "品牌", "数量", "单价"],
        ["打印机", "甲牌", "1", "100"],
        ["名称", "品牌", "数量", "单价"],
        ["数量", "单价(元)", "品牌（如有）", "标的名称"],
        ["2", "150", "乙牌", "扫描仪"],
    ]
    items = parse_item_tables(rows, source_file="a.html", table_index=1)
    assert [(x.product_name, x.quantity, x.unit_price) for x in items] == [
        ("打印机", 1, 100), ("扫描仪", 2, 150),
    ]


def test_nested_layout_tables_do_not_duplicate_items_or_absorb_contact_blocks():
    html = '''<table><tr><td>三、中标情况</td></tr><tr><td><table>
    <tr><td>名称</td><td>品牌</td><td>数量</td><td>单价</td></tr>
    <tr><td>3.0T 磁共振设备</td><td>甲</td><td>1</td><td>100</td></tr>
    <tr><td>见附件</td><td>见附件</td><td>见附件</td><td>见附件元</td></tr>
    </table></td></tr><tr><td>联系人：某人</td></tr></table>'''.encode()
    _, items, _ = parse_document(SourceDocument("a.html", html))
    assert [x.product_name for x in items] == ["3.0T 磁共振设备"]


def test_package_award_summary_does_not_become_an_item_price():
    rows = [["包号", "采购内容", "供应商名称", "中标金额", "单位"],
            ["包1", "实验室设备采购项目", "某公司", "100000", "元"]]
    assert parse_item_tables(rows, source_file="a.html", table_index=1) == []


@pytest.mark.parametrize("header,value,expected", [
    ("金额(元)", "2,000.00", Decimal(2000)),
    ("金额（万元）", "2.5", Decimal(25000)),
    ("金额(元)", "0", Decimal(0)),
    ("金额(元)", "投标总价折扣率：99.8（%）", None),
    ("金额(元)", "见附件", None),
])
def test_standard_amount_headers_preserve_units_without_guessing(header, value, expected):
    items = parse_item_tables([["采购标的", "数量", header], ["电脑", "1", value]],
                              source_file="a.html", table_index=1)
    assert len(items) == 1
    assert items[0].total_price == expected


def test_ccgp_metadata_stays_within_cells_and_wins_over_attachment_text():
    from app.config import Settings
    from app.ingestion import extract_notice
    from app.parsers import extract_metadata

    html = '''<table><tr><td colspan="4">公告信息：</td></tr>
    <tr><td><p>采购项目名称</p></td><td colspan="3"><p>设备采购项目</p></td></tr>
    <tr><td>品目</td><td>医疗设备</td></tr>
    <tr><td>采购单位</td><td>某医院</td></tr>
    <tr><td>行政区域</td><td>某市</td><td>公告时间</td><td>2026年9月25日</td></tr>
    <tr><td>评审专家名单</td><td>甲、乙、丙</td></tr>
    <tr><td>总中标金额</td><td>￥10.25 万元（人民币）</td></tr>
    <tr><td>联系人及联系方式：</td></tr></table>
    <p>1、采购项目编号：ABC-2026</p><p>2、采购方式：公开招标</p>'''.encode()
    document = SourceDocument("a.html", html)
    text, _, _ = parse_document(document)
    metadata = extract_metadata(text)
    assert metadata.project_name == "设备采购项目"
    assert metadata.procurement_unit == "某医院"
    assert metadata.project_number == "ABC-2026"
    assert metadata.announced_total_award == 102500
    attachment = SourceDocument("a.txt", "项目名称：附件示例项目".encode())
    result = extract_notice([document, attachment], extraction_mode="rules",
                            model_settings=Settings())
    assert result.metadata == metadata
    result = extract_notice([attachment, document], extraction_mode="rules",
                            model_settings=Settings())
    assert result.metadata == metadata


def test_project_number_does_not_include_next_section_or_title_suffix():
    from app.parsers import extract_metadata

    assert extract_metadata(
        "一、项目编号：N5103012026000302 二、项目名称：设备采购"
    ).project_number == "N5103012026000302"
    assert extract_metadata(
        "某项目（项目编号：ABC-2026）电子化公开招标中标结果公告"
    ).project_number == "ABC-2026"


def test_empty_metadata_cells_do_not_steal_next_row_and_table_headers_are_not_values():
    from app.parsers import extract_metadata

    html = '''<table><tr><td>采购单位</td><td></td></tr>
    <tr><td>行政区域</td><td>某市</td></tr>
    <tr><td>项目名称</td><td>中标金额</td><td>采购单位</td></tr></table>'''.encode()
    text, _, _ = parse_document(SourceDocument("a.html", html))
    assert extract_metadata(text).model_dump() == {
        "project_name": None, "project_number": None, "procurement_unit": None,
        "project_budget": None, "announced_total_award": None,
    }


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
