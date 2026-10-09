import pytest

from app.attachment_scope import (
    classify_attachment_scope,
    filter_attachment_items,
    filter_attachment_participants,
)
from app.schemas import ItemCandidate, ParticipantCandidate


def row(filename, name, evidence=None):
    return ItemCandidate(
        source_file=filename, source_location="model_text_evidence",
        product_name=name, source_evidence=evidence,
    )


@pytest.mark.parametrize(("filename", "text", "name"), [
    ("archive.zip!/案例一览表.png",
     ("案例一览表 项目编号：当前招标编号 项目名称：采购包1 合同签订日期 项目单位地址 "
      "陈景润的科学人生出版项目 2025.9.9"), "陈景润的科学人生出版项目"),
    ("archive.zip!/附件.zip!/新建文件夹/采购包1业绩.bmp",
     ("案例一览表（证明材料） 合同签订日期 项目单位名称 项目单位地址 "
      "2024年度济南市应急指挥平台运行保障服务项目 1198000.00"),
     "2024年度济南市应急指挥平台运行保障服务项目"),
    ("供应商同类项目实施情况一览表.pdf",
     ("供应商同类项目实施情况一览表 项目名称：本项目 当前采购编号 "
      "采购单位名称 项目名称 采购内容 智慧停车场智能道闸设备系统采购及安装项目 1宗"),
     "智慧停车场智能道闸设备系统采购及安装项目"),
])
def test_historical_evidence_is_real_but_is_not_current_purchase(filename, text, name):
    decision = classify_attachment_scope(filename, text)
    assert decision.scope == "historical_qualification"
    assert not decision.extract_items
    assert not decision.extract_metadata
    assert not decision.extract_participants
    kept, warnings = filter_attachment_items(filename, text, [row(filename, name, name)])
    assert kept == []
    assert any(filename in warning and "保留审计来源" in warning for warning in warnings)
    assert any("排除采购标的 1 条" in warning for warning in warnings)


def test_filename_alone_never_discards_unparsed_historical_or_mixed_material():
    decision = classify_attachment_scope("采购包1业绩.bmp")
    assert decision.requires_parse
    assert decision.extract_items and decision.extract_participants
    assert "filename_history_hint" in decision.reason_codes
    with pytest.raises(ValueError, match="解析后"):
        filter_attachment_items("采购包1业绩.bmp", "", [], decision)


def test_current_goods_list_in_history_named_file_survives():
    filename = "业绩及本次供货.docx"
    text = "本次采购清单 货物名称 数量 台式计算机 64台"
    candidate = row(filename, "台式计算机", "台式计算机 64台")
    assert classify_attachment_scope(filename, text).scope == "current_procurement"
    assert filter_attachment_items(filename, text, [candidate])[0] == [candidate]


def test_history_named_parent_archive_does_not_classify_current_leaf():
    decision = classify_attachment_scope(
        "历史业绩.zip!/投标文件.zip!/货物清单.pdf", "采购清单 打印机 2台",
    )
    assert decision.scope == "current_procurement"
    assert decision.extract_items


@pytest.mark.parametrize("filename", ["招标文件.docx", "技术要求.pdf", "业绩要求.pdf"])
def test_requirement_to_supply_history_table_is_not_completed_history(filename):
    text = "供应商须提供供应商同类项目实施情况一览表作为业绩证明。采购清单：打印机2台。"
    decision = classify_attachment_scope(filename, text)
    assert decision.scope == "current_procurement"
    assert decision.extract_items
    assert filter_attachment_items(filename, text, [row(filename, "打印机", "打印机2台")])[0]


def test_current_evaluation_retains_participants_without_score_as_items():
    filename = "评审结果.pdf"
    text = "评审结果汇总表 投标人名称 综合得分 得分排名 甲公司 92.5 1"
    decision = classify_attachment_scope(filename, text)
    assert decision.scope == "current_evaluation"
    assert decision.extract_participants and not decision.extract_metadata
    kept, warnings = filter_attachment_items(filename, text, [row(filename, "甲公司", "甲公司 92.5")])
    assert not kept
    assert any("保留当前参与主体" in warning for warning in warnings)


