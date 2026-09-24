import os
import uuid
from decimal import Decimal
from pathlib import Path

import pytest

from app import analytics
from app.graph import (
    CYPHER_SCENES,
    _sum_amounts,
    create_driver,
    graph_snapshot,
    query_neo4j,
    sqlite_graph,
    sync_to_neo4j,
)
from app.storage import connect, initialize


def _org(db, name):
    return db.execute(
        "INSERT INTO organizations(canonical_name,normalized_name) VALUES (?,?)",
        (name, name.casefold()),
    ).lastrowid


def test_sqlite_graph_contains_roles_and_keeps_package_edges(tmp_path: Path):
    path = tmp_path / "graph.db"
    initialize(path)
    with connect(path) as db:
        buyer = _org(db, "买方")
        supplier = _org(db, "供应商")
        db.execute("INSERT INTO notices(project_name,source_files_json,warnings_json) VALUES ('项目','[]','[]')")
        notice = db.execute("SELECT last_insert_rowid()").fetchone()[0]
        db.execute("INSERT INTO projects(notice_id,project_name,buyer_organization_id) VALUES (?,?,?)", (notice, "项目", buyer))
        project = db.execute("SELECT last_insert_rowid()").fetchone()[0]
        db.execute("INSERT INTO packages(project_id,package_code) VALUES (?,?)", (project, "包1"))
        package = db.execute("SELECT last_insert_rowid()").fetchone()[0]
        db.execute("INSERT INTO bid_participations(package_id,organization_id,raw_name,outcome) VALUES (?,?,?,?)", (package, supplier, "供应商", "winner"))
        db.execute("INSERT INTO awards(package_id,organization_id,raw_name,award_amount) VALUES (?,?,?,?)", (package, supplier, "供应商", "12.50"))
        db.execute("INSERT INTO procurement_items(notice_id,package_id,product_name,source_file,source_location,extraction_method) VALUES (?,?,?,?,?,?)", (notice, package, "设备", "x.html", "row:1", "test"))

    snapshot = graph_snapshot(path)
    assert snapshot["project_count"] == 1
    labels = {node["label"] for node in snapshot["nodes"]}
    assert labels == {"Organization", "Project", "Package", "Item"}
    assert {edge["type"] for edge in snapshot["edges"]} == {"PURCHASES", "HAS_PACKAGE", "BID_IN", "AWARDED_TO", "HAS_ITEM"}
    payload = sqlite_graph(path, limit=1)
    assert payload["backend"] == "sqlite"
    assert any(link["relation"] == "AWARDED_TO" and link["properties"]["award_amount"] == "12.50" for link in payload["links"])


def test_cypher_scenes_are_parameterized_and_cover_five_operations():
    assert set(CYPHER_SCENES) == {"buyer_awardees", "buyer_bidders", "supplier_co_bidders", "common_buyers", "common_projects"}
    for name, statement in CYPHER_SCENES.items():
        assert "$dataset" in statement, name
        assert "$" in statement, name
        assert "MATCH" in statement and "RETURN" in statement
    assert "collect(DISTINCT a)" in CYPHER_SCENES["buyer_awardees"]
    assert "all(id IN $supplier_ids" in CYPHER_SCENES["common_projects"]


def _fixture(path):
    initialize(path)
    with connect(path) as db:
        buyer = _org(db, "采购中心")
        a, b, c = (_org(db, name) for name in ("甲公司", "乙公司", "丙公司"))
        packages = []
        for index in range(2):
            notice = db.execute("INSERT INTO notices(source_files_json,warnings_json) VALUES ('[]','[]')").lastrowid
            project = db.execute("INSERT INTO projects(notice_id,project_name,buyer_organization_id) VALUES (?,?,?)", (notice, f"项目{index}", buyer)).lastrowid
            for code in (["包1", "包2"] if index == 0 else ["包1"]):
                package = db.execute("INSERT INTO packages(project_id,package_code) VALUES (?,?)", (project, code)).lastrowid
                packages.append(package)
                # Two items per package must never double award amounts.
                for product in ("设备1", "设备2"):
                    db.execute("INSERT INTO procurement_items(notice_id,package_id,product_name,brand,source_file,source_location,extraction_method) VALUES (?,?,?,'测试品牌','test.html','row','test')", (notice, package, product))
        # Project 1 has A and B in different packages; project 2 shares one
        # package and has two different awards of the same monetary value.
        for package, bidders, awards in (
            (packages[0], [(a, "winner"), (c, "nonwinner")], [(a, "0.10")]),
            (packages[1], [(b, "winner")], [(b, "0.20")]),
            (packages[2], [(a, "winner"), (b, "winner"), (c, "nonwinner")], [(a, "0.10"), (b, "0.10")]),
        ):
            for org, outcome in bidders:
                db.execute("INSERT INTO bid_participations(package_id,organization_id,raw_name,outcome) VALUES (?,?,?,?)", (package, org, str(org), outcome))
            for org, amount in awards:
                db.execute("INSERT INTO awards(package_id,organization_id,raw_name,award_amount) VALUES (?,?,?,?)", (package, org, str(org), amount))
    return buyer, a, b, c


