from pathlib import Path

from app.analytics import (
    buyer_awardees,
    buyer_bidders,
    common_award_buyers,
    common_bid_packages,
    supplier_co_bidders,
)
from app.storage import connect, initialize


def _add_org(connection, name):
    normalized = "".join(name.split()).casefold()
    cursor = connection.execute(
        "INSERT INTO organizations(canonical_name, normalized_name) VALUES (?, ?)",
        (name, normalized),
    )
    return cursor.lastrowid


def _add_project(connection, name, buyer_id, number):
    cursor = connection.execute(
        """INSERT INTO notices(project_name, source_files_json, warnings_json)
           VALUES (?, '[]', '[]')""",
        (name,),
    )
    notice_id = cursor.lastrowid
    cursor = connection.execute(
        "INSERT INTO projects(notice_id, project_name, project_number, buyer_organization_id) VALUES (?, ?, ?, ?)",
        (notice_id, name, number, buyer_id),
    )
    return cursor.lastrowid


def _add_package(connection, project_id, code):
    cursor = connection.execute(
        "INSERT INTO packages(project_id, package_code) VALUES (?, ?)", (project_id, code)
    )
    return cursor.lastrowid


def test_five_relationship_queries_return_expected_relationships_and_amounts(tmp_path: Path):
    database = tmp_path / "analytics.db"
    initialize(database)
    with connect(database) as connection:
        buyer = _add_org(connection, "甲市采购中心")
        other_buyer = _add_org(connection, "乙市采购中心")
        supplier_a = _add_org(connection, "供应商甲")
        supplier_b = _add_org(connection, "供应商乙")
        bidder_c = _add_org(connection, "供应商丙")
        project_1 = _add_project(connection, "项目一", buyer, "P-1")
        project_2 = _add_project(connection, "项目二", buyer, "P-2")
        project_3 = _add_project(connection, "项目三", other_buyer, "P-3")
        package_1 = _add_package(connection, project_1, "包1")
        package_2 = _add_package(connection, project_2, "包1")
        package_3 = _add_package(connection, project_3, "包1")
        package_1b = _add_package(connection, project_1, "包2")

        def add_bid(package_id, organization_id, name, outcome):
            connection.execute(
                """INSERT INTO bid_participations(package_id, organization_id, raw_name, outcome)
                   VALUES (?, ?, ?, ?)""",
                (package_id, organization_id, name, outcome),
            )

        def add_award(package_id, organization_id, name, amount):
            connection.execute(
                """INSERT INTO awards(package_id, organization_id, raw_name, award_amount)
                   VALUES (?, ?, ?, ?)""",
                (package_id, organization_id, name, amount),
            )

        add_bid(package_1, supplier_a, "供应商甲", "winner")
        add_bid(package_1, supplier_b, "供应商乙", "nonwinner")
        add_bid(package_1, bidder_c, "供应商丙", "nonwinner")
        add_award(package_1, supplier_a, "供应商甲", "80")
        add_bid(package_2, supplier_a, "供应商甲", "nonwinner")
        add_bid(package_2, supplier_b, "供应商乙", "winner")
        add_award(package_2, supplier_b, "供应商乙", "120")
        add_bid(package_3, supplier_a, "供应商甲", "winner")
        add_bid(package_3, supplier_b, "供应商乙", "winner")
        add_award(package_3, supplier_a, "供应商甲", "60")
        add_award(package_3, supplier_b, "供应商乙", "40")
        add_bid(package_1b, supplier_a, "供应商甲", "winner")
        add_bid(package_1b, supplier_b, "供应商乙", "nonwinner")
        add_bid(package_1b, bidder_c, "供应商丙", "winner")
        add_award(package_1b, supplier_a, "供应商甲", "20")
        add_award(package_1b, bidder_c, "供应商丙", "30")
        connection.execute(
            """INSERT INTO procurement_items(notice_id, package_id, brand, product_name,
               source_file, source_location, extraction_method, confidence)
               VALUES ((SELECT notice_id FROM projects WHERE id = ?), ?, 'Brand-X', '设备1', 'a.html', 'row:1', 'test', 1),
                      ((SELECT notice_id FROM projects WHERE id = ?), ?, 'Brand-Y', '设备2', 'a.html', 'row:2', 'test', 1)""",
            (project_1, package_1, project_1, package_1),
        )

    awardees = buyer_awardees(database, buyer)
    by_name = {row["name"]: row for row in awardees["awardees"]}
    assert by_name["供应商甲"]["award_package_count"] == 2
    assert by_name["供应商甲"]["award_amount_total"] == "100"
    assert by_name["供应商甲"]["product_brands"] == ["Brand-X", "Brand-Y"]

    bidders = buyer_bidders(database, buyer)
    counts = {row["canonical_name"]: row["project_count"] for row in bidders["top_bidders"]}
    assert bidders["include_winners"] is False
    assert counts == {"供应商乙": 1, "供应商丙": 1, "供应商甲": 1}
    assert bidders["co_bidder_pairs"][0]["project_count"] == 1

    all_bidders = buyer_bidders(database, buyer, include_winners=True)
    all_counts = {
        row["canonical_name"]: row["project_count"]
        for row in all_bidders["top_bidders"]
    }
    assert all_counts["供应商甲"] == 2
    assert all_counts["供应商乙"] == 2
    assert all_bidders["co_bidder_pairs"][0]["project_count"] == 2

    co_bidders = supplier_co_bidders(database, supplier_a)
    co_count = {row["canonical_name"]: row["project_count"] for row in co_bidders["top_co_bidders"]}
    assert co_bidders["include_winners"] is False
    assert co_count == {"供应商丙": 1, "供应商乙": 1}
    co_by_name = {row["canonical_name"]: row for row in co_bidders["top_co_bidders"]}
    assert co_by_name["供应商乙"]["award_package_count"] == 2
    assert co_by_name["供应商乙"]["selected_supplier_award_amount_total"] == "100"
    assert co_by_name["供应商丙"]["selected_supplier_award_amount_total"] == "80"

    all_co_bidders = supplier_co_bidders(database, supplier_a, include_winners=True)
    all_co_count = {
        row["canonical_name"]: row["project_count"]
        for row in all_co_bidders["top_co_bidders"]
    }
    assert all_co_count["供应商乙"] == 2
    assert all_co_count["供应商丙"] == 1
    co_all_by_name = {row["canonical_name"]: row for row in all_co_bidders["top_co_bidders"]}
    assert co_all_by_name["供应商乙"]["award_package_count"] == 3
    assert co_all_by_name["供应商乙"]["selected_supplier_award_amount_total"] == "160"

    common_buyers = common_award_buyers(database, [supplier_a, supplier_b])
    assert {row["buyer"]["canonical_name"] for row in common_buyers["buyers"]} == {
        "甲市采购中心",
        "乙市采购中心",
    }

    common_projects = common_bid_packages(database, [supplier_a, supplier_b])
    assert common_projects["package_count"] == 4
    assert common_projects["project_count"] == 3
    project_amounts = {
        row["project_id"]: row["award_amount_total_unique_awards"]
        for row in common_projects["projects"]
    }
    assert project_amounts == {
        project_1: "130",
        project_2: "120",
        project_3: "100",
    }
    assert common_projects["award_amount_total_unique_awards"] == "350"
    package_amounts = {
        row["package_id"]: row["award_amount_total_unique_awards"]
        for row in common_projects["packages"]
    }
    assert package_amounts[package_1] == "80"
    assert package_amounts[package_3] == "100"


