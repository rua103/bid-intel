"""Configurable monetary evaluation and display policy.

The local proxy defaults intentionally mirror the historical evaluator.  Official
policies must be selected explicitly by profile name after the organiser confirms
their rules.
"""

from __future__ import annotations

from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator


class UnitConversion(StrEnum):
    CNY = "cny"
    WAN = "wan"
    YI = "yi"


class IntegerPartMatching(StrEnum):
    EXACT = "exact"
    IGNORE_DECIMAL = "ignore_decimal"


class MissingValuePolicy(StrEnum):
    STRICT = "strict"
    BOTH_MISSING_ONLY = "both_missing_only"
    MISSING_IS_UNKNOWN = "missing_is_unknown"


class EvaluationPolicy(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    amount_storage_precision: int = Field(default=28, ge=0)
    display_decimal_places: int = Field(default=2, ge=0, le=8)
    unit_conversion: UnitConversion = UnitConversion.CNY
    amount_tolerance: Decimal = Field(default=Decimal("0.01"), ge=0)
    integer_part_matching: IntegerPartMatching = IntegerPartMatching.EXACT
    missing_value_policy: MissingValuePolicy = MissingValuePolicy.STRICT

    @field_validator("amount_tolerance", mode="before")
    @classmethod
    def decimal_tolerance(cls, value: Any) -> Decimal:
        try:
            result = Decimal(str(value))
        except (InvalidOperation, ValueError) as exc:
            raise ValueError("amount_tolerance must be a Decimal-compatible value") from exc
        if not result.is_finite() or result < 0:
            raise ValueError("amount_tolerance must be finite and non-negative")
        return result


LOCAL_PROXY_POLICY = EvaluationPolicy()
PROFILES = {"local_proxy": LOCAL_PROXY_POLICY}


def policy_for_profile(profile: str) -> EvaluationPolicy:
    try:
        return PROFILES[profile]
    except KeyError as exc:
        raise ValueError(f"unknown evaluation policy profile: {profile}") from exc


def to_storage_amount(value: Any, policy: EvaluationPolicy = LOCAL_PROXY_POLICY) -> Decimal | None:
    if value is None or (isinstance(value, str) and not value.strip()):
        return None
    amount = value if isinstance(value, Decimal) else Decimal(str(value))
    if not amount.is_finite():
        raise ValueError("amount must be finite")
    scale = Decimal(1)
    if policy.unit_conversion == UnitConversion.WAN:
        scale = Decimal(10000)
    elif policy.unit_conversion == UnitConversion.YI:
        scale = Decimal(100000000)
    return amount * scale


def display_amount(value: Any, policy: EvaluationPolicy = LOCAL_PROXY_POLICY) -> str:
    amount = to_storage_amount(value, policy)
    if amount is None:
        return "—"
    quantum = Decimal(1).scaleb(-policy.display_decimal_places)
    return format(amount.quantize(quantum, rounding=ROUND_HALF_UP), f",.{policy.display_decimal_places}f")


def amounts_equal(left: Any, right: Any, policy: EvaluationPolicy = LOCAL_PROXY_POLICY) -> bool:
    left_amount, right_amount = to_storage_amount(left, policy), to_storage_amount(right, policy)
    if left_amount is None or right_amount is None:
        return left_amount is None and right_amount is None
    if policy.integer_part_matching == IntegerPartMatching.IGNORE_DECIMAL:
        return int(left_amount) == int(right_amount)
    return abs(left_amount - right_amount) <= policy.amount_tolerance
