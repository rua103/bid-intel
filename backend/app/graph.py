"""SQLite graph projection and optional dataset-scoped Neo4j mirror.

SQLite remains the source of truth. Money is kept as decimal text even in
Neo4j; Cypher collects unique award records before Python sums their values.
The optional driver is imported only when a Neo4j operation is requested.
"""

from __future__ import annotations

from collections.abc import Mapping
from decimal import Decimal
from pathlib import Path
from typing import Any

from app.storage import connect, initialize

NODE_LABELS = {"Organization", "Project", "Package", "Item"}
EDGE_TYPES = {"PURCHASES", "HAS_PACKAGE", "BID_IN", "AWARDED_TO", "HAS_ITEM"}
CATEGORY_LABELS = {"Organization": "organization", "Project": "project", "Package": "package", "Item": "item"}


def _uid(label: str, value: int) -> str:
    return f"{label.lower()}:{value}"


def graph_snapshot(path: Path, *, project_limit: int | None = None) -> dict[str, Any]:
    """Read one consistent SQLite snapshot, retaining all edges of selected projects.

    An unlimited snapshot is used for Neo4j sync. The browser preview selects
    recent projects; limits never silently truncate the full Neo4j export.
    """
    initialize(path)
    with connect(path) as db:
        db.execute("BEGIN")
        total = int(db.execute("SELECT COUNT(*) FROM projects").fetchone()[0])
        statement = "SELECT * FROM projects ORDER BY id DESC"
        values: list[Any] = []
        if project_limit is not None:
            statement += " LIMIT ?"
            values = [max(1, min(int(project_limit), 200))]
        projects = [dict(row) for row in db.execute(statement, values)]
        project_ids = {row["id"] for row in projects}
        packages = [dict(row) for row in db.execute("SELECT * FROM packages ORDER BY id") if row["project_id"] in project_ids]
        package_ids = {row["id"] for row in packages}
        bids = [dict(row) for row in db.execute("SELECT * FROM bid_participations ORDER BY id") if row["package_id"] in package_ids]
        awards = [dict(row) for row in db.execute("SELECT * FROM awards ORDER BY id") if row["package_id"] in package_ids]
        items = [dict(row) for row in db.execute("SELECT * FROM procurement_items ORDER BY id") if row["package_id"] in package_ids]
        org_ids = {row["buyer_organization_id"] for row in projects if row["buyer_organization_id"] is not None}
        org_ids.update(row["organization_id"] for row in bids + awards)
        organizations = [dict(row) for row in db.execute("SELECT * FROM organizations ORDER BY id") if row["id"] in org_ids]

    nodes: list[dict[str, Any]] = []
    edges: list[dict[str, Any]] = []

    def node(label: str, row: dict[str, Any], name: str) -> None:
        properties = {key: value for key, value in row.items() if value is not None}
        properties["name"] = name
        nodes.append({"uid": _uid(label, row["id"]), "label": label, "properties": properties})

    def edge(source: str, target: str, kind: str, properties: dict[str, Any] | None = None) -> None:
        edges.append({"source": source, "target": target, "type": kind,
                      "properties": {key: value for key, value in (properties or {}).items() if value is not None}})

    for row in organizations:
        node("Organization", row, row["canonical_name"])
    for row in projects:
        node("Project", row, row["project_name"] or row["project_number"] or f"项目 {row['id']}")
        if row["buyer_organization_id"] is not None:
            edge(_uid("Organization", row["buyer_organization_id"]), _uid("Project", row["id"]), "PURCHASES")
    for row in packages:
        node("Package", row, row["package_name"] or row["package_code"])
        edge(_uid("Project", row["project_id"]), _uid("Package", row["id"]), "HAS_PACKAGE")
    for row in bids:
        edge(_uid("Organization", row["organization_id"]), _uid("Package", row["package_id"]), "BID_IN", row)
    for row in awards:
        edge(_uid("Package", row["package_id"]), _uid("Organization", row["organization_id"]), "AWARDED_TO", row)
    for row in items:
        node("Item", row, row["product_name"] or f"标的 {row['id']}")
        edge(_uid("Package", row["package_id"]), _uid("Item", row["id"]), "HAS_ITEM")
    return {"nodes": nodes, "edges": edges, "project_count": len(projects),
            "total_project_count": total, "truncated": total > len(projects)}


