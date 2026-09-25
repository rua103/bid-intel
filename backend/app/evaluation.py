"""Strict, reproducible local extraction evaluation; never an official competition score."""

from __future__ import annotations

import json
import re
import unicodedata
from collections.abc import Sequence
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

ITEM_FIELDS = (
    "product_name",
    "category",
    "brand",
    "model",
    "unit_price",
    "quantity",
    "total_price",
)
NUMERIC_FIELDS = {"unit_price", "quantity", "total_price", "award_amount"}
NumericValue = str | int | float | None
DISCLAIMER = (
    "Local evaluation only, not an official competition score. Accuracy is the local "
    "open-extraction proxy TP/(TP+FP+FN); true negatives are undefined. Official "
    "matching and scoring rules must be confirmed before claiming official results."
)


def normalize_name(value: str | None) -> str | None:
    """NFKC + casefold + remove Unicode whitespace; retain legal suffixes/punctuation."""
    if value is None:
        return None
    return "".join(unicodedata.normalize("NFKC", value).casefold().split()) or None


def normalize_number(value: NumericValue, *, monetary: bool = False) -> Decimal | None:
    """Parse explicit decimal forms; monetary values use CNY and support 万/亿."""
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (str, int, float, Decimal)):
        # Pydantic wraps ValueError as a validation error; it does not wrap TypeError.
        raise ValueError("numeric values must be finite numbers, decimal strings, or null")  # noqa: TRY004
    text = unicodedata.normalize("NFKC", str(value)).strip()
    if not text:
        return None
    text = re.sub(r"\s+", "", text)
    if monetary:
        text = re.sub(r"^(?:人民币|CNY|RMB|¥)", "", text, flags=re.IGNORECASE)
        text = re.sub(r"(?:人民币|CNY|RMB)$", "", text, flags=re.IGNORECASE)
        text = text.removesuffix("元")
    scale = Decimal(1)
    if monetary and text.endswith(("万", "亿")):
        scale = Decimal(10000 if text[-1] == "万" else 100000000)
        text = text[:-1]
    # Reject malformed grouping (e.g. 1,23), percentages, arbitrary currency/quantity units.
    if not re.fullmatch(r"[+-]?(?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d+)?(?:[eE][+-]?\d+)?", text):
        raise ValueError(f"invalid {'CNY amount' if monetary else 'quantity'}: {value!r}")
    try:
        result = Decimal(text.replace(",", "")) * scale
    except InvalidOperation as exc:
        raise ValueError("invalid finite numeric value") from exc
    if not result.is_finite():
        raise ValueError("numeric value must be finite")
    return result


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, allow_inf_nan=False)


class Identified(StrictModel):
    @field_validator("*", mode="before")
    @classmethod
    def nonblank_ids(cls, value: Any, info: Any) -> Any:
        if (
            info.field_name.endswith("_id")
            and isinstance(value, str)
            and (not value or value != value.strip())
        ):
            raise ValueError("IDs must be nonempty and have no surrounding whitespace")
        return value


class EvaluationItem(Identified):
    item_id: str
    product_name: str | None
    category: str | None
    brand: str | None
    model: str | None
    unit_price: NumericValue
    quantity: NumericValue
    total_price: NumericValue

    @field_validator("unit_price", "quantity", "total_price", mode="before")
    @classmethod
    def valid_number(cls, value: Any, info: Any) -> Any:
        normalize_number(value, monetary=info.field_name != "quantity")
        return value

    @model_validator(mode="after")
    def nonempty_record(self) -> EvaluationItem:
        if all(normalized_field(field, getattr(self, field)) is None for field in ITEM_FIELDS):
            raise ValueError("an item must contain at least one nonempty field")
        return self


class EvaluationEntity(Identified):
    entity_id: str
    name: str

    @field_validator("name")
    @classmethod
    def nonblank_name(cls, value: str) -> str:
        if normalize_name(value) is None:
            raise ValueError("entity name must not be blank")
        return value


class EvaluationWinner(EvaluationEntity):
    award_amount: NumericValue

    @field_validator("award_amount", mode="before")
    @classmethod
    def valid_amount(cls, value: Any) -> Any:
        normalize_number(value, monetary=True)
        return value


