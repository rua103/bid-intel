"""Independent scene 1/4 oracle over synthetic records, never query-derived answers."""

import os
import uuid
from copy import deepcopy
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient

from app import analytics
from app.config import settings
from app.graph import create_driver, query_neo4j, sync_to_neo4j
from app.main import app
from app.storage import connect, initialize

# Zeta wins two equal-valued awards in one project; Alpha spans two projects.
# Buyer B is common despite the suppliers never winning the same project there.
PROJECTS = [
    ("Buyer A", "P1", [
        ("1", [("Zeta", "0.10"), ("Beta", "0.10")], [("Brand Z", "0.03"), ("Brand Z", "0.03")]),
        ("2", [("Zeta", "0.10")], [("Brand Z", None)]),
        ("3", [("Alpha", "9007199254740993.01")], [("Brand A", "0.20")]),
    ]),
    ("Buyer A", "P2", [
        ("1", [("Alpha", "0.10"), ("Gamma", None)], [("Brand A", "0.20"), ("Brand Missing", None)]),
    ]),
    ("Buyer B", "P3", [("1", [("Alpha", "1.00")], [])]),
    ("Buyer B", "P4", [("1", [("Zeta", "2.00")], [])]),
    ("Buyer C", "P5", [("1", [("Gamma", None), ("Unknown", None)], [])]),
    ("Empty Buyer", "Empty", [("1", [], [])]),
]


def _total(values):
    disclosed = [Decimal(value) for value in values if value is not None]
    return str(sum(disclosed, Decimal(0))) if disclosed else None


def _fixture(path, projects=PROJECTS):
    initialize(path)
    orgs, records, items = {}, [], []
    with connect(path) as db:
        # Deliberately insert names in a different order from ranking order.
        for name in ("Buyer B", "Buyer A", "Buyer C", "Empty Buyer", "Zeta", "Beta", "Gamma", "Alpha", "Unknown"):
            orgs[name] = db.execute(
                "INSERT INTO organizations(canonical_name,normalized_name) VALUES (?,?)",
                (name, name.casefold()),
            ).lastrowid
        for buyer, number, packages in projects:
            notice = db.execute(
                "INSERT INTO notices(project_name,source_files_json,warnings_json) VALUES (?,'[]','[]')",
                (number,),
            ).lastrowid
            project = db.execute(
                "INSERT INTO projects(notice_id,project_name,project_number,buyer_organization_id) VALUES (?,?,?,?)",
                (notice, number, number, orgs[buyer]),
            ).lastrowid
            for code, awards, products in packages:
                package = db.execute(
                    "INSERT INTO packages(project_id,package_code) VALUES (?,?)", (project, code),
                ).lastrowid
                for name, value in awards:
                    award = db.execute(
                        "INSERT INTO awards(package_id,organization_id,raw_name,award_amount) VALUES (?,?,?,?)",
                        (package, orgs[name], name, value),
                    ).lastrowid
                    records.append({"id": award, "buyer": buyer, "supplier": name, "project": project, "package": package, "amount": value})
                # Unrelated participation rows must not multiply monetary values.
                for name in ("Zeta", "Alpha", "Beta", "Gamma"):
                    db.execute(
                        "INSERT INTO bid_participations(package_id,organization_id,raw_name,outcome) VALUES (?,?,?,'unknown')",
                        (package, orgs[name], name),
                    )
                for brand, total in products:
                    item = db.execute(
                        "INSERT INTO procurement_items(notice_id,package_id,product_name,brand,total_price,source_file,source_location,source_evidence,extraction_method) VALUES (?,?,'Device',?,?, 'synthetic.html','row','synthetic evidence','test')",
                        (notice, package, brand, total),
                    ).lastrowid
                    items.append({"buyer": buyer, "brand": brand, "item_id": item, "project_id": project, "package_id": package, "product_name": "Device", "total_price": total, "source_file": "synthetic.html", "source_location": "row", "source_evidence": "synthetic evidence", "amount_source": "item.total_price" if total is not None else None})
    return {"orgs": orgs, "awards": records, "items": items}


