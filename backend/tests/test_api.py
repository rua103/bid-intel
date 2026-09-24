import json

import httpx
from fastapi.testclient import TestClient

from app.config import settings
from app.main import app


def test_upload_html_extracts_items_and_searches(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "database_path", str(tmp_path / "api.db"))
    html = """<html><body>
      项目名称：打印设备采购项目 采购单位：某市采购中心
      <table><tr><th>品目名称</th><th>采购标的</th><th>品牌</th><th>规格型号</th><th>数量</th><th>单价(元)</th><th>总价(元)</th></tr>
      <tr><td>办公设备</td><td>激光打印机</td><td>Canon</td><td>LBP 2900</td><td>2(台)</td><td>1800</td><td>3600</td></tr></table>
    </body></html>"""
    with TestClient(app) as client:
        response = client.post(
            "/api/v1/notices/import",
            files=[("files", ("notice.html", html.encode(), "text/html"))],
        )
        assert response.status_code == 200, response.text
        payload = response.json()
        assert payload["items_found"] == 1
        assert payload["items"][0]["total_price"] == "3600"

        search = client.get("/api/v1/items", params={"brand": "Canon"})
        assert search.status_code == 200
        assert search.json()[0]["product_name"] == "激光打印机"


def test_configured_model_import_populates_relationship_queries(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "database_path", str(tmp_path / "model-api.db"))
    monkeypatch.setattr(settings, "model_base_url", "https://model.example/v1")
    monkeypatch.setattr(settings, "model_api_key", "test-key")
    monkeypatch.setattr(settings, "model_name", "qwen-test")
    model_output = {
        "metadata": {
            "project_name": "设备采购项目",
            "project_number": "X-2026",
            "procurement_unit": "某市中心医院",
            "announced_total_award": 120000,
        },
        "participants": [
            {
                "organization_name": "供应商甲",
                "outcome": "winner",
                "award_amount": 120000,
                "source_evidence": "供应商甲以120000元中标",
            },
            {
                "organization_name": "供应商乙",
                "outcome": "nonwinner",
                "source_evidence": "供应商乙参与投标但未中标",
            },
        ],
        "items": [],
    }

    def fake_post(url, **kwargs):
        request = httpx.Request("POST", url)
        response_body = {
            "choices": [{"message": {"content": json.dumps(model_output, ensure_ascii=False)}}]
        }
        return httpx.Response(200, json=response_body, request=request)

    monkeypatch.setattr(httpx, "post", fake_post)
    html = """<html><body>
      项目名称：设备采购项目 项目编号：X-2026 采购单位：某市中心医院
      供应商甲以120000元中标。供应商乙参与投标但未中标。
      <table><tr><th>品目名称</th><th>采购标的</th><th>品牌</th><th>规格型号</th><th>数量</th><th>单价(元)</th><th>总价(元)</th></tr>
      <tr><td>办公设备</td><td>打印机</td><td>演示牌</td><td>X-1</td><td>1台</td><td>120000</td><td>120000</td></tr></table>
    </body></html>"""
    with TestClient(app) as client:
        imported = client.post(
            "/api/v1/notices/import",
            files=[("files", ("notice.html", html.encode(), "text/html"))],
        )
        assert imported.status_code == 200, imported.text
        buyer = client.get("/api/v1/organizations", params={"query": "某市中心医院"}).json()[0]
        awardees = client.get(f"/api/v1/analytics/buyers/{buyer['id']}/awardees").json()
        assert awardees["awardees"][0]["name"] == "供应商甲"
        bidders = client.get(f"/api/v1/analytics/buyers/{buyer['id']}/bidders").json()
        assert {row["canonical_name"] for row in bidders["top_bidders"]} == {"供应商甲", "供应商乙"}