class EvaluationBidder(EvaluationEntity):
    outcome: Literal["winner", "nonwinner", "unknown"]


class EvaluationPackage(Identified):
    package_id: str
    items: list[EvaluationItem]
    buyer: EvaluationEntity | None
    winners: list[EvaluationWinner]
    bidders: list[EvaluationBidder]

    @model_validator(mode="after")
    def unique_ids(self) -> EvaluationPackage:
        for field, key in (
            ("items", "item_id"),
            ("winners", "entity_id"),
            ("bidders", "entity_id"),
        ):
            values = [getattr(row, key) for row in getattr(self, field)]
            if len(values) != len(set(values)):
                raise ValueError(f"duplicate {key} in package {field}")
        return self


class EvaluationNotice(Identified):
    notice_id: str
    packages: list[EvaluationPackage]

    @model_validator(mode="after")
    def unique_packages(self) -> EvaluationNotice:
        values = [package.package_id for package in self.packages]
        if len(values) != len(set(values)):
            raise ValueError("duplicate package_id in notice")
        return self


class DatasetBase(StrictModel):
    schema_version: Literal["1.0"] = "1.0"
    notices: list[EvaluationNotice]

    @model_validator(mode="after")
    def unique_notices(self) -> DatasetBase:
        values = [notice.notice_id for notice in self.notices]
        if len(values) != len(set(values)):
            raise ValueError("duplicate notice_id in dataset")
        return self


class GoldDataset(DatasetBase):
    status: Literal["draft", "reviewed"]


class PredictionDataset(DatasetBase):
    status: Literal["predicted"] = "predicted"


class EvaluationConfig(StrictModel):
    monetary_absolute_tolerance: float = Field(default=0.01, ge=0)
    quantity_absolute_tolerance: float = Field(default=0.000001, ge=0)
    relative_tolerance: float = Field(default=1e-9, ge=0)


class Metrics(StrictModel):
    tp: int = 0
    fp: int = 0
    fn: int = 0
    precision: float | None = None
    recall: float | None = None
    f1: float | None = None
    accuracy: float | None = None
    weighted_score: float | None = None


class ItemAlignment(StrictModel):
    notice_id: str
    package_id: str
    gold_item_id: str | None
    predicted_item_id: str | None
    matching_fields: int
    exact_record: bool
    fields: dict[str, Literal["correct", "wrong", "missing", "extra", "empty"]]


class EvaluationReport(StrictModel):
    schema_version: Literal["1.0"] = "1.0"
    evaluation_kind: Literal["local_proxy"] = "local_proxy"
    disclaimer: str = DISCLAIMER
    gold_status: Literal["draft", "reviewed"]
    provisional: bool
    config: EvaluationConfig
    gold_notice_count: int
    predicted_notice_count: int
    missing_notice_ids: list[str]
    extra_notice_ids: list[str]
    field_micro: Metrics
    by_field: dict[str, Metrics]
    records: Metrics
    entities: dict[str, Metrics]
    alignments: list[ItemAlignment]
    warnings: list[str]


def normalized_field(field: str, value: Any) -> str | Decimal | None:
    if field in NUMERIC_FIELDS:
        return normalize_number(value, monetary=field != "quantity")
    return normalize_name(value)


def values_equal(field: str, gold: Any, predicted: Any, config: EvaluationConfig) -> bool:
    left, right = normalized_field(field, gold), normalized_field(field, predicted)
    if left is None or right is None:
        return left is right
    if field not in NUMERIC_FIELDS:
        return left == right
    absolute = (
        config.quantity_absolute_tolerance
        if field == "quantity"
        else config.monetary_absolute_tolerance
    )
    tolerance = max(
        Decimal(str(absolute)), Decimal(str(config.relative_tolerance)) * max(abs(left), abs(right))
    )
    return abs(left - right) <= tolerance


