"""Validate the five SQLite relationship queries against reviewed annotation gold.

The expected results are calculated directly from the gold JSON. The analytics
functions are used only for the system side of the comparison.

Run from ``backend``::

    python -m scripts.validate_gold_queries

The command creates a new isolated SQLite file and JSON report under
``backend/.data/query-validation``. It never opens the application's default DB.
"""

from __future__ import annotations

import argparse
import hashlib
import itertools
import json
import os
import sys
from collections import defaultdict
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

from app import analytics
from app.schemas import ImportResult, ItemCandidate, NoticeMetadata, ParticipantCandidate
from app.storage import connect, save_import

BACKEND_DIR = Path(__file__).resolve().parents[1]
DEFAULT_GOLD = (
    BACKEND_DIR
    / ".data"
    / "annotation-tasks"
    / "official-20260926-LHH-YHR"
    / "gold.merged.json"
)
DEFAULT_OUTPUT_DIR = BACKEND_DIR / ".data" / "query-validation"
TOP = 5


def normalize_name(name: str) -> str:
    """Mirror the storage identity key, without consulting query code."""
    return "".join(name.strip().split()).casefold()


def amount(value: Any) -> Decimal | None:
    if value in (None, ""):
        return None
    return Decimal(str(value))


def sum_amounts(values: list[Any]) -> Decimal | None:
    present = [amount(value) for value in values if value not in (None, "")]
    return sum(present, Decimal(0)) if present else None


def equal_amount(left: Any, right: Any) -> bool:
    return amount(left) == amount(right)


def stable_case_id(value: Any) -> str:
    serialized = json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()[:12]


def query_database(report: dict[str, Any]) -> Path:
    return Path(report["database_path"])


def load_gold(path: Path) -> dict[str, Any]:
    raw_bytes = path.read_bytes()
    data = json.loads(raw_bytes.decode("utf-8"))
    if data.get("status") != "reviewed":
        raise ValueError(f"gold must be reviewed; got {data.get('status')!r}")
    if not data.get("notices"):
        raise ValueError("gold contains no notices")
    data["_sha256"] = hashlib.sha256(raw_bytes).hexdigest()
    return data


def to_import_result(notice: dict[str, Any]) -> ImportResult:
    notice_key = str(notice["notice_id"])
    packages = notice.get("packages", [])
    buyer_names = {
        normalize_name(package["buyer"]["name"]): package["buyer"]["name"]
        for package in packages
        if package.get("buyer") and package["buyer"].get("name")
    }
    if len(buyer_names) > 1:
        raise ValueError(f"package-level buyers differ within notice {notice_key}")

    items: list[ItemCandidate] = []
    participants: list[ParticipantCandidate] = []
    winner_amounts: list[Decimal] = []
    package_keys: set[str] = set()

    for package_index, package in enumerate(packages):
        package_code = str(package["package_id"])
        package_keys.add(package_code)
        winners = {
            normalize_name(row["name"]): row
            for row in package.get("winners", [])
            if row.get("name")
        }
        bidder_names = {
            normalize_name(row["name"])
            for row in package.get("bidders", [])
            if row.get("name")
        }
        if set(winners) - bidder_names:
            raise ValueError(f"winner is absent from bidders in {notice_key}/{package_code}")

        for item_index, item in enumerate(package.get("items", [])):
            item_id = item.get("item_id", str(item_index))
            items.append(
                ItemCandidate(
                    package_code=package_code,
                    product_name=item.get("product_name"),
                    category=item.get("category"),
                    brand=item.get("brand"),
                    model=item.get("model"),
                    quantity=item.get("quantity"),
                    unit_price=item.get("unit_price"),
                    total_price=item.get("total_price"),
                    source_file=f"gold:{notice_key}/{package_code}/{item_id}",
                    source_location=f"gold.merged.json#/notices/{notice_key}/{package_index}/{item_id}",
                    source_evidence=None,
                    extraction_method="reviewed_gold",
                    confidence=1.0,
                )
            )

        for bidder_index, bidder in enumerate(package.get("bidders", [])):
            name = bidder["name"]
            outcome = bidder["outcome"]
            winner = winners.get(normalize_name(name))
            if (outcome == "winner") != (winner is not None):
                raise ValueError(
                    f"winner status/list disagree in {notice_key}/{package_code}/{name}"
                )
            award_amount = amount(winner.get("award_amount")) if winner else None
            if award_amount is not None:
                winner_amounts.append(award_amount)
            participants.append(
                ParticipantCandidate(
                    package_code=package_code,
                    organization_name=name,
                    outcome=outcome,
                    award_amount=award_amount,
                    source_file=f"gold:{notice_key}/{package_code}/bidder-{bidder_index}",
                    source_location=f"gold.merged.json#/notices/{notice_key}/{package_index}/bidders/{bidder_index}",
                    source_evidence=None,
                    extraction_method="reviewed_gold",
                    confidence=1.0,
                )
            )

    buyer_name = next(iter(buyer_names.values()), None)
    award_total = sum(winner_amounts, Decimal(0)) if winner_amounts else None
    return ImportResult(
        notice_id=0,
        source_files=["gold.merged.json"],
        items_found=len(items),
        items=items,
        metadata=NoticeMetadata(
            project_name=f"gold project {notice_key}",
            project_number=notice_key,
            procurement_unit=buyer_name,
            announced_total_award=award_total,
        ),
        participants=participants,
        warnings=[],
    )


