import json
from copy import deepcopy

import pytest

from app.evaluation import EvaluationConfig
from app.evaluation_diagnostics import diagnose, main
from app.evaluation_policy import MissingValuePolicy


def item(item_id, **values):
    return (
        dict.fromkeys(
            ("product_name", "category", "brand", "model", "unit_price", "quantity", "total_price")
        )
        | {"item_id": item_id, "product_name": "server"}
        | values
    )


def dataset(items, *, predicted=False, package_id="1"):
    return {
        "status": "predicted" if predicted else "reviewed",
        "notices": [
            {
                "notice_id": "n",
                "packages": [
                    {
                        "package_id": package_id,
                        "items": items,
                        "buyer": None,
                        "winners": [],
                        "bidders": [],
                    }
                ],
            }
        ],
    }


def test_partial_pair_has_fixed_gold_coverage_even_when_qa_recall_is_perfect():
    gold = dataset([item("g", model="A")])
    predicted = dataset([item("p", model="B")], predicted=True)
    result = diagnose(gold, predicted)
    assert result["exact_record"]["exact_gold_coverage"] == 0
    assert result["alignment"]["paired_nonexact_item_count"] == 1
    assert result["alignment"]["unpaired_gold_item_count"] == 0
    assert result["fixed_gold_field_slots"]["slot_count"] == 7
    assert result["errors"]["error_field_counts"] == {"model": 1}


def test_tn_sources_and_duplicate_multiset_are_explicit():
    gold = dataset([item("g")])
    predicted = dataset([item("p"), item("p2")], predicted=True)
    before = deepcopy(predicted)
    result = diagnose(gold, predicted)
    assert predicted == before
    assert result["exact_record"] == {
        "exact_matches_current_alignment": 1,
        "exact_precision": 0.5,
        "exact_gold_coverage": 1,
    }
    assert result["tn_sources_observable_only"] == {
        "paired_both_present_empty_field": 6,
        "prediction_only_empty_field": 6,
        "gold_only_empty_field": 0,
    }
    assert result["normalized_exact_duplicate_item_extras"] == {"gold": 0, "predicted": 1}
    assert (
        sum(
            result["fixed_gold_field_slots"]["by_field_status"][f].get("empty", 0)
            for f in result["fixed_gold_field_slots"]["by_field_status"]
        )
        == 6
    )


def test_package_boundary_prevents_exact_matches_and_counts_gold_only_tn():
    result = diagnose(dataset([item("g")]), dataset([item("p")], predicted=True, package_id="2"))
    assert result["alignment"]["maximum_exact_record_matches"] == 0
    assert result["package_sets"]["missing_package_count"] == 1
    assert result["tn_sources_observable_only"]["gold_only_empty_field"] == 6
    assert result["per_notice"][0]["unpaired_gold"] == 1
    assert result["per_notice"][0]["unpaired_prediction"] == 1


def test_max_exact_alignment_is_distinct_from_field_optimum():
    gold = dataset([item("a", product_name="x"), item("b", product_name="x", model="y")])
    predicted = dataset(
        [
            item("a", product_name="x", model="z"),
            item("b", product_name="x"),
        ],
        predicted=True,
    )
    result = diagnose(gold, predicted)
    assert result["alignment"]["current_field_weight_alignment_exact_matches"] == 0
    assert result["alignment"]["maximum_exact_record_matches"] == 1
    assert result["alignment"]["exact_alignment_gap"] == 1


def test_exact_matching_respects_numeric_tolerance_and_null_denominators():
    result = diagnose(
        dataset([item("g", unit_price=100)]),
        dataset([item("p", unit_price=100.005)], predicted=True),
    )
    assert result["alignment"]["maximum_exact_record_matches"] == 1
    empty = diagnose(dataset([]), dataset([], predicted=True))
    assert empty["exact_record"]["exact_gold_coverage"] is None
    assert empty["exact_record"]["exact_precision"] is None
    assert len(empty["per_package"]) == 1


def test_non_strict_missing_semantics_are_rejected():
    with pytest.raises(ValueError, match="strict"):
        diagnose(
            dataset([]),
            dataset([], predicted=True),
            config=EvaluationConfig(missing_value_policy=MissingValuePolicy.MISSING_IS_UNKNOWN),
        )


def test_cli_existing_route_dir_stdout_and_protected_outputs(tmp_path, capsys):
    gold = tmp_path / "gold.reviewed.json"
    gold.write_text(json.dumps(dataset([item("g")])), encoding="utf-8")
    for route in ("rules", "hybrid", "model"):
        (tmp_path / f"predictions-{route}.json").write_text(
            json.dumps(dataset([item("p")], predicted=True)), encoding="utf-8"
        )
    assert main(["--gold", str(gold), "--run-dir", str(tmp_path)]) == 0
    assert set(json.loads(capsys.readouterr().out)["routes"]) == {"rules", "hybrid", "model"}
    original = gold.read_bytes()
    with pytest.raises(SystemExit) as exc:
        main(["--gold", str(gold), "--run-dir", str(tmp_path), "--output", str(gold)])
    assert exc.value.code == 2
    assert gold.read_bytes() == original
    existing_report = tmp_path / "evaluation-model.json"
    existing_report.write_text("keep", encoding="utf-8")
    with pytest.raises(SystemExit):
        main(["--gold", str(gold), "--run-dir", str(tmp_path), "--output", str(existing_report)])
    assert existing_report.read_text(encoding="utf-8") == "keep"


def test_cli_new_output_and_invalid_inputs(tmp_path):
    gold = tmp_path / "g.json"
    predicted = tmp_path / "p.json"
    output = tmp_path / "diagnostics.json"
    gold.write_text(json.dumps(dataset([])), encoding="utf-8")
    predicted.write_text(json.dumps(dataset([], predicted=True)), encoding="utf-8")
    assert (
        main(["--gold", str(gold), "--predictions", str(predicted), "--output", str(output)]) == 0
    )
    assert json.loads(output.read_text(encoding="utf-8"))["counts"]["gold"]["items"] == 0
    with pytest.raises(SystemExit) as exc:
        main(["--gold", str(gold), "--predictions", str(tmp_path / "missing.json")])
    assert exc.value.code == 2
