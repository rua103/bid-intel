from __future__ import annotations

import sqlite3
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

from app.storage import connect, initialize


def _sum_decimal(values: list[str | None]) -> str | None:
    present = [value for value in values if value not in (None, "")]
    if not present:
        return None
    total = Decimal(0)
    for value in present:
        try:
            total += Decimal(str(value))
        except InvalidOperation:
            continue
    return str(total)


def _organization(connection: sqlite3.Connection, organization_id: int) -> dict[str, Any] | None:
    row = connection.execute(
        "SELECT id, canonical_name FROM organizations WHERE id = ?", (organization_id,)
    ).fetchone()
    return dict(row) if row else None


def list_organizations(
    path: Path, query: str | None = None, limit: int = 100
) -> list[dict[str, Any]]:
    initialize(path)
    with connect(path) as connection:
        if query:
            rows = connection.execute(
                """SELECT id, canonical_name FROM organizations
                   WHERE canonical_name LIKE ? ORDER BY canonical_name LIMIT ?""",
                (f"%{query}%", max(1, min(limit, 500))),
            ).fetchall()
        else:
            rows = connection.execute(
                "SELECT id, canonical_name FROM organizations ORDER BY canonical_name LIMIT ?",
                (max(1, min(limit, 500)),),
            ).fetchall()
        return [dict(row) for row in rows]


def buyer_awardees(path: Path, buyer_id: int) -> dict[str, Any]:
    initialize(path)
    with connect(path) as connection:
        buyer = _organization(connection, buyer_id)
        if not buyer:
            return {"buyer": None, "awardees": []}
        rows = connection.execute(
            """SELECT a.id AS award_id, a.organization_id, o.canonical_name, p.id AS package_id,
                      x.id AS project_id, a.award_amount
               FROM awards a
               JOIN packages p ON p.id = a.package_id
               JOIN projects x ON x.id = p.project_id
               JOIN organizations o ON o.id = a.organization_id
               WHERE x.buyer_organization_id = ?
               ORDER BY o.canonical_name, x.id, p.id""",
            (buyer_id,),
        ).fetchall()
        by_supplier: dict[int, dict[str, Any]] = {}
        package_ids: dict[int, set[int]] = {}
        project_ids: dict[int, set[int]] = {}
        seen_awards: set[int] = set()
        for row in rows:
            if row["award_id"] in seen_awards:
                continue
            seen_awards.add(row["award_id"])
            supplier_id = int(row["organization_id"])
            item = by_supplier.setdefault(
                supplier_id,
                {
                    "organization_id": supplier_id,
                    "name": row["canonical_name"],
                    "award_project_count": 0,
                    "award_package_count": 0,
                    "award_amount_total": None,
                    "product_brands": [],
                },
            )
            package_ids.setdefault(supplier_id, set()).add(int(row["package_id"]))
            project_ids.setdefault(supplier_id, set()).add(int(row["project_id"]))
            item["award_amount_total"] = _sum_decimal(
                [item["award_amount_total"], row["award_amount"]]
            )
        for supplier_id, supplier in by_supplier.items():
            pids = sorted(package_ids[supplier_id])
            supplier["award_project_count"] = len(project_ids[supplier_id])
            supplier["award_package_count"] = len(pids)
            marks = ",".join("?" for _ in pids)
            brands = connection.execute(
                f"""SELECT DISTINCT brand FROM procurement_items
                    WHERE package_id IN ({marks}) AND brand IS NOT NULL AND TRIM(brand) != ''
                    ORDER BY brand""",
                pids,
            ).fetchall()
            supplier["product_brands"] = [row["brand"] for row in brands]
        item_rows = connection.execute(
            """SELECT i.id AS item_id, i.brand, i.product_name, i.total_price,
                      i.source_file, i.source_location, i.source_evidence,
                      p.id AS package_id, x.id AS project_id
               FROM procurement_items i
               JOIN packages p ON p.id = i.package_id
               JOIN projects x ON x.id = p.project_id
               WHERE x.buyer_organization_id = ?
                 AND i.brand IS NOT NULL AND TRIM(i.brand) != ''
               ORDER BY i.brand, x.id, p.id, i.id""",
            (buyer_id,),
        ).fetchall()
        product_suppliers: dict[str, dict[str, Any]] = {}
        for row in item_rows:
            name = str(row["brand"]).strip()
            supplier = product_suppliers.setdefault(
                name,
                {
                    "name": name,
                    "project_ids": set(),
                    "package_ids": set(),
                    "amount_values": [],
                    "evidence": [],
                },
            )
            supplier["project_ids"].add(int(row["project_id"]))
            supplier["package_ids"].add(int(row["package_id"]))
            if row["total_price"] not in (None, ""):
                supplier["amount_values"].append(row["total_price"])
            supplier["evidence"].append(
                {
                    "item_id": int(row["item_id"]),
                    "project_id": int(row["project_id"]),
                    "package_id": int(row["package_id"]),
                    "product_name": row["product_name"],
                    "total_price": row["total_price"],
                    "source_file": row["source_file"],
                    "source_location": row["source_location"],
                    "source_evidence": row["source_evidence"],
                    "amount_source": "item.total_price"
                    if row["total_price"] not in (None, "")
                    else None,
                }
            )
        product_supplier_rows = []
        for supplier in sorted(
            product_suppliers.values(),
            key=lambda item: (-len(item["project_ids"]), item["name"]),
        ):
            product_supplier_rows.append(
                {
                    "name": supplier["name"],
                    "project_count": len(supplier["project_ids"]),
                    "package_count": len(supplier["package_ids"]),
                    "amount_total": _sum_decimal(supplier["amount_values"]),
                    "evidence": supplier["evidence"],
                }
            )
        awardees = sorted(
            by_supplier.values(),
            key=lambda x: (-x["award_project_count"], x["name"], x["organization_id"]),
        )
        return {
            "buyer": buyer,
            "awardees": awardees,
            "product_suppliers": product_supplier_rows,
        }


