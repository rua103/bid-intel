"""Post-process a completed three-route Gold evaluation for GAP reporting.

This module is deliberately separate from extraction.  It reads the immutable Gold
file, the route runner's ``progress.json``/predictions/evaluation reports, and the
sample manifest.  It adds the comparison dimensions that the route runner does not
need while it is extracting: package-set alignment, duplicate candidates, and
attachment/OCR coverage.

The output is a team-local validation report.  It must never be described as an
official competition score.  ``stage=final`` is reserved for a reviewed, independent
holdout after extraction code and prompts have been frozen; ``stage=prep`` and
``stage=targeted`` intentionally leave the route recommendation pending.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import math
import os
import tempfile
import unicodedata
from collections import Counter
from collections.abc import Iterable
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from app.evaluation import GoldDataset, PredictionDataset

MODES = ("rules", "hybrid", "model")
REPORT_SCHEMA_VERSION = 1
LOCAL_DISCLAIMER = (
    "团队本地 Gold 验证：指标采用仓库 local_proxy 口径，不是官方比赛成绩；"
    "官方字段匹配和评分规则如有不同，应以官方说明为准。"
)
OCR_MARKERS = (
    "ocr",
    "扫描",
    "识别",
    "图像文字",
    "文字识别",
)
ATTACHMENT_WARNING_MARKERS = (
    "附件",
    "压缩",
    "展开",
    "成员读取",
    "跳过",
)


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8-sig"))


def _atomic_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    handle, temporary_name = tempfile.mkstemp(
        dir=path.parent,
        prefix=f".{path.name}.",
        suffix=".tmp",
    )
    try:
        with os.fdopen(handle, "w", encoding="utf-8", newline="\n") as stream:
            json.dump(value, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
        os.replace(temporary_name, path)
    except BaseException:
        try:
            os.unlink(temporary_name)
        except OSError:
            pass
        raise


def _atomic_text(path: Path, value: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    handle, temporary_name = tempfile.mkstemp(
        dir=path.parent,
        prefix=f".{path.name}.",
        suffix=".tmp",
    )
    try:
        with os.fdopen(handle, "w", encoding="utf-8", newline="\n") as stream:
            stream.write(value)
        os.replace(temporary_name, path)
    except BaseException:
        try:
            os.unlink(temporary_name)
        except OSError:
            pass
        raise


def _split_manifest_list(value: str | None) -> list[str]:
    return [part.strip() for part in (value or "").split(";") if part.strip()]


def load_manifest(path: str | Path) -> dict[str, dict[str, Any]]:
    """Load and validate the route runner's sample manifest.

    The runner itself remains the authority for source loading.  This copy is only
    used to report sample stratification and attachment/OCR coverage.
    """

    path = Path(path)
    with path.open("r", encoding="utf-8-sig", newline="") as stream:
        rows = list(csv.DictReader(stream))
    result: dict[str, dict[str, Any]] = {}
    for row in rows:
        notice_id = (row.get("notice_id") or "").strip()
        if not notice_id or notice_id in result:
            raise ValueError("manifest 含空 notice_id 或重复 notice_id")
        source_files = _split_manifest_list(row.get("source_files"))
        result[notice_id] = {
            "notice_id": notice_id,
            "official_file": (row.get("official_file") or "").strip(),
            "assignment": (row.get("assignment") or "").strip(),
            "categories": _split_manifest_list(row.get("categories")),
            "source_files": source_files,
        }
    if not result:
        raise ValueError("manifest 为空")
    return result


def _normal_text(value: Any) -> str | None:
    if value is None:
        return None
    text = unicodedata.normalize("NFKC", str(value))
    return "".join(text.casefold().split()) or None


def _normal_number(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, bool):
        return str(value).casefold()
    try:
        number = float(value)
    except (TypeError, ValueError):
        return _normal_text(value)
    if not math.isfinite(number):
        return _normal_text(value)
    # ``.15`` and ``0.150`` should be the same fingerprint while preserving large
    # integers well enough for duplicate diagnostics.
    return format(number, ".15g")


def _fingerprint(values: Iterable[Any], numeric_indexes: set[int] | None = None) -> str:
    numeric_indexes = numeric_indexes or set()
    normalized = [
        _normal_number(value) if index in numeric_indexes else _normal_text(value)
        for index, value in enumerate(values)
    ]
    return json.dumps(normalized, ensure_ascii=False, separators=(",", ":"))


def _duplicate_count(
    rows: Iterable[dict[str, Any]],
    fields: tuple[str, ...],
    numeric_fields: set[str],
) -> dict[str, int]:
    by_scope: dict[tuple[str, str], Counter[str]] = {}
    for row in rows:
        scope = (str(row.get("_package_id", "")), str(row.get("_list", "")))
        values = [row.get(field) for field in fields]
        numeric_indexes = {index for index, field in enumerate(fields) if field in numeric_fields}
        by_scope.setdefault(scope, Counter())[_fingerprint(values, numeric_indexes)] += 1
    extra = sum(max(0, count - 1) for counts in by_scope.values() for count in counts.values())
    groups = sum(count > 1 for counts in by_scope.values() for count in counts.values())
    packages = sum(any(count > 1 for count in counts.values()) for counts in by_scope.values())
    return {"extra_rows": extra, "duplicate_groups": groups, "packages_with_duplicates": packages}


def duplicate_candidates(predicted: dict[str, Any]) -> dict[str, Any]:
    """Count exact duplicate rows within each package/list as a multiset.

    A row is a duplicate only when all exported evaluation fields match after
    NFKC/case/whitespace normalization.  The first row is retained and every extra
    copy is counted, matching the local evaluator's FP treatment of duplicate rows.
    Rows in ``winners`` and ``bidders`` are checked within their own lists; the same
    organization appearing in both lists is valid and is not called a duplicate.
    """

    item_rows: list[dict[str, Any]] = []
    winner_rows: list[dict[str, Any]] = []
    bidder_rows: list[dict[str, Any]] = []
    for notice in predicted.get("notices", []):
        for package in notice.get("packages", []):
            package_id = package.get("package_id", "")
            for item in package.get("items", []):
                item_rows.append({"_package_id": package_id, "_list": "items", **item})
            for winner in package.get("winners", []):
                winner_rows.append({"_package_id": package_id, "_list": "winners", **winner})
            for bidder in package.get("bidders", []):
                bidder_rows.append({"_package_id": package_id, "_list": "bidders", **bidder})
    items = _duplicate_count(
        item_rows,
        ("product_name", "category", "brand", "model", "unit_price", "quantity", "total_price"),
        {"unit_price", "quantity", "total_price"},
    )
    winners = _duplicate_count(
        winner_rows,
        ("name", "award_amount"),
        {"award_amount"},
    )
    bidders = _duplicate_count(
        bidder_rows,
        ("name", "outcome"),
        set(),
    )
    return {
        "items": items,
        "winners": winners,
        "bidders": bidders,
        # In the route reports "candidate" means an extracted item candidate;
        # entity duplicates remain visible separately and are not mixed into the
        # item FP diagnostic.
        "candidate_extra_rows": items["extra_rows"],
        "entity_extra_rows": winners["extra_rows"] + bidders["extra_rows"],
    }


def package_alignment(gold: dict[str, Any], predicted: dict[str, Any]) -> dict[str, Any]:
    """Compare package-id sets at the documented notice boundary."""

    gold_by_notice = {
        notice["notice_id"]: {package["package_id"] for package in notice.get("packages", [])}
        for notice in gold.get("notices", [])
    }
    predicted_by_notice = {
        notice["notice_id"]: {package["package_id"] for package in notice.get("packages", [])}
        for notice in predicted.get("notices", [])
    }
    notice_ids = sorted(gold_by_notice.keys() | predicted_by_notice.keys())
    exact = 0
    matched = missing = extra = 0
    per_notice: list[dict[str, Any]] = []
    for notice_id in notice_ids:
        gold_packages = gold_by_notice.get(notice_id, set())
        predicted_packages = predicted_by_notice.get(notice_id, set())
        intersection = gold_packages & predicted_packages
        missing_ids = sorted(gold_packages - predicted_packages)
        extra_ids = sorted(predicted_packages - gold_packages)
        is_exact = gold_packages == predicted_packages
        exact += int(is_exact)
        matched += len(intersection)
        missing += len(missing_ids)
        extra += len(extra_ids)
        per_notice.append(
            {
                "notice_id": notice_id,
                "gold_package_count": len(gold_packages),
                "predicted_package_count": len(predicted_packages),
                "matched_package_count": len(intersection),
                "missing_package_ids": missing_ids,
                "extra_package_ids": extra_ids,
                "exact_set": is_exact,
            }
        )
    gold_count = sum(len(packages) for packages in gold_by_notice.values())
    predicted_count = sum(len(packages) for packages in predicted_by_notice.values())
    precision = matched / predicted_count if predicted_count else None
    recall = matched / gold_count if gold_count else None
    f1 = (
        2 * precision * recall / (precision + recall)
        if precision is not None and recall is not None and precision + recall
        else None
    )
    return {
        "notice_count": len(gold_by_notice),
        "notice_count_compared": len(notice_ids),
        "exact_notice_count": exact,
        "exact_notice_rate": exact / len(gold_by_notice) if gold_by_notice else None,
        "gold_package_count": gold_count,
        "predicted_package_count": predicted_count,
        "matched_package_count": matched,
        "missing_package_count": missing,
        "extra_package_count": extra,
        "package_precision": precision,
        "package_recall": recall,
        "package_f1": f1,
        "per_notice": per_notice,
    }


def _contains_marker(value: str, markers: tuple[str, ...]) -> bool:
    lowered = value.casefold()
    return any(marker.casefold() in lowered for marker in markers)


def _coverage(
    manifest: dict[str, dict[str, Any]],
    notice_ids: list[str],
    rows: list[dict[str, Any]],
    *,
    scope: str,
    telemetry: dict[str, Any],
) -> dict[str, Any]:
    selected = [manifest[notice_id] for notice_id in notice_ids if notice_id in manifest]
    attachment_rows = [
        row
        for row in selected
        if any(
            Path(name).suffix.casefold() not in {".html", ".htm"} for name in row["source_files"]
        )
    ]
    ocr_hint_rows = [
        row
        for row in selected
        if any(
            "ocr" in category.casefold() or "识别" in category or "扫描" in category
            for category in row["categories"]
        )
    ]
    warnings = [
        warning for row in rows for warning in row.get("warnings", []) if isinstance(warning, str)
    ]
    errors = [error for row in rows for error in row.get("errors", []) if isinstance(error, str)]
    ocr_warning_rows = [
        row
        for row in rows
        if any(_contains_marker(str(warning), OCR_MARKERS) for warning in row.get("warnings", []))
    ]
    attachment_warning_rows = [
        row
        for row in rows
        if any(
            _contains_marker(str(warning), ATTACHMENT_WARNING_MARKERS)
            for warning in row.get("warnings", [])
        )
    ]
    completed_ids = {row.get("notice_id") for row in rows if row.get("status") == "done"}
    completed_attachment = sum(row["notice_id"] in completed_ids for row in attachment_rows)
    completed_ocr_hint = sum(row["notice_id"] in completed_ids for row in ocr_hint_rows)
    denominator = len(selected)
    attachment_denominator = len(attachment_rows)
    ocr_denominator = len(ocr_hint_rows)
    return {
        "scope": scope,
        "sample_notice_count": denominator,
        "completed_notice_count": len(completed_ids),
        "completed_notice_rate": len(completed_ids) / denominator if denominator else None,
        "notices_with_attachments": attachment_denominator,
        "listed_attachment_files": sum(
            sum(
                Path(name).suffix.casefold() not in {".html", ".htm"}
                for name in row["source_files"]
            )
            for row in selected
        ),
        "attachment_notice_processed_count": completed_attachment,
        "attachment_notice_processed_rate": (
            completed_attachment / attachment_denominator if attachment_denominator else None
        ),
        "notices_with_ocr_hint": ocr_denominator,
        "ocr_hint_notice_processed_count": completed_ocr_hint,
        "ocr_hint_notice_processed_rate": (
            completed_ocr_hint / ocr_denominator if ocr_denominator else None
        ),
        "notices_with_ocr_warning": len({row.get("notice_id") for row in ocr_warning_rows}),
        "ocr_warning_count": sum(
            _contains_marker(str(warning), OCR_MARKERS) for warning in warnings
        ),
        "notices_with_attachment_warning": len(
            {row.get("notice_id") for row in attachment_warning_rows}
        ),
        "warning_count": len(warnings),
        "error_count": len(errors),
        "source_documents_parsed": telemetry.get("source_documents_parsed"),
        "parse_cache_hits": telemetry.get("parse_cache_hits"),
        "model_cache_hits": telemetry.get("model_cache_hits"),
        "coverage_note": (
            "附件/OCR 指标是清单覆盖、完成状态和警告遥测；现有 predictions schema 不保留"
            "每页 OCR 成功标志，因此不能把 OCR hint processed rate 宣称为 OCR 正确率。"
        ),
    }


def _metric(report: dict[str, Any], section: str, field: str) -> Any:
    return (report.get(section) or {}).get(field)


def _route_score(report: dict[str, Any]) -> tuple[float, float, float]:
    """Return a balanced local quality tuple for the final recommendation.

    Both field and complete-record local weighted scores matter.  The minimum is
    primary so a route cannot win by sacrificing one dimension; the mean and package
    exact-set rate are deterministic tie-breakers.  This is a team-local decision
    rule, not a claim about the official competition formula.
    """

    field = _metric(report, "evaluation", "field_micro")
    records = _metric(report, "evaluation", "records")
    field_score = (field or {}).get("weighted_score") if field else None
    record_score = (records or {}).get("weighted_score") if records else None
    if field_score is None or record_score is None:
        return (-math.inf, -math.inf, -math.inf)
    return (
        min(field_score, record_score),
        (field_score + record_score) / 2,
        (report.get("package_alignment") or {}).get("exact_notice_rate") or 0,
    )


def _recommendation(
    modes: dict[str, dict[str, Any]],
    *,
    stage: str,
) -> dict[str, Any]:
    if stage != "final":
        return {
            "status": "pending_freeze_and_independent_holdout",
            "route": None,
            "decision_rule": (
                "final only: maximize min(field_micro.weighted_score, records.weighted_score); "
                "then mean and package exact-set rate"
            ),
            "rationale": ("当前输出属于调优或定向复评，不能据此冻结 model/hybrid 路线。"),
        }
    candidates = {mode: modes.get(mode) for mode in ("hybrid", "model")}
    if any(not row or row.get("status") != "done" for row in candidates.values()):
        return {
            "status": "blocked_incomplete_routes",
            "route": None,
            "decision_rule": "same as final",
            "rationale": "model 与 hybrid 必须都完成同一 reviewed holdout 才能推荐。",
        }
    scores = {mode: _route_score(row) for mode, row in candidates.items()}
    if scores["model"] == scores["hybrid"]:
        # Equal quality gets the lower-cost/lower-duplicate route.  The comparison is
        # intentionally observable in the output rather than hidden in prose.
        model_dup = candidates["model"]["duplicates"]["candidate_extra_rows"]
        hybrid_dup = candidates["hybrid"]["duplicates"]["candidate_extra_rows"]
        if model_dup != hybrid_dup:
            route = "model" if model_dup < hybrid_dup else "hybrid"
            tie_break = "candidate_extra_rows"
        else:
            model_requests = candidates["model"].get("telemetry", {}).get("requests") or 0
            hybrid_requests = candidates["hybrid"].get("telemetry", {}).get("requests") or 0
            route = "model" if model_requests <= hybrid_requests else "hybrid"
            tie_break = "requests"
    else:
        route = "model" if scores["model"] > scores["hybrid"] else "hybrid"
        tie_break = "quality_tuple"
    return {
        "status": "determined",
        "route": route,
        "decision_rule": (
            "maximize min(field_micro.weighted_score, records.weighted_score), then their "
            "mean, then package exact-set rate; ties use fewer duplicate candidate rows "
            "and then fewer model requests"
        ),
        "quality_tuples": scores,
        "tie_break_used": tie_break,
        "rationale": (
            f"在上述团队本地规则下，{route} 的 holdout 质量排序优于另一条路线；"
            "这不是官方成绩或官方评分推断。"
        ),
    }


def _load_evaluation_reports(run_dir: Path) -> dict[str, dict[str, Any]]:
    reports: dict[str, dict[str, Any]] = {}
    for mode in MODES:
        path = run_dir / f"evaluation-{mode}.json"
        if path.is_file():
            reports[mode] = _read_json(path)
    return reports


def _load_predictions(run_dir: Path) -> dict[str, dict[str, Any]]:
    predictions: dict[str, dict[str, Any]] = {}
    for mode in MODES:
        path = run_dir / f"predictions-{mode}.json"
        if path.is_file():
            predictions[mode] = _read_json(path)
    return predictions


def _validate_identity(
    progress: dict[str, Any],
    gold_path: Path,
    manifest_path: Path,
) -> tuple[str, list[str]]:
    identity = progress.get("identity") or {}
    scope = identity.get("scope")
    if scope not in {"html", "attachments"}:
        raise ValueError("progress.identity.scope 必须是 html 或 attachments")
    expected_gold = identity.get("gold_sha256")
    actual_gold = _sha256_file(gold_path)
    if expected_gold and expected_gold != actual_gold:
        raise ValueError("run 的 Gold SHA-256 与传入 Gold 不一致")
    expected_manifest = identity.get("manifest_sha256")
    actual_manifest = _sha256_file(manifest_path)
    if expected_manifest and expected_manifest != actual_manifest:
        raise ValueError("run 的 manifest SHA-256 与传入 manifest 不一致")
    notice_ids = [str(value) for value in identity.get("notice_ids", [])]
    if len(notice_ids) != len(set(notice_ids)):
        raise ValueError("progress.identity.notice_ids 重复")
    return scope, notice_ids


def _check_tuning_disjoint(gold_path: Path, tuning_path: Path) -> None:
    current = GoldDataset.model_validate(_read_json(gold_path))
    tuning = GoldDataset.model_validate(_read_json(tuning_path))
    if current.status != "reviewed" or tuning.status != "reviewed":
        raise ValueError("final 阶段的 holdout 和 tuning Gold 都必须是 reviewed")
    current_ids = {notice.notice_id for notice in current.notices}
    tuning_ids = {notice.notice_id for notice in tuning.notices}
    overlap = sorted(current_ids & tuning_ids)
    if overlap:
        raise ValueError(f"holdout/tuning Gold notice_id 重叠：{overlap}")


def build_report(
    *,
    gold_path: str | Path,
    manifest_path: str | Path,
    run_dir: str | Path,
    dataset_role: str = "tuning",
    stage: str = "prep",
    tuning_gold_path: str | Path | None = None,
) -> dict[str, Any]:
    """Build the machine-readable GAP report without invoking extraction or a model."""

    gold_path = Path(gold_path).resolve(strict=True)
    manifest_path = Path(manifest_path).resolve(strict=True)
    run_dir = Path(run_dir).resolve(strict=True)
    if dataset_role not in {"tuning", "targeted", "holdout"}:
        raise ValueError("dataset_role 必须是 tuning、targeted 或 holdout")
    if stage not in {"prep", "targeted", "final"}:
        raise ValueError("stage 必须是 prep、targeted 或 final")
    if stage == "final" and dataset_role != "holdout":
        raise ValueError("stage=final 只能用于 dataset_role=holdout")
    if stage == "targeted" and dataset_role not in {"tuning", "targeted"}:
        raise ValueError("stage=targeted 只能用于调优集或定向复评")
    if stage == "final":
        if tuning_gold_path is None:
            raise ValueError("stage=final 必须传入 --tuning-gold 以检查留出集独立性")
        _check_tuning_disjoint(gold_path, Path(tuning_gold_path).resolve(strict=True))

    gold_data = _read_json(gold_path)
    gold = GoldDataset.model_validate(gold_data)
    if gold.status != "reviewed":
        raise ValueError("只能读取 reviewed Gold；draft 不能作为评测真值")
    manifest = load_manifest(manifest_path)
    progress_path = run_dir / "progress.json"
    if not progress_path.is_file():
        raise ValueError("run-dir 缺少 progress.json")
    progress = _read_json(progress_path)
    scope, notice_ids = _validate_identity(progress, gold_path, manifest_path)
    identity = progress.get("identity") or {}
    if stage == "final":
        if progress.get("status") != "completed":
            raise ValueError("stage=final 要求三条路线全部 completed")
        if not isinstance(identity.get("code_sha256"), dict) or not identity["code_sha256"]:
            raise ValueError("stage=final 缺少 route runner 的 code_sha256 冻结记录")
    if not notice_ids:
        notice_ids = [notice["notice_id"] for notice in gold_data.get("notices", [])]
    gold_ids = {notice.notice_id for notice in gold.notices}
    if not set(notice_ids) <= gold_ids:
        raise ValueError("run 中存在不在 Gold 内的 notice_id")
    if not set(notice_ids) <= set(manifest):
        raise ValueError("manifest 缺少 run 中的 notice_id")

    evaluation_reports = _load_evaluation_reports(run_dir)
    prediction_files = _load_predictions(run_dir)
    summary = _read_json(run_dir / "summary.json") if (run_dir / "summary.json").is_file() else {}
    summary_modes = summary.get("modes") or {}
    progress_modes = progress.get("completed") or {}
    route_rows: dict[str, dict[str, Any]] = {}
    for mode in MODES:
        prediction_data = prediction_files.get(mode)
        if prediction_data is None:
            route_rows[mode] = {"status": "pending", "mode": mode}
            continue
        # Pydantic validation catches accidental hand edits to a route prediction before
        # a table is written to GAP.  The route runner already performs this validation;
        # repeating it here makes the post-processor safe to run independently.
        PredictionDataset.model_validate(prediction_data)
        report = evaluation_reports.get(mode)
        if report is None:
            route_rows[mode] = {"status": "pending", "mode": mode}
            continue
        predicted_ids = {notice["notice_id"] for notice in prediction_data.get("notices", [])}
        if predicted_ids != set(notice_ids):
            raise ValueError(f"{mode}: prediction notice_id 与 run 范围不一致")
        telemetry = dict(summary_modes.get(mode) or {})
        entries = progress_modes.get(mode) or {}
        mode_entries = [entries.get(notice_id) or {} for notice_id in notice_ids]
        duplicate = duplicate_candidates(prediction_data)
        alignment = package_alignment(
            {
                "notices": [
                    notice
                    for notice in gold_data.get("notices", [])
                    if notice["notice_id"] in set(notice_ids)
                ]
            },
            prediction_data,
        )
        coverage = _coverage(
            manifest,
            notice_ids,
            mode_entries,
            scope=scope,
            telemetry=telemetry,
        )
        route_rows[mode] = {
            "status": "done"
            if all(entry.get("status") == "done" for entry in mode_entries)
            else "incomplete",
            "mode": mode,
            "evaluation": report,
            "package_alignment": alignment,
            "duplicates": duplicate,
            "telemetry": {
                key: telemetry.get(key)
                for key in (
                    "completed_notices",
                    "failed_or_incomplete_notices",
                    "elapsed_seconds_sum",
                    "elapsed_seconds_median",
                    "elapsed_seconds_max",
                    "attempt_count",
                    "parse_seconds",
                    "model_seconds",
                    "source_documents_parsed",
                    "parse_cache_hits",
                    "model_cache_hits",
                    "requests",
                    "successful_responses",
                    "failed_transports",
                    "prompt_tokens",
                    "completion_tokens",
                    "token_usage_reported_requests",
                )
            },
            "coverage": coverage,
        }

    final_status = progress.get("status")
    result = {
        "schema_version": REPORT_SCHEMA_VERSION,
        "evaluation_kind": "three_route_local_gold_gap_report",
        "disclaimer": LOCAL_DISCLAIMER,
        "dataset_role": dataset_role,
        "stage": stage,
        "status": final_status,
        "scope": scope,
        "gold_status": gold.status,
        "gold_sha256": _sha256_file(gold_path),
        "manifest_sha256": _sha256_file(manifest_path),
        "run_dir": str(run_dir),
        "run_identity": {
            key: identity.get(key)
            for key in (
                "model_name",
                "model_config_fingerprint",
                "model_max_chars",
                "model_max_output_tokens",
                "model_disable_thinking",
                "ocr_enabled",
                "ocr_language",
                "ocr_timeout_seconds",
                "max_calls_per_notice",
                "code_sha256",
            )
            if key in identity
        },
        "notice_count": len(notice_ids),
        "notice_ids": notice_ids,
        "modes": route_rows,
        "recommendation": _recommendation(route_rows, stage=stage),
        "notes": [
            "包号对齐率按 notice_id 内 package_id 集合精确比较；同时报告包级 precision/recall。",
            (
                "重复候选数按同一 notice/package/items 内完全相同的评测字段，多重集额外行计数；"
                "winner/bidder 重复另列为 entity_extra_rows。"
            ),
            "请求、耗时和 token 来自 runner 的 telemetry；provider 未返回 usage 时 token 为 null。",
            "附件/OCR coverage 是输入清单与处理警告遥测，不等同于 OCR 内容正确率。",
        ],
    }
    return result


def _fmt_pct(value: Any) -> str:
    if value is None:
        return "N/A"
    return f"{float(value):.2%}"


def _fmt_num(value: Any) -> str:
    if value is None:
        return "N/A"
    if isinstance(value, float):
        return f"{value:.3f}"
    return str(value)


def render_markdown(report: dict[str, Any]) -> str:
    """Render a GAP-ready table while retaining the local-score disclaimer."""

    lines = [
        "# Rules / Hybrid / Model 三路线评测",
        "",
        f"> {report['disclaimer']}",
        "> 本报告不使用或引用任何旧的单一百分比作为最终结论。",
        "",
        (
            f"数据角色：`{report['dataset_role']}`；阶段：`{report['stage']}`；"
            f"范围：`{report['scope']}`；公告：{report['notice_count']} 条；"
            f"Gold status：`{report['gold_status']}`。"
        ),
        f"Gold SHA-256：`{report['gold_sha256']}`；manifest SHA-256：`{report['manifest_sha256']}`。",
        "",
        "## 路线汇总",
        "",
        "| 路线 | 完成 | 字段 Weighted | 记录 Weighted | 包号集合精确对齐 | 包级 P/R | 重复候选额外行 | 请求 | 墙钟合计 | 中位单篇 | Prompt tokens | Completion tokens |",
        "|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for mode in MODES:
        row = report["modes"].get(mode, {})
        evaluation = row.get("evaluation") or {}
        alignment = row.get("package_alignment") or {}
        telemetry = row.get("telemetry") or {}
        duplicates = row.get("duplicates") or {}
        lines.append(
            f"| {mode} | {telemetry.get('completed_notices', 'N/A')}/{report['notice_count']} | "
            f"{_fmt_pct(_metric({'evaluation': evaluation}, 'evaluation', 'field_micro') and (evaluation.get('field_micro') or {}).get('weighted_score'))} | "
            f"{_fmt_pct((evaluation.get('records') or {}).get('weighted_score'))} | "
            f"{alignment.get('exact_notice_count', 'N/A')}/{alignment.get('notice_count', report['notice_count'])} "
            f"({_fmt_pct(alignment.get('exact_notice_rate'))}) | "
            f"{_fmt_pct(alignment.get('package_precision'))}/{_fmt_pct(alignment.get('package_recall'))} | "
            f"{duplicates.get('candidate_extra_rows', 'N/A')} | {telemetry.get('requests', 'N/A')} | "
            f"{_fmt_num(telemetry.get('elapsed_seconds_sum'))}s | "
            f"{_fmt_num(telemetry.get('elapsed_seconds_median'))}s | "
            f"{telemetry.get('prompt_tokens', 'N/A')} | {telemetry.get('completion_tokens', 'N/A')} |"
        )

    lines += [
        "",
        "## 字段级指标",
        "",
        "| 字段 | rules TP/FP/FN | hybrid TP/FP/FN | model TP/FP/FN |",
        "|---|---:|---:|---:|",
    ]
    fields = (
        "product_name",
        "category",
        "brand",
        "model",
        "unit_price",
        "quantity",
        "total_price",
    )
    for field in fields:
        cells = []
        for mode in MODES:
            metrics = (
                (report["modes"].get(mode, {}).get("evaluation") or {}).get("by_field") or {}
            ).get(field) or {}
            cells.append(
                f"{metrics.get('tp', 'N/A')}/{metrics.get('fp', 'N/A')}/{metrics.get('fn', 'N/A')} "
                f"({_fmt_pct(metrics.get('weighted_score'))})"
            )
        lines.append(f"| `{field}` | {cells[0]} | {cells[1]} | {cells[2]} |")

    lines += [
        "",
        "## 记录级与实体指标",
        "",
        "| 路线 | 记录 TP/FP/FN | 记录 F1 | buyer | winners | bidders | winner_amounts |",
        "|---|---:|---:|---:|---:|---:|---:|",
    ]
    for mode in MODES:
        evaluation = report["modes"].get(mode, {}).get("evaluation") or {}
        records = evaluation.get("records") or {}
        entities = evaluation.get("entities") or {}
        entity_cells = []
        for name in ("buyer", "winners", "bidders", "winner_amounts"):
            metrics = entities.get(name) or {}
            entity_cells.append(
                f"{metrics.get('tp', 'N/A')}/{metrics.get('fp', 'N/A')}/{metrics.get('fn', 'N/A')}"
            )
        lines.append(
            f"| {mode} | {records.get('tp', 'N/A')}/{records.get('fp', 'N/A')}/{records.get('fn', 'N/A')} | "
            f"{_fmt_pct(records.get('f1'))} | {entity_cells[0]} | {entity_cells[1]} | {entity_cells[2]} | {entity_cells[3]} |"
        )

    lines += [
        "",
        "## 附件 / OCR 覆盖",
        "",
        "| 路线 | 有附件公告 | 附件处理完成 | OCR 提示公告 | OCR 提示处理完成 | OCR 警告公告/条数 | 解析文档数 |",
        "|---|---:|---:|---:|---:|---:|---:|",
    ]
    for mode in MODES:
        coverage = report["modes"].get(mode, {}).get("coverage") or {}
        lines.append(
            f"| {mode} | {coverage.get('notices_with_attachments', 'N/A')} | "
            f"{coverage.get('attachment_notice_processed_count', 'N/A')} ({_fmt_pct(coverage.get('attachment_notice_processed_rate'))}) | "
            f"{coverage.get('notices_with_ocr_hint', 'N/A')} | "
            f"{coverage.get('ocr_hint_notice_processed_count', 'N/A')} ({_fmt_pct(coverage.get('ocr_hint_notice_processed_rate'))}) | "
            f"{coverage.get('notices_with_ocr_warning', 'N/A')}/{coverage.get('ocr_warning_count', 'N/A')} | "
            f"{coverage.get('source_documents_parsed', 'N/A')} |"
        )
    lines += [
        "",
        "## 结论",
        "",
        f"状态：`{report['recommendation']['status']}`。",
        report["recommendation"].get("rationale", ""),
        "",
        f"决策规则：{report['recommendation'].get('decision_rule', '')}。",
        "",
        "所有数字均为团队本地 Gold 验证，不能写成官方成绩。调优集/受影响公告的定向复评只能用于诊断；最终推荐必须等待 Agent 1 冻结代码和提示后，在独立 reviewed holdout 上生成。",
    ]
    return "\n".join(lines) + "\n"


def write_report(
    report: dict[str, Any],
    *,
    json_path: str | Path,
    markdown_path: str | Path,
) -> None:
    _atomic_json(Path(json_path), report)
    _atomic_text(Path(markdown_path), render_markdown(report))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--gold", type=Path, required=True)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--run-dir", type=Path, required=True)
    parser.add_argument(
        "--dataset-role", choices=("tuning", "targeted", "holdout"), default="tuning"
    )
    parser.add_argument("--stage", choices=("prep", "targeted", "final"), default="prep")
    parser.add_argument("--tuning-gold", type=Path)
    parser.add_argument("--output-json", type=Path)
    parser.add_argument("--output-markdown", type=Path)
    args = parser.parse_args(argv)
    output_json = args.output_json or args.run_dir / "gap-route-report.json"
    output_markdown = args.output_markdown or args.run_dir / "gap-route-report.md"
    try:
        report = build_report(
            gold_path=args.gold,
            manifest_path=args.manifest,
            run_dir=args.run_dir,
            dataset_role=args.dataset_role,
            stage=args.stage,
            tuning_gold_path=args.tuning_gold,
        )
        write_report(report, json_path=output_json, markdown_path=output_markdown)
    except (OSError, ValueError, ValidationError, json.JSONDecodeError) as exc:
        parser.exit(2, f"route evaluation report failed: {exc}\n")
    print(
        json.dumps(
            {
                "status": report["status"],
                "stage": report["stage"],
                "dataset_role": report["dataset_role"],
                "notice_count": report["notice_count"],
                "output_json": str(Path(output_json).resolve()),
                "output_markdown": str(Path(output_markdown).resolve()),
            },
            ensure_ascii=False,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