def test_mixed_history_scoring_and_goods_are_checked_per_section():
    filename = "报价及业绩评分.pdf"
    text = (
        "分项报价明细表\n台式计算机 64台\n"
        "供应商同类项目实施情况一览表\n旧教学机房改造 2024年 合同签订日期\n"
        "评审结果汇总表\n甲公司 综合得分 98\n"
        "本次采购清单\n教学软件 1套"
    )
    rows = [row(filename, name, evidence) for name, evidence in [
        ("台式计算机", "台式计算机 64台"), ("旧教学机房改造", "旧教学机房改造 2024年"),
        ("甲公司", "甲公司 综合得分 98"), ("教学软件", "教学软件 1套"),
    ]]
    decision = classify_attachment_scope(filename, text)
    assert decision.scope == "mixed"
    assert decision.extract_participants and not decision.extract_metadata
    kept, warnings = filter_attachment_items(filename, text, rows, decision)
    assert [item.product_name for item in kept] == ["台式计算机", "教学软件"]
    assert sum("排除非本次采购清单候选" in warning for warning in warnings) == 2
    assert any("原文存在仍不代表本次采购" in warning for warning in warnings)


def test_mixed_ambiguous_repeated_boundary_or_absent_evidence_is_preserved():
    filename = "报价及业绩.pdf"
    text = "采购清单\n打印机2台\n案例一览表\n打印机2台 历史投影仪"
    rows = [row(filename, "重复名称", "打印机2台"), row(filename, "缺失证据"),
            row(filename, "不存在证据", "原文没有"),
            row(filename, "跨分节", "打印机2台 案例一览表 打印机2台")]
    kept, warnings = filter_attachment_items(filename, text, rows)
    assert kept == rows
    assert sum("用途待核验" in warning for warning in warnings) == 4


def test_unknown_does_not_guess_scope_or_package_and_warns():
    filename = "附件.bmp"
    text = "项目编号 当前编号 采购包1 项目名称 技术服务 数量1"
    candidate = row(filename, "技术服务", "技术服务 数量1")
    decision = classify_attachment_scope(filename, text)
    assert decision.scope == "unknown"
    kept, warnings = filter_attachment_items(filename, text, [candidate])
    assert kept == [candidate] and kept[0].package_code == "default"
    assert warnings and filename in warnings[0]


@pytest.mark.parametrize("text", ["", "  \n\t"])
def test_unreadable_history_named_file_requires_review_instead_of_exclusion(text):
    decision = classify_attachment_scope("采购包1业绩.bmp", text)
    assert decision.scope == "unknown" and decision.extract_items
    assert "正文为空" in decision.warnings[0]


def test_generic_history_filename_needs_contract_or_heading_corroboration():
    decision = classify_attachment_scope("业绩.png", "供应商名称 甲公司 技术服务")
    assert decision.scope == "unknown" and decision.extract_participants
    confirmed = classify_attachment_scope("业绩.png", "合同签订日期 项目单位地址 技术服务")
    assert confirmed.scope == "historical_qualification"


def test_qualification_source_is_auditable_but_does_not_become_current_metadata():
    decision = classify_attachment_scope("营业执照.png", "营业执照 企业名称甲公司 注册日期2020年")
    assert decision.scope == "historical_qualification"
    assert not decision.extract_items and not decision.extract_metadata


def test_filename_current_conflict_preserves_potentially_mixed_document():
    decision = classify_attachment_scope("报价明细及业绩.pdf", "案例一览表 旧项目 合同签订日期")
    assert decision.scope == "mixed"
    assert decision.extract_items


