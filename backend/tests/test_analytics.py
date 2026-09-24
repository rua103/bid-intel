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


def test_five_relationship_queries_use_packages_and_do_not_duplicate_amounts(tmp_path: Path):
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
        connection.execute(
            """INSERT INTO procurement_items(notice_id, package_id, brand, product_name,
               source_file, source_location, extraction_method, confidence)
               VALUES ((SELECT notice_id FROM projects WHERE id = ?), ?, 'Brand-X', '设备1', 'a.html', 'row:1', 'test', 1),
                      ((SELECT notice_id FROM projects WHERE id = ?), ?, 'Brand-Y', '设备2', 'a.html', 'row:2', 'test', 1)""",
            (project_1, package_1, project_1, package_1),
        )

    awardees = buyer_awardees(database, buyer)
    by_name = {row["name"]: row for row in awardees["awardees"]}
    assert by_name["供应商甲"]["award_package_count"] == 1
    assert by_name["供应商甲"]["award_amount_total"] == "80"
    assert by_name["供应商甲"]["product_brands"] == ["Brand-X", "Brand-Y"]

    bidders = buyer_bidders(database, buyer)
    counts = {row["canonical_name"]: row["package_count"] for row in bidders["top_bidders"]}
    assert counts["供应商甲"] == 2
    assert counts["供应商乙"] == 2
    assert bidders["co_bidder_pairs"][0]["package_count"] == 2

    co_bidders = supplier_co_bidders(database, supplier_a)
    co_count = {row["canonical_name"]: row["package_count"] for row in co_bidders["top_co_bidders"]}
    assert co_count["供应商乙"] == 2
    assert co_count["供应商丙"] == 1

    common_buyers = common_award_buyers(database, [supplier_a, supplier_b])
    assert {row["buyer"]["canonical_name"] for row in common_buyers["buyers"]} == {
        "甲市采购中心",
        "乙市采购中心",
    }

    common_projects = common_bid_packages(database, [supplier_a, supplier_b])
    assert common_projects["package_count"] == 3
    assert common_projects["project_count"] == 3
    assert common_projects["packages"][0]["award_amount_total_unique_awards"] == "80"
    assert common_projects["packages"][2]["award_amount_total_unique_awards"] == "100"