def sqlite_graph(path: Path, *, limit: int = 100) -> dict[str, Any]:
    snapshot = graph_snapshot(path, project_limit=limit)
    return {
        "backend": "sqlite", "dataset": path.name,
        "nodes": [{"id": n["uid"], "name": n["properties"]["name"],
                   "category": CATEGORY_LABELS[n["label"]], "properties": n["properties"]}
                  for n in snapshot["nodes"]],
        "links": [{"id": f"{e['source']}:{e['type']}:{e['target']}",
                   "source": e["source"], "target": e["target"],
                   "relation": e["type"], "properties": e["properties"]}
                  for e in snapshot["edges"]],
        "categories": list(CATEGORY_LABELS.values()),
        **{key: snapshot[key] for key in ("project_count", "total_project_count", "truncated")},
    }


# Each statement is parameterized. Dataset-scoped nodes are separate even when
# SQLite IDs overlap between datasets. Award/item fan-out is deliberately kept
# in independent subqueries. Monetary aggregation stays decimal-exact in Python.
CYPHER_SCENES = {
    "buyer_awardees": """
MATCH (:Organization {dataset:$dataset,id:$buyer_id})-[:PURCHASES]->
      (:Project {dataset:$dataset})-[:HAS_PACKAGE]->(k:Package {dataset:$dataset})
      -[a:AWARDED_TO]->(s:Organization {dataset:$dataset})
WITH s, collect(DISTINCT k) AS packages, collect(DISTINCT a) AS awards
CALL {
  WITH packages
  UNWIND packages AS k
  OPTIONAL MATCH (k)-[:HAS_ITEM]->(i:Item)
  WITH DISTINCT i.brand AS brand WHERE brand IS NOT NULL AND trim(brand) <> ''
  RETURN collect(brand) AS product_brands
}
RETURN s.id AS organization_id, s.name AS name, size(packages) AS award_package_count,
       [a IN awards | a.award_amount] AS amount_values, product_brands
ORDER BY award_package_count DESC, name
""",
    "buyer_bidders": """
MATCH (buyer:Organization {dataset:$dataset,id:$buyer_id})
CALL {
  WITH buyer
  MATCH (buyer)-[:PURCHASES]->(:Project)-[:HAS_PACKAGE]->(k:Package)<-[b:BID_IN]-(o:Organization)
  WHERE $include_winners OR b.outcome <> 'winner'
  WITH o, count(DISTINCT k) AS package_count
  ORDER BY package_count DESC, o.name LIMIT $top
  RETURN collect({id:o.id,canonical_name:o.name,package_count:package_count}) AS top_bidders
}
CALL {
  WITH buyer
  MATCH (buyer)-[:PURCHASES]->(:Project)-[:HAS_PACKAGE]->(k:Package)
  MATCH (a:Organization)-[ba:BID_IN]->(k)<-[bb:BID_IN]-(b:Organization)
  WHERE a.id < b.id AND ($include_winners OR (ba.outcome <> 'winner' AND bb.outcome <> 'winner'))
  WITH a,b,count(DISTINCT k) AS package_count
  ORDER BY package_count DESC, a.name, b.name LIMIT $top
  RETURN collect({org1:a.id,org2:b.id,name1:a.name,name2:b.name,package_count:package_count}) AS co_bidder_pairs
}
RETURN top_bidders,co_bidder_pairs
""",
    "supplier_co_bidders": """
MATCH (supplier:Organization {dataset:$dataset,id:$supplier_id})
CALL {
  WITH supplier
  MATCH (supplier)<-[:AWARDED_TO]-(k:Package)<-[:BID_IN]-(o:Organization)
  WHERE o.id <> supplier.id
  WITH o,count(DISTINCT k) AS package_count
  ORDER BY package_count DESC,o.name LIMIT $top
  RETURN collect({organization_id:o.id,canonical_name:o.name,package_count:package_count}) AS top_co_bidders
}
CALL {
  WITH supplier
  MATCH (supplier)<-[a:AWARDED_TO]-(k:Package)<-[:HAS_PACKAGE]-(p:Project)
  OPTIONAL MATCH (buyer:Organization)-[:PURCHASES]->(p)
  CALL {
    WITH k
    OPTIONAL MATCH (o:Organization)-[b:BID_IN]->(k)
    WITH o,b ORDER BY o.name
    RETURN collect(CASE WHEN o IS NOT NULL THEN {organization_id:o.id,canonical_name:o.name,outcome:b.outcome} END) AS participants
  }
  WITH k,p,buyer,a,participants ORDER BY p.id,k.id
  RETURN collect({package_id:k.id,project_id:p.id,project_name:p.project_name,
    project_number:p.project_number,buyer_name:buyer.name,target_award_amount:a.award_amount,
    participants:participants}) AS packages
}
RETURN top_co_bidders,packages
""",
    "common_buyers": """
MATCH (buyer:Organization {dataset:$dataset})-[:PURCHASES]->(:Project)-[:HAS_PACKAGE]->
      (k:Package)-[a:AWARDED_TO]->(s:Organization)
WHERE s.id IN $supplier_ids
WITH buyer,s,collect(DISTINCT k) AS packages,collect(DISTINCT a) AS awards
ORDER BY s.id
WITH buyer,collect({organization_id:s.id,name:s.name,award_package_count:size(packages),
                   amount_values:[a IN awards | a.award_amount]}) AS suppliers,
           collect(s.id) AS seen
WHERE all(id IN $supplier_ids WHERE id IN seen)
RETURN {id:buyer.id,canonical_name:buyer.name} AS buyer,suppliers
ORDER BY buyer.id
""",
    "common_projects": """
MATCH (s:Organization {dataset:$dataset})-[:BID_IN]->(k:Package {dataset:$dataset})
WHERE s.id IN $supplier_ids
WITH k,collect(DISTINCT s.id) AS seen
WHERE all(id IN $supplier_ids WHERE id IN seen)
MATCH (p:Project)-[:HAS_PACKAGE]->(k)
OPTIONAL MATCH (buyer:Organization)-[:PURCHASES]->(p)
CALL {
  WITH k
  MATCH (o:Organization)-[b:BID_IN]->(k)
  WITH o,b ORDER BY o.name
  RETURN collect({organization_id:o.id,canonical_name:o.name,outcome:b.outcome}) AS participants
}
CALL {
  WITH k
  OPTIONAL MATCH (k)-[a:AWARDED_TO]->(:Organization)
  WITH DISTINCT a ORDER BY a.id
  RETURN collect(a.award_amount) AS amount_values
}
RETURN k.id AS package_id,k.package_code AS package_code,k.package_name AS package_name,
       p.id AS project_id,p.project_name AS project_name,p.project_number AS project_number,
       p.announced_total_award AS announced_total_award,k.package_award_total AS package_award_total,
       buyer.id AS buyer_organization_id,
       CASE WHEN buyer IS NOT NULL THEN {id:buyer.id,canonical_name:buyer.name} END AS buyer,
       participants,amount_values
ORDER BY project_id,package_id
""",
}


