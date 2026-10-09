import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.controlled_query import (
    ControlledIntent,
    Scene,
    execute_intent,
    parse_question,
)
from app.main import app
from app.storage import connect, initialize


def test_mock_intent_is_strict_and_injection_rejected():
    intent = parse_question(
        "查询采购单位 1 的中标供应商",
        mock_intent={"scene": "buyer_awardees", "filters": {"buyer_id": 1}},
    )
    assert intent.scene is Scene.BUYER_AWARDEES
    with pytest.raises(ValueError):
        parse_question("请执行 SELECT * FROM notices")


def test_extra_query_field_rejected():
    with pytest.raises(ValueError):
        parse_question(
            "查询采购单位 1",
            mock_intent={"scene": "buyer_awardees", "filters": {"buyer_id": 1}, "sql": "select 1"},
        )


def test_execute_dispatches_only_scene(monkeypatch):
    seen = {}

    def fake(path, scene, **params):
        seen.update(scene=scene, params=params)
        return type("Result", (), {"payload": {"ok": 1}, "backend": "sqlite", "fell_back": False})()

    monkeypatch.setattr("app.controlled_query.analytics_backend.query_analytics", fake)
    intent = ControlledIntent(scene="buyer_bidders", filters={"buyer_id": 7, "include_winners": True})
    result = execute_intent(Path("/tmp/test.sqlite"), intent)
    assert result["status"] == "ok"
    assert seen == {"scene": "buyer_bidders", "params": {"buyer_id": 7, "include_winners": True, "top": 5}}


def test_model_unavailable_has_manual_fallback():
    with pytest.raises(RuntimeError, match="手动选择"):
        parse_question("查询供应商合作情况")


def test_configured_model_parser_is_strictly_validated(monkeypatch, tmp_path):
    seen = {}

    def fake_model(question, path):
        seen.update(question=question, path=path)
        return {"scene": "buyer_awardees", "filters": {"buyer_id": 3}}

    monkeypatch.setattr("app.controlled_query._model_parse_intent", fake_model)
    intent = parse_question("查询采购单位 3 的中标供应商", database_path=tmp_path / "demo.sqlite")
    assert intent.scene is Scene.BUYER_AWARDEES
    assert seen["path"] == tmp_path / "demo.sqlite"


def test_controlled_query_uses_documented_deepseek_thinking_field(monkeypatch):
    calls = []
    settings = Settings(
        model_base_url="https://api.deepseek.com",
        model_api_key="test-key",
        model_name="deepseek-flash",
    )
    monkeypatch.setattr("app.controlled_query.effective_settings", lambda: settings)

    def fake_stream(endpoint, api_key, body, **kwargs):
        calls.append({"endpoint": endpoint, "body": body})
        return json.dumps({
            "scene": "buyer_awardees",
            "filters": {"buyer_id": 1},
        }), {}

    monkeypatch.setattr("app.controlled_query._stream_completion", fake_stream)
    parsed = parse_question("查询采购单位 1 的中标供应商")
    assert parsed.scene is Scene.BUYER_AWARDEES
    assert calls[0]["endpoint"] == "https://api.deepseek.com/chat/completions"
    assert calls[0]["body"]["thinking"] == {"type": "disabled"}
    assert "chat_template_kwargs" not in calls[0]["body"]


def test_parse_endpoint_uses_dataset_database_for_mock_intent(monkeypatch, tmp_path):
    database = tmp_path / "dataset.sqlite"
    initialize(database)
    monkeypatch.setattr("app.config.settings.database_path", str(database))
    with TestClient(app) as client:
        response = client.post(
            "/api/v1/controlled-query/parse",
            headers={"X-Dataset-ID": "default"},
            json={
                "question": "查询采购单位 3 的中标供应商",
                "mock_intent": {"scene": "buyer_awardees", "filters": {"buyer_id": 3}},
            },
        )
    assert response.status_code == 200
    assert response.json()["intent"]["filters"]["buyer_id"] == 3