def import_gold(gold: dict[str, Any], database: Path) -> dict[str, Any]:
    for notice in gold["notices"]:
        save_import(database, to_import_result(notice))

    expected = {
        "notices": len(gold["notices"]),
        "projects": len(gold["notices"]),
        "packages": sum(len(n.get("packages", [])) for n in gold["notices"]),
        "items": sum(
            len(p.get("items", [])) for n in gold["notices"] for p in n.get("packages", [])
        ),
        "participants": sum(
            len(p.get("bidders", [])) for n in gold["notices"] for p in n.get("packages", [])
        ),
        "awards": sum(
            len(p.get("winners", [])) for n in gold["notices"] for p in n.get("packages", [])
        ),
    }
    with connect(database) as connection:
        actual = {
            "notices": connection.execute("SELECT COUNT(*) FROM notices").fetchone()[0],
            "projects": connection.execute("SELECT COUNT(*) FROM projects").fetchone()[0],
            "packages": connection.execute("SELECT COUNT(*) FROM packages").fetchone()[0],
            "items": connection.execute("SELECT COUNT(*) FROM procurement_items").fetchone()[0],
            "participants": connection.execute("SELECT COUNT(*) FROM bid_participations").fetchone()[0],
            "awards": connection.execute("SELECT COUNT(*) FROM awards").fetchone()[0],
        }
        orgs = connection.execute(
            "SELECT id, canonical_name FROM organizations ORDER BY id"
        ).fetchall()
        projects = connection.execute(
            """SELECT p.id, p.project_number, p.buyer_organization_id
               FROM projects p ORDER BY p.id"""
        ).fetchall()
        packages = connection.execute(
            """SELECT k.id, x.project_number, k.package_code
               FROM packages k JOIN projects x ON x.id=k.project_id ORDER BY k.id"""
        ).fetchall()
    if expected != actual:
        raise ValueError(f"gold import count mismatch: expected={expected}, actual={actual}")
    return {
        "expected_counts": expected,
        "actual_counts": actual,
        "organization_ids": {normalize_name(row["canonical_name"]): row["id"] for row in orgs},
        "canonical_names": {
            normalize_name(row["canonical_name"]): row["canonical_name"] for row in orgs
        },
        "project_ids": {row["project_number"]: row["id"] for row in projects},
        "project_buyers": {
            row["project_number"]: row["buyer_organization_id"] for row in projects
        },
        "package_ids": {
            (row["project_number"], row["package_code"]): row["id"] for row in packages
        },
        "package_keys_by_id": {
            row["id"]: (row["project_number"], row["package_code"]) for row in packages
        },
    }