def create_driver(uri: str, user: str, password: str):
    try:
        from neo4j import GraphDatabase
    except ImportError as exc:
        raise RuntimeError('请安装可选依赖：pip install -e ".[graph]"') from exc
    return GraphDatabase.driver(uri, auth=(user, password))


def _validate_dataset(dataset: str) -> str:
    if not isinstance(dataset, str) or not dataset.strip():
        raise ValueError("dataset 必须是非空的稳定名称")
    return dataset.strip()


def _write_snapshot(tx: Any, snapshot: dict[str, Any], dataset: str) -> dict[str, Any]:
    # Updating the registry node acquires a per-dataset write lock. Replacing
    # nodes and edges is one transaction: failed writes restore the old graph.
    tx.run("MERGE (d:BidIntelDataset {name:$dataset}) SET d.updated_at=datetime()", dataset=dataset).consume()
    tx.run("MATCH (n:BidIntelNode {dataset:$dataset}) DETACH DELETE n", dataset=dataset).consume()
    for label in sorted(NODE_LABELS):
        rows = [{"uid": n["uid"], "properties": n["properties"]} for n in snapshot["nodes"] if n["label"] == label]
        if rows:
            tx.run(
                f"UNWIND $rows AS row MERGE (n:BidIntelNode:{label} {{dataset:$dataset,uid:row.uid}}) "
                "SET n += row.properties", dataset=dataset, rows=rows,
            ).consume()
    for kind in sorted(EDGE_TYPES):
        rows = [e for e in snapshot["edges"] if e["type"] == kind]
        if rows:
            tx.run(
                "UNWIND $rows AS row "
                "MATCH (a:BidIntelNode {dataset:$dataset,uid:row.source}) "
                "MATCH (b:BidIntelNode {dataset:$dataset,uid:row.target}) "
                f"MERGE (a)-[r:{kind}]->(b) SET r += row.properties,r.dataset=$dataset",
                dataset=dataset, rows=rows,
            ).consume()
    return {"backend": "neo4j", "dataset": dataset, "nodes": len(snapshot["nodes"]),
            "relationships": len(snapshot["edges"]), "project_count": snapshot["project_count"]}