def test_parse_endpoint_rejects_sql_without_calling_model(monkeypatch):
    def forbidden_model(*args, **kwargs):
        pytest.fail("SQL injection must be rejected before model parsing")

    monkeypatch.setattr("app.controlled_query._model_parse_intent_async", forbidden_model)
    with TestClient(app) as client:
        response = client.post(
            "/api/v1/controlled-query/parse",
            json={"question": "请执行 SELECT * FROM notices"},
        )
    assert response.status_code == 422
    assert "拒绝" in response.json()["detail"]


def test_clarification_without_guessed_id_never_executes(monkeypatch, tmp_path):
    def forbidden_query(*args, **kwargs):
        pytest.fail("an unresolved question must not execute analytics")

    monkeypatch.setattr("app.controlled_query.analytics_backend.query_analytics", forbidden_query)
    intent = parse_question(
        "查询采购中心的中标供应商",
        mock_intent={
            "scene": "buyer_awardees",
            "needs_clarification": True,
            "clarification_options": ["甲采购中心", "乙采购中心"],
        },
    )
    assert intent.filters.buyer_id is None
    result = execute_intent(tmp_path / "unused.sqlite", intent)
    assert result["status"] == "clarification_required"
    with TestClient(app) as client:
        response = client.post("/api/v1/controlled-query/execute", json={"request": intent.model_dump(mode="json")})
    assert response.status_code == 200
    assert response.json()["status"] == "clarification_required"


@pytest.mark.parametrize("ids", [[1, 1], [0, 2], [-1, 2]])
def test_shared_scenes_require_distinct_positive_ids(ids):
    with pytest.raises(ValueError):
        ControlledIntent(scene="common_projects", filters={"supplier_ids": ids})


@pytest.mark.parametrize("scene, table", [("common_projects", "bid_participations"), ("common_buyers", "awards")])
def test_common_scene_sources_exclude_unshared_notices(tmp_path, monkeypatch, scene, table):
    database = tmp_path / "sources.sqlite"
    initialize(database)
    with connect(database) as db:
        org_ids = []
        for name in ("甲供应商", "乙供应商", "共同采购人", "独有采购人"):
            org_ids.append(db.execute(
                "INSERT INTO organizations(canonical_name, normalized_name) VALUES (?, ?)",
                (name, name),
            ).lastrowid)
        supplier_a, supplier_b, common_buyer, other_buyer = org_ids
        notices = []
        for index, (buyer, suppliers) in enumerate(((common_buyer, (supplier_a, supplier_b)), (other_buyer, (supplier_a,)))):
            notice = db.execute(
                "INSERT INTO notices(project_name, source_files_json, warnings_json) VALUES (?, '[]', '[]')",
                (f"项目{index}",),
            ).lastrowid
            notices.append(notice)
            project = db.execute(
                "INSERT INTO projects(notice_id, buyer_organization_id) VALUES (?, ?)", (notice, buyer)
            ).lastrowid
            package = db.execute("INSERT INTO packages(project_id) VALUES (?)", (project,)).lastrowid
            for supplier in suppliers:
                db.execute(
                    f"INSERT INTO {table}(package_id, organization_id, raw_name) VALUES (?, ?, ?)",
                    (package, supplier, str(supplier)),
                )
    result = execute_intent(database, ControlledIntent(scene=scene, filters={"supplier_ids": [supplier_a, supplier_b]}))
    assert result["status"] == "ok"
    assert [row["id"] for row in result["source_notices"]] == notices[:1]
    monkeypatch.setattr("app.config.settings.database_path", str(database))
    with TestClient(app) as client:
        response = client.post("/api/v1/controlled-query/execute", json={"request": {
            "scene": scene, "filters": {"supplier_ids": [supplier_a, supplier_b]},
        }})
    assert response.status_code == 200
    assert [row["id"] for row in response.json()["source_notices"]] == notices[:1]
