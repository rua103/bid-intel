from app.analytics import (
    buyer_awardees,
    buyer_bidders,
    common_award_buyers,
    common_bid_packages,
    supplier_co_bidders,
)
from app.demo_seed import DEMO_DATASET_ID, seed_offline_demo
from app.storage import connect


def test_offline_demo_seed_builds_a_complete_synthetic_query_snapshot(tmp_path):
    path = seed_offline_demo(tmp_path)
    assert path.name == f"{DEMO_DATASET_ID}.sqlite"
    with connect(path) as connection:
        assert connection.execute("SELECT count(*) FROM notices").fetchone()[0] == 3
        assert connection.execute("SELECT count(*) FROM procurement_items").fetchone()[0] == 3
        assert connection.execute("SELECT count(*) FROM bid_participations").fetchone()[0] == 7
        assert connection.execute("SELECT count(*) FROM awards").fetchone()[0] == 3
        organizations = {
            row["canonical_name"]: row["id"]
            for row in connection.execute("SELECT id, canonical_name FROM organizations")
        }
        buyer_id = organizations["示例市公共服务中心（虚构）"]
        supplier_a = organizations["示例打印科技有限公司（虚构）"]
        supplier_b = organizations["示例文印设备有限公司（虚构）"]

    assert buyer_awardees(path, buyer_id)["awardees"]
    bidder_results = buyer_bidders(path, buyer_id)
    assert len(bidder_results["top_bidders"]) == 3
    assert bidder_results["co_bidder_pairs"]
    assert supplier_co_bidders(path, supplier_a)["top_co_bidders"]
    assert common_award_buyers(path, [supplier_a, supplier_b])["buyers"]
    assert common_bid_packages(path, [supplier_a, supplier_b])["package_count"] == 2


def test_offline_demo_seed_is_repeatable_and_replaces_only_its_fixed_fixture(tmp_path):
    first_path = seed_offline_demo(tmp_path)
    second_path = seed_offline_demo(tmp_path)
    assert first_path == second_path
    with connect(second_path) as connection:
        assert connection.execute("SELECT count(*) FROM notices").fetchone()[0] == 3
