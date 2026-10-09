"""Read-only diagnostics for auditing evaluation semantics.

This module deliberately does not alter :mod:`app.evaluation` or any saved report.
It gives fixed-denominator coverage and alignment diagnostics that are otherwise
easy to confuse with the team's historical local_proxy/official_qa metrics.
The input files are never written; ``--output`` only creates a new file.
"""

from __future__ import annotations

import argparse
import json
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

from app.evaluation import (
    ITEM_FIELDS,
    EvaluationConfig,
    GoldDataset,
    PredictionDataset,
    evaluate_dataset,
    load_gold,
    load_predictions,
    maximum_weight_alignment,
    normalized_field,
    values_equal,
)
from app.evaluation_policy import MissingValuePolicy


def _items(dataset: GoldDataset | PredictionDataset) -> dict[tuple[str, str], list[Any]]:
    return {
        (notice.notice_id, package.package_id): list(package.items)
        for notice in dataset.notices
        for package in notice.packages
    }


def _entity_counts(dataset: GoldDataset | PredictionDataset) -> dict[str, int]:
    return {
        "notices": len(dataset.notices),
        "packages": sum(len(notice.packages) for notice in dataset.notices),
        "items": sum(
            len(package.items) for notice in dataset.notices for package in notice.packages
        ),
        "buyers": sum(
            package.buyer is not None for notice in dataset.notices for package in notice.packages
        ),
        "winners": sum(
            len(package.winners) for notice in dataset.notices for package in notice.packages
        ),
        "bidders": sum(
            len(package.bidders) for notice in dataset.notices for package in notice.packages
        ),
    }


def _exact(gold_item: Any, predicted_item: Any, config: EvaluationConfig) -> bool:
    return all(
        values_equal(field, getattr(gold_item, field), getattr(predicted_item, field), config)
        for field in ITEM_FIELDS
    )


def _max_exact_matches(
    gold_items: list[Any], predicted_items: list[Any], config: EvaluationConfig
) -> int:
    """Maximum cardinality of exact seven-field item matches in one package."""
    if not gold_items or not predicted_items:
        return 0
    weights = [
        [int(_exact(gold, predicted, config)) for predicted in predicted_items]
        for gold in gold_items
    ]
    return len(maximum_weight_alignment(weights))


def _record_alignment_diagnostics(
    gold: GoldDataset,
    predicted: PredictionDataset,
    report: Any,
) -> dict[str, Any]:
    gold_items, predicted_items = _items(gold), _items(predicted)
    scopes = sorted(set(gold_items) | set(predicted_items))
    current_exact = sum(alignment.exact_record for alignment in report.alignments)
    max_exact = sum(
        _max_exact_matches(gold_items.get(scope, []), predicted_items.get(scope, []), report.config)
        for scope in scopes
    )
    paired = sum(
        alignment.gold_item_id is not None and alignment.predicted_item_id is not None
        for alignment in report.alignments
    )
    unpaired_gold = sum(
        alignment.gold_item_id is not None and alignment.predicted_item_id is None
        for alignment in report.alignments
    )
    unpaired_prediction = sum(
        alignment.gold_item_id is None and alignment.predicted_item_id is not None
        for alignment in report.alignments
    )
    return {
        "paired_item_count": paired,
        "paired_nonexact_item_count": paired - current_exact,
        "unpaired_gold_item_count": unpaired_gold,
        "unpaired_prediction_item_count": unpaired_prediction,
        "current_field_weight_alignment_exact_matches": current_exact,
        "maximum_exact_record_matches": max_exact,
        "exact_alignment_gap": max_exact - current_exact,
        "alignment_scope_count": len(scopes),
    }


def _tn_sources(report: Any) -> dict[str, int]:
    """Break observable QA TN field slots down by alignment provenance.

    These are observable empty slots only.  They are not the complement of the
    predicted set and cannot stand for an enumeration of all absent values.
    """
    counts = Counter(
        {
            "paired_both_present_empty_field": 0,
            "prediction_only_empty_field": 0,
            "gold_only_empty_field": 0,
        }
    )
    for alignment in report.alignments:
        if alignment.gold_item_id is not None and alignment.predicted_item_id is not None:
            source = "paired_both_present_empty_field"
        elif alignment.gold_item_id is None and alignment.predicted_item_id is not None:
            source = "prediction_only_empty_field"
        elif alignment.gold_item_id is not None and alignment.predicted_item_id is None:
            source = "gold_only_empty_field"
        else:
            continue
        counts[source] += sum(status == "empty" for status in alignment.fields.values())
    return dict(counts)


def _error_diagnostics(report: Any) -> dict[str, Any]:
    fields = Counter()
    statuses = Counter()
    for alignment in report.alignments:
        for field, status in alignment.fields.items():
            statuses[status] += 1
            if status in {"wrong", "missing", "extra"}:
                fields[field] += 1
    return {
        "field_status_counts": dict(sorted(statuses.items())),
        "error_field_counts": dict(sorted(fields.items())),
    }