def test_decision_and_mixed_candidate_source_must_match():
    decision = classify_attachment_scope("other.pdf", "采购清单")
    with pytest.raises(ValueError, match="同一文件"):
        filter_attachment_items("source.pdf", "采购清单", [], decision)
    with pytest.raises(ValueError, match="来源"):
        filter_attachment_items("source.pdf", "采购清单\n案例一览表", [row("other.pdf", "打印机")])
    with pytest.raises(ValueError, match="来源"):
        filter_attachment_items("source.pdf", "案例一览表", [row("other.pdf", "打印机")])


def test_real_goods_header_with_score_mention_is_not_discarded():
    filename = "结果附件.pdf"
    text = "成交详情\n品目名称\t品牌\t规格型号\t数量\n打印机 示例牌 P100 2台\n评审总得分 92.5"
    candidate = row(filename, "打印机", "打印机 示例牌 P100 2台")
    decision = classify_attachment_scope(filename, text)
    assert decision.extract_items
    assert filter_attachment_items(filename, text, [candidate])[0] == [candidate]


def test_prose_and_product_names_do_not_become_history_document_heading():
    filename = "货物.xlsx"
    text = "名称\t品牌\t规格型号\t数量\n电子档案历史项目清单展示软件 示例牌 V1 1套"
    candidate = row(filename, "电子档案历史项目清单展示软件", text.splitlines()[1])
    decision = classify_attachment_scope(filename, text)
    assert decision.scope == "current_procurement"
    assert filter_attachment_items(filename, text, [candidate])[0] == [candidate]


@pytest.mark.parametrize("following", [
    "新购设备参数\n名称\t品牌\t规格型号\t数量\n打印机 示例 P1 2台",
    "本次供应内容\n打印机 示例 P1 2台",
])
def test_unknown_section_ends_history_scope_inheritance(following):
    filename = "附件.pdf"
    text = "案例一览表\n旧项目 2024年\n" + following
    candidates = [row(filename, "旧项目", "旧项目 2024年"),
                  row(filename, "打印机", "打印机 示例 P1 2台")]
    decision = classify_attachment_scope(filename, text)
    assert decision.scope == "mixed"
    kept, _ = filter_attachment_items(filename, text, candidates)
    assert [item.product_name for item in kept] == ["打印机"]


def test_mixed_participants_exclude_historical_clients_and_preserve_current_scores():
    filename = "评审与业绩.pdf"
    text = ("案例一览表\n历史采购人乙公司 项目2024年\n"
            "评审结果汇总表\n投标人甲公司 98分\n来源不明说明\n待核验丙公司")
    participants = [ParticipantCandidate(
        organization_name=name, source_file=filename, source_location="model_text_evidence",
        source_evidence=evidence,
    ) for name, evidence in [("乙公司", "历史采购人乙公司 项目2024年"),
                             ("甲公司", "投标人甲公司 98分"), ("丙公司", "待核验丙公司")]]
    decision = classify_attachment_scope(filename, text)
    assert decision.scope == "mixed" and not decision.extract_metadata
    kept, warnings = filter_attachment_participants(filename, text, participants, decision)
    assert [participant.organization_name for participant in kept] == ["甲公司", "丙公司"]
    assert any("排除历史段落参与主体 乙公司" in warning for warning in warnings)
    assert any("主体用途待核验" in warning for warning in warnings)


def test_pure_history_participants_rejected_and_current_evaluation_retained():
    filename = "附件.pdf"
    participant = ParticipantCandidate(
        organization_name="甲公司", source_file=filename, source_location="model_text_evidence",
        source_evidence="甲公司",
    )
    assert not filter_attachment_participants(filename, "案例一览表\n甲公司", [participant])[0]
    assert filter_attachment_participants(filename, "评审结果\n甲公司 98分", [participant])[0]


def test_history_table_footnote_is_not_treated_as_new_unknown_section():
    filename = "案例一览表.png"
    text = "案例一览表及证明材料\n旧项目 2024年\n页、签字盖章页及相应标的明细等内容"
    decision = classify_attachment_scope(filename, text)
    assert decision.scope == "historical_qualification"
