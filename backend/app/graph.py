"""SQLite graph projection and optional dataset-scoped Neo4j mirror.

SQLite remains the source of truth. Money is kept as decimal text even in
Neo4j; Cypher collects unique award records before Python sums their values.
The optional driver is imported only when a Neo4j operation is requested.
"""

from __future__ import annotations

from collections.abc import Mapping
from decimal import Decimal, InvalidOperation
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
        package_projects = {row["id"]: row["project_id"] for row in packages}
        for item in items:
            item["project_id"] = package_projects.get(item["package_id"])
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
      (project:Project {dataset:$dataset})-[:HAS_PACKAGE]->(k:Package {dataset:$dataset})
      -[a:AWARDED_TO]->(s:Organization {dataset:$dataset})
WITH s, collect(DISTINCT project) AS projects, collect(DISTINCT k) AS packages,
     collect(DISTINCT a) AS awards
CALL (packages) {
  UNWIND packages AS k
  OPTIONAL MATCH (k)-[:HAS_ITEM]->(i:Item)
  WITH DISTINCT i.brand AS brand WHERE brand IS NOT NULL AND trim(brand) <> ''
  RETURN collect(brand) AS product_brands
}
RETURN s.id AS organization_id, s.name AS name, size(projects) AS award_project_count,
       size(packages) AS award_package_count,
       [a IN awards | a.award_amount] AS amount_values, product_brands
ORDER BY award_project_count DESC, name, organization_id
""",
    "buyer_bidders": """
MATCH (buyer:Organization {dataset:$dataset,id:$buyer_id})
CALL (buyer) {
  MATCH (buyer)-[:PURCHASES]->(p:Project)-[:HAS_PACKAGE]->(k:Package)<-[b:BID_IN]-(o:Organization)
  WHERE $include_winners OR b.outcome = 'nonwinner'
  WITH o, count(DISTINCT p) AS project_count
  ORDER BY project_count DESC, o.name LIMIT $top
  RETURN collect({id:o.id,canonical_name:o.name,project_count:project_count}) AS top_bidders
}
CALL (buyer) {
  MATCH (buyer)-[:PURCHASES]->(p:Project)-[:HAS_PACKAGE]->(k:Package)
  MATCH (a:Organization)-[ba:BID_IN]->(k)<-[bb:BID_IN]-(b:Organization)
  WHERE a.id < b.id AND ($include_winners OR (ba.outcome = 'nonwinner' AND bb.outcome = 'nonwinner'))
  WITH a,b,count(DISTINCT p) AS project_count
  ORDER BY project_count DESC, a.name, b.name LIMIT $top
  RETURN collect({org1:a.id,org2:b.id,name1:a.name,name2:b.name,project_count:project_count}) AS co_bidder_pairs
}
RETURN top_bidders,co_bidder_pairs
""",
    "supplier_co_bidders": """
