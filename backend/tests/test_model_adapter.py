import json

import httpx

from app.config import Settings
from app.model_adapter import extract_unstructured_items


def test_model_adapter_accepts_only_source_verified_quotes(monkeypatch):
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
    response_body = {"choices": [{"message": {"content": json.dumps(content, ensure_ascii=False)}}]}

    def fake_post(url, **kwargs):
        request = httpx.Request("POST", url)
        return httpx.Response(200, json=response_body, request=request)

    monkeypatch.setattr(httpx, "post", fake_post)
    settings = Settings(
        model_base_url="https://model.example/v1",
        model_api_key="test-key",
        model_name="qwen-test-model",
    )
    metadata, items, participants, warnings = extract_unstructured_items(
        filename="notice.html",
        text=source,
        settings=settings,
        include_participants=True,
    )
    assert metadata.project_name == "打印机采购项目"
    assert metadata.announced_total_award == 120000
    assert len(items) == 1 and items[0].source_file == "notice.html"
    assert [row.organization_name for row in participants] == ["中标供应商甲"]
    assert any("忽略无法在原文中定位证据" in warning for warning in warnings)
