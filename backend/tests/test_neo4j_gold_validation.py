from scripts import validate_neo4j_gold


def test_party_selection_coverage_counts_two_and_three_party_queries():
    calls = {
        validate_neo4j_gold._call_key("common_award_buyers", ([11, 12],), {}): {},
        validate_neo4j_gold._call_key("common_award_buyers", ([11, 12, 13],), {}): {},
        validate_neo4j_gold._call_key("common_bid_packages", ([21, 22],), {}): {},
        validate_neo4j_gold._call_key("common_bid_packages", ([21, 22, 23],), {}): {},
        validate_neo4j_gold._call_key("buyer_awardees", (31,), {}): {},
    }

    assert validate_neo4j_gold._party_selection_coverage(calls) == {
        "scene4_common_award_buyers": {"two_party": 1, "three_party": 1},
        "scene5_common_bid_projects": {"two_party": 1, "three_party": 1},
    }


def test_validation_report_uses_source_gold_sha256(tmp_path):
    gold = {"status": "reviewed", "_sha256": "raw-file-sha256"}

    report = validate_neo4j_gold._validation_report("Neo4j", gold, tmp_path / "gold.sqlite")

    assert report["gold_sha256"] == "raw-file-sha256"


def test_scene_samples_include_matching_live_results_for_all_five_scenes():
    results = {
        "buyer_awardees": ({"awardees": [{"name": "Supplier A"}]}, [41]),
        "buyer_bidders": ({"top_bidders": [{"name": "Supplier B"}]}, [42]),
        "supplier_co_bidders": ({"top_co_bidders": [{"name": "Supplier C"}]}, [43]),
        "common_award_buyers": ({"buyers": [{"name": "Buyer D"}]}, [[44, 45]]),
        "common_bid_packages": ({"packages": [{"package_code": "P1"}]}, [[46, 47]]),
    }
    sqlite_calls = {
        validate_neo4j_gold._call_key(name, args, {}): result
        for name, (result, args) in results.items()
    }
    neo4j_calls = {key: value for key, value in sqlite_calls.items()}

    samples = validate_neo4j_gold._scene_samples(sqlite_calls, neo4j_calls)

    assert set(samples) == {
        "scene1_buyer_awardees",
        "scene2_buyer_bidders",
        "scene3_supplier_co_bidders",
        "scene4_common_award_buyers",
        "scene5_common_bid_projects",
    }
    assert all(sample["matches"] for sample in samples.values())
    assert samples["scene5_common_bid_projects"]["neo4j"]["packages"] == [
        {"package_code": "P1"}
    ]


def test_scene_samples_prefer_a_nonempty_result():
    empty_key = validate_neo4j_gold._call_key("buyer_awardees", (1,), {})
    populated_key = validate_neo4j_gold._call_key("buyer_awardees", (2,), {})

    samples = validate_neo4j_gold._scene_samples(
        {
            empty_key: {"buyer": {"id": 1}, "awardees": []},
            populated_key: {"buyer": {"id": 2}, "awardees": [{"name": "Supplier"}]},
        },
        {
            empty_key: {"buyer": {"id": 1}, "awardees": []},
            populated_key: {"buyer": {"id": 2}, "awardees": [{"name": "Supplier"}]},
        },
    )

    assert '"buyer_awardees", [2]' in samples["scene1_buyer_awardees"]["call"]
