import json

import pytest

from app.config import Settings
from app.model_adapter import extract_unstructured_items, model_call_budget

REASONING_MARKER = "先读原文，再逐条抽取。"


def model_settings() -> Settings:
    return Settings(
        model_base_url="https://model.example/v1",
        model_api_key="test-key",
        model_name="deepseek-flash",
    )


def test_model_adapter_accepts_only_source_verified_quotes(stub_model_stream):
    source = "项目名称：打印机采购项目 采购单位：某市采购中心 中标供应商甲以 120000 元中标，激光打印机 品牌A 型号X1 数量2台 单价60000元 总价120000元。"
    content = {
        "metadata": {
            "project_name": "打印机采购项目",
            "procurement_unit": "某市采购中心",
            "announced_total_award": 120000,
        },
        "participants": [
            {
                "organization_name": "中标供应商甲",
                "outcome": "winner",
                "award_amount": 120000,
                "source_evidence": "中标供应商甲以 120000 元中标",
            },
            {
                "organization_name": "虚构供应商",
                "outcome": "nonwinner",
                "source_evidence": "虚构供应商参与了本项目",
            },
        ],
        "items": [
            {
                "product_name": "激光打印机",
                "category": "办公设备",
                "brand": "品牌A",
                "model": "型号X1",
                "quantity": 2,
                "quantity_unit": "台",
                "unit_price": 60000,
                "total_price": 120000,
                "source_evidence": "激光打印机 品牌A 型号X1 数量2台 单价60000元 总价120000元",
            }
        ],
    }
    stub_model_stream(json.dumps(content, ensure_ascii=False))
    metadata, items, participants, warnings = extract_unstructured_items(
        filename="notice.html",
        text=source,
        settings=model_settings(),
        include_participants=True,
    )
    assert metadata.project_name == "打印机采购项目"
    assert metadata.announced_total_award == 120000
    assert len(items) == 1 and items[0].source_file == "notice.html"
    assert [row.organization_name for row in participants] == ["中标供应商甲"]
    assert any("忽略无法在原文中定位证据" in warning for warning in warnings)


def test_extraction_uses_streaming_and_disables_thinking(stub_model_stream):
    calls = stub_model_stream("{}")
    extract_unstructured_items(filename="notice.html", text="项目名称：X", settings=model_settings())
    assert len(calls) == 1
    assert calls[0]["method"] == "POST"
    assert calls[0]["url"] == "https://model.example/v1/chat/completions"
    body = calls[0]["json"]
    assert body["stream"] is True
    assert body["stream_options"] == {"include_usage": True}
    assert body["thinking"] == {"type": "disabled"}
    assert "chat_template_kwargs" not in body
    assert "enable_thinking" not in body
    assert "reasoning_effort" not in body


def test_thinking_switch_can_be_turned_off_for_other_endpoints(stub_model_stream):
    calls = stub_model_stream("{}")
    settings = model_settings().model_copy(update={"model_disable_thinking": False})
    extract_unstructured_items(filename="notice.html", text="项目名称：X", settings=settings)
    assert "chat_template_kwargs" not in calls[0]["json"]


def test_qwen_endpoint_keeps_legacy_thinking_setting(stub_model_stream):
    calls = stub_model_stream("{}")
    settings = model_settings().model_copy(update={"model_name": "qwen-test-model"})
    extract_unstructured_items(filename="notice.html", text="项目名称：X", settings=settings)
    assert calls[0]["json"]["chat_template_kwargs"] == {"enable_thinking": False}
    assert "thinking" not in calls[0]["json"]


def test_deepseek_thinking_is_disabled_with_official_field(stub_model_stream):
    calls = stub_model_stream("{}")
    extract_unstructured_items(filename="notice.html", text="项目名称：X", settings=model_settings())
    body = calls[0]["json"]
    assert body["thinking"] == {"type": "disabled"}
    assert "chat_template_kwargs" not in body