def buyer_bidders(
    path: Path, buyer_id: int, *, include_winners: bool = True, top: int = 5
) -> dict[str, Any]:
    initialize(path)
    with connect(path) as connection:
        buyer = _organization(connection, buyer_id)
        if not buyer:
            return {"buyer": None, "top_bidders": [], "co_bidder_pairs": []}
        status_clause = "" if include_winners else "AND b.outcome = 'nonwinner'"
        bidder_rows = connection.execute(
            f"""SELECT o.id, o.canonical_name, COUNT(DISTINCT x.id) AS project_count
                FROM bid_participations b
                JOIN packages p ON p.id = b.package_id
                JOIN projects x ON x.id = p.project_id
                JOIN organizations o ON o.id = b.organization_id
                WHERE x.buyer_organization_id = ? {status_clause}
                GROUP BY o.id, o.canonical_name
                ORDER BY project_count DESC, o.canonical_name LIMIT ?""",
            (buyer_id, max(1, min(top, 100))),
        ).fetchall()
        pair_rows = connection.execute(
            f"""SELECT b1.organization_id AS org1, b2.organization_id AS org2,
                       o1.canonical_name AS name1, o2.canonical_name AS name2,
                       COUNT(DISTINCT x.id) AS project_count
                FROM bid_participations b1
                JOIN bid_participations b2 ON b2.package_id = b1.package_id
                     AND b1.organization_id < b2.organization_id
                JOIN packages p ON p.id = b1.package_id
                JOIN projects x ON x.id = p.project_id
                JOIN organizations o1 ON o1.id = b1.organization_id
                JOIN organizations o2 ON o2.id = b2.organization_id
                WHERE x.buyer_organization_id = ?
                  {"" if include_winners else "AND b1.outcome = 'nonwinner' AND b2.outcome = 'nonwinner'"}
                GROUP BY b1.organization_id, b2.organization_id, o1.canonical_name, o2.canonical_name
                ORDER BY project_count DESC, name1, name2 LIMIT ?""",
            (buyer_id, max(1, min(top, 100))),
        ).fetchall()
        return {
            "buyer": buyer,
            "include_winners": include_winners,
            "top_bidders": [dict(row) for row in bidder_rows],
            "co_bidder_pairs": [dict(row) for row in pair_rows],
        }