def _supplier_row(data, name, rows):
    unique = {row["id"]: row for row in rows}
    return {
        "organization_id": data["orgs"][name], "name": name,
        "award_project_count": len({row["project"] for row in unique.values()}),
        "award_package_count": len({row["package"] for row in unique.values()}),
        "award_amount_total": _total([row["amount"] for row in unique.values()]),
    }


def _rank(row):
    return -row["award_project_count"], row["name"], row["organization_id"]


def _scene1_expected(data, buyer):
    awards = [row for row in data["awards"] if row["buyer"] == buyer]
    rows = []
    for name in {row["supplier"] for row in awards}:
        supplier_awards = [row for row in awards if row["supplier"] == name]
        row = _supplier_row(data, name, supplier_awards)
        packages = {record["package"] for record in supplier_awards}
        row["product_brands"] = sorted({item["brand"] for item in data["items"] if item["package_id"] in packages})
        rows.append(row)
    products = []
    buyer_items = [item for item in data["items"] if item["buyer"] == buyer]
    for brand in {item["brand"] for item in buyer_items}:
        evidence = [{key: value for key, value in item.items() if key not in ("buyer", "brand")} for item in buyer_items if item["brand"] == brand]
        products.append({
            "name": brand, "project_count": len({item["project_id"] for item in evidence}),
            "package_count": len({item["package_id"] for item in evidence}),
            "amount_total": _total([item["total_price"] for item in evidence]), "evidence": evidence,
        })
    return {
        "buyer": {"id": data["orgs"][buyer], "canonical_name": buyer},
        "awardees": sorted(rows, key=_rank),
        "product_suppliers": sorted(products, key=lambda row: (-row["project_count"], row["name"])),
    }


def _scene4_expected(data, selected):
    buyers_by_supplier = [{row["buyer"] for row in data["awards"] if row["supplier"] == name} for name in selected]
    common = set.intersection(*buyers_by_supplier)
    buyers = []
    for buyer in sorted(common, key=data["orgs"].__getitem__):
        rows = [row for row in data["awards"] if row["buyer"] == buyer and row["supplier"] in selected]
        buyers.append({
            "buyer": {"id": data["orgs"][buyer], "canonical_name": buyer},
            "suppliers": sorted([_supplier_row(data, name, [row for row in rows if row["supplier"] == name]) for name in selected], key=_rank),
            "award_amount_total_unique_awards": _total([row["amount"] for row in {row["id"]: row for row in rows}.values()]),
        })
    return {
        "selected_suppliers": [{"id": data["orgs"][name], "canonical_name": name} for name in sorted(set(selected), key=data["orgs"].__getitem__)],
        "project_count_scope": "per_supplier_at_common_buyer", "buyers": buyers,
    }


def test_sqlite_cooperation_uses_projects_and_unique_awards(tmp_path):
    path = tmp_path / "cooperation.sqlite"
    data = _fixture(path)
    for buyer in ("Buyer A", "Buyer B", "Buyer C", "Empty Buyer"):
        assert analytics.buyer_awardees(path, data["orgs"][buyer]) == _scene1_expected(data, buyer)
    assert analytics.buyer_awardees(path, 99999) == {"buyer": None, "awardees": []}
    for selected in (("Alpha", "Zeta"), ("Alpha", "Zeta", "Beta"), ("Gamma", "Unknown"), ("Alpha", "Unknown")):
        assert analytics.common_award_buyers(path, [data["orgs"][name] for name in selected]) == _scene4_expected(data, selected)
    a = analytics.buyer_awardees(path, data["orgs"]["Buyer A"])
    by_name = {row["name"]: row for row in a["awardees"]}
    assert (by_name["Zeta"]["award_project_count"], by_name["Zeta"]["award_package_count"], by_name["Zeta"]["award_amount_total"]) == (1, 2, "0.20")
    assert by_name["Alpha"]["award_project_count"] == 2
    assert by_name["Gamma"]["award_amount_total"] is None
    assert [row["name"] for row in a["awardees"]] == ["Alpha", "Beta", "Gamma", "Zeta"]


