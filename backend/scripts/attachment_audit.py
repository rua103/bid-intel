"""Build a read-only, per-source attachment audit ledger from a completed job snapshot."""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import zipfile
from collections import defaultdict
from pathlib import Path, PurePosixPath
from typing import Any
from urllib.parse import unquote, urlparse

from bs4 import BeautifulSoup

from app.warning_categories import WarningCategory, classify_warning

BACKEND_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_RESULTS = BACKEND_ROOT / ".data/jobs/47508ea8390b4522a6766d79c0188b5d/notices"
DEFAULT_CORPUS = BACKEND_ROOT.parent.parent / "official-corpus"
DEFAULT_OUTPUT = BACKEND_ROOT / ".data/attachment-audit-ledger.json"
MAX_MEMBER_BYTES = 64 * 1024 * 1024

FIELDS = (
    "record_id", "notice_id", "source_file", "sha256", "file_type", "html_references",
    "archive_member", "status", "warning_categories", "warnings", "retryable",
    "manual_disposition", "supplement_note",
)


def _digest(path: Path) -> str | None:
    try:
        value = hashlib.sha256()
        with path.open("rb") as source:
            for chunk in iter(lambda: source.read(1024 * 1024), b""):
                value.update(chunk)
        return value.hexdigest()
    except OSError:
        return None


def _notice_id(result_path: Path, result: dict[str, Any]) -> str:
    if result.get("notice_id"):
        return str(result["notice_id"])
    return result_path.name.removesuffix(".result.json")


def _reference_names(html_path: Path) -> set[str]:
    try:
        soup = BeautifulSoup(html_path.read_bytes(), "html.parser")
    except OSError:
        return set()
    values: set[str] = set()
    for tag in soup.find_all(["a", "iframe", "object", "embed"]):
        for attr in ("href", "src", "data"):
            raw = tag.get(attr)
            if raw:
                parsed = urlparse(str(raw))
                name = unquote(PurePosixPath(parsed.path.replace("\\", "/")).name)
                if name:
                    values.add(name.casefold())
    return values


def _normalized_name(value: str) -> str:
    return PurePosixPath(value.replace("\\", "/").split("!/")[-1]).name.casefold()


def _source_digest(path: Path, member: str) -> str | None:
    """Hash a source itself; never substitute an archive's hash for a leaf.

    Nested and non-ZIP members remain unresolved. Stream only the exact member
    with a size bound, so an audit does not expand every archive into memory.
    """
    if not member:
        return _digest(path)
    if path.suffix.lower() != ".zip" or "!/" in member:
        return None
    try:
        with zipfile.ZipFile(path) as archive:
            matches = [info for info in archive.infolist()
                       if info.filename.replace("\\", "/") == member and not info.is_dir()]
            if len(matches) != 1 or matches[0].file_size > MAX_MEMBER_BYTES:
                return None
            digest = hashlib.sha256()
            total = 0
            with archive.open(matches[0]) as source:
                for chunk in iter(lambda: source.read(1024 * 1024), b""):
                    total += len(chunk)
                    if total > MAX_MEMBER_BYTES:
                        return None
                    digest.update(chunk)
            return digest.hexdigest()
    except (OSError, RuntimeError, NotImplementedError, zipfile.BadZipFile):
        return None


def _warning_for_source(warning: str, source: str) -> bool:
    normalized = source.replace("\\", "/")
    return normalized in warning.replace("\\", "/")


def _status(categories: set[str], has_content: bool) -> str:
    if WarningCategory.SOURCE_UNAVAILABLE.value in categories:
        return "download_error_response"
    if WarningCategory.SOURCE_CORRUPT.value in categories or WarningCategory.ARCHIVE_FAILURE.value in categories:
        return "corrupt_file"
    if WarningCategory.ARCHIVE_MEMBER_FAILURE.value in categories:
        return "archive_member_read_failure"
    if WarningCategory.PARSER_FAILURE.value in categories:
        return "program_parse_failure"
    if WarningCategory.UNSUPPORTED_FORMAT.value in categories:
        return "unsupported_format"
    if WarningCategory.ROLE_SKIPPED.value in categories:
        return "reference_material"
    if WarningCategory.FORMAT_MISMATCH.value in categories:
        return "naming_mismatch"
    if "notice_html" in categories:
        return "parsed_or_partial"
    if "orphan" in categories:
        return "orphan_or_unattributed"
    if has_content:
        return "parsed_or_partial"
    return "orphan_or_unattributed"