def gold_indexes(gold: dict[str, Any]) -> dict[str, Any]:
    projects: dict[str, dict[str, Any]] = {}
    packages: dict[tuple[str, str], dict[str, Any]] = {}
    buyers: set[str] = set()
    participants: set[str] = set()
    winners: set[str] = set()
    for notice in gold["notices"]:
        notice_key = str(notice["notice_id"])
        projects[notice_key] = {"notice": notice, "packages": []}
        for package in notice.get("packages", []):
            key = (notice_key, str(package["package_id"]))
            buyer = package.get("buyer")
            buyer_key = normalize_name(buyer["name"]) if buyer and buyer.get("name") else None
            record = {
                "notice_key": notice_key,
                "package_code": key[1],
                "buyer_key": buyer_key,
                "bidders": {
                    normalize_name(row["name"]): row["outcome"]
                    for row in package.get("bidders", [])
                },
                "winners": {
                    normalize_name(row["name"]): row
                    for row in package.get("winners", [])
                },
                "items": package.get("items", []),
                "raw": package,
            }
            packages[key] = record
            projects[notice_key]["packages"].append(record)
            if buyer_key:
                buyers.add(buyer_key)
            participants.update(record["bidders"])
            winners.update(record["winners"])
    return {"projects": projects, "packages": packages, "buyers": buyers,
            "participants": participants, "winners": winners}


def ranked_expected(counts: dict[str, set[str]], canonical: dict[str, str]) -> list[tuple[str, int]]:
    ordered = sorted(counts, key=lambda key: (-len(counts[key]), canonical[key]))[:TOP]
    return [(key, len(counts[key])) for key in ordered]


def ranked_actual(rows: list[dict[str, Any]], count_names: tuple[str, ...]) -> list[tuple[str, int]]:
    result = []
    for row in rows:
        key_name = row.get("canonical_name", row.get("name", ""))
        count = next((row[name] for name in count_names if name in row), None)
        result.append((normalize_name(key_name), int(count)))
    return result


def record_case(report: dict[str, Any], scene: str, case: Any, issues: list[str]) -> None:
    row = {"case": stable_case_id(case), "issues": sorted(set(issues))}
    report["scenes"][scene]["checked"] += 1
    if issues:
        report["scenes"][scene]["failed"] += 1
        if len(report["mismatches"]) < 200:
            report["mismatches"].append({"scene": scene, **row})


def validate_scene_1(
    gold: dict[str, Any], index: dict[str, Any], imported: dict[str, Any], report: dict[str, Any]
) -> None:
    for buyer_key in sorted(index["buyers"]):
        buyer_id = imported["organization_ids"][buyer_key]
        expected: dict[str, dict[str, Any]] = {}
        for package in index["packages"].values():
            if package["buyer_key"] != buyer_key:
                continue
            for supplier_key, winner in package["winners"].items():
                row = expected.setdefault(
                    supplier_key,
                    {"packages": set(), "amounts": [], "brands": set()},
                )
                row["packages"].add((package["notice_key"], package["package_code"]))
                row["amounts"].append(winner.get("award_amount"))
                row["brands"].update(
                    item["brand"]
                    for item in package["items"]
                    if item.get("brand") not in (None, "")
                )
        expected_rows = sorted(
            expected.items(),
            key=lambda pair: (-len(pair[1]["packages"]), imported["canonical_names"][pair[0]]),
        )
        actual = analytics.buyer_awardees(query_database(report), buyer_id)
        actual_rows = {normalize_name(row["name"]): row for row in actual["awardees"]}
        issues: list[str] = []
        if normalize_name(actual["buyer"]["canonical_name"]) != buyer_key:
            issues.append("buyer_identity")
        if set(actual_rows) != set(expected):
            issues.append("awardee_membership")
        for supplier_key, expected_row in expected_rows:
            actual_row = actual_rows.get(supplier_key)
            if actual_row is None:
                continue
            if actual_row["award_package_count"] != len(expected_row["packages"]):
                issues.append("award_package_count")
            if not equal_amount(actual_row["award_amount_total"], sum_amounts(expected_row["amounts"])):
                issues.append("award_amount_total")
            if actual_row["product_brands"] != sorted(expected_row["brands"]):
                issues.append("product_brands")
        if list(actual_rows) != [key for key, _ in expected_rows]:
            issues.append("awardee_order")
        record_case(report, "scene1_buyer_awardees", buyer_key, issues)