def maximum_weight_alignment(weights: Sequence[Sequence[int]]) -> list[tuple[int, int]]:
    """Hungarian assignment with private dummy columns; zero-weight edges stay unmatched.

    Maximizes total nonnegative integer weight, not greedy local choices. Stable row/column
    order and strict tie comparison make ties reproducible. Callers sort stable IDs first.
    """
    rows = len(weights)
    if not rows:
        return []
    columns = len(weights[0])
    if any(len(row) != columns for row in weights):
        raise ValueError("weight matrix must be rectangular")
    if any(isinstance(w, bool) or not isinstance(w, int) or w < 0 for row in weights for w in row):
        raise ValueError("weights must be nonnegative integers")
    if not columns:
        return []
    # There are always at least as many columns as rows, so all rows can be assigned.
    costs = [[-weight for weight in row] + [0] * rows for row in weights]
    width = columns + rows
    u, v, p, way = [0] * (rows + 1), [0] * (width + 1), [0] * (width + 1), [0] * (width + 1)
    infinity = sum(max(row, default=0) for row in weights) + 1
    for i in range(1, rows + 1):
        p[0] = i
        j0 = 0
        minv, used = [infinity] * (width + 1), [False] * (width + 1)
        while True:
            used[j0] = True
            i0, delta, j1 = p[j0], infinity, 0
            for j in range(1, width + 1):
                if not used[j]:
                    current = costs[i0 - 1][j - 1] - u[i0] - v[j]
                    if current < minv[j]:
                        minv[j], way[j] = current, j0
                    if minv[j] < delta:
                        delta, j1 = minv[j], j
            for j in range(width + 1):
                if used[j]:
                    u[p[j]] += delta
                    v[j] -= delta
                else:
                    minv[j] -= delta
            j0 = j1
            if p[j0] == 0:
                break
        while True:
            j1 = way[j0]
            p[j0] = p[j1]
            j0 = j1
            if j0 == 0:
                break
    return sorted(
        (p[j] - 1, j - 1) for j in range(1, columns + 1) if p[j] and weights[p[j] - 1][j - 1] > 0
    )


def _metrics(counts: Sequence[int]) -> Metrics:
    tp, fp, fn = counts
    precision = tp / (tp + fp) if tp + fp else None
    recall = tp / (tp + fn) if tp + fn else None
    union = tp + fp + fn
    accuracy = tp / union if union else None
    f1 = 2 * tp / (2 * tp + fp + fn) if union else None
    weighted = (
        0.4 * accuracy + 0.3 * precision + 0.3 * recall
        if accuracy is not None and precision is not None and recall is not None
        else None
    )
    return Metrics(
        tp=tp,
        fp=fp,
        fn=fn,
        precision=precision,
        recall=recall,
        f1=f1,
        accuracy=accuracy,
        weighted_score=weighted,
    )


def _field_status(field: str, gold: Any, predicted: Any, config: EvaluationConfig) -> str:
    left, right = normalized_field(field, gold), normalized_field(field, predicted)
    if left is None and right is None:
        return "empty"
    if left is None:
        return "extra"
    if right is None:
        return "missing"
    return "correct" if values_equal(field, gold, predicted, config) else "wrong"


def _add_status(counts: list[int], status: str) -> None:
    if status == "correct":
        counts[0] += 1
    if status in ("wrong", "extra"):
        counts[1] += 1
    if status in ("wrong", "missing"):
        counts[2] += 1


def _entity_counts(
    gold: Sequence[EvaluationEntity],
    predicted: Sequence[EvaluationEntity],
    *,
    outcome: bool = False,
) -> tuple[list[int], list[tuple[int, int]]]:
    weights = [
        [
            int(
                normalize_name(g.name) == normalize_name(p.name)
                and (not outcome or g.outcome == p.outcome)
            )
            for p in predicted
        ]
        for g in gold
    ]
    pairs = maximum_weight_alignment(weights)
    return [len(pairs), len(predicted) - len(pairs), len(gold) - len(pairs)], pairs