def _fixed_gold_slots(report: Any, gold_items: int) -> dict[str, Any]:
    by_field = {field: Counter() for field in ITEM_FIELDS}
    for alignment in report.alignments:
        if alignment.gold_item_id is not None:
            for field, status in alignment.fields.items():
                by_field[field][status] += 1
    return {
        "slot_count": gold_items * len(ITEM_FIELDS),
        "by_field_status": {field: dict(counts) for field, counts in by_field.items()},
        "definition": (
            "Only the seven slots of each Gold item; excludes prediction-only rows. "
            "Alignment still depends on predictions. No accuracy or weighted score is asserted."
        ),
    }


def _duplicate_items(dataset: GoldDataset | PredictionDataset) -> int:
    extra = 0
    for items in _items(dataset).values():
        counts = Counter(
            tuple(normalized_field(field, getattr(item, field)) for field in ITEM_FIELDS)
            for item in items
        )
        extra += sum(count - 1 for count in counts.values())
    return extra


def _package_diagnostics(
    report: Any, gold: GoldDataset, predicted: PredictionDataset
) -> list[dict[str, Any]]:
    rows: dict[tuple[str, str], dict[str, Any]] = defaultdict(
        lambda: {
            "notice_id": "",
            "package_id": "",
            "alignment_count": 0,
            "gold_item_count": 0,
            "predicted_item_count": 0,
            "exact_matches": 0,
            "paired_nonexact": 0,
            "wrong_fields": 0,
            "missing_fields": 0,
            "extra_fields": 0,
            "unpaired_gold": 0,
            "unpaired_prediction": 0,
        }
    )
    for alignment in report.alignments:
        key = (alignment.notice_id, alignment.package_id)
        row = rows[key]
        row["notice_id"], row["package_id"] = key
        row["alignment_count"] += 1
        row["exact_matches"] += int(alignment.exact_record)
        row["gold_item_count"] += int(alignment.gold_item_id is not None)
        row["predicted_item_count"] += int(alignment.predicted_item_id is not None)
        row["paired_nonexact"] += int(
            alignment.gold_item_id is not None
            and alignment.predicted_item_id is not None
            and not alignment.exact_record
        )
        for status in alignment.fields.values():
            if status == "wrong":
                row["wrong_fields"] += 1
            elif status == "missing":
                row["missing_fields"] += 1
            elif status == "extra":
                row["extra_fields"] += 1
        row["unpaired_gold"] += int(
            alignment.gold_item_id is not None and alignment.predicted_item_id is None
        )
        row["unpaired_prediction"] += int(
            alignment.gold_item_id is None and alignment.predicted_item_id is not None
        )
    for scope in set(_items(gold)) | set(_items(predicted)):
        row = rows[scope]
        row["notice_id"], row["package_id"] = scope
    return [rows[key] for key in sorted(rows)]