def test_chunked_stream_is_reassembled_and_reasoning_content_is_dropped(stub_model_stream):
    source = (
        "项目名称：打印机采购项目 采购单位：某市采购中心 "
        "激光打印机 品牌A 型号X1 数量2台 单价60000元 总价120000元"
    )
    payload = {
        "metadata": {"project_name": "打印机采购项目", "procurement_unit": "某市采购中心"},
        "participants": [],
        "items": [
            {
                "product_name": "激光打印机",
                "brand": "品牌A",
                "model": "型号X1",
                "quantity": 2,
                "quantity_unit": "台",
                "unit_price": 60000,
                "total_price": 120000,
                "source_evidence": "激光打印机 品牌A 型号X1 数量2台 单价60000元 总价120000元",
            }
        ],
    }
    # chunk_size 11 forces the JSON object to span many SSE frames.
    stub_model_stream(json.dumps(payload, ensure_ascii=False), chunk_size=11)
    metadata, items, _, warnings = extract_unstructured_items(
        filename="notice.html", text=source, settings=model_settings()
    )
    # The object only parses if every delta was concatenated and reasoning was skipped.
    assert metadata.project_name == "打印机采购项目"
    assert [item.product_name for item in items] == ["激光打印机"]
    assert items[0].total_price == 120000
    assert warnings == []
    assert all(REASONING_MARKER not in (item.source_evidence or "") for item in items)


def test_stream_token_usage_is_recorded(stub_model_stream):
    stub_model_stream("{}", usage={"prompt_tokens": 101, "completion_tokens": 23})
    with model_call_budget(2) as usage:
        extract_unstructured_items(
            filename="notice.html", text="项目名称：X", settings=model_settings()
        )
    assert usage.requests == 1
    assert usage.successful_responses == 1
    assert usage.prompt_tokens == 101
    assert usage.completion_tokens == 23


def test_empty_stream_reports_a_truncation_warning(stub_model_stream):
    stub_model_stream("")
    _, items, _, warnings = extract_unstructured_items(
        filename="notice.html", text="项目名称：X", settings=model_settings()
    )
    assert items == []
    assert any("模型返回空内容" in warning for warning in warnings)


def test_model_package_labels_are_canonicalized_before_hybrid_alignment(stub_model_stream):
    source = "合同包1：激光打印机；中标供应商甲。"
    payload = {
        "items": [{
            "package_code": "合同包1",
            "product_name": "激光打印机",
            "source_evidence": "合同包1：激光打印机",
        }],
        "participants": [{
            "package_code": "包号：01",
            "organization_name": "中标供应商甲",
            "outcome": "winner",
            "source_evidence": "中标供应商甲",
        }],
    }
    stub_model_stream(json.dumps(payload, ensure_ascii=False))

    _, items, participants, warnings = extract_unstructured_items(
        filename="notice.html", text=source, settings=model_settings(),
        include_participants=True,
    )

    assert not warnings
    assert [row.package_code for row in items] == ["1"]
    assert [row.package_code for row in participants] == ["1"]


def test_named_model_package_requires_explicit_label_evidence(stub_model_stream):
    source = "项目名称：教学仪器采购项目 分包名称：教学仪器 主要中标标的信息：精密注塑成型机"
    payload = {
        "items": [{
            "package_code": "教学仪器",
            "product_name": "精密注塑成型机",
            "source_evidence": "主要中标标的信息：精密注塑成型机",
        }],
        "participants": [],
    }
    stub_model_stream(json.dumps(payload, ensure_ascii=False))

    _, items, _, warnings = extract_unstructured_items(
        filename="notice.html", text=source, settings=model_settings(),
    )

    assert [row.package_code for row in items] == ["default"]
    assert any("名称型包号" in warning for warning in warnings)


def test_named_model_package_accepts_only_label_and_value_quote(stub_model_stream):
    source = "项目名称：教学仪器采购项目 分包名称：教学仪器 主要中标标的信息：精密注塑成型机"
    payload = {
        "items": [{
            "package_code": "教学仪器",
            "product_name": "精密注塑成型机",
            "source_evidence": "主要中标标的信息：精密注塑成型机",
            "package_source_evidence": "分包名称：教学仪器",
        }],
        "participants": [],
    }
    stub_model_stream(json.dumps(payload, ensure_ascii=False))

    _, items, _, warnings = extract_unstructured_items(
        filename="notice.html", text=source, settings=model_settings(),
    )

    assert [row.package_code for row in items] == ["教学仪器"]
    assert "分包名称：教学仪器" in (items[0].source_evidence or "")
    assert warnings == []