def evaluate_dataset(
    gold: GoldDataset | dict[str, Any],
    predicted: PredictionDataset | dict[str, Any],
    config: EvaluationConfig | None = None,
    *,
    allow_draft: bool = False,
) -> EvaluationReport:
    gold = GoldDataset.model_validate(gold)
    predicted = PredictionDataset.model_validate(predicted)
    config = config or EvaluationConfig()
    if gold.status != "reviewed" and not allow_draft:
        raise ValueError("gold status is draft; review first or explicitly set allow_draft=True")
    fields = {field: [0, 0, 0] for field in ITEM_FIELDS}
    records = [0, 0, 0]
    entities = {
        role: [0, 0, 0]
        for role in (
            "buyer",
            "winners",
            "bidders",
            "nonwinners",
            "bidder_outcomes",
            "winner_amounts",
        )
    }
    gold_packages = {(n.notice_id, p.package_id): p for n in gold.notices for p in n.packages}
    pred_packages = {(n.notice_id, p.package_id): p for n in predicted.notices for p in n.packages}
    alignments: list[ItemAlignment] = []
    warnings = [DISCLAIMER]
    if gold.status == "draft":
        warnings.append("PROVISIONAL: draft annotations are not a reviewed gold standard.")
    for scope in sorted(gold_packages.keys() | pred_packages.keys()):
        gp, pp = gold_packages.get(scope), pred_packages.get(scope)
        gi = sorted(gp.items if gp else [], key=lambda item: item.item_id)
        pi = sorted(pp.items if pp else [], key=lambda item: item.item_id)
        weights = [
            [
                sum(
                    _field_status(field, getattr(g, field), getattr(p, field), config) == "correct"
                    for field in ITEM_FIELDS
                )
                for p in pi
            ]
            for g in gi
        ]
        pairs = maximum_weight_alignment(weights)
        matched_gold, matched_pred = {g for g, _ in pairs}, {p for _, p in pairs}
        all_pairs = [(g, p) for g, p in pairs]
        all_pairs += [(g, None) for g in range(len(gi)) if g not in matched_gold]
        all_pairs += [(None, p) for p in range(len(pi)) if p not in matched_pred]
        for gidx, pidx in all_pairs:
            g, p = gi[gidx] if gidx is not None else None, pi[pidx] if pidx is not None else None
            statuses = {
                field: _field_status(
                    field, getattr(g, field, None), getattr(p, field, None), config
                )
                for field in ITEM_FIELDS
            }
            for field, status in statuses.items():
                _add_status(fields[field], status)
            exact = (
                g is not None
                and p is not None
                and all(status in ("correct", "empty") for status in statuses.values())
            )
            if exact:
                records[0] += 1
            else:
                records[1] += int(p is not None)
                records[2] += int(g is not None)
            alignments.append(
                ItemAlignment(
                    notice_id=scope[0],
                    package_id=scope[1],
                    gold_item_id=g.item_id if g else None,
                    predicted_item_id=p.item_id if p else None,
                    matching_fields=sum(status == "correct" for status in statuses.values()),
                    exact_record=exact,
                    fields=statuses,
                )
            )
        for role in ("buyer", "winners", "bidders", "nonwinners", "bidder_outcomes"):

            def role_rows(
                package: EvaluationPackage | None, role_name: str
            ) -> list[EvaluationEntity]:
                if package is None:
                    return []
                if role_name == "buyer":
                    return [package.buyer] if package.buyer else []
                if role_name == "nonwinners":
                    return [row for row in package.bidders if row.outcome == "nonwinner"]
                return sorted(
                    getattr(package, "bidders" if role_name == "bidder_outcomes" else role_name),
                    key=lambda row: row.entity_id,
                )

            gr, pr = role_rows(gp, role), role_rows(pp, role)
            counts, _ = _entity_counts(gr, pr, outcome=role == "bidder_outcomes")
            entities[role] = [a + b for a, b in zip(entities[role], counts)]
        # A winner amount is correct only when both winner name and amount agree.
        gw = sorted(gp.winners if gp else [], key=lambda row: row.entity_id)
        pw = sorted(pp.winners if pp else [], key=lambda row: row.entity_id)
        winner_weights = [
            [
                (2 + int(values_equal("award_amount", g.award_amount, p.award_amount, config)))
                if normalize_name(g.name) == normalize_name(p.name)
                else 0
                for p in pw
            ]
            for g in gw
        ]
        winner_pairs = maximum_weight_alignment(winner_weights)
        mg, mp = {g for g, _ in winner_pairs}, {p for _, p in winner_pairs}
        for gidx, pidx in winner_pairs:
            _add_status(
                entities["winner_amounts"],
                _field_status("award_amount", gw[gidx].award_amount, pw[pidx].award_amount, config),
            )
        for index, row in enumerate(gw):
            if index not in mg and normalize_number(row.award_amount, monetary=True) is not None:
                entities["winner_amounts"][2] += 1
        for index, row in enumerate(pw):
            if index not in mp and normalize_number(row.award_amount, monetary=True) is not None:
                entities["winner_amounts"][1] += 1
    gold_ids = {n.notice_id for n in gold.notices}
    pred_ids = {n.notice_id for n in predicted.notices}
    return EvaluationReport(
        gold_status=gold.status,
        provisional=gold.status == "draft",
        config=config,
        gold_notice_count=len(gold_ids),
        predicted_notice_count=len(pred_ids),
        missing_notice_ids=sorted(gold_ids - pred_ids),
        extra_notice_ids=sorted(pred_ids - gold_ids),
        field_micro=_metrics([sum(row[index] for row in fields.values()) for index in range(3)]),
        by_field={name: _metrics(count) for name, count in fields.items()},
        records=_metrics(records),
        entities={name: _metrics(count) for name, count in entities.items()},
        alignments=alignments,
        warnings=warnings,
    )


