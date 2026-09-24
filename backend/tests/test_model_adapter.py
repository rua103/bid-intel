import json

from app.config import Settings
from app.model_adapter import extract_unstructured_items, model_call_budget

REASONING_MARKER = "先读原文，再逐条抽取。"


def model_settings() -> Settings:
    return Settings(
        model_base_url="https://model.example/v1",
        model_api_key="test-key",
        model_name="qwen-test-model",
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
    # Regression guard, measured against the configured gateway on a real notice:
    # deepseek-v4-flash otherwise spends ~5000 reasoning tokens before emitting any
    # JSON -- 208s per notice, and the JSON is truncated at max_tokens (six of six
    # requests came back empty or truncated). Reasoning tokens are compared with
    # the switch off; only this chat-template form works, so do not "simplify" it to
    # a top-level field. Top-level "thinking"/"enable_thinking" and
    # "reasoning_effort" were all measured as ignored (reasoning ~5000, 213-216s),
    # and "thinking": {"type": "disabled"} returned empty content outright.
    assert body["chat_template_kwargs"] == {"enable_thinking": False}
    assert "thinking" not in body
    assert "enable_thinking" not in body
    assert "reasoning_effort" not in body


def test_thinking_switch_can_be_turned_off_for_other_endpoints(stub_model_stream):
    calls = stub_model_stream("{}")
    settings = model_settings().model_copy(update={"model_disable_thinking": False})
    extract_unstructured_items(filename="notice.html", text="项目名称：X", settings=settings)
    assert "chat_template_kwargs" not in calls[0]["json"]


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
