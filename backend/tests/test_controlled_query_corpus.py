import json
from pathlib import Path


def test_local_controlled_query_corpus_has_at_least_50_cases_and_required_categories():
    path = Path(__file__).parent / "fixtures" / "controlled_query_questions.json"
    cases = json.loads(path.read_text(encoding="utf-8"))
    assert len(cases) >= 50
    assert sum(case["valid"] for case in cases) >= 20
    assert sum(not case["valid"] for case in cases) >= 20
    assert {case["scene"] for case in cases if case["valid"]} == {
        "buyer_awardees", "buyer_bidders", "supplier_co_bidders", "common_buyers", "common_projects"
    }
