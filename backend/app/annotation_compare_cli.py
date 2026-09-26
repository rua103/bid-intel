"""Compare two independently reviewed pilot sets; this command never adjudicates them."""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from app.evaluation import (
    ITEM_FIELDS,
    GoldDataset,
    PredictionDataset,
    evaluate_dataset,
    normalize_name,
    normalize_number,
    normalized_field,
)


def _read(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"无法读取 JSON {path}: {exc}") from exc


def _load_reviewed(path: Path) -> GoldDataset:
    try:
        value = GoldDataset.model_validate(_read(path))
    except ValidationError as exc:
        raise ValueError(f"Gold JSON 无效：{path}") from exc
    if value.status != "reviewed" or not value.notices:
        raise ValueError(f"只接受非空、reviewed 状态的 Gold：{path}")
    return value


def _package_map(dataset: GoldDataset) -> dict[tuple[str, str], Any]:
    return {
        (notice.notice_id, package.package_id): package
        for notice in dataset.notices
        for package in notice.packages
    }


def _compare_entities(left: Any, right: Any) -> list[dict[str, Any]]:
    differences = []
    left_buyer = left.buyer.model_dump(mode="json") if left.buyer else None
    right_buyer = right.buyer.model_dump(mode="json") if right.buyer else None
    if normalize_name(left.buyer.name if left.buyer else None) != normalize_name(
        right.buyer.name if right.buyer else None
    ):
        differences.append({"role": "buyer", "annotator_a": left_buyer, "annotator_b": right_buyer})

    for role in ("winners", "bidders"):
        def signature(person: Any, *, current_role: str = role) -> tuple:
            name = normalize_name(person.name)
            if current_role == "winners":
                return name, normalize_number(person.award_amount, monetary=True)
            return name, person.outcome

        left_rows = sorted((signature(person) for person in getattr(left, role)), key=repr)
        right_rows = sorted((signature(person) for person in getattr(right, role)), key=repr)
        if left_rows != right_rows:
            differences.append({
                "role": role,
                "annotator_a": [person.model_dump(mode="json") for person in getattr(left, role)],
                "annotator_b": [person.model_dump(mode="json") for person in getattr(right, role)],
            })
    return differences


def compare_pilot_files(annotator_a_path: Path, annotator_b_path: Path) -> dict[str, Any]:
    gold_a = _load_reviewed(annotator_a_path)
    gold_b = _load_reviewed(annotator_b_path)
    notice_a = {notice.notice_id: notice for notice in gold_a.notices}
    notice_b = {notice.notice_id: notice for notice in gold_b.notices}
    if set(notice_a) != set(notice_b):
        raise ValueError("两份试标公告 ID 不一致；请确认导入的是同一 pilot")

    prediction_b = PredictionDataset.model_validate({
        **gold_b.model_dump(mode="json"),
        "status": "predicted",
    })
    metrics = evaluate_dataset(gold_a, prediction_b)
    package_a = _package_map(gold_a)
    package_b = _package_map(gold_b)
    disagreements = []
    for key in sorted(package_a.keys() | package_b.keys()):
        left, right = package_a.get(key), package_b.get(key)
        if left is None or right is None:
            disagreements.append({
                "notice_id": key[0],
                "package_id": key[1],
                "differences": [{
                    "role": "package_presence",
                    "annotator_a": left is not None,
                    "annotator_b": right is not None,
                }],
            })
            continue
        item_signature = lambda row: tuple(
            normalized_field(field, getattr(row, field)) for field in ITEM_FIELDS
        )
        left_items = sorted((item_signature(item) for item in left.items), key=repr)
        right_items = sorted((item_signature(item) for item in right.items), key=repr)
        differences = _compare_entities(left, right)
        if left_items != right_items:
            differences.append({
                "role": "items",
                "annotator_a": [item.model_dump(mode="json") for item in left.items],
                "annotator_b": [item.model_dump(mode="json") for item in right.items],
            })
        if differences:
            disagreements.append({
                "notice_id": key[0],
                "package_id": key[1],
                "differences": differences,
            })

    return {
        "schema_version": "annotation-pilot-comparison/1",
        "kind": "independent_annotation_agreement_review",
        "disclaimer": "仅列两位标注员差异和本地一致性代理指标；不同包号按原文分别报告，不自动猜测别名；不裁决哪方正确。协调员必须对照原文决定。",
        "notice_count": len(notice_a),
        "agreement_metrics": metrics.model_dump(mode="json"),
        "disagreement_count": len(disagreements),
        "disagreements": disagreements,
    }


def _write_new(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(value, ensure_ascii=False, indent=2) + "\n"
    try:
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o666)
    except OSError as exc:
        raise ValueError(f"无法新建比较报告 {path}；为保护已有文件拒绝覆盖：{exc}") from exc
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as stream:
            stream.write(payload)
    except OSError:
        path.unlink(missing_ok=True)
        raise


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="比较两位标注员的相同 pilot Gold；只报告差异，不投票、不替协调员裁决。"
    )
    parser.add_argument("--annotator-a", type=Path, required=True, help="标注员 A 导出的 reviewed Gold")
    parser.add_argument("--annotator-b", type=Path, required=True, help="标注员 B 导出的 reviewed Gold")
    parser.add_argument("--output", type=Path, required=True, help="新建的差异 JSON 路径")
    args = parser.parse_args(argv)
    try:
        report = compare_pilot_files(args.annotator_a, args.annotator_b)
        _write_new(args.output, report)
    except (OSError, ValueError, ValidationError) as exc:
        print(f"Pilot comparison failed: {exc}", file=sys.stderr)
        return 2
    print(f"Compared {report['notice_count']} pilot notices; disagreements: {report['disagreement_count']}; report: {args.output}")
    print("Review every listed difference against the original notices; no answer was selected.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
