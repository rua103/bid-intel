from __future__ import annotations

from copy import deepcopy

import pytest

from app.evaluation import EvaluationConfig, evaluate_dataset, render_markdown
from app.evaluation_policy import MissingValuePolicy

FIELDS = (
    "product_name",
    "category",
    "brand",
    "model",
    "unit_price",
    "quantity",
    "total_price",
)


def _gold(*, brand: str | None = "品牌甲") -> dict:
    item = {
        "item_id": "g1",
        "product_name": "服务器",
        "category": "计算设备",
        "brand": brand,
        "model": "X-1",
        "unit_price": "1000.00",
        "quantity": "2",
        "total_price": "2000.00",
    }
    return {
        "schema_version": "1.0",
        "status": "reviewed",
        "notices": [
            {
                "notice_id": "n1",
                "packages": [
                    {
                        "package_id": "default",
                        "items": [item],
                        "buyer": None,
                        "winners": [],
                        "bidders": [],
                    }
                ],
            }
        ],
    }


def _prediction(gold: dict) -> dict:
    value = deepcopy(gold)
    value["status"] = "predicted"
    return value


def test_official_qa_uses_mutually_exclusive_confusion_matrix():
    gold = _gold()
    predicted = _prediction(gold)
    predicted["notices"][0]["packages"][0]["items"][0]["brand"] = "错误品牌"
    report = evaluate_dataset(
        gold,
        predicted,
        EvaluationConfig(policy_profile="official_qa"),
    )

    assert report.evaluation_kind == "official_qa"
    assert (report.field_micro.tp, report.field_micro.fp, report.field_micro.tn, report.field_micro.fn) == (
        6,
        1,
        0,
        0,
    )
    assert report.field_micro.accuracy == pytest.approx(6 / 7)
    assert report.field_micro.precision == pytest.approx(6 / 7)
    assert report.field_micro.recall == 1
    assert "TN" in render_markdown(report)
    assert "非空但值错误只计 FP" in " ".join(report.warnings)


def test_official_qa_counts_both_missing_as_tn_and_missing_as_fn():
    gold = _gold(brand=None)
    predicted = _prediction(gold)
    item = predicted["notices"][0]["packages"][0]["items"][0]
    item["model"] = None
    report = evaluate_dataset(
        gold,
        predicted,
        EvaluationConfig(policy_profile="official_qa"),
    )

    assert report.by_field["brand"].tn == 1
    assert report.by_field["model"].fn == 1
    assert report.by_field["model"].tn == 0


def test_official_qa_rejects_unknown_missing_value_semantics():
    with pytest.raises(ValueError, match="strict missing-value"):
        evaluate_dataset(
            _gold(),
            _prediction(_gold()),
            EvaluationConfig(
                policy_profile="official_qa",
                missing_value_policy=MissingValuePolicy.MISSING_IS_UNKNOWN,
            ),
        )


@pytest.mark.parametrize("side", ["gold", "prediction"])
@pytest.mark.parametrize("profile", ["local_proxy", "official_qa"])
def test_all_empty_items_are_rejected_before_scoring(side, profile):
    gold = _gold()
    prediction = _prediction(gold)
    target = gold if side == "gold" else prediction
    target["notices"][0]["packages"][0]["items"][0].update(dict.fromkeys(FIELDS))
    with pytest.raises(ValueError, match="at least one nonempty field"):
        evaluate_dataset(gold, prediction, EvaluationConfig(policy_profile=profile))


@pytest.mark.parametrize("brand", [None, "错误品牌"])
def test_official_qa_inexact_matched_record_is_one_fp(brand):
    gold = _gold()
    prediction = _prediction(gold)
    prediction["notices"][0]["packages"][0]["items"][0]["brand"] = brand
    report = evaluate_dataset(gold, prediction, EvaluationConfig(policy_profile="official_qa"))
    assert (report.records.tp, report.records.fp, report.records.fn, report.records.tn) == (0, 1, 0, 0)
    assert report.by_field["brand"].fn == int(brand is None)
    assert report.by_field["brand"].fp == int(brand is not None)