def _notice_diagnostics(packages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    notices: dict[str, Counter] = defaultdict(Counter)
    for package in packages:
        for field, value in package.items():
            if field not in {"notice_id", "package_id"}:
                notices[package["notice_id"]][field] += value
    return [dict(notice_id=notice_id, **notices[notice_id]) for notice_id in sorted(notices)]


def _package_set_diagnostics(gold: GoldDataset, predicted: PredictionDataset) -> dict[str, Any]:
    gold_scopes = {(n.notice_id, p.package_id) for n in gold.notices for p in n.packages}
    prediction_scopes = {(n.notice_id, p.package_id) for n in predicted.notices for p in n.packages}
    return {
        "gold_package_count": len(gold_scopes),
        "predicted_package_count": len(prediction_scopes),
        "missing_package_count": len(gold_scopes - prediction_scopes),
        "extra_package_count": len(prediction_scopes - gold_scopes),
        "missing_package_scopes": [
            list(scope) for scope in sorted(gold_scopes - prediction_scopes)
        ],
        "extra_package_scopes": [list(scope) for scope in sorted(prediction_scopes - gold_scopes)],
    }


def diagnose(
    gold: GoldDataset | dict[str, Any],
    predicted: PredictionDataset | dict[str, Any],
    *,
    profile: str = "official_qa",
    config: EvaluationConfig | None = None,
    allow_draft: bool = False,
) -> dict[str, Any]:
    """Return semantic diagnostics without modifying either input dataset."""
    gold_model = gold if isinstance(gold, GoldDataset) else GoldDataset.model_validate(gold)
    predicted_model = (
        predicted
        if isinstance(predicted, PredictionDataset)
        else PredictionDataset.model_validate(predicted)
    )
    selected_config = config or EvaluationConfig(policy_profile=profile)
    if selected_config.missing_value_policy != MissingValuePolicy.STRICT:
        raise ValueError("diagnostics require strict missing-value classification")
    report = evaluate_dataset(gold_model, predicted_model, selected_config, allow_draft=allow_draft)
    gold_counts, prediction_counts = _entity_counts(gold_model), _entity_counts(predicted_model)
    exact_matches = sum(alignment.exact_record for alignment in report.alignments)
    predicted_item_count, gold_item_count = prediction_counts["items"], gold_counts["items"]
    package_diagnostics = _package_diagnostics(report, gold_model, predicted_model)
    return {
        "schema_version": 1,
        "diagnostic_kind": "evaluation_semantics_audit",
        "metric_profile": report.evaluation_kind,
        "disclaimer": (
            "只读诊断；exact_precision 分母为全部预测条目，exact_gold_coverage 分母为固定 Gold 条目；"
            "不是旧 profile 的 precision/recall/weighted，也不是官方成绩。"
        ),
        "gold_status": report.gold_status,
        "config": selected_config.model_dump(mode="json"),
        "counts": {"gold": gold_counts, "predicted": prediction_counts},
        "exact_record": {
            "exact_matches_current_alignment": exact_matches,
            "exact_precision": exact_matches / predicted_item_count
            if predicted_item_count
            else None,
            "exact_gold_coverage": exact_matches / gold_item_count if gold_item_count else None,
        },
        "alignment": _record_alignment_diagnostics(gold_model, predicted_model, report),
        "package_sets": _package_set_diagnostics(gold_model, predicted_model),
        "tn_sources_observable_only": _tn_sources(report),
        "fixed_gold_field_slots": _fixed_gold_slots(report, gold_item_count),
        "observable_field_slot_count": len(report.alignments) * len(ITEM_FIELDS),
        "normalized_exact_duplicate_item_extras": {
            "gold": _duplicate_items(gold_model),
            "predicted": _duplicate_items(predicted_model),
        },
        "errors": _error_diagnostics(report),
        "per_package": package_diagnostics,
        "per_notice": _notice_diagnostics(package_diagnostics),
        "interpretation_limits": [
            "TN 只来自当前对齐槽位中双方均为空的字段；预测独有/Gold独有行的空字段会随预测集合变化。",
            "额外预测行无法仅凭评测结果判为幻觉，可能是历史材料、真遗漏、范围不同或重复变体，需回源逐条审查。",
            "七字段严格 exact 受正文泛项目名与附件具体名称的 Gold 标注粒度影响；该粒度未由官方形式化。",
            "用此 holdout 调参会破坏独立性；后续应冻结代码/提示并采用新来源的新 holdout。",
        ],
    }


def diagnose_files(
    gold_path: str | Path,
    predictions_path: str | Path,
    *,
    profile: str = "official_qa",
    allow_draft: bool = False,
) -> dict[str, Any]:
    return diagnose(
        load_gold(gold_path),
        load_predictions(predictions_path),
        profile=profile,
        allow_draft=allow_draft,
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Read-only evaluation semantics diagnostics")
    parser.add_argument("--gold", required=True, type=Path)
    inputs = parser.add_mutually_exclusive_group(required=True)
    inputs.add_argument("--predictions", type=Path)
    inputs.add_argument(
        "--run-dir", type=Path, help="Read existing predictions-{rules,hybrid,model}.json"
    )
    parser.add_argument("--profile", choices=("official_qa", "local_proxy"), default="official_qa")
    parser.add_argument("--allow-draft", action="store_true")
    parser.add_argument(
        "--output", type=Path, help="Optional JSON output; stdout is used otherwise"
    )
    args = parser.parse_args(argv)
    gold_path = args.gold.resolve()
    prediction_paths = (
        {
            route: args.run_dir / f"predictions-{route}.json"
            for route in ("rules", "hybrid", "model")
        }
        if args.run_dir
        else {"single": args.predictions}
    )
    protected = {gold_path, *(path.resolve() for path in prediction_paths.values())}
    if args.output and (args.output.resolve() in protected or args.output.exists()):
        parser.error(
            "--output must be a new file; existing inputs and reports are never overwritten"
        )
    try:
        results = {
            route: diagnose_files(
                gold_path, path, profile=args.profile, allow_draft=args.allow_draft
            )
            for route, path in prediction_paths.items()
        }
        result = {"routes": results} if args.run_dir else results["single"]
        text = json.dumps(result, ensure_ascii=False, indent=2) + "\n"
        if args.output:
            args.output.parent.mkdir(parents=True, exist_ok=True)
            with args.output.open("x", encoding="utf-8") as output:
                output.write(text)
        else:
            print(text, end="")
    except (OSError, ValueError) as exc:
        parser.exit(2, f"evaluation diagnostics failed: {exc}\n")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
