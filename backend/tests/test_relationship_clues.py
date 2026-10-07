"""Synthetic coverage for the explainable relationship-clue surface.

The fixture deliberately stays independent from model prompts, extraction, and
holdout data.  It is also useful to downstream implementations as a compact
contract for the four first-release clue families.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.relation_clues import query_relation_clues
from app.storage import connect, initialize


def _org(db, name: str) -> int:
    return int(
        db.execute(
            "INSERT INTO organizations(canonical_name, normalized_name) VALUES (?, ?)",
            (name, name.casefold()),
        ).lastrowid
    )


def relationship_clue_fixture(path: Path) -> dict[str, int]:
    """Create two projects in one dataset and one isolated copy in another.

    Project 1 uses the implicit ``default`` package and an OCR warning; project
    2 has an explicit package.  A and B co-bid in both projects, while A wins
    project 1 and B wins project 2.  Every participation and award carries a
    source pointer so a clue can navigate back to evidence.
    """

    initialize(path)
    with connect(path) as db:
        buyer = _org(db, "甲市采购中心")
        a = _org(db, "供应商甲")
        b = _org(db, "供应商乙")
        c = _org(db, "供应商丙")
        notices: list[int] = []
        projects: list[int] = []
        packages: list[int] = []
        for index, (name, number, package_code, warning) in enumerate(
            (("设备采购一期", "P-2025-01", "default", "ocr_failed"),
             ("设备采购二期", "P-2026-02", "包1", ""))
        ):
            notice = int(
                db.execute(
                    "INSERT INTO notices(project_name, project_number, source_files_json, warnings_json) VALUES (?, ?, ?, ?)",
                    (name, number, json.dumps([f"notice-{index + 1}.html"]), json.dumps([warning] if warning else [])),
                ).lastrowid
            )
            project = int(
                db.execute(
                    "INSERT INTO projects(notice_id, project_name, project_number, buyer_organization_id) VALUES (?, ?, ?, ?)",
                    (notice, name, number, buyer),
                ).lastrowid
            )
            package = int(
                db.execute(
                    "INSERT INTO packages(project_id, package_code, package_name) VALUES (?, ?, ?)",
                    (project, package_code, "默认包" if package_code == "default" else "核心设备"),
                ).lastrowid
            )
            notices.append(notice)
            projects.append(project)
            packages.append(package)
        rows = [
            (packages[0], a, "nonwinner", "notice-1.html", "table:bidders", "投标供应商甲"),
            (packages[0], b, "nonwinner", "notice-1.html", "table:bidders", "投标供应商乙"),
            (packages[0], c, "nonwinner", "notice-1.html", "table:bidders", "投标供应商丙"),
            (packages[1], a, "nonwinner", "notice-2.html", "table:bidders", "投标供应商甲"),
            (packages[1], b, "nonwinner", "notice-2.html", "table:bidders", "投标供应商乙"),
            (packages[1], c, "winner", "notice-2.html", "table:award", "中标供应商丙"),
        ]
        for package, org, outcome, source, location, evidence in rows:
            db.execute(
                "INSERT INTO bid_participations(package_id, organization_id, raw_name, outcome, source_file, source_location, source_evidence) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (package, org, evidence, outcome, source, location, evidence),
            )
        for package, org, amount, source in (
            (packages[0], a, "100", "notice-1.html"),
            (packages[1], c, "120", "notice-2.html"),
        ):
            db.execute(
                "INSERT INTO awards(package_id, organization_id, raw_name, award_amount, source_file, source_location, source_evidence) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (package, org, "中标" + ("供应商甲" if org == a else "供应商乙"), amount, source, "table:award", "中标结果"),
            )
    return {"buyer": buyer, "supplier_a": a, "supplier_b": b, "supplier_c": c, "project_1": projects[0], "project_2": projects[1], "package_default": packages[0], "package_explicit": packages[1], "notice_1": notices[0], "notice_2": notices[1]}


@pytest.fixture
def clue_fixture(tmp_path: Path) -> tuple[Path, dict[str, int]]:
    path = tmp_path / "clues.db"
    return path, relationship_clue_fixture(path)


def test_fixture_covers_default_package_warning_and_source_evidence(clue_fixture):
    path, ids = clue_fixture
    with connect(path) as db:
        package = db.execute("SELECT package_code FROM packages WHERE id = ?", (ids["package_default"],)).fetchone()
        warning = db.execute("SELECT warnings_json FROM notices WHERE id = ?", (ids["notice_1"],)).fetchone()
        evidence = db.execute("SELECT source_file, source_location FROM bid_participations WHERE package_id = ? AND organization_id = ?", (ids["package_default"], ids["supplier_b"])).fetchone()
    assert package["package_code"] == "default"
    assert "ocr_failed" in json.loads(warning["warnings_json"])
    assert evidence["source_file"] == "notice-1.html"
    assert evidence["source_location"] == "table:bidders"


def test_relationship_clues_cover_four_kinds_and_evidence(clue_fixture):
    path, _ids = clue_fixture
    payload = query_relation_clues(path, kind="all")
    assert payload.get("clues") is not None
    assert set(payload["counts"]) == {"common_bidding", "repeat_cooperation", "supplier_distribution", "buyer_network"}
    assert payload["counts"]["common_bidding"] >= 1
    assert payload["counts"]["repeat_cooperation"] >= 1
    assert payload["counts"]["supplier_distribution"] >= 1
    assert payload["counts"]["buyer_network"] >= 1
    for clue in payload["clues"]:
        assert clue.get("notices")
        assert clue.get("projects")
        assert clue.get("packages")
        assert clue.get("time_range")
        assert clue.get("warnings") is not None
        assert "待核查线索" in clue.get("label", "待核查线索")
    common = next(clue for clue in payload["clues"] if clue["type"] == "common_bidding")
    assert common["metrics"]["project_count"] == 2
    assert any("default" in warning for warning in common["warnings"])
    assert any("OCR" in warning for warning in common["warnings"])


def test_relationship_clues_default_excludes_winners_and_empty_result(clue_fixture):
    path, ids = clue_fixture
    default = query_relation_clues(path, kind="common_bidding")
    inclusive = query_relation_clues(path, kind="common_bidding", include_winners=True)
    assert default["definitions"]["common_bidding"]["scope"]["include_winners"] is False
    assert inclusive["definitions"]["common_bidding"]["scope"]["include_winners"] is True
    assert query_relation_clues(path, kind="repeat_cooperation", supplier_id=ids["supplier_c"])["clues"] == []