def build_ledger(results_dir: Path, corpus_dir: Path) -> list[dict[str, Any]]:
    """Create stable rows; unresolved evidence remains explicitly unresolved."""
    rows: list[dict[str, Any]] = []
    digest_cache: dict[tuple[Path, str], str | None] = {}
    reported_results = sorted(results_dir.glob("*.result.json"))
    for result_path in reported_results:
        result = json.loads(result_path.read_text(encoding="utf-8"))
        notice_id = _notice_id(result_path, result)
        sources = list(dict.fromkeys(str(source) for source in result.get("source_files", [])))
        warnings = [str(value) for value in result.get("warnings", [])]
        categories_by_source: dict[str, set[str]] = defaultdict(set)
        warning_by_source: dict[str, list[str]] = defaultdict(list)
        for warning in warnings:
            category = classify_warning(warning).value
            for source in sources:
                if _warning_for_source(warning, source):
                    categories_by_source[source].add(category)
                    warning_by_source[source].append(warning)

        html_sources = [source for source in sources if PurePosixPath(source.replace("\\", "/")).suffix.lower() in {".html", ".htm"}]
        html_refs: set[str] = set()
        for source in html_sources:
            path = corpus_dir / PurePosixPath(source.replace("\\", "/")).name
            html_refs.update(_reference_names(path))

        file_entries = result.get("source_hashes", [])
        hash_by_source = {str(entry.get("source_file")): entry["sha256"]
                          for entry in file_entries
                          if isinstance(entry, dict) and entry.get("sha256")}
        for source in sources:
            normalized = source.replace("\\", "/")
            # The corpus stores top-level files; archive leaf provenance is retained as-is.
            physical = corpus_dir / PurePosixPath(normalized.split("!/", 1)[0]).name
            archive_member = normalized.split("!/", 1)[1] if "!/" in normalized else ""
            hash_key = (physical, archive_member)
            if hash_key not in digest_cache:
                digest_cache[hash_key] = _source_digest(physical, archive_member)
            actual_hash = digest_cache[hash_key]
            digest = hash_by_source.get(source) or actual_hash
            categories = categories_by_source[source]
            matched_html = _normalized_name(source) in html_refs
            has_content = source not in html_sources
            if source in html_sources:
                categories.add("notice_html")
            if archive_member and not digest:
                categories.add("source_hash_unavailable")
            if matched_html:
                categories.add("html_reference_match")
            if source not in html_sources and not matched_html and not archive_member and not physical.is_file():
                categories.add("orphan")
            if digest and actual_hash and digest != actual_hash:
                categories.add("same_name_different_hash")
            record_id = hashlib.sha256(f"{notice_id}\0{source}\0{digest or ''}".encode()).hexdigest()
            rows.append({
                "record_id": record_id,
                "notice_id": notice_id,
                "source_file": source,
                "sha256": digest,
                "file_type": PurePosixPath(normalized.split("!/", 1)[-1]).suffix.lower().lstrip(".") or "unknown",
                "html_references": matched_html,
                "archive_member": archive_member or None,
                "status": _status(categories, has_content),
                "warning_categories": sorted(categories),
                "warnings": list(dict.fromkeys(warning_by_source[source])),
                "retryable": any(category in categories for category in (WarningCategory.SOURCE_UNAVAILABLE.value, WarningCategory.ARCHIVE_MEMBER_FAILURE.value)),
                "manual_disposition": (
                    "pending"
                    if categories - {"html_reference_match", "notice_html"}
                    or (source not in html_sources and not matched_html and has_content)
                    else "not_required"
                ),
                "supplement_note": "",
            })

    # Include failed download/member warnings not attached to source_files.
    for result_path in reported_results:
        result = json.loads(result_path.read_text(encoding="utf-8"))
        notice_id = _notice_id(result_path, result)
        sources = list(dict.fromkeys(str(source) for source in result.get("source_files", [])))
        for warning in result.get("warnings", []):
            if any(_warning_for_source(str(warning), source) for source in sources):
                continue
            category = classify_warning(str(warning)).value
            if category not in {WarningCategory.SOURCE_UNAVAILABLE.value, WarningCategory.ARCHIVE_MEMBER_FAILURE.value}:
                continue
            source_name = str(warning).split("：", 1)[-1].split("（", 1)[0].strip()
            record_id = hashlib.sha256(f"{notice_id}\0{source_name}\0".encode()).hexdigest()
            rows.append({
                "record_id": record_id, "notice_id": notice_id, "source_file": source_name,
                "sha256": None, "file_type": PurePosixPath(source_name).suffix.lower().lstrip(".") or "unknown",
                "html_references": False,
                "archive_member": source_name.split("!/", 1)[1] if "!/" in source_name else None,
                "status": "download_error_response" if category == WarningCategory.SOURCE_UNAVAILABLE.value else "archive_member_read_failure",
                "warning_categories": [category], "warnings": [str(warning)],
                "retryable": True, "manual_disposition": "pending", "supplement_note": "",
            })
    rows.sort(key=lambda row: (row["notice_id"], row["source_file"], row["record_id"]))
    return rows


def write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8-sig", newline="") as output:
        writer = csv.DictWriter(output, fieldnames=FIELDS)
        writer.writeheader()
        for row in rows:
            writer.writerow({**row,
                "warning_categories": json.dumps(row["warning_categories"], ensure_ascii=False),
                "warnings": json.dumps(row["warnings"], ensure_ascii=False)})


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--results", type=Path, default=DEFAULT_RESULTS)
    parser.add_argument("--corpus", type=Path, default=DEFAULT_CORPUS)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--format", choices=("json", "csv"), default="json")
    args = parser.parse_args()
    rows = build_ledger(args.results, args.corpus)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    if args.format == "csv":
        write_csv(args.output, rows)
    else:
        args.output.write_text(json.dumps(rows, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"Wrote {len(rows)} attachment audit records to {args.output}")


if __name__ == "__main__":
    main()