def load_gold(path: str | Path) -> GoldDataset:
    return GoldDataset.model_validate_json(Path(path).read_text(encoding="utf-8-sig"))


def load_predictions(path: str | Path) -> PredictionDataset:
    return PredictionDataset.model_validate_json(Path(path).read_text(encoding="utf-8-sig"))


def write_machine_report(report: EvaluationReport, path: str | Path) -> None:
    destination = Path(path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(
        json.dumps(report.model_dump(mode="json"), ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def render_markdown(report: EvaluationReport) -> str:
    def number(value: float | None) -> str:
        return "N/A" if value is None else f"{value:.6f}"

    lines = [
        "# 本地抽取评估报告",
        "",
        report.disclaimer,
        "",
        f"Gold 状态：`{report.gold_status}`；草稿预览：`{str(report.provisional).lower()}`。",
        f"Gold 公告数：{report.gold_notice_count}；预测公告数：{report.predicted_notice_count}。",
        "",
        "Accuracy = TP / (TP + FP + FN)，不计真阴性；",
        "Weighted = 0.4 × Accuracy + 0.3 × Precision + 0.3 × Recall（仅本地代理）。",
        "分母为零时为 N/A；F1 = 2TP/(2TP+FP+FN)。",
        "",
        "| 指标 | TP | FP | FN | Precision | Recall | F1 | Accuracy | Weighted |",
        "|---|---:|---:|---:|---:|---:|---:|---:|---:|",
    ]
    rows = [("字段 micro", report.field_micro), ("完整记录", report.records)]
    rows += [(f"字段/{key}", value) for key, value in report.by_field.items()]
    rows += [(f"实体/{key}", value) for key, value in report.entities.items()]
    for label, metric in rows:
        lines.append(
            f"| {label} | {metric.tp} | {metric.fp} | {metric.fn} | "
            + " | ".join(
                number(getattr(metric, field))
                for field in ("precision", "recall", "f1", "accuracy", "weighted_score")
            )
            + " |"
        )
    lines += [
        "",
        "## 覆盖和对齐",
        "",
        f"缺失公告 ID：{json.dumps(report.missing_notice_ids, ensure_ascii=False)}",
        f"多余公告 ID：{json.dumps(report.extra_notice_ids, ensure_ascii=False)}",
        "同一 notice_id + package_id 内，以七字段正确非空值数量为权重进行全局一对一最大权重匹配。",
        "相同权重按稳定 ID 排序后确定性求解；不同公告/包不匹配，零权重不匹配。",
        "预测重复行按多重集计数，多出来的行计 FP。空值不加 TP，错误非空值同时计 FP 与 FN。",
        "",
        "## 本次参数",
        "",
        "```json",
        json.dumps(report.config.model_dump(), ensure_ascii=False, indent=2),
        "```",
        "",
        "逐条匹配与错误字段请查看配套 JSON 的 alignments。",
        "",
    ]
    if report.warnings:
        lines += ["## 核验提示", ""]
        lines += [f"- {warning}" for warning in report.warnings]
        lines.append("")
    return "\n".join(lines)
