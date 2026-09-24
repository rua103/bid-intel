import itertools
import json
import random
from copy import deepcopy
from pathlib import Path

import pytest
from pydantic import ValidationError

from app.evaluation import (
    ITEM_FIELDS,
    EvaluationConfig,
    GoldDataset,
    PredictionDataset,
    evaluate_dataset,
    load_gold,
    load_predictions,
    maximum_weight_alignment,
    normalize_name,
    normalize_number,
    render_markdown,
)
from app.evaluation_cli import main


def item(item_id="g1", **changes):
    value = {
        "item_id": item_id,
        "product_name": "服务器",
        "category": "计算设备",
        "brand": "品牌甲",
        "model": "X-1",
        "unit_price": "1,000.00",
        "quantity": "2",
        "total_price": 2000,
    }
    return value | changes


def dataset(items=None, *, status="reviewed", notice_id="notice-a", package_id="package-1"):
    return {
        "schema_version": "1.0",
        "status": status,
        "notices": [
            {
                "notice_id": notice_id,
                "packages": [
                    {
                        "package_id": package_id,
                        "items": [item()] if items is None else items,
                        "buyer": {"entity_id": "buyer-1", "name": "采购单位"},
                        "winners": [
                            {"entity_id": "supplier-1", "name": "供应商甲", "award_amount": 2000}
                        ],
                        "bidders": [
                            {"entity_id": "supplier-1", "name": "供应商甲", "outcome": "winner"},
                            {"entity_id": "supplier-2", "name": "供应商乙", "outcome": "nonwinner"},
                        ],
                    }
                ],
            }
        ],
    }


def prediction(gold):
    value = deepcopy(gold)
    value["status"] = "predicted"
    return value


def package(value):
    return value["notices"][0]["packages"][0]


def test_perfect_match_normalizes_strings_and_money_but_not_ids():
    gold = dataset()
    pred = prediction(gold)
    row = package(pred)["items"][0]
    row.update(
        item_id="independent-prediction-id",
        model=" Ｘ-１ ",
        unit_price="人民币0.1万元",
        total_price="CNY 2,000.00",
        quantity=2.0,
    )
    package(pred)["buyer"]["name"] = "采购 单位"
    report = evaluate_dataset(gold, pred)
    assert report.field_micro.tp == 7
    assert report.records.tp == 1
    assert report.field_micro.weighted_score == pytest.approx(1)
    assert report.entities["nonwinners"].tp == 1
    assert report.entities["winner_amounts"].tp == 1
    assert report.evaluation_kind == "local_proxy"
    assert "not an official" in report.disclaimer


def test_wrong_nonempty_value_counts_fp_and_fn_and_record_failure():
    gold = dataset()
    pred = prediction(gold)
    package(pred)["items"][0]["brand"] = "错误品牌"
    report = evaluate_dataset(gold, pred)
    assert (report.field_micro.tp, report.field_micro.fp, report.field_micro.fn) == (6, 1, 1)
    assert report.by_field["brand"].accuracy == 0
    assert report.records.fp == report.records.fn == 1
    assert report.field_micro.accuracy == pytest.approx(6 / 8)
    assert report.field_micro.weighted_score == pytest.approx(0.4 * 6 / 8 + 0.6 * 6 / 7)


def test_null_blank_and_zero_are_not_confused():
    gold = dataset([item(brand=None, category="  ", total_price=0)])
    pred = prediction(gold)
    package(pred)["items"][0].update(brand="\t", category=None, total_price="0.00")
    report = evaluate_dataset(gold, pred)
    assert report.field_micro.tp == 5
    assert report.by_field["brand"].accuracy is None
    assert report.by_field["total_price"].tp == 1
    assert report.records.tp == 1
    assert report.alignments[0].fields["brand"] == "empty"


def test_missing_and_extra_values_records_and_notices_are_counted():
    gold = dataset([item("g1"), item("g2", product_name="显示屏")])
    pred = prediction(gold)
    package(pred)["items"] = [item("p1", model=None, brand="额外品牌")]
    report = evaluate_dataset(gold, pred)
    assert report.records.fn == 2 and report.records.fp == 1
    assert report.by_field["model"].fn == 2
    assert report.by_field["brand"].fp == 1 and report.by_field["brand"].fn == 2
    missing = evaluate_dataset(gold, {"status": "predicted", "notices": []})
    assert missing.field_micro.fn == 14
    assert missing.records.fn == 2
    assert missing.missing_notice_ids == ["notice-a"]
    extra = evaluate_dataset({"status": "reviewed", "notices": []}, pred)
    assert extra.field_micro.fp == 6
    assert extra.records.fp == 1
    assert extra.extra_notice_ids == ["notice-a"]