def test_scene14_api_and_controlled_query_keep_counts_and_dataset_scope(tmp_path, monkeypatch):
    path = tmp_path / "default.sqlite"
    data = _fixture(path)
    monkeypatch.setattr(settings, "database_path", str(path))
    monkeypatch.setattr(settings, "analytics_backend", "sqlite")
    other_id = uuid.uuid4().hex
    other_path = tmp_path / "datasets" / f"{other_id}.sqlite"
    other_data = _fixture(other_path, [("Buyer A", "Isolated", [("1", [("Zeta", None), ("Alpha", None)], [])])])
    with TestClient(app) as client:
        for scope, records in (("default", data), (other_id, other_data)):
            headers = {"X-Dataset-ID": scope}
            buyer = records["orgs"]["Buyer A"]
            selected = [records["orgs"][name] for name in ("Alpha", "Zeta")]
            scene1 = client.get(f"/api/v1/analytics/buyers/{buyer}/awardees", headers=headers)
            assert scene1.status_code == 200
            assert scene1.json() == _scene1_expected(records, "Buyer A")
            scene4 = client.post("/api/v1/analytics/common-buyers", headers=headers, json={"organization_ids": selected})
            assert scene4.status_code == 200
            assert scene4.json() == _scene4_expected(records, ("Alpha", "Zeta"))
            for scene, filters, expected in (
                ("buyer_awardees", {"buyer_id": buyer}, scene1.json()),
                ("common_buyers", {"supplier_ids": selected}, scene4.json()),
            ):
                response = client.post("/api/v1/controlled-query/execute", headers=headers, json={"request": {"scene": scene, "filters": filters}})
                assert response.status_code == 200
                assert response.json()["payload"] == expected
                assert "项目" in response.json()["query_semantics"]


def test_live_neo4j_matches_independent_scene14_oracle(tmp_path):
    uri, password = os.getenv("BIDINTEL_TEST_NEO4J_URI"), os.getenv("BIDINTEL_TEST_NEO4J_PASSWORD")
    if not uri or not password:
        pytest.skip("live Neo4j credentials required")
    path, other_path = tmp_path / "main.sqlite", tmp_path / "other.sqlite"
    data = _fixture(path)
    projects = deepcopy(PROJECTS)
    projects[0][2][0][1][0] = ("Zeta", "42.00")
    other_data = _fixture(other_path, projects)
    datasets = [f"gap212-test-{uuid.uuid4().hex}" for _ in range(2)]
    driver = create_driver(uri, os.getenv("BIDINTEL_TEST_NEO4J_USER", "neo4j"), password)
    try:
        for database, records, dataset in ((path, data, datasets[0]), (other_path, other_data, datasets[1])):
            sync_to_neo4j(database, driver, dataset=dataset)
            for buyer in ("Buyer A", "Buyer B", "Buyer C", "Empty Buyer"):
                expected = _scene1_expected(records, buyer)
                actual = query_neo4j(driver, "buyer_awardees", dataset=dataset, buyer_id=records["orgs"][buyer])
                assert actual == expected == analytics.buyer_awardees(database, records["orgs"][buyer])
            assert query_neo4j(driver, "buyer_awardees", dataset=dataset, buyer_id=99999) == {"buyer": None, "awardees": []}
            for selected in (("Alpha", "Zeta"), ("Alpha", "Zeta", "Beta"), ("Gamma", "Unknown"), ("Alpha", "Unknown")):
                ids = [records["orgs"][name] for name in selected]
                expected = _scene4_expected(records, selected)
                assert query_neo4j(driver, "common_buyers", dataset=dataset, supplier_ids=ids) == expected == analytics.common_award_buyers(database, ids)
    finally:
        with driver.session() as session:
            session.run("MATCH (n:BidIntelNode) WHERE n.dataset IN $datasets DETACH DELETE n", datasets=datasets).consume()
            session.run("MATCH (d:BidIntelDataset) WHERE d.name IN $datasets DELETE d", datasets=datasets).consume()
        driver.close()