MATCH (supplier:Organization {dataset:$dataset,id:$supplier_id})
CALL (supplier) {
  MATCH (supplier)<-[award:AWARDED_TO]-(k:Package)<-[:HAS_PACKAGE]-(p:Project)
  MATCH (o:Organization)-[b:BID_IN]->(k)
  WHERE o.id <> supplier.id AND ($include_winners OR b.outcome = 'nonwinner')
  WITH o,count(DISTINCT p) AS project_count, count(DISTINCT k) AS award_package_count,
       collect(DISTINCT award) AS awards
  ORDER BY project_count DESC,o.name LIMIT $top
  RETURN collect({organization_id:o.id,canonical_name:o.name,project_count:project_count,
                  award_package_count:award_package_count,
                  amount_values:[award IN awards | award.award_amount]}) AS top_co_bidders
}
CALL (supplier) {
  MATCH (supplier)<-[a:AWARDED_TO]-(k:Package)<-[:HAS_PACKAGE]-(p:Project)
  OPTIONAL MATCH (buyer:Organization)-[:PURCHASES]->(p)
  CALL (k) {
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
MATCH (buyer:Organization {dataset:$dataset})-[:PURCHASES]->(project:Project)-[:HAS_PACKAGE]->
      (k:Package)-[a:AWARDED_TO]->(s:Organization)
WHERE s.id IN $supplier_ids
WITH buyer,s,collect(DISTINCT k) AS packages,collect(DISTINCT a) AS awards,
     collect(DISTINCT project.id) AS project_ids
ORDER BY size(project_ids) DESC, s.name, s.id
WITH buyer,collect({organization_id:s.id,name:s.name,
                   award_project_count:size(project_ids),award_package_count:size(packages),
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
CALL (k) {
  MATCH (o:Organization)-[b:BID_IN]->(k)
  WITH o,b ORDER BY o.name
  RETURN collect({organization_id:o.id,canonical_name:o.name,outcome:b.outcome}) AS participants
}
CALL (k) {
  OPTIONAL MATCH (k)-[a:AWARDED_TO]->(:Organization)
  WITH DISTINCT a ORDER BY a.id
  RETURN collect(a.award_amount) AS amount_values
}
RETURN k.id AS package_id,k.package_code AS package_code,properties(k)['package_name'] AS package_name,
       p.id AS project_id,p.project_name AS project_name,p.project_number AS project_number,
       p.announced_total_award AS announced_total_award,k.package_award_total AS package_award_total,
       buyer.id AS buyer_organization_id,
       CASE WHEN buyer IS NOT NULL THEN {id:buyer.id,canonical_name:buyer.name} END AS buyer,
       participants,amount_values
ORDER BY project_id,package_id
""",
}


# Read items independently of awards: a buyer can have brand-bearing items
# without disclosed award records. Aggregate in Python to match SQLite's
# brand.strip(), Decimal arithmetic, and original-brand evidence ordering.
CYPHER_PRODUCT_SUPPLIERS = """
MATCH (:Organization {dataset:$dataset,id:$buyer_id})-[:PURCHASES]->
      (p:Project {dataset:$dataset})-[:HAS_PACKAGE]->(k:Package {dataset:$dataset})
      -[:HAS_ITEM]->(i:Item {dataset:$dataset})
WHERE i.brand IS NOT NULL
RETURN i.brand AS brand, i.id AS item_id, p.id AS project_id, k.id AS package_id,
       i.product_name AS product_name, i.total_price AS total_price,
       i.source_file AS source_file, i.source_location AS source_location,
       i.source_evidence AS source_evidence
ORDER BY brand, project_id, package_id, item_id
"""


def create_driver(uri: str, user: str, password: str):
    try:
        from neo4j import GraphDatabase
    except ImportError as exc:
        raise RuntimeError('请安装可选依赖：pip install -e ".[graph]"') from exc
    driver = None
    try:
        driver = GraphDatabase.driver(uri, auth=(user, password))
        driver.verify_connectivity()
    except Exception as exc:
        if driver is not None:
            driver.close()
        raise RuntimeError(f"Neo4j unavailable at {uri}: {exc}") from exc
    return driver


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
    try:
        with driver.session(database=database) as session:
            session.run("CREATE CONSTRAINT bidintel_node_identity IF NOT EXISTS "
                        "FOR (n:BidIntelNode) REQUIRE (n.dataset,n.uid) IS UNIQUE").consume()
            session.run("CREATE CONSTRAINT bidintel_dataset_identity IF NOT EXISTS "
                        "FOR (d:BidIntelDataset) REQUIRE d.name IS UNIQUE").consume()
            return session.execute_write(_write_snapshot, snapshot, dataset)
    except ValueError:
        raise
    except Exception as exc:
        raise RuntimeError(f"Neo4j unavailable while exporting dataset {dataset}: {exc}") from exc


def _sum_amounts(values: list[str | None]) -> str | None:
    present = [str(value) for value in values if value not in (None, "")]
    total = Decimal(0)
    for value in present:
        try:
            total += Decimal(value)
        except InvalidOperation:
            continue
    return str(total) if present else None


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
        if primary is None:
            return {"buyer": None, "awardees": []}
        product_supplier_payload = [
            record.data()
            for record in tx.run(CYPHER_PRODUCT_SUPPLIERS, **params)
        ]
        for row in rows:
            row["award_amount_total"] = _sum_amounts(row.pop("amount_values"))
            row["product_brands"] = sorted(row["product_brands"])
        suppliers: dict[str, dict[str, Any]] = {}
        for item in product_supplier_payload:
            raw_name = str(item.pop("brand"))
            if not raw_name.strip(" "):  # Match SQLite TRIM before Python strip().
                continue
            name = raw_name.strip()
            supplier = suppliers.setdefault(name, {
                "name": name, "project_ids": set(), "package_ids": set(),
                "amount_values": [], "evidence": [],
            })
            supplier["project_ids"].add(item["project_id"])
            supplier["package_ids"].add(item["package_id"])
            supplier["amount_values"].append(item["total_price"])
            item["amount_source"] = (
                "item.total_price" if item["total_price"] not in (None, "") else None
            )
            supplier["evidence"].append(item)
        product_rows = [
            {
                "name": supplier["name"],
                "project_count": len(supplier["project_ids"]),
                "package_count": len(supplier["package_ids"]),
                "amount_total": _sum_amounts(supplier["amount_values"]),
                "evidence": supplier["evidence"],
            }
            for supplier in suppliers.values()
        ]
        return {"buyer": primary, "awardees": rows, "product_suppliers": sorted(
            product_rows, key=lambda item: (-item["project_count"], item["name"]))}
    if scene == "buyer_bidders":
        return {"buyer": primary, "include_winners": params["include_winners"],
                **(rows[0] if rows else {"top_bidders": [], "co_bidder_pairs": []})}
    if scene == "supplier_co_bidders":
        result = rows[0] if rows else {"top_co_bidders": [], "packages": []}
        for bidder in result["top_co_bidders"]:
            bidder["selected_supplier_award_amount_total"] = _sum_amounts(
                bidder.pop("amount_values", [])
            )
        return {"supplier": primary, "base": "packages where supplier has an award",
                "include_winners": params["include_winners"], **result}
    if scene == "common_buyers":
        for row in rows:
            all_amounts = []
            for supplier in row["suppliers"]:
                values = supplier.pop("amount_values")
                all_amounts.extend(values)
                supplier["award_amount_total"] = _sum_amounts(values)
            row["award_amount_total_unique_awards"] = _sum_amounts(all_amounts)
        return {
            "selected_suppliers": selected,
            "project_count_scope": "per_supplier_at_common_buyer",
            "buyers": rows,
        }
    projects: dict[int, dict[str, Any]] = {}
    all_amounts: list[str | None] = []
    for row in rows:
        amounts = row.pop("amount_values")
        row["award_amount_total_unique_awards"] = _sum_amounts(amounts)
        all_amounts.extend(amounts)
        project = projects.setdefault(
            int(row["project_id"]),
            {
                "project_id": row["project_id"],
                "project_name": row["project_name"],
                "project_number": row["project_number"],
                "buyer": row["buyer"],
                "package_count": 0,
                "amounts": [],
            },
        )
        project["package_count"] += 1
        project["amounts"].extend(amounts)
    project_rows = [
        {
            **{key: project[key] for key in (
                "project_id", "project_name", "project_number", "buyer", "package_count"
            )},
            "award_amount_total_unique_awards": _sum_amounts(project["amounts"]),
        }
        for project in sorted(projects.values(), key=lambda value: value["project_id"])
    ]
    return {"required_entities": selected, "packages": rows, "projects": project_rows,
            "package_count": len(rows), "project_count": len(project_rows),
            "award_amount_total_unique_awards": _sum_amounts(all_amounts)}


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
    try:
        with driver.session(database=database) as session:
            return session.execute_read(_read_scene, scene, params)
    except ValueError:
        raise
    except Exception as exc:
        raise RuntimeError(f"Neo4j unavailable while querying scene {scene}: {exc}") from exc