def test_named_model_package_does_not_use_project_or_category_text(stub_model_stream):
    source = (
        "项目名称：城市安全风险综合监测预警平台试点建设项目综合运维服务 "
        "分包名称：城市安全风险综合监测预警平台试点建设项目综合运维服务 "
        "品目：物理治疗 分包名称：物理治疗"
    )
    payload = {
        "metadata": {
            "project_name": "城市安全风险综合监测预警平台试点建设项目综合运维服务",
        },
        "items": [{
            "package_code": "城市安全风险综合监测预警平台试点建设项目综合运维服务",
            "product_name": "城市安全风险综合监测预警平台试点建设项目综合运维服务",
            "source_evidence": "项目名称：城市安全风险综合监测预警平台试点建设项目综合运维服务",
            "package_source_evidence": "项目名称：城市安全风险综合监测预警平台试点建设项目综合运维服务",
            "category": None,
        }, {
            "package_code": "物理治疗",
            "product_name": "脊柱定位周期减压牵引系统",
            "category": "物理治疗",
            "source_evidence": "品目：物理治疗",
            "package_source_evidence": "分包名称：物理治疗",
        }],
        "participants": [],
    }
    stub_model_stream(json.dumps(payload, ensure_ascii=False))

    _, items, _, warnings = extract_unstructured_items(
        filename="notice.html", text=source, settings=model_settings(),
    )

    assert [row.package_code for row in items] == ["default", "default"]
    assert sum("名称型包号" in warning for warning in warnings) == 2


def test_named_participant_package_does_not_use_item_category(stub_model_stream):
    source = "分包名称：物理治疗 采购标的：脊柱定位周期减压牵引系统 品目：物理治疗 供应商：甲公司"
    payload = {
        "items": [{
            "package_code": "default",
            "product_name": "脊柱定位周期减压牵引系统",
            "category": "物理治疗",
            "source_evidence": "采购标的：脊柱定位周期减压牵引系统 品目：物理治疗",
        }],
        "participants": [{
            "package_code": "物理治疗",
            "organization_name": "甲公司",
            "outcome": "winner",
            "source_evidence": "供应商：甲公司",
            "package_source_evidence": "分包名称：物理治疗",
        }],
    }
    stub_model_stream(json.dumps(payload, ensure_ascii=False))

    _, _, participants, warnings = extract_unstructured_items(
        filename="notice.html", text=source, settings=model_settings(),
        include_participants=True,
    )

    assert [row.package_code for row in participants] == ["default"]
    assert any("名称型包号" in warning for warning in warnings)


@pytest.mark.parametrize(
    ("notice_id", "source", "package_code", "package_evidence"),
    [
        (
            "932b26e5",
            (
                "项目名称：青岛工程职业学院智能制造学院技能大赛设备采购项目 "
                "分包名称：教学仪器 精密注塑成型机"
            ),
            "教学仪器",
            "分包名称：教学仪器",
        ),
        (
            "228ad7fc",
            "标段名称：公共阅读数字资源订购 标的：公共阅读数字资源订购",
            "公共阅读数字资源订购",
            "标段名称：公共阅读数字资源订购",
        ),
    ],
)
def test_gold_named_package_samples_are_accepted(
    stub_model_stream, notice_id, source, package_code, package_evidence,
):
    payload = {
        "items": [{
            "package_code": package_code,
            "product_name": "公共阅读数字资源订购",
            "source_evidence": source,
            "package_source_evidence": package_evidence,
        }],
        "participants": [],
    }
    stub_model_stream(json.dumps(payload, ensure_ascii=False))

    _, items, _, warnings = extract_unstructured_items(
        filename=f"{notice_id}.html", text=source, settings=model_settings(),
    )

    assert [row.package_code for row in items] == [package_code]
    assert warnings == []
