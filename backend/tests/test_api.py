import hashlib
import json

import pytest
from fastapi.testclient import TestClient

from app import analytics_backend
from app.analytics_backend import AnalyticsResult
from app.config import settings
from app.main import app
from app.schemas import ImportResult, NoticeMetadata, ParticipantCandidate
from app.storage import save_import


def test_cors_allows_private_lan_frontend_and_rejects_unlisted_public_origins():
    with TestClient(app) as client:
        response = client.options(
            "/api/v1/health",
            headers={
                "Origin": "http://192.168.1.45:5173",
                "Access-Control-Request-Method": "GET",
            },
        )
        assert response.status_code == 200
        assert response.headers["access-control-allow-origin"] == "http://192.168.1.45:5173"
        assert response.headers["access-control-allow-credentials"] == "true"
        actual = client.get(
            "/api/v1/health", headers={"Origin": "http://192.168.1.45:5173"}
        )
        assert "X-Analytics-Backend" in actual.headers["access-control-expose-headers"]

        external = client.options(
            "/api/v1/health",
            headers={
                "Origin": "https://reviewer.example.com",
                "Access-Control-Request-Method": "GET",
            },
        )
        assert "access-control-allow-origin" not in external.headers


@pytest.mark.parametrize("origin,allowed", [
    ("http://10.2.3.4:5173", True),
    ("http://172.16.1.2:5173", True),
    ("http://172.31.1.2:5173", True),
    ("http://172.32.1.2:5173", False),
    ("https://192.168.1.20.example.com", False),
])
def test_cors_private_origin_boundaries(origin, allowed):
    with TestClient(app) as client:
        response = client.options(
            "/api/v1/evaluation/run",
            headers={"Origin": origin, "Access-Control-Request-Method": "POST"},
        )
        assert (response.headers.get("access-control-allow-origin") == origin) is allowed


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

        detail = client.get(f"/api/v1/notices/{payload['notice_id']}")
        assert detail.status_code == 200
        detail_payload = detail.json()
        assert detail_payload["items"][0]["source_evidence"]
        assert detail_payload["items"][0]["source_location"].startswith("table:")
        assert detail_payload["items"][0]["evidence_status"] == "available"
        source = detail_payload["source_files"][0]
        assert source["sha256"] == hashlib.sha256(html.encode()).hexdigest()
        assert source["file_available"] is False
        assert detail_payload["metadata_evidence_status"] == "missing"
        assert detail_payload["capabilities"]["explicit_empty_status"] == "not_saved"

        search = client.get("/api/v1/items", params={"brand": "Canon"})
        assert search.status_code == 200
        assert search.json()[0]["product_name"] == "激光打印机"
        assert search.json()[0]["package_code"] == "default"


def test_notice_detail_returns_404_for_unknown_notice(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "database_path", str(tmp_path / "missing-notice.db"))
    with TestClient(app) as client:
        response = client.get("/api/v1/notices/999")
    assert response.status_code == 404


def test_configured_model_import_populates_relationship_queries(
    tmp_path, monkeypatch, stub_model_stream
):
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

    stub_model_stream(json.dumps(model_output, ensure_ascii=False))
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
        assert bidders["include_winners"] is True
        assert {row["canonical_name"] for row in bidders["top_bidders"]} == {"供应商甲", "供应商乙"}
        all_bidders = client.get(
            f"/api/v1/analytics/buyers/{buyer['id']}/bidders",
            params={"include_winners": "true"},
        ).json()
        assert all_bidders["include_winners"] is True
        assert {row["canonical_name"] for row in all_bidders["top_bidders"]} == {
            "供应商甲",
            "供应商乙",
        }


def test_analytics_api_routes_report_backend_for_all_five_scenes(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "database_path", str(tmp_path / "api-backend.db"))
    monkeypatch.setattr(settings, "analytics_backend", "neo4j")
    calls = []

    def query(_path, scene, **parameters):
        calls.append((scene, parameters))
        return AnalyticsResult({"scene": scene}, "neo4j")

    monkeypatch.setattr(analytics_backend, "query_analytics", query)
    with TestClient(app) as client:
        responses = [
            client.get("/api/v1/analytics/buyers/1/awardees"),
            client.get("/api/v1/analytics/buyers/1/bidders"),
            client.get("/api/v1/analytics/suppliers/2/co-bidders"),
            client.post("/api/v1/analytics/common-buyers", json={"organization_ids": [2, 3]}),
            client.post("/api/v1/analytics/common-projects", json={"organization_ids": [2, 3]}),
        ]

    assert [response.status_code for response in responses] == [200] * 5
    assert [response.headers["X-Analytics-Backend"] for response in responses] == ["neo4j"] * 5
    assert [response.json()["scene"] for response in responses] == [
        "buyer_awardees", "buyer_bidders", "supplier_co_bidders", "common_buyers", "common_projects"
    ]
    assert [scene for scene, _ in calls] == [
        "buyer_awardees", "buyer_bidders", "supplier_co_bidders", "common_buyers", "common_projects"
    ]


def test_analytics_api_falls_back_to_sqlite_when_neo4j_connection_fails(tmp_path, monkeypatch):
    database = tmp_path / "api-fallback.db"
    monkeypatch.setattr(settings, "database_path", str(database))
    monkeypatch.setattr(settings, "analytics_backend", "neo4j")

    def fail_to_connect(*_args):
        raise RuntimeError("Neo4j offline")

    monkeypatch.setattr(analytics_backend, "create_driver", fail_to_connect)
    save_import(
        database,
        ImportResult(
            notice_id=0,
            source_files=["seed.html"],
            items_found=0,
            items=[],
            metadata=NoticeMetadata(
                project_name="回退测试项目",
                project_number="FALLBACK-1",
                procurement_unit="回退测试采购中心",
                announced_total_award="120",
            ),
            participants=[
                ParticipantCandidate(
                    organization_name="中标供应商",
                    outcome="winner",
                    award_amount="120",
                    source_file="seed.html",
                    source_location="row:1",
                ),
                ParticipantCandidate(
                    organization_name="未中标供应商",
                    outcome="nonwinner",
                    source_file="seed.html",
                    source_location="row:2",
                ),
            ],
            warnings=[],
        ),
    )

    with TestClient(app) as client:
        buyer = client.get(
            "/api/v1/organizations", params={"query": "回退测试采购中心"}
        ).json()[0]
        response = client.get(f"/api/v1/analytics/buyers/{buyer['id']}/bidders")

    assert response.status_code == 200
    assert response.headers["X-Analytics-Backend"] == "sqlite"
    assert response.headers["X-Analytics-Fallback"] == "sqlite"
    assert [row["canonical_name"] for row in response.json()["top_bidders"]] == ["中标供应商", "未中标供应商"]