def test_duplicate_prediction_rows_are_not_silently_deduplicated():
    gold = dataset()
    pred = prediction(gold)
    package(pred)["items"].append(item("duplicate-value-different-id"))
    package(pred)["bidders"].append(
        {"entity_id": "duplicate", "name": "供应商乙", "outcome": "nonwinner"}
    )
    report = evaluate_dataset(gold, pred)
    assert (report.records.tp, report.records.fp, report.records.fn) == (1, 1, 0)
    assert report.field_micro.fp == 7
    assert report.entities["nonwinners"].fp == 1
    assert report.entities["bidders"].fp == 1


@pytest.mark.parametrize("change", ["notice", "package"])
def test_identical_values_cannot_match_across_notice_or_package(change):
    gold = dataset()
    pred = prediction(gold)
    if change == "notice":
        pred["notices"][0]["notice_id"] = "different-notice"
    else:
        package(pred)["package_id"] = "different-package"
    report = evaluate_dataset(gold, pred)
    assert report.field_micro.tp == 0
    assert report.field_micro.fp == report.field_micro.fn == 7
    assert report.entities["buyer"].fp == report.entities["buyer"].fn == 1


def test_maximum_assignment_avoids_greedy_trap():
    assert maximum_weight_alignment([[7, 6], [6, 0]]) == [(0, 1), (1, 0)]
    assert maximum_weight_alignment([[0, 0], [0, 0]]) == []
    assert maximum_weight_alignment([[], []]) == []
    assert maximum_weight_alignment([]) == []
    with pytest.raises(ValueError):
        maximum_weight_alignment([[1], [1, 2]])


def test_assignment_agrees_with_brute_force_on_small_matrices():
    rng = random.Random(23)
    for rows, columns in itertools.product(range(1, 5), repeat=2):
        for _ in range(8):
            weights = [[rng.randrange(8) for _ in range(columns)] for _ in range(rows)]
            best = max(
                sum(weights[i][j] for i, j in enumerate(assignment) if j < columns)
                for assignment in itertools.permutations(range(columns + rows), rows)
            )
            pairs = maximum_weight_alignment(weights)
            assert sum(weights[i][j] for i, j in pairs) == best
            assert len({i for i, _ in pairs}) == len(pairs)
            assert len({j for _, j in pairs}) == len(pairs)


def test_item_alignment_is_optimal_and_input_order_independent():
    # Greedy g1->p1 gets 3; global g1->p2 and g2->p1 gets 2+2.
    empty = {field: None for field in ITEM_FIELDS}
    gold = dataset(
        [
            dict(empty, item_id="g1", product_name="A", category="B", brand="C", model="D"),
            dict(empty, item_id="g2", product_name="A", category="X", brand="C", model="Y"),
        ]
    )
    pred = prediction(gold)
    package(pred)["items"] = [
        dict(empty, item_id="p1", product_name="A", category="B", brand="C"),
        dict(empty, item_id="p2", category="B", model="D"),
    ]
    report = evaluate_dataset(gold, pred)
    assert report.field_micro.tp == 4
    assert [(a.gold_item_id, a.predicted_item_id) for a in report.alignments] == [
        ("g1", "p2"),
        ("g2", "p1"),
    ]
    package(gold)["items"].reverse()
    package(pred)["items"].reverse()
    assert evaluate_dataset(gold, pred).model_dump() == report.model_dump()


def test_tolerance_is_explicit_and_names_are_conservative():
    gold = dataset()
    pred = prediction(gold)
    package(pred)["items"][0].update(unit_price="1000.01", quantity="2.000002")
    report = evaluate_dataset(gold, pred)
    assert report.by_field["unit_price"].tp == 1
    assert report.by_field["quantity"].fp == report.by_field["quantity"].fn == 1
    exact = evaluate_dataset(
        gold,
        pred,
        EvaluationConfig(
            monetary_absolute_tolerance=0.0, quantity_absolute_tolerance=0.0, relative_tolerance=0.0
        ),
    )
    assert exact.by_field["unit_price"].fp == 1
    assert normalize_name("甲有限公司") != normalize_name("甲有限公司北京分公司")
    assert normalize_name("甲（北京）公司") == normalize_name("甲(北京)公司")