def actual_pair_ranking(rows: list[dict[str, Any]], count_names: tuple[str, ...]) -> list[tuple[tuple[str, str], int]]:
    result = []
    for row in rows:
        count = next((row[name] for name in count_names if name in row), None)
        pair = tuple(sorted((normalize_name(row["name1"]), normalize_name(row["name2"]))))
        result.append((pair, int(count)))
    return result


def expected_pair_ranking(
    counts: dict[tuple[str, str], set[str]], canonical: dict[str, str], org_ids: dict[str, int]
) -> list[tuple[tuple[str, str], int]]:
    ordered = sorted(
        counts,
        key=lambda pair: (
            -len(counts[pair]),
            *(canonical[key] for key in sorted(pair, key=org_ids.__getitem__)),
        ),
    )[:TOP]
    return [(pair, len(counts[pair])) for pair in ordered]


def validate_scene_2(
    index: dict[str, Any], imported: dict[str, Any], report: dict[str, Any]
) -> None:
    for buyer_key in sorted(index["buyers"]):
        buyer_id = imported["organization_ids"][buyer_key]
        for include_winners in (False, True):
            expected_bidders: dict[str, set[str]] = defaultdict(set)
            expected_pairs: dict[tuple[str, str], set[str]] = defaultdict(set)
            for package in index["packages"].values():
                if package["buyer_key"] != buyer_key:
                    continue
                eligible = sorted(
                    key for key, outcome in package["bidders"].items()
                    if include_winners or outcome == "nonwinner"
                )
                for bidder_key in eligible:
                    expected_bidders[bidder_key].add(package["notice_key"])
                for pair in itertools.combinations(eligible, 2):
                    expected_pairs[pair].add(package["notice_key"])
            expected_top = ranked_expected(expected_bidders, imported["canonical_names"])
            expected_pair_top = expected_pair_ranking(
                expected_pairs, imported["canonical_names"], imported["organization_ids"]
            )
            actual = analytics.buyer_bidders(
                query_database(report), buyer_id, include_winners=include_winners, top=TOP
            )
            actual_top = ranked_actual(actual["top_bidders"], ("project_count", "package_count"))
            actual_pairs = actual_pair_ranking(
                actual["co_bidder_pairs"], ("project_count", "package_count")
            )
            issues = []
            if actual_top != expected_top:
                issues.append("top_bidder_membership_rank_or_project_count")
            if actual_pairs != expected_pair_top:
                issues.append("co_bidder_membership_rank_or_project_count")
            if actual["include_winners"] != include_winners:
                issues.append("include_winners_echo")
            record_case(report, "scene2_buyer_bidders", [buyer_key, include_winners], issues)


