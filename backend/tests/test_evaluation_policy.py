import json
from decimal import Decimal

import pytest

from app.evaluation import (
    EvaluationConfig,
    evaluate_with_policy_profile,
    render_markdown,
    values_equal,
    write_machine_report,
)
from app.evaluation_policy import (
    EvaluationPolicy,
    IntegerPartMatching,
    MissingValuePolicy,
    amounts_equal,
    display_amount,
    policy_for_profile,
    to_storage_amount,
)


@pytest.mark.parametrize(("value", "unit", "expected"), [("1", "cny", "1"), ("1", "wan", "10000"), ("1", "yi", "100000000")])
def test_unit_conversion_uses_decimal(value, unit, expected):
    policy = EvaluationPolicy(unit_conversion=unit)
    assert to_storage_amount(value, policy) == Decimal(expected)


@pytest.mark.parametrize(("value", "expected"), [("0.004", "0.00"), ("0.005", "0.01")])
def test_display_rounds_only_at_display_boundary(value, expected):
    policy = EvaluationPolicy(display_decimal_places=2)
    assert to_storage_amount(value, policy) == Decimal(value)
    assert display_amount(value, policy) == expected


def test_tolerance_and_integer_matching_are_configurable():
    assert amounts_equal("1.004", "1.005", EvaluationPolicy(amount_tolerance=Decimal("0.001")))
    assert not amounts_equal("1.004", "1.006", EvaluationPolicy(amount_tolerance=Decimal("0.001")))
    integer_policy = EvaluationPolicy(
        integer_part_matching=IntegerPartMatching.IGNORE_DECIMAL, amount_tolerance=Decimal(0)
    )
    assert amounts_equal("12.01", "12.99", integer_policy)


def test_missing_values_and_profile_are_explicit():
    policy = EvaluationPolicy(missing_value_policy=MissingValuePolicy.MISSING_IS_UNKNOWN)
    assert policy.missing_value_policy == MissingValuePolicy.MISSING_IS_UNKNOWN
    assert policy_for_profile("local_proxy") == EvaluationPolicy()
    with pytest.raises(ValueError):
        policy_for_profile("official")


def test_decimal_tolerance_reaches_evaluator_and_serializable_reports(tmp_path):
    config = EvaluationConfig(monetary_absolute_tolerance="0.001", relative_tolerance=0.0)
    assert config.monetary_absolute_tolerance == Decimal("0.001")
    assert values_equal("unit_price", "1.004", "1.005", config)
    assert not values_equal("unit_price", "1.004", "1.006", config)
    report = evaluate_with_policy_profile(
        {"status": "reviewed", "notices": []}, {"notices": []}, "local_proxy"
    )
    destination = tmp_path / "report.json"
    write_machine_report(report, destination)
    saved = json.loads(destination.read_text(encoding="utf-8"))
    assert saved["config"]["monetary_absolute_tolerance"] == "0.01"
    assert saved["config"]["policy_profile"] == "local_proxy"
    assert '"monetary_absolute_tolerance": "0.01"' in render_markdown(report)