def test_entity_roles_outcomes_and_amounts_do_not_leak_between_each_other():
    gold = dataset()
    pred = prediction(gold)
    package(pred)["buyer"]["name"] = "供应商甲"
    package(pred)["bidders"][1]["outcome"] = "unknown"
    package(pred)["winners"][0]["award_amount"] = 2001
    report = evaluate_dataset(gold, pred)
    assert report.entities["buyer"].fp == report.entities["buyer"].fn == 1
    assert report.entities["winners"].tp == 1
    assert report.entities["bidders"].tp == 2
    assert report.entities["nonwinners"].fn == 1
    assert report.entities["bidder_outcomes"].fp == report.entities["bidder_outcomes"].fn == 1
    assert report.entities["winner_amounts"].fp == report.entities["winner_amounts"].fn == 1


def test_draft_requires_explicit_preview_and_empty_metrics_are_undefined():
    gold = {"status": "draft", "notices": []}
    pred = {"status": "predicted", "notices": []}
    with pytest.raises(ValueError, match="review first"):
        evaluate_dataset(gold, pred)
    report = evaluate_dataset(gold, pred, allow_draft=True)
    assert report.provisional is True
    assert report.field_micro.accuracy is None
    assert report.field_micro.weighted_score is None
    assert report.field_micro.f1 is None
    assert "PROVISIONAL" in " ".join(report.warnings)
    assert "N/A" in render_markdown(report)


@pytest.mark.parametrize(
    "mutate",
    [
        lambda d: d.update(schema_version="2.0"),
        lambda d: d.update(extra="not allowed"),
        lambda d: package(d)["items"][0].pop("brand"),
        lambda d: package(d)["items"][0].update(quantity=True),
        lambda d: package(d)["items"][0].update(quantity=float("inf")),
        lambda d: package(d)["items"][0].update(unit_price="USD 1000"),
        lambda d: package(d)["items"][0].update(quantity="2台"),
        lambda d: package(d)["items"][0].update(unit_price="1,00"),
        lambda d: package(d)["items"][0].update(**{field: None for field in ITEM_FIELDS}),
        lambda d: d["notices"].append(deepcopy(d["notices"][0])),
        lambda d: d["notices"][0]["packages"].append(deepcopy(package(d))),
        lambda d: package(d)["items"].append(deepcopy(package(d)["items"][0])),
        lambda d: package(d)["buyer"].update(name="   "),
        lambda d: d["notices"][0].update(notice_id=12),
        lambda d: d["notices"][0].update(notice_id=" space "),
    ],
)
def test_strict_contract_rejects_ambiguous_or_invalid_data(mutate):
    gold = dataset()
    mutate(gold)
    with pytest.raises(ValidationError):
        GoldDataset.model_validate(gold)


def test_predictions_cannot_be_used_as_reviewed_gold():
    with pytest.raises(ValidationError):
        GoldDataset.model_validate(prediction(dataset()))
    with pytest.raises(ValidationError):
        PredictionDataset.model_validate(dataset())


def test_examples_validate_and_cli_outputs_both_formats_without_overwriting(tmp_path):
    examples = Path(__file__).resolve().parents[2] / "examples"
    gold_path = examples / "evaluation_gold.json"
    pred_path = examples / "evaluation_predictions.json"
    assert load_gold(gold_path).status == "reviewed"
    assert load_predictions(pred_path).status == "predicted"
    json_output, md_output = tmp_path / "report.json", tmp_path / "report.md"
    args = [
        "--gold",
        str(gold_path),
        "--predictions",
        str(pred_path),
        "--json-output",
        str(json_output),
        "--markdown-output",
        str(md_output),
    ]
    assert main(args) == 0
    result = json.loads(json_output.read_text(encoding="utf-8"))
    assert result["evaluation_kind"] == "local_proxy"
    assert result["records"]["fp"] > 0  # Demonstration fixture intentionally has an error.
    assert "本地" in md_output.read_text(encoding="utf-8")
    assert main(args[:-4] + ["--json-output", str(gold_path)]) == 2


def test_numeric_normalization_does_not_round_before_comparing():
    assert str(normalize_number("￥ 1.234万元", monetary=True)) == "12340.000"
    assert normalize_number("0") == 0
    assert normalize_number(" ") is None
    with pytest.raises(ValueError):
        normalize_number("NaN")
