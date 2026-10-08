from pathlib import Path

from fastapi.testclient import TestClient

from app.main import app
from app.relation_clues import query_relation_clues
from app.storage import connect, initialize


def _org(db, name):
    return db.execute(
        "INSERT INTO organizations(canonical_name,normalized_name) VALUES (?,?)",
        (name, name.casefold()),
    ).lastrowid


def _fixture(path: Path):
    initialize(path)
    with connect(path) as db:
        buyer = _org(db, "甲市采购中心")
        buyer2 = _org(db, "乙市采购中心")
        a = _org(db, "供应商甲")
        b = _org(db, "供应商乙")
        c = _org(db, "供应商丙")
        projects = []
        packages = []
        for index, purchaser in enumerate((buyer, buyer, buyer2), 1):
            notice = db.execute(
                "INSERT INTO notices(project_name, procurement_unit, source_files_json, warnings_json, created_at) VALUES (?,?,?,?,?)",
                (
                    f"项目{index}",
                    "甲市采购中心" if purchaser == buyer else "乙市采购中心",
                    '["公告.html"]',
                    '["附件解析失败：scan.pdf"]' if index == 2 else '[]',
                    f"2026-01-0{index} 00:00:00",
                ),
            ).lastrowid
            project = db.execute(
                "INSERT INTO projects(notice_id,project_name,project_number,buyer_organization_id) VALUES (?,?,?,?)",
                (notice, f"项目{index}", f"P-{index}", purchaser),
            ).lastrowid
            code = "default" if index == 3 else "1"
            package = db.execute(
                "INSERT INTO packages(project_id,package_code) VALUES (?,?)",
                (project, code),
            ).lastrowid
            projects.append(project)
            packages.append(package)
        # A and B jointly appear in projects 1 and 2, giving repeat cooperation.
        for package, rows in (
            (packages[0], ((a, "nonwinner", "a.html"), (b, "nonwinner", "b.html"), (c, "winner", "c.html"))),
            (packages[1], ((a, "nonwinner", "a2.html"), (b, "nonwinner", "b2.html"))),
            (packages[2], ((a, "nonwinner", "a3.html"), (c, "unknown", None))),
        ):
            for organization, outcome, source in rows:
                db.execute(
                    "INSERT INTO bid_participations(package_id,organization_id,raw_name,outcome,source_file,source_location,source_evidence) VALUES (?,?,?,?,?,?,?)",
                    (package, organization, str(organization), outcome, source, "row:1" if source else None, "原文证据" if source else None),
                )
        db.execute(
            "INSERT INTO awards(package_id,organization_id,raw_name,award_amount,source_file,source_location,source_evidence) VALUES (?,?,?,?,?,?,?)",
            (packages[0], c, "供应商丙", "10", "c.html", "row:2", "中标证据"),
        )
        db.execute(
            "INSERT INTO awards(package_id,organization_id,raw_name,award_amount,source_file,source_location,source_evidence) VALUES (?,?,?,?,?,?,?)",
            (packages[1], b, "供应商乙", "20", "b2.html", "row:2", "中标证据"),
        )
    return buyer, buyer2, a, b, c


def test_relation_clues_cover_pairs_repeat_distribution_network_and_evidence(tmp_path: Path):
    path = tmp_path / "relations.db"
    buyer, _, a, _b, _ = _fixture(path)
    payload = query_relation_clues(path, kind="all")
    assert payload["backend"] == "sqlite"
    assert set(payload["counts"]) == {
        "common_bidding",
        "repeat_cooperation",
        "supplier_distribution",
        "buyer_network",
    }
    repeat = payload["definitions"]["repeat_cooperation"]
    assert "unknown" in repeat["scope"]["participation_outcomes"]
    pair = next(row for row in payload["clues"] if row["type"] == "repeat_cooperation")
    assert pair["metrics"]["project_count"] == 2
    assert pair["notices"] and pair["projects"] and pair["packages"]
    assert pair["time_range"]["start"] == "2026-01-01 00:00:00"
    assert all("待核查线索" == pair["label"] for pair in payload["clues"])
    network = query_relation_clues(path, kind="buyer-network", buyer_id=buyer)
    assert network["counts"]["buyer_network"] >= 1
    distribution = query_relation_clues(path, kind="supplier-distribution", supplier_id=a)
    assert distribution["clues"][0]["metrics"]["participation_project_count"] == 3
    assert any("default" in warning for clue in payload["clues"] for warning in clue["warnings"])
    assert any("附件解析" in warning for clue in payload["clues"] for warning in clue["warnings"])
    assert any(link["notice_url"].startswith("/api/v1/notices/") for clue in payload["clues"] for link in clue["evidence_links"])
    assert all(word not in str(payload) for word in ("围标", "串标", "违法"))


def test_relation_clues_api_preserves_dataset_isolation_and_empty_result(tmp_path, monkeypatch):
    default = tmp_path / "default.db"
    _, _, _, _, _ = _fixture(default)
    monkeypatch.setattr("app.config.settings.database_path", str(default))
    with TestClient(app) as client:
        response = client.get("/api/v1/analytics/relation-clues", params={"kind": "repeat-cooperation"})
        assert response.status_code == 200
        assert response.json()["counts"]["repeat_cooperation"] == 2
        empty = client.get("/api/v1/analytics/relation-clues", params={"kind": "common-bidding", "supplier_id": 99999})
        assert empty.status_code == 200
        assert empty.json()["clues"] == []
        forbidden = client.get(
            "/api/v1/analytics/relation-clues",
            headers={"X-Dataset-ID": "f" * 32},
        )
        assert forbidden.status_code == 404


def test_supplier_filter_keeps_other_endpoint_and_repeat_counts(tmp_path):
    path = tmp_path / "relations.db"
    _, _, a, b, _ = _fixture(path)
    for kind in ("common_bidding", "repeat_cooperation"):
        filtered = query_relation_clues(path, kind=kind, supplier_id=a)
        assert len(filtered["clues"]) == 2
        clue = filtered["clues"][0]
        assert {entity["organization_id"] for entity in clue["entities"]} == {a, b}
        assert clue["metrics"]["project_count"] == 2
        assert {link["organization_id"] for link in clue["evidence_links"]} == {a, b}