def validate_scene_3(
    index: dict[str, Any], imported: dict[str, Any], report: dict[str, Any]
) -> None:
    for supplier_key in sorted(index["winners"]):
        supplier_id = imported["organization_ids"][supplier_key]
        for include_winners in (False, True):
            expected_counts: dict[str, set[str]] = defaultdict(set)
            expected_packages: dict[tuple[str, str], dict[str, Any]] = {}
            for package in index["packages"].values():
                winner = package["winners"].get(supplier_key)
                if winner is None:
                    continue
                key = (package["notice_key"], package["package_code"])
                expected_packages[key] = package
                for bidder_key, outcome in package["bidders"].items():
                    if bidder_key == supplier_key:
                        continue
                    if include_winners or outcome == "nonwinner":
                        expected_counts[bidder_key].add(package["notice_key"])
            expected_top = ranked_expected(expected_counts, imported["canonical_names"])
            actual = analytics.supplier_co_bidders(
                query_database(report), supplier_id, include_winners=include_winners, top=TOP
            )
            actual_top = ranked_actual(actual["top_co_bidders"], ("project_count", "package_count"))
            issues = []
            if actual_top != expected_top:
                issues.append("top_co_bidder_membership_rank_or_project_count")
            if actual["include_winners"] != include_winners:
                issues.append("include_winners_echo")
            if actual.get("base") != "packages where supplier has an award":
                issues.append("query_base")
            actual_packages = {}
            for row in actual["packages"]:
                key = imported["package_keys_by_id"].get(row["package_id"])
                if key is None:
                    issues.append("unknown_package")
                    continue
                actual_packages[key] = row
            if set(actual_packages) != set(expected_packages):
                issues.append("award_package_membership")
            for key, expected_package in expected_packages.items():
                row = actual_packages.get(key)
                if row is None:
                    continue
                winner = expected_package["winners"][supplier_key]
                if not equal_amount(row["target_award_amount"], winner.get("award_amount")):
                    issues.append("target_award_amount")
                expected_buyer = expected_package["raw"].get("buyer")
                expected_buyer_key = normalize_name(expected_buyer["name"]) if expected_buyer else None
                actual_buyer_key = normalize_name(row["buyer_name"]) if row.get("buyer_name") else None
                if expected_buyer_key != actual_buyer_key:
                    issues.append("package_buyer")
                expected_participants = sorted(expected_package["bidders"].items())
                actual_participants = sorted(
                    (normalize_name(p["canonical_name"]), p["outcome"])
                    for p in row["participants"]
                )
                if actual_participants != expected_participants:
                    issues.append("package_participants")
            record_case(
                report, "scene3_supplier_co_bidders", [supplier_key, include_winners], issues
            )


def expected_common_buyers(
    index: dict[str, Any], selected: tuple[str, ...]
) -> dict[str, dict[str, list[tuple[tuple[str, str], Any]]]]:
    grouped: dict[str, dict[str, list[tuple[tuple[str, str], Any]]]] = defaultdict(
        lambda: defaultdict(list)
    )
    for package_key, package in index["packages"].items():
        buyer_key = package["buyer_key"]
        if buyer_key is None:
            continue
        for supplier_key, winner in package["winners"].items():
            if supplier_key in selected:
                grouped[buyer_key][supplier_key].append((package_key, winner.get("award_amount")))
    return {
        buyer: suppliers
        for buyer, suppliers in grouped.items()
        if set(selected).issubset(suppliers)
    }


def validate_one_common_buyer_selection(
    index: dict[str, Any], imported: dict[str, Any], report: dict[str, Any], selected: tuple[str, ...]
) -> None:
    ids = [imported["organization_ids"][key] for key in selected]
    actual = analytics.common_award_buyers(query_database(report), ids)
    expected = expected_common_buyers(index, selected)
    actual_buyers = {normalize_name(row["buyer"]["canonical_name"]): row for row in actual["buyers"]}
    issues = []
    returned_selected = {normalize_name(row["canonical_name"]) for row in actual["selected_suppliers"]}
    if returned_selected != set(selected):
        issues.append("selected_supplier_echo")
    if set(actual_buyers) != set(expected):
        issues.append("common_buyer_membership")
    for buyer_key, suppliers in expected.items():
        row = actual_buyers.get(buyer_key)
        if row is None:
            continue
        actual_suppliers = {
            normalize_name(supplier["name"]): supplier for supplier in row["suppliers"]
        }
        if set(actual_suppliers) != set(selected):
            issues.append("supplier_membership")
        all_amounts: list[Any] = []
        for supplier_key in selected:
            supplier = actual_suppliers.get(supplier_key)
            if supplier is None:
                continue
            award_rows = suppliers[supplier_key]
            expected_amount = sum_amounts([amount_value for _, amount_value in award_rows])
            all_amounts.extend(amount_value for _, amount_value in award_rows)
            if supplier["award_package_count"] != len({package_key for package_key, _ in award_rows}):
                issues.append("award_package_count")
            if not equal_amount(supplier["award_amount_total"], expected_amount):
                issues.append("supplier_award_amount_total")
        if not equal_amount(row["award_amount_total_unique_awards"], sum_amounts(all_amounts)):
            issues.append("unique_award_amount_total")
    record_case(report, "scene4_common_award_buyers", selected, issues)


