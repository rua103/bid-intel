from decimal import Decimal

import pytest

from app.parsers import (
    SourceDocument,
    expand_uploads,
    parse_document,
    parse_document_with_participants,
    parse_item_tables,
    parse_participant_tables,
)


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


def test_review_table_extracts_all_explicit_bidders_without_infering_from_rank():
    rows = [
        ["合同包1"],
        ["供应商", "资格性审查", "符合性审查", "综合得分", "推荐排名"],
        ["甲科技有限公司", "通过", "通过", "96.2", "1"],
        ["供应商", "资格性审查", "符合性审查", "综合得分", "推荐排名"],
        ["乙设备有限公司", "通过", "通过", "88.3", "2"],
    ]
    participants = parse_participant_tables(rows, source_file="notice.html", table_index=3)

    assert [(row.organization_name, row.package_code, row.outcome) for row in participants] == [
        ("甲科技有限公司", "包1", "unknown"),
        ("乙设备有限公司", "包1", "unknown"),
    ]
    assert participants[0].source_location == "table:3/row:3"
    assert participants[0].source_evidence == "合同包1 | 甲科技有限公司 | 通过 | 通过 | 96.2 | 1"


def test_explicit_unsuccessful_reason_labels_only_listed_bidders_as_nonwinners():
    rows = [
        ["序号", "供应商", "未中标（成交）原因"],
        ["1", "乙设备有限公司", "综合评审得分较低"],
        ["2", "丙服务有限公司", "响应报价超预算"],
    ]
    participants = parse_participant_tables(rows, source_file="notice.html", table_index=4)

    assert [(row.organization_name, row.outcome) for row in participants] == [
        ("乙设备有限公司", "nonwinner"),
        ("丙服务有限公司", "nonwinner"),
    ]


def test_ragged_unsuccessful_rows_keep_package_and_do_not_treat_reason_as_vendor():
    rows = [
        ["标包", "投标人名称", "未中标原因"],
        ["包一：设备采购", "甲设备有限公司", "综合得分较低"],
        ["乙设备有限公司", "报价较低"],
    ]

    participants = parse_participant_tables(rows, source_file="notice.html", table_index=4)

    assert [(row.organization_name, row.package_code, row.outcome) for row in participants] == [
        ("甲设备有限公司", "包1", "nonwinner"),
        ("乙设备有限公司", "包1", "nonwinner"),
    ]


def test_failed_qualification_is_nonwinner_but_pass_and_rank_are_not_outcomes():
    rows = [
        ["供应商", "资格审查结果", "符合性审查结果", "评审排名"],
        ["甲设备有限公司", "通过", "通过", "1"],
        ["乙设备有限公司", "不通过", "未审查", "2"],
    ]

    participants = parse_participant_tables(rows, source_file="notice.html", table_index=2)

    assert [(row.organization_name, row.outcome) for row in participants] == [
        ("甲设备有限公司", "unknown"),
        ("乙设备有限公司", "nonwinner"),
    ]


def test_key_value_winner_name_without_amount_is_explicit_winner():
    rows = [["中标供应商", "甲设备有限公司", "企业类型", "小微企业"]]

    participants = parse_participant_tables(rows, source_file="notice.html", table_index=2)

    assert len(participants) == 1
    assert participants[0].organization_name == "甲设备有限公司"
    assert participants[0].outcome == "winner"
    assert participants[0].award_amount is None


def test_explicit_award_amount_identifies_winner_and_converts_units():
    rows = [
        ["供应商名称", "中标（成交）金额（万元）"],
        ["甲科技有限公司", "12.5"],
    ]
    participants = parse_participant_tables(rows, source_file="notice.html", table_index=2)

    assert len(participants) == 1
    assert participants[0].organization_name == "甲科技有限公司"
    assert participants[0].outcome == "winner"
    assert participants[0].award_amount == 125_000


def test_generic_supplier_table_is_not_treated_as_bidder_evidence():
    rows = [
        ["供应商名称", "供应商地址", "联系人"],
        ["甲科技有限公司", "某市某路", "张某"],
    ]
    assert parse_participant_tables(rows, source_file="notice.html", table_index=1) == []


def test_html_rules_import_includes_review_bidders_and_preserves_unknown_outcomes():
    html = """<html><body>
      <p>一、采购项目</p>
      <table><tr><th>供应商名称</th><th>供应商地址</th></tr>
        <tr><td>采购代理有限公司</td><td>某市</td></tr></table>
      <table><tr><td>合同包1(办公设备):</td></tr>
        <tr><th>供应商</th><th>资格性审查</th><th>符合性审查</th><th>综合得分</th><th>推荐排名</th></tr>
        <tr><td>甲科技有限公司</td><td>通过</td><td>通过</td><td>96.2</td><td>1</td></tr>
        <tr><td>乙设备有限公司</td><td>通过</td><td>通过</td><td>88.3</td><td>2</td></tr>
      </table>
    </body></html>""".encode()
    from app.config import Settings
    from app.ingestion import extract_notice

    result = extract_notice(
        [SourceDocument("notice.html", html)], extraction_mode="rules",
        model_settings=Settings(),
    )

    assert [(row.organization_name, row.package_code, row.outcome) for row in result.participants] == [
        ("甲科技有限公司", "包1", "unknown"),
        ("乙设备有限公司", "包1", "unknown"),
    ]


def test_html_nested_winner_detail_inherits_package_from_outer_package_column():
    html = """<html><body><table>
      <tr><th>包号</th><th>供货明细</th></tr>
      <tr><td>1</td><td><table><tr>
        <td>中标供应商</td><td>甲设备有限公司</td><td>成交金额</td><td>100元</td>
      </tr></table></td></tr>
    </table></body></html>""".encode()

    _, _, participants, _ = parse_document_with_participants(SourceDocument("notice.html", html))

    assert len(participants) == 1
    assert participants[0].organization_name == "甲设备有限公司"
    assert participants[0].package_code == "包1"
    assert participants[0].outcome == "winner"


def test_participant_aware_parse_api_preserves_legacy_parse_document_shape():
    html = """<table><tr><th>供应商</th><th>资格性审查</th></tr>
      <tr><td>甲科技有限公司</td><td>通过</td></tr></table>""".encode()
    document = SourceDocument("notice.html", html)

    text, items, warnings = parse_document(document)
    detailed_text, detailed_items, participants, detailed_warnings = parse_document_with_participants(document)

    assert (text, items, warnings) == (detailed_text, detailed_items, detailed_warnings)
    assert len(participants) == 1