def test_projection_preserves_multi_package_and_award_semantics(tmp_path):
    path = tmp_path / "fixture.db"
    buyer, a, b, _ = _fixture(path)
    projection = graph_snapshot(path)
    memberships = {}
    for edge in projection["edges"]:
        if edge["type"] == "BID_IN":
            memberships.setdefault(edge["target"], set()).add(edge["source"])
    joint = {k for k, bidders in memberships.items() if {f"organization:{a}", f"organization:{b}"} <= bidders}
    assert len(joint) == 1  # same project but separate packages does not qualify
    joint_awards = [edge["properties"]["award_amount"] for edge in projection["edges"] if edge["type"] == "AWARDED_TO" and edge["source"] in joint]
    assert _sum_amounts(joint_awards) == "0.20"  # do not SUM(DISTINCT amount)
    expected = analytics.common_bid_packages(path, [a, b])
    assert expected["package_count"] == 1
    assert expected["packages"][0]["award_amount_total_unique_awards"] == _sum_amounts(joint_awards)
    assert len([node for node in projection["nodes"] if node["label"] == "Item"]) == 6
    assert Decimal(analytics.buyer_awardees(path, buyer)["awardees"][0]["award_amount_total"]) in {Decimal(".2"), Decimal(".3")}
    assert _sum_amounts([None, None]) is None


def test_preview_limit_does_not_truncate_full_export(tmp_path):
    path = tmp_path / "many.db"
    _fixture(path)
    preview = sqlite_graph(path, limit=1)
    assert preview["project_count"] == 1 and preview["truncated"]
    assert preview["total_project_count"] == 2
    assert graph_snapshot(path)["project_count"] == 2
    ids = {node["id"] for node in preview["nodes"]}
    assert all(link["source"] in ids and link["target"] in ids for link in preview["links"])


class _Result:
    def consume(self):
        return self


class _Session:
    def __init__(self):
        self.calls = []
        self.transactions = 0
        self.in_transaction = False

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False

    def run(self, statement, **params):
        self.calls.append((statement, params, self.in_transaction))
        return _Result()

    def execute_write(self, function, *args):
        self.transactions += 1
        self.in_transaction = True
        try:
            return function(self, *args)
        finally:
            self.in_transaction = False


class _Driver:
    def __init__(self):
        self.current = _Session()

    def session(self, **_):
        return self.current


def test_sync_uses_one_transaction_and_scopes_every_mutation(tmp_path):
    path = tmp_path / "graph.db"
    _fixture(path)
    driver = _Driver()
    first = sync_to_neo4j(path, driver, dataset="test-data")
    second = sync_to_neo4j(path, driver, dataset="test-data")
    assert first == second
    assert driver.current.transactions == 2
    mutations = [(statement, params, inside) for statement, params, inside in driver.current.calls if not statement.startswith("CREATE CONSTRAINT")]
    assert all(inside and params["dataset"] == "test-data" for _, params, inside in mutations)
    deletes = [statement for statement, _, _ in mutations if "DELETE" in statement]
    assert deletes == ["MATCH (n:BidIntelNode {dataset:$dataset}) DETACH DELETE n"] * 2
    assert first["project_count"] == 2
    with pytest.raises(ValueError):
        sync_to_neo4j(path, driver, dataset=" ")
    with pytest.raises(ValueError):
        query_neo4j(driver, "common_projects", dataset="test-data", supplier_ids=[1, 1])


@pytest.mark.skipif(not os.getenv("BIDINTEL_TEST_NEO4J_URI"), reason="optional live Neo4j endpoint not configured")
def test_live_neo4j_all_five_scenes_match_sqlite_and_isolate_datasets(tmp_path):
    """Opt-in integration test: real Cypher, idempotency, and scope isolation."""
    path = tmp_path / "live.db"
    buyer, a, b, _ = _fixture(path)
    dataset = f"pytest-{uuid.uuid4()}"
    other = dataset + "-other"
    driver = create_driver(os.environ["BIDINTEL_TEST_NEO4J_URI"], os.getenv("BIDINTEL_TEST_NEO4J_USER", "neo4j"), os.environ["BIDINTEL_TEST_NEO4J_PASSWORD"])
    try:
        sync_to_neo4j(path, driver, dataset=other)
        initial = sync_to_neo4j(path, driver, dataset=dataset)
        assert sync_to_neo4j(path, driver, dataset=dataset) == initial
        comparisons = [
            ("buyer_awardees", {"buyer_id": buyer}, analytics.buyer_awardees(path, buyer)),
            ("buyer_bidders", {"buyer_id": buyer}, analytics.buyer_bidders(path, buyer)),
            ("buyer_bidders", {"buyer_id": buyer, "include_winners": False}, analytics.buyer_bidders(path, buyer, include_winners=False)),
            ("supplier_co_bidders", {"supplier_id": a}, analytics.supplier_co_bidders(path, a)),
            ("common_buyers", {"supplier_ids": [a, b]}, analytics.common_award_buyers(path, [a, b])),
            ("common_projects", {"supplier_ids": [a, b]}, analytics.common_bid_packages(path, [a, b])),
        ]
        for scene, params, expected in comparisons:
            assert query_neo4j(driver, scene, dataset=dataset, **params) == expected, scene
            assert query_neo4j(driver, scene, dataset=other, **params) == expected, scene
    finally:
        with driver.session() as session:
            session.run("MATCH (n:BidIntelNode) WHERE n.dataset IN $datasets DETACH DELETE n", datasets=[dataset, other]).consume()
            session.run("MATCH (n:BidIntelDataset) WHERE n.name IN $datasets DELETE n", datasets=[dataset, other]).consume()
        driver.close()