def validate_scene_4(
    index: dict[str, Any], imported: dict[str, Any], report: dict[str, Any]
) -> None:
    winner_keys = sorted(index["winners"])
    for selected in itertools.combinations(winner_keys, 2):
        validate_one_common_buyer_selection(index, imported, report, selected)

    # Exercise multi-selection beyond the API's minimum of two organizations.
    winners_by_buyer: dict[str, set[str]] = defaultdict(set)
    for package in index["packages"].values():
        if package["buyer_key"]:
            winners_by_buyer[package["buyer_key"]].update(package["winners"])
    triples = {
        tuple(sorted(group))
        for group in winners_by_buyer.values()
        for group in itertools.combinations(sorted(group), 3)
    }
    if not triples and len(winner_keys) >= 3:
        triples.add(tuple(winner_keys[:3]))
    for selected in sorted(triples):
        validate_one_common_buyer_selection(index, imported, report, selected)


def expected_common_packages(
    index: dict[str, Any], selected: tuple[str, ...]
) -> dict[tuple[str, str], dict[str, Any]]:
    return {
        key: package
        for key, package in index["packages"].items()
        if set(selected).issubset(package["bidders"])
    }


def validate_one_common_package_selection(
    index: dict[str, Any], imported: dict[str, Any], report: dict[str, Any], selected: tuple[str, ...]
) -> None:
    ids = [imported["organization_ids"][key] for key in selected]
    actual = analytics.common_bid_packages(query_database(report), ids)
    expected = expected_common_packages(index, selected)
    actual_rows = {}
    issues = []
    selected_echo = {normalize_name(row["canonical_name"]) for row in actual["required_entities"]}
    if selected_echo != set(selected):
        issues.append("selected_entity_echo")
    for row in actual["packages"]:
        key = imported["package_keys_by_id"].get(row["package_id"])
        if key is None:
            issues.append("unknown_package")
            continue
        actual_rows[key] = row
    if set(actual_rows) != set(expected):
        issues.append("common_package_membership")
    expected_projects = {key[0] for key in expected}
    if actual["package_count"] != len(expected):
        issues.append("package_count")
    if actual["project_count"] != len(expected_projects):
        issues.append("project_count")
    for key, expected_package in expected.items():
        row = actual_rows.get(key)
        if row is None:
            continue
        notice_key, package_code = key
        expected_project_id = imported["project_ids"][notice_key]
        if row["project_id"] != expected_project_id:
            issues.append("project_identity")
        if row["project_name"] != f"gold project {notice_key}" or row["project_number"] != notice_key:
            issues.append("project_metadata")
        expected_buyer = expected_package["raw"].get("buyer")
        expected_buyer_key = normalize_name(expected_buyer["name"]) if expected_buyer else None
        actual_buyer_key = (
            normalize_name(row["buyer"]["canonical_name"]) if row.get("buyer") else None
        )
        if actual_buyer_key != expected_buyer_key:
            issues.append("buyer_identity")
        expected_participants = sorted(expected_package["bidders"].items())
        actual_participants = sorted(
            (normalize_name(participant["canonical_name"]), participant["outcome"])
            for participant in row["participants"]
        )
        if actual_participants != expected_participants:
            issues.append("package_participants")
        winner_amounts = [row.get("award_amount") for row in expected_package["winners"].values()]
        if not equal_amount(row["award_amount_total_unique_awards"], sum_amounts(winner_amounts)):
            issues.append("unique_award_amount")
        expected_project_amount = sum_amounts(
            [
                winner.get("award_amount")
                for project_package in index["projects"][notice_key]["packages"]
                for winner in project_package["winners"].values()
            ]
        )
        if not equal_amount(row["announced_total_award"], expected_project_amount):
            issues.append("project_award_total")
        # Gold has no separate package-name field; package_code is the ground-truth label.
        if row["package_code"] != package_code:
            issues.append("package_code")
    record_case(report, "scene5_common_bid_projects", selected, issues)