def supplier_co_bidders(
    path: Path, supplier_id: int, *, include_winners: bool = True, top: int = 5
) -> dict[str, Any]:
    initialize(path)
    with connect(path) as connection:
        supplier = _organization(connection, supplier_id)
        if not supplier:
            return {
                "supplier": None,
                "include_winners": include_winners,
                "top_co_bidders": [],
                "packages": [],
            }
        co_bidder_rows = connection.execute(
            f"""SELECT b.organization_id, o.canonical_name, x.id AS project_id,
                      p.id AS package_id, own_award.award_amount
               FROM awards own_award
               JOIN packages p ON p.id = own_award.package_id
               JOIN projects x ON x.id = p.project_id
               JOIN bid_participations b ON b.package_id = own_award.package_id
                    AND b.organization_id != own_award.organization_id
               JOIN organizations o ON o.id = b.organization_id
               WHERE own_award.organization_id = ?
                 {"" if include_winners else "AND b.outcome = 'nonwinner'"}
               ORDER BY o.canonical_name, x.id, p.id""",
            (supplier_id,),
        ).fetchall()
        co_bidders: dict[int, dict[str, Any]] = {}
        for row in co_bidder_rows:
            bidder_id = int(row["organization_id"])
            bidder = co_bidders.setdefault(
                bidder_id,
                {
                    "organization_id": bidder_id,
                    "canonical_name": row["canonical_name"],
                    "project_ids": set(),
                    "package_ids": set(),
                    "award_amounts": [],
                },
            )
            bidder["project_ids"].add(int(row["project_id"]))
            bidder["package_ids"].add(int(row["package_id"]))
            bidder["award_amounts"].append(row["award_amount"])
        top_co_bidders = [
            {
                "organization_id": bidder["organization_id"],
                "canonical_name": bidder["canonical_name"],
                "project_count": len(bidder["project_ids"]),
                "award_package_count": len(bidder["package_ids"]),
                "selected_supplier_award_amount_total": _sum_decimal(
                    bidder["award_amounts"]
                ),
            }
            for bidder in sorted(
                co_bidders.values(),
                key=lambda item: (-len(item["project_ids"]), item["canonical_name"]),
            )[: max(1, min(top, 100))]
        ]
        package_rows = connection.execute(
            """SELECT DISTINCT p.id AS package_id, x.id AS project_id, x.project_name,
                      x.project_number, o.canonical_name AS buyer_name,
                      own_award.award_amount AS target_award_amount
               FROM awards own_award
               JOIN packages p ON p.id = own_award.package_id
               JOIN projects x ON x.id = p.project_id
               LEFT JOIN organizations o ON o.id = x.buyer_organization_id
               WHERE own_award.organization_id = ?
               ORDER BY x.id, p.id""",
            (supplier_id,),
        ).fetchall()
        packages = []
        for package in package_rows:
            bidders = connection.execute(
                """SELECT o.id AS organization_id, o.canonical_name, b.outcome
                   FROM bid_participations b JOIN organizations o ON o.id = b.organization_id
                   WHERE b.package_id = ? ORDER BY o.canonical_name""",
                (package["package_id"],),
            ).fetchall()
            packages.append({**dict(package), "participants": [dict(row) for row in bidders]})
        return {
            "supplier": supplier,
            "base": "packages where supplier has an award",
            "include_winners": include_winners,
            "top_co_bidders": top_co_bidders,
            "packages": packages,
        }


def common_award_buyers(path: Path, supplier_ids: list[int]) -> dict[str, Any]:
    supplier_ids = sorted(set(supplier_ids))
    if len(supplier_ids) < 2:
        raise ValueError("至少选择两家中标供应商")
    initialize(path)
    with connect(path) as connection:
        selected = [_organization(connection, supplier_id) for supplier_id in supplier_ids]
        if any(org is None for org in selected):
            raise ValueError("供应商 ID 不存在")
        marks = ",".join("?" for _ in supplier_ids)
        buyers = connection.execute(
            f"""SELECT x.buyer_organization_id AS buyer_id, COUNT(DISTINCT a.organization_id) AS supplier_count
                FROM awards a JOIN packages p ON p.id = a.package_id
                JOIN projects x ON x.id = p.project_id
                WHERE a.organization_id IN ({marks}) AND x.buyer_organization_id IS NOT NULL
                GROUP BY x.buyer_organization_id
                HAVING COUNT(DISTINCT a.organization_id) = ?
                ORDER BY x.buyer_organization_id""",
            [*supplier_ids, len(supplier_ids)],
        ).fetchall()
        results = []
        for buyer_row in buyers:
            buyer = _organization(connection, int(buyer_row["buyer_id"]))
            per_supplier = []
            all_award_values: list[str | None] = []
            for supplier in selected:
                supplier_id = int(supplier["id"])
                award_rows = connection.execute(
                    """SELECT a.id AS award_id, a.package_id, x.id AS project_id, a.award_amount
                       FROM awards a JOIN packages p ON p.id = a.package_id
                       JOIN projects x ON x.id = p.project_id
                       WHERE x.buyer_organization_id = ? AND a.organization_id = ?""",
                    (buyer_row["buyer_id"], supplier_id),
                ).fetchall()
                amounts = list({row["award_id"]: row["award_amount"] for row in award_rows}.values())
                all_award_values.extend(amounts)
                per_supplier.append(
                    {
                        "organization_id": supplier_id,
                        "name": supplier["canonical_name"],
                        "award_project_count": len({row["project_id"] for row in award_rows}),
                        "award_package_count": len({row["package_id"] for row in award_rows}),
                        "award_amount_total": _sum_decimal(amounts),
                    }
                )
            results.append(
                {
                    "buyer": buyer,
                    "suppliers": sorted(
                        per_supplier,
                        key=lambda row: (-row["award_project_count"], row["name"], row["organization_id"]),
                    ),
                    "award_amount_total_unique_awards": _sum_decimal(all_award_values),
                }
            )
        return {
            "selected_suppliers": selected,
            "project_count_scope": "per_supplier_at_common_buyer",
            "buyers": results,
        }