def sync_to_neo4j(path: Path, driver: Any, *, dataset: str, database: str = "neo4j") -> dict[str, Any]:
    dataset = _validate_dataset(dataset)
    if not path.is_file():
        raise ValueError(f"SQLite 数据库不存在：{path}")
    snapshot = graph_snapshot(path)
    with driver.session(database=database) as session:
        session.run("CREATE CONSTRAINT bidintel_node_identity IF NOT EXISTS "
                    "FOR (n:BidIntelNode) REQUIRE (n.dataset,n.uid) IS UNIQUE").consume()
        session.run("CREATE CONSTRAINT bidintel_dataset_identity IF NOT EXISTS "
                    "FOR (d:BidIntelDataset) REQUIRE d.name IS UNIQUE").consume()
        return session.execute_write(_write_snapshot, snapshot, dataset)


def _sum_amounts(values: list[str | None]) -> str | None:
    present = [str(value) for value in values if value not in (None, "")]
    return str(sum((Decimal(value) for value in present), Decimal(0))) if present else None


def _organization(tx: Any, dataset: str, organization_id: int) -> dict[str, Any] | None:
    record = tx.run("MATCH (o:Organization {dataset:$dataset,id:$id}) "
                    "RETURN o.id AS id,o.name AS canonical_name", dataset=dataset, id=organization_id).single()
    return record.data() if record else None


def _read_scene(tx: Any, scene: str, parameters: Mapping[str, Any]) -> dict[str, Any]:
    params = dict(parameters)
    selected: list[dict[str, Any]] = []
    primary = None
    if scene.startswith("buyer_"):
        primary = _organization(tx, params["dataset"], params["buyer_id"])
    elif scene == "supplier_co_bidders":
        primary = _organization(tx, params["dataset"], params["supplier_id"])
    else:
        for organization_id in params["supplier_ids"]:
            org = _organization(tx, params["dataset"], organization_id)
            if org is None:
                raise ValueError("主体 ID 不存在")
            selected.append(org)
    rows = [record.data() for record in tx.run(CYPHER_SCENES[scene], **params)]
    if scene == "buyer_awardees":
        for row in rows:
            row["award_amount_total"] = _sum_amounts(row.pop("amount_values"))
            row["product_brands"] = sorted(row["product_brands"])
        return {"buyer": primary, "awardees": rows}
    if scene == "buyer_bidders":
        return {"buyer": primary, "include_winners": params["include_winners"],
                **(rows[0] if rows else {"top_bidders": [], "co_bidder_pairs": []})}
    if scene == "supplier_co_bidders":
        return {"supplier": primary, "base": "packages where supplier has an award",
                **(rows[0] if rows else {"top_co_bidders": [], "packages": []})}
    if scene == "common_buyers":
        for row in rows:
            all_amounts = []
            for supplier in row["suppliers"]:
                values = supplier.pop("amount_values")
                all_amounts.extend(values)
                supplier["award_amount_total"] = _sum_amounts(values)
            row["award_amount_total_unique_awards"] = _sum_amounts(all_amounts)
        return {"selected_suppliers": selected, "buyers": rows}
    for row in rows:
        row["award_amount_total_unique_awards"] = _sum_amounts(row.pop("amount_values"))
    return {"required_entities": selected, "packages": rows, "package_count": len(rows),
            "project_count": len({row["project_id"] for row in rows})}


def query_neo4j(driver: Any, scene: str, *, dataset: str, database: str = "neo4j",
                buyer_id: int | None = None, supplier_id: int | None = None,
                supplier_ids: list[int] | None = None, top: int = 5,
                include_winners: bool = True) -> dict[str, Any]:
    if scene not in CYPHER_SCENES:
        raise ValueError(f"未知场景：{scene}")
    params = {"dataset": _validate_dataset(dataset), "buyer_id": buyer_id,
              "supplier_id": supplier_id, "supplier_ids": sorted(set(supplier_ids or [])),
              "top": max(1, min(int(top), 100)), "include_winners": bool(include_winners)}
    if scene.startswith("buyer_") and buyer_id is None:
        raise ValueError("该场景需要 buyer_id")
    if scene == "supplier_co_bidders" and supplier_id is None:
        raise ValueError("该场景需要 supplier_id")
    if scene.startswith("common_") and len(params["supplier_ids"]) < 2:
        raise ValueError("至少选择两家不同主体")
    with driver.session(database=database) as session:
        return session.execute_read(_read_scene, scene, params)
