"""Merge independently annotated gold JSON files against canonical predictions.

Run from ``backend`` with::

    python -m app.annotation_merge_cli --gold-files part-1.json part-2.json \
        --predictions predictions.json --output gold.merged.json
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from collections import defaultdict
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from app.evaluation import GoldDataset, PredictionDataset


def _resolved(path: Path) -> Path:
    return path.expanduser().resolve()


def _read_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"无法读取 JSON {path}: {exc}") from exc


def _load_gold(path: Path) -> GoldDataset:
    try:
        dataset = GoldDataset.model_validate(_read_json(path))
    except ValidationError as exc:
        raise ValueError(f"Gold JSON 无效或 status 不是 draft/reviewed（{path}）: {exc}") from exc
    if dataset.status != "reviewed":
        raise ValueError(f"Gold 必须是 reviewed 状态，拒绝 draft：{path}")
    if not dataset.notices:
        raise ValueError(f"Gold 文件没有公告：{path}")
    return dataset


def _load_predictions(path: Path) -> PredictionDataset:
    try:
        dataset = PredictionDataset.model_validate(_read_json(path))
    except ValidationError as exc:
        raise ValueError(f"预测 JSON 无效或 status 不是 predicted（{path}）: {exc}") from exc
    if not dataset.notices:
        raise ValueError(f"预测文件没有公告：{path}")
    return dataset


def _duplicates(
    datasets: list[tuple[Path, GoldDataset]],
) -> dict[str, list[str]]:
    sources: dict[str, list[str]] = defaultdict(list)
    for path, dataset in datasets:
        for notice in dataset.notices:
            sources[notice.notice_id].append(str(path))
    return {
        notice_id: sorted(paths)
        for notice_id, paths in sorted(sources.items())
        if len(paths) > 1
    }


def _format_duplicates(conflicts: dict[str, list[str]]) -> str:
    lines = ["检测到重复 notice_id；没有选择或覆盖任何标注："]
    for notice_id, paths in conflicts.items():
        lines.append(f"- {notice_id}")
        lines.extend(f"  - {path}" for path in paths)
    lines.append("请人工裁决冲突后再合并。")
    return "\n".join(lines)


def _ensure_disjoint_paths(
    gold_paths: list[Path], predictions_path: Path, output_path: Path, conflicts_path: Path | None
) -> None:
    inputs = {_resolved(path) for path in [*gold_paths, predictions_path]}
    destinations = [output_path, *([conflicts_path] if conflicts_path is not None else [])]
    resolved_destinations = [_resolved(path) for path in destinations]
    if len(resolved_destinations) != len(set(resolved_destinations)):
        raise ValueError("merged gold 输出与冲突报告必须使用不同路径")
    if inputs.intersection(resolved_destinations):
        raise ValueError("输出路径不能覆盖任何 gold 输入或 canonical predictions 文件")


def _validate_annotation_content(gold: GoldDataset) -> None:
    if not any(
        package.items or package.buyer is not None or package.winners or package.bidders
        for notice in gold.notices
        for package in notice.packages
    ):
        raise ValueError("合并后的 gold 没有任何人工标注记录，拒绝生成空 gold")


def validate_id_sets(
    gold: GoldDataset, predictions: PredictionDataset, *, require_complete: bool = False
) -> list[dict[str, Any]]:
    gold_notice_ids = {notice.notice_id for notice in gold.notices}
    prediction_notice_ids = {notice.notice_id for notice in predictions.notices}
    missing = sorted(gold_notice_ids - prediction_notice_ids)
    extra = sorted(prediction_notice_ids - gold_notice_ids) if require_complete else []
    issues: list[str] = []
    if missing:
        issues.append(f"predictions 缺少 gold 公告：{', '.join(missing)}")
    if extra:
        issues.append(f"predictions 多出公告：{', '.join(extra)}")

    if issues:
        raise ValueError("公告 ID 集合不一致：\n- " + "\n- ".join(issues))

    prediction_packages = {
        notice.notice_id: {package.package_id for package in notice.packages}
        for notice in predictions.notices
    }
    package_differences = []
    for notice in gold.notices:
        gold_package_ids = {package.package_id for package in notice.packages}
        predicted_package_ids = prediction_packages.get(notice.notice_id, set())
        gold_only = sorted(gold_package_ids - predicted_package_ids)
        prediction_only = sorted(predicted_package_ids - gold_package_ids)
        if gold_only or prediction_only:
            package_differences.append({
                "notice_id": notice.notice_id,
                "gold_only": gold_only,
                "prediction_only": prediction_only,
            })
    return package_differences


def _write_new_json(path: Path, value: Any) -> None:
    """Create a JSON file without overwriting an existing file or input."""
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(value, ensure_ascii=False, indent=2) + "\n"
    descriptor: int | None = None
    created = False
    try:
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o666)
        created = True
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as output:
            descriptor = None
            output.write(payload)
    except OSError as exc:
        if descriptor is not None:
            os.close(descriptor)
        if created:
            try:
                path.unlink(missing_ok=True)
            except OSError:
                pass
        raise ValueError(f"无法新建输出文件 {path}: {exc}") from exc


def merge_gold_files(
    gold_paths: list[Path],
    predictions_path: Path,
    output_path: Path,
    *,
    conflicts_report: Path | None = None,
    require_complete: bool = False,
) -> GoldDataset:
    if len(gold_paths) < 1:
        raise ValueError("至少需要一个 --gold-files 输入")
    _ensure_disjoint_paths(gold_paths, predictions_path, output_path, conflicts_report)

    datasets = [(path, _load_gold(path)) for path in gold_paths]
    conflicts = _duplicates(datasets)
    if conflicts:
        if conflicts_report is not None:
            _write_new_json(
                conflicts_report,
                {
                    "conflicts": [
                        {
                            "notice_id": notice_id,
                            "sources": paths,
                            "resolution": "unresolved; no source selected",
                        }
                        for notice_id, paths in conflicts.items()
                    ]
                },
            )
        raise ValueError(_format_duplicates(conflicts))

    predictions = _load_predictions(predictions_path)
    notices = [notice for _, dataset in datasets for notice in dataset.notices]
    merged = GoldDataset(
        schema_version="1.0",
        status="reviewed",
        notices=sorted(notices, key=lambda notice: notice.notice_id),
    )
    _validate_annotation_content(merged)
    validate_id_sets(merged, predictions, require_complete=require_complete)
    _write_new_json(output_path, merged.model_dump(mode="json"))
    return merged


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Merge independently reviewed gold JSON files; validate against one canonical "
            "predictions JSON. Duplicate notice IDs are always rejected."
        )
    )
    parser.add_argument("--gold-files", nargs="+", required=True, type=Path, help="Reviewed gold JSON files")
    parser.add_argument("--predictions", required=True, type=Path, help="Canonical predictions JSON")
    parser.add_argument("--output", required=True, type=Path, help="New merged gold JSON path")
    parser.add_argument(
        "--conflicts-report",
        type=Path,
        help="On duplicate IDs, write a JSON conflict report and still fail without merging",
    )
    parser.add_argument(
        "--require-complete",
        action="store_true",
        help="also require the merged Gold to contain every notice in canonical predictions",
    )
    args = parser.parse_args(argv)
    try:
        merged = merge_gold_files(
            args.gold_files,
            args.predictions,
            args.output,
            conflicts_report=args.conflicts_report,
            require_complete=args.require_complete,
        )
    except (OSError, ValueError, ValidationError) as exc:
        print(f"Gold merge failed: {exc}", file=sys.stderr)
        return 2
    print(f"Merged reviewed gold: {args.output} ({len(merged.notices)} notices)")
    print(f"Validated Gold notice IDs against predictions: {args.predictions}")
    predictions = _load_predictions(args.predictions)
    package_differences = validate_id_sets(merged, predictions)
    if package_differences:
        print(f"Package structure differs in {len(package_differences)} notice(s); evaluation will count these as package-level misses/extras:")
        for difference in package_differences:
            details = []
            if difference["gold_only"]:
                details.append(f"Gold 独有={difference['gold_only']}")
            if difference["prediction_only"]:
                details.append(f"预测独有={difference['prediction_only']}")
            print(f"- {difference['notice_id']}: {'; '.join(details)}")
    else:
        print("Package IDs match between Gold and predictions.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