def test_scenario_2_and_3_count_projects_and_exclude_unknown_outcomes(tmp_path: Path):
    database = tmp_path / "project-grain.db"
    initialize(database)
    with connect(database) as connection:
        buyer = _add_org(connection, "采购中心")
        winner = _add_org(connection, "中标公司")
        bidder_a = _add_org(connection, "明确落标公司")
        bidder_b = _add_org(connection, "结果未知公司")
        project_1 = _add_project(connection, "多包项目", buyer, "P-1")
        project_2 = _add_project(connection, "单包项目", buyer, "P-2")
        package_1 = _add_package(connection, project_1, "1")
        package_2 = _add_package(connection, project_1, "2")
        package_3 = _add_package(connection, project_2, "1")

        for package_id, outcome_b in (
            (package_1, "unknown"),
            (package_2, "unknown"),
            (package_3, "nonwinner"),
        ):
            for org_id, name, outcome in (
                (winner, "中标公司", "winner"),
                (bidder_a, "明确落标公司", "nonwinner"),
                (bidder_b, "结果未知公司", outcome_b),
            ):
                connection.execute(
                    """INSERT INTO bid_participations(package_id,organization_id,raw_name,outcome)
                       VALUES (?,?,?,?)""",
                    (package_id, org_id, name, outcome),
                )
            connection.execute(
                "INSERT INTO awards(package_id,organization_id,raw_name,award_amount) VALUES (?,?,?,?)",
                (package_id, winner, "中标公司", "10"),
            )

    default = buyer_bidders(database, buyer, top=100)
    default_counts = {row["canonical_name"]: row["project_count"] for row in default["top_bidders"]}
    assert default_counts == {"明确落标公司": 2, "结果未知公司": 1}
    default_pairs = {
        frozenset((row["name1"], row["name2"])): row["project_count"]
        for row in default["co_bidder_pairs"]
    }
    assert default_pairs == {frozenset(("明确落标公司", "结果未知公司")): 1}

    all_participants = buyer_bidders(database, buyer, include_winners=True, top=100)
    all_counts = {
        row["canonical_name"]: row["project_count"] for row in all_participants["top_bidders"]
    }
    assert all_counts == {"中标公司": 2, "明确落标公司": 2, "结果未知公司": 2}
    all_pairs = {
        frozenset((row["name1"], row["name2"])): row["project_count"]
        for row in all_participants["co_bidder_pairs"]
    }
    assert len(all_pairs) == 3
    assert set(all_pairs.values()) == {2}

    co_bidders = supplier_co_bidders(database, winner, top=100)
    co_counts = {row["canonical_name"]: row["project_count"] for row in co_bidders["top_co_bidders"]}
    assert co_counts == {"明确落标公司": 2, "结果未知公司": 1}
    co_amounts = {
        row["canonical_name"]: row["selected_supplier_award_amount_total"]
        for row in co_bidders["top_co_bidders"]
    }
    assert co_amounts == {"明确落标公司": "30", "结果未知公司": "10"}
    co_all = supplier_co_bidders(database, winner, include_winners=True, top=100)
    co_all_counts = {
        row["canonical_name"]: row["project_count"] for row in co_all["top_co_bidders"]
    }
    assert co_all_counts == {"明确落标公司": 2, "结果未知公司": 2}
    co_all_amounts = {
        row["canonical_name"]: row["selected_supplier_award_amount_total"]
        for row in co_all["top_co_bidders"]
    }
    assert co_all_amounts == {"明确落标公司": "30", "结果未知公司": "30"}