def common_bid_packages(path: Path, supplier_ids: list[int]) -> dict[str, Any]:
    supplier_ids = sorted(set(supplier_ids))
    if len(supplier_ids) < 2:
        raise ValueError("至少选择两家投标主体")
    initialize(path)
    with connect(path) as connection:
        selected = [_organization(connection, supplier_id) for supplier_id in supplier_ids]
        if any(org is None for org in selected):
            raise ValueError("主体 ID 不存在")
        marks = ",".join("?" for _ in supplier_ids)
        package_rows = connection.execute(
            f"""SELECT p.id AS package_id, p.package_code, p.package_name,
                       x.id AS project_id, x.project_name, x.project_number,
                       x.announced_total_award, p.package_award_total,
                       x.buyer_organization_id
                FROM bid_participations b
                JOIN packages p ON p.id = b.package_id
                JOIN projects x ON x.id = p.project_id
                WHERE b.organization_id IN ({marks})
                GROUP BY p.id
                HAVING COUNT(DISTINCT b.organization_id) = ?
                ORDER BY x.id, p.id""",
            [*supplier_ids, len(supplier_ids)],
        ).fetchall()
        results: list[dict[str, Any]] = []
        projects: dict[int, dict[str, Any]] = {}
        for package in package_rows:
            participants = connection.execute(
                """SELECT o.id AS organization_id, o.canonical_name, b.outcome
                   FROM bid_participations b JOIN organizations o ON o.id = b.organization_id
                   WHERE b.package_id = ? ORDER BY o.canonical_name""",
                (package["package_id"],),
            ).fetchall()
            award_rows = connection.execute(
                "SELECT id, award_amount FROM awards WHERE package_id = ? ORDER BY id",
                (package["package_id"],),
            ).fetchall()
            buyer = (
                _organization(connection, int(package["buyer_organization_id"]))
                if package["buyer_organization_id"]
                else None
            )
            project_id = int(package["project_id"])
            project = projects.setdefault(
                project_id,
                {
                    "project_id": project_id,
                    "project_name": package["project_name"],
                    "project_number": package["project_number"],
                    "buyer": buyer,
                    "package_ids": set(),
                    "awards": {},
                },
            )
            project["package_ids"].add(int(package["package_id"]))
            project["awards"].update(
                {int(row["id"]): row["award_amount"] for row in award_rows}
            )
            results.append(
                {
                    **dict(package),
                    "buyer": buyer,
                    "participants": [dict(row) for row in participants],
                    "award_amount_total_unique_awards": _sum_decimal(
                        [row["award_amount"] for row in award_rows]
                    ),
                }
            )
        project_results = [
            {
                "project_id": project["project_id"],
                "project_name": project["project_name"],
                "project_number": project["project_number"],
                "buyer": project["buyer"],
                "package_count": len(project["package_ids"]),
                "award_amount_total_unique_awards": _sum_decimal(
                    list(project["awards"].values())
                ),
            }
            for project in sorted(projects.values(), key=lambda item: item["project_id"])
        ]
        return {
            "required_entities": selected,
            "packages": results,
            "projects": project_results,
            "package_count": len(results),
            "project_count": len(project_results),
            "award_amount_total_unique_awards": _sum_decimal(
                [
                    amount
                    for project in projects.values()
                    for amount in project["awards"].values()
                ]
            ),
        }