def validate_scene_5(
    index: dict[str, Any], imported: dict[str, Any], report: dict[str, Any]
) -> None:
    participant_keys = sorted(index["participants"])
    for selected in itertools.combinations(participant_keys, 2):
        validate_one_common_package_selection(index, imported, report, selected)

    # Also verify multi-party intersections from real gold packages.
    triples = {
        tuple(sorted(group))
        for package in index["packages"].values()
        for group in itertools.combinations(sorted(package["bidders"]), 3)
    }
    if not triples and len(participant_keys) >= 3:
        triples.add(tuple(participant_keys[:3]))
    for selected in sorted(triples):
        validate_one_common_package_selection(index, imported, report, selected)


def new_path(directory: Path, requested: Path | None, suffix: str) -> Path:
    if requested is not None:
        path = requested.resolve()
        if path.exists():
            raise FileExistsError(f"refusing to overwrite existing file: {path}")
        path.parent.mkdir(parents=True, exist_ok=True)
        return path
    directory.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    return directory / f"gap-6a-{stamp}-{os.getpid()}.{suffix}"


def run(gold_path: Path, database_path: Path, report_path: Path | None) -> dict[str, Any]:
    if database_path.exists():
        raise FileExistsError(f"refusing to overwrite existing DB: {database_path}")
    gold = load_gold(gold_path)
    imported = import_gold(gold, database_path)
    index = gold_indexes(gold)
    report: dict[str, Any] = {
        "validation": "GAP 6a reviewed-gold SQLite query validation",
        "created_at_utc": datetime.now(UTC).isoformat(),
        "gold_status": gold["status"],
        "gold_sha256": gold["_sha256"],
        "gold_path": str(gold_path.resolve()),
        "database_path": str(database_path.resolve()),
        "query_backend": "SQLite app.analytics",
        "identity_rule": "exact name after storage-compatible whitespace removal and casefold; no fuzzy alias merge",
        "frequency_rule": {
            "scene2_scene3": "distinct project, winner/nonwinner/unknown by default; include_winners=False is an explicit nonwinner-only control",
            "scene5": "common package membership with project_count reported as distinct projects",
        },
        "import": imported["actual_counts"],
        "scenes": {
            "scene1_buyer_awardees": {"checked": 0, "failed": 0},
            "scene2_buyer_bidders": {"checked": 0, "failed": 0},
            "scene3_supplier_co_bidders": {"checked": 0, "failed": 0},
            "scene4_common_award_buyers": {"checked": 0, "failed": 0},
            "scene5_common_bid_projects": {"checked": 0, "failed": 0},
        },
        "mismatches": [],
        "notes": [
            "Expected values were computed from gold JSON and did not call analytics helpers.",
            "Gold entity_id values are mention-level: repeated exact names have multiple IDs; query identity comparison therefore follows storage's conservative exact-name normalization.",
            "Neo4j was not exercised; production API relationship queries use SQLite and the gold test imports into an isolated SQLite file.",
        ],
    }
    validate_scene_1(gold, index, imported, report)
    validate_scene_2(index, imported, report)
    validate_scene_3(index, imported, report)
    validate_scene_4(index, imported, report)
    validate_scene_5(index, imported, report)
    report["summary"] = {
        "checked": sum(row["checked"] for row in report["scenes"].values()),
        "failed": sum(row["failed"] for row in report["scenes"].values()),
        "passed": sum(row["checked"] - row["failed"] for row in report["scenes"].values()),
    }
    if report_path is not None:
        report_path.parent.mkdir(parents=True, exist_ok=True)
        report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--gold", type=Path, default=DEFAULT_GOLD)
    parser.add_argument("--database", type=Path, default=None, help="new SQLite path; must not exist")
    parser.add_argument("--report", type=Path, default=None, help="JSON report path; must not exist")
    args = parser.parse_args(argv)
    database = new_path(DEFAULT_OUTPUT_DIR, args.database, "sqlite")
    report = new_path(DEFAULT_OUTPUT_DIR, args.report, "json")
    result = run(args.gold, database, report)
    print(json.dumps(result, ensure_ascii=True, indent=2))
    return 0 if result["summary"]["failed"] == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
