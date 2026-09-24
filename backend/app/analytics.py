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
            """SELECT a.organization_id, o.canonical_name, p.id AS package_id,
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
        for row in rows:
            supplier_id = int(row["organization_id"])
            item = by_supplier.setdefault(
                supplier_id,
                {
                    "organization_id": supplier_id,
                    "name": row["canonical_name"],
                    "award_package_count": 0,
                    "award_amount_total": None,
                    "product_brands": [],
                },
            )
            package_ids.setdefault(supplier_id, set()).add(int(row["package_id"]))
            item["award_amount_total"] = _sum_decimal(
                [item["award_amount_total"], row["award_amount"]]
            )
        for supplier_id, supplier in by_supplier.items():
            pids = sorted(package_ids[supplier_id])
            supplier["award_package_count"] = len(pids)
            marks = ",".join("?" for _ in pids)
            brands = connection.execute(
                f"""SELECT DISTINCT brand FROM procurement_items
                    WHERE package_id IN ({marks}) AND brand IS NOT NULL AND TRIM(brand) != ''
                    ORDER BY brand""",
                pids,
            ).fetchall()
            supplier["product_brands"] = [row["brand"] for row in brands]
        awardees = sorted(
            by_supplier.values(), key=lambda x: (-x["award_package_count"], x["name"])
        )
        return {"buyer": buyer, "awardees": awardees}


def buyer_bidders(
    path: Path, buyer_id: int, *, include_winners: bool = True, top: int = 5
) -> dict[str, Any]:
    initialize(path)
    with connect(path) as connection:
        buyer = _organization(connection, buyer_id)
        if not buyer:
            return {"buyer": None, "top_bidders": [], "co_bidder_pairs": []}
        status_clause = "" if include_winners else "AND b.outcome != 'winner'"
        bidder_rows = connection.execute(
            f"""SELECT o.id, o.canonical_name, COUNT(DISTINCT b.package_id) AS package_count
                FROM bid_participations b
                JOIN packages p ON p.id = b.package_id
                JOIN projects x ON x.id = p.project_id
                JOIN organizations o ON o.id = b.organization_id
                WHERE x.buyer_organization_id = ? {status_clause}
                GROUP BY o.id, o.canonical_name
                ORDER BY package_count DESC, o.canonical_name LIMIT ?""",
            (buyer_id, max(1, min(top, 100))),
        ).fetchall()
        pair_rows = connection.execute(
            f"""SELECT b1.organization_id AS org1, b2.organization_id AS org2,
                       o1.canonical_name AS name1, o2.canonical_name AS name2,
                       COUNT(DISTINCT b1.package_id) AS package_count
                FROM bid_participations b1
                JOIN bid_participations b2 ON b2.package_id = b1.package_id
                     AND b1.organization_id < b2.organization_id
                JOIN packages p ON p.id = b1.package_id
                JOIN projects x ON x.id = p.project_id
                JOIN organizations o1 ON o1.id = b1.organization_id
                JOIN organizations o2 ON o2.id = b2.organization_id
                WHERE x.buyer_organization_id = ?
                  {"" if include_winners else "AND b1.outcome != 'winner' AND b2.outcome != 'winner'"}
                GROUP BY b1.organization_id, b2.organization_id, o1.canonical_name, o2.canonical_name
                ORDER BY package_count DESC, name1, name2 LIMIT ?""",
            (buyer_id, max(1, min(top, 100))),
        ).fetchall()
        return {
            "buyer": buyer,
            "include_winners": include_winners,
            "top_bidders": [dict(row) for row in bidder_rows],
            "co_bidder_pairs": [dict(row) for row in pair_rows],
        }


def supplier_co_bidders(path: Path, supplier_id: int, *, top: int = 5) -> dict[str, Any]:
    initialize(path)
    with connect(path) as connection:
        supplier = _organization(connection, supplier_id)
        if not supplier:
            return {"supplier": None, "top_co_bidders": [], "packages": []}
        rows = connection.execute(
            """SELECT b.organization_id, o.canonical_name, COUNT(DISTINCT b.package_id) AS package_count
               FROM awards own_award
               JOIN bid_participations b ON b.package_id = own_award.package_id
                    AND b.organization_id != own_award.organization_id
               JOIN organizations o ON o.id = b.organization_id
               WHERE own_award.organization_id = ?
               GROUP BY b.organization_id, o.canonical_name
               ORDER BY package_count DESC, o.canonical_name LIMIT ?""",
            (supplier_id, max(1, min(top, 100))),
        ).fetchall()
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
            "top_co_bidders": [dict(row) for row in rows],
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
                    """SELECT DISTINCT a.package_id, a.award_amount
                       FROM awards a JOIN packages p ON p.id = a.package_id
                       JOIN projects x ON x.id = p.project_id
                       WHERE x.buyer_organization_id = ? AND a.organization_id = ?""",
                    (buyer_row["buyer_id"], supplier_id),
                ).fetchall()
                amounts = [row["award_amount"] for row in award_rows]
                all_award_values.extend(amounts)
                per_supplier.append(
                    {
                        "organization_id": supplier_id,
                        "name": supplier["canonical_name"],
                        "award_package_count": len({row["package_id"] for row in award_rows}),
                        "award_amount_total": _sum_decimal(amounts),
                    }
                )
            results.append(
                {
                    "buyer": buyer,
                    "suppliers": per_supplier,
                    "award_amount_total_unique_awards": _sum_decimal(all_award_values),
                }
            )
        return {"selected_suppliers": selected, "buyers": results}


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
        for package in package_rows:
            participants = connection.execute(
                """SELECT o.id AS organization_id, o.canonical_name, b.outcome
                   FROM bid_participations b JOIN organizations o ON o.id = b.organization_id
                   WHERE b.package_id = ? ORDER BY o.canonical_name""",
                (package["package_id"],),
            ).fetchall()
            award_rows = connection.execute(
                "SELECT award_amount FROM awards WHERE package_id = ? ORDER BY id",
                (package["package_id"],),
            ).fetchall()
            buyer = (
                _organization(connection, int(package["buyer_organization_id"]))
                if package["buyer_organization_id"]
                else None
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
        project_ids = {row["project_id"] for row in results}
        return {
            "required_entities": selected,
            "packages": results,
            "package_count": len(results),
            "project_count": len(project_ids),
        }
