"""Summarize the completed 1038-notice parser run without touching source files."""
from __future__ import annotations

import argparse
import collections
import json
import sqlite3
from pathlib import Path, PurePosixPath
from typing import Any

from app.warning_categories import WarningCategory, classify_warning

BACKEND_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DATASET = BACKEND_ROOT / ".data/datasets/282bf10d09b44260942269fcd4242026.sqlite"
DEFAULT_RESULTS = BACKEND_ROOT / ".data/jobs/47508ea8390b4522a6766d79c0188b5d/notices"
DEFAULT_OUTPUT = BACKEND_ROOT.parent / "docs/benchmarks/official-attachments-20260930.json"


def _warning_source(warning: str, sources: list[str]) -> str | None:
    """Link a warning to its leaf path while preserving whole archive provenance."""
    candidates = sorted(sources, key=len, reverse=True)
    for source in candidates:
        if warning.startswith(source + ":"):
            return source
    if warning.startswith("解析失败："):
        value = warning[len("解析失败："):]
        for source in candidates:
            if value.startswith((source + "（", source + "(")):
                return source
    if warning.startswith("暂不支持附件格式："):
        value = warning.split("：", 1)[1]
        return value if value in sources else None
    return None


def build_report(dataset: Path, result_dir: Path) -> dict[str, Any]:
    result_files = sorted(result_dir.glob("*.result.json"))
    if not result_files:
        raise ValueError(f"no completed result files found in {result_dir}")

    suffix_counts: collections.Counter[str] = collections.Counter()
    warning_counts: collections.Counter[str] = collections.Counter()
    warning_notices: dict[str, set[str]] = collections.defaultdict(set)
    file_categories: dict[str, set[str]] = collections.defaultdict(set)
    parser_failure_suffixes: collections.Counter[str] = collections.Counter()
    ocr_notice_ids: set[str] = set()
    ocr_no_text_notice_ids: set[str] = set()
    format_overrides: collections.Counter[str] = collections.Counter()
    html_sources = 0
    attachment_sources = 0
    source_paths: set[str] = set()

    for result_path in result_files:
        result = json.loads(result_path.read_text(encoding="utf-8"))
        result_id = result_path.stem.removesuffix(".result")
        sources = result.get("source_files", [])
        source_paths.update(sources)
        categories_for_source: dict[str, set[str]] = collections.defaultdict(set)
        for warning in result.get("warnings", []):
            category = classify_warning(warning)
            category_name = category.value
            warning_counts[category_name] += 1
            warning_notices[category_name].add(result_id)
            source = _warning_source(warning, sources)
            if source:
                categories_for_source[source].add(category_name)
            if category in {
                WarningCategory.OCR_NO_TEXT,
                WarningCategory.OCR_FAILURE,
                WarningCategory.OCR_SKIPPED,
                WarningCategory.OCR_REVIEW,
            }:
                ocr_notice_ids.add(result_id)
            if category == WarningCategory.OCR_NO_TEXT:
                ocr_no_text_notice_ids.add(result_id)
            if category == WarningCategory.PARSER_FAILURE and source:
                suffix = PurePosixPath(source.replace("\\", "/")).suffix.lower() or "(none)"
                parser_failure_suffixes[suffix] += 1
            if "实际为" in warning:
                label = warning.split("实际为", 1)[1].split("，", 1)[0].strip()
                format_overrides[label] += 1

        for source in sources:
            normalized = source.replace("\\", "/")
            suffix = PurePosixPath(normalized).suffix.lower() or "(no extension)"
            if suffix in {".html", ".htm"}:
                html_sources += 1
                continue
            attachment_sources += 1
            suffix_counts[suffix] += 1
            file_categories[source] = categories_for_source.get(source, set())

    states: collections.Counter[str] = collections.Counter()
    for source in source_paths:
        if PurePosixPath(source.replace("\\", "/")).suffix.lower() in {".html", ".htm"}:
            continue
        categories = file_categories.get(source, set())
        if WarningCategory.SOURCE_CORRUPT.value in categories:
            states["source_corrupt"] += 1
        elif WarningCategory.PARSER_FAILURE.value in categories:
            states["parser_failure"] += 1
        elif WarningCategory.UNSUPPORTED_FORMAT.value in categories:
            states["unsupported_format"] += 1
        elif WarningCategory.ROLE_SKIPPED.value in categories:
            states["intentional_role_skip"] += 1
        else:
            states["parsed_or_partial"] += 1

    unavailable_records = warning_counts[WarningCategory.SOURCE_UNAVAILABLE.value]
    archive_member_failures = warning_counts[WarningCategory.ARCHIVE_MEMBER_FAILURE.value]
    # These source responses and failed members are detected during expansion, so they
    # are intentionally not counted as parser leaf files in `attachment_sources`.
    category_totals = dict(sorted(states.items()))
    parsed_or_partial = category_totals.get("parsed_or_partial", 0)
    intentional_skip = category_totals.get("intentional_role_skip", 0)
    parser_failures = category_totals.get("parser_failure", 0)
    unsupported = category_totals.get("unsupported_format", 0)
    source_corrupt = category_totals.get("source_corrupt", 0)

    with sqlite3.connect(f"file:{dataset.resolve().as_posix()}?mode=ro", uri=True) as connection:
        notice_count = connection.execute("SELECT COUNT(*) FROM notices").fetchone()[0]
        receipt_count = connection.execute("SELECT COUNT(*) FROM import_receipts").fetchone()[0]

    return {
        "date": "2026-09-30",
        "purpose": "1038-notice attachment format, parse outcome, failure-isolation, and OCR coverage summary",
        "source": {
            "dataset_database": dataset.name,
            "completed_result_files": len(result_files),
            "database_notices": notice_count,
            "idempotent_import_receipts": receipt_count,
            "raw_materials_modified": False,
        },
        "scope": {
            "html_notices": html_sources,
            "expanded_attachment_leaf_documents": attachment_sources,
            "attachment_suffix_counts": dict(sorted(suffix_counts.items())),
            "suffix_count_sum": sum(suffix_counts.values()),
        },
        "attachment_outcomes": {
            "parsed_or_partial_text_ocr_or_table": parsed_or_partial,
            "intentionally_retained_reference_materials_not_parsed": intentional_skip,
            "parser_failures": parser_failures,
            "unsupported_formats": unsupported,
            "source_corrupt_detected_before_parse": source_corrupt,
            "unavailable_download_response_members_excluded_before_parse": unavailable_records,
            "compressed_member_read_failures_excluded_before_parse": archive_member_failures,
            "outcome_sum_including_parser_leaves": sum(category_totals.values()),
            "parser_failure_suffixes": dict(sorted(parser_failure_suffixes.items())),
        },
        "warning_records": dict(sorted(warning_counts.items())),
        "notices_with_warning_category": {
            key: len(value) for key, value in sorted(warning_notices.items())
            if key != WarningCategory.OTHER.value
        },
        "detected_format_overrides": dict(sorted(format_overrides.items())),
        "ocr": {
            "notices_with_ocr_warnings_or_results": len(ocr_notice_ids),
            "rapidocr_result_warning_records": warning_counts[WarningCategory.OCR_REVIEW.value],
            "notices_with_no_trusted_ocr_text": len(ocr_no_text_notice_ids),
            "no_trusted_ocr_text_warning_records": warning_counts[WarningCategory.OCR_NO_TEXT.value],
        },
        "pdf_recovery_probe": {
            "pypdf_parse_failures": 17,
            "pdfplumber_fallback_recovered": 9,
            "pypdf_and_pdfplumber_both_failed": 8,
            "poppler_pdfinfo_and_pdftotext_both_also_failed": 8,
            "poppler_pdftotext_succeeded_after_pypdf_failure": 9,
            "interpretation": "9 of 17 pypdf failures now return through the pdfplumber fallback; 8 remain unreadable by both parser paths and Poppler. This is evidence of source-level damage, not a universal proof that recovery is impossible.",
        },
        "targeted_pdf_ocr_replay": {
            "pdfs_replayed_with_current_parser_and_local_rapidocr": 17,
            "pdfs_recovered_by_pdfplumber_fallback": 9,
            "pdfs_unreadable_by_pypdf_and_pdfplumber": 8,
            "rapidocr_result_warning_records": 12,
            "pdfs_with_ocr_result_warnings": 9,
            "notices_with_new_ocr_coverage_after_comparing_baseline": 6,
            "combined_notices_with_ocr_coverage": 365,
            "combined_rapidocr_result_warning_records": 2273,
            "new_no_trusted_ocr_text_warnings": 0,
            "projected_outcomes_if_recovery_replaced_in_full_snapshot": {
                "parsed_or_partial": parsed_or_partial + 9,
                "intentionally_retained_reference_materials_not_parsed": intentional_skip,
                "parser_failures_remaining": parser_failures - 9,
                "unsupported_formats": unsupported,
            },
            "interpretation": "This read-only targeted replay did not update the stored 1038-notice database or its job result files.",
        },
        "interpretation_limits": [
            "Counts describe the completed 2026-09-26 rules + RapidOCR job result snapshot; no 1038-notice rerun was performed for this summary.",
            "PDF Poppler recovery counts come from a read-only probe of the 17 stored pypdf exceptions using pdfinfo and pdftotext; they are not accuracy measures.",
            "A parser exception is classified as a program/parser failure unless an independent source-integrity check also fails.",
            "The 87 unavailable download responses remain unmodified source material and contain no recoverable attachment content.",
        ],
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dataset", type=Path, default=DEFAULT_DATASET)
    parser.add_argument("--results", type=Path, default=DEFAULT_RESULTS)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    args = parser.parse_args()
    report = build_report(args.dataset, args.results)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(report["attachment_outcomes"], ensure_ascii=False, indent=2))
    print(f"Wrote {args.output}")


if __name__ == "__main__":
    main()
