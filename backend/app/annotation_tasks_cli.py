"""Create offline, stratified annotation task bundles from a completed local batch job.

The bundles and copied source files contain official corpus data and must stay under
``backend/.data``. No model configuration is read and this command never calls a model.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import random
import shutil
import sys
import tempfile
import zipfile
from collections import Counter
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from app.config import settings
from app.schemas import ImportResult

QUOTAS = {
    "attachment": 6,
    "html_table": 6,
    "no_zip": 2,
    "ocr": 4,
    "multi_package": 4,
    "high_candidate": 2,
    "zero_candidate": 2,
}
CATEGORY_LABELS = {
    "attachment": "有附件",
    "html_table": "正文中有标的候选",
    "no_zip": "没有配对ZIP",
    "ocr": "OCR提示",
    "multi_package": "多包候选",
    "high_candidate": "候选超过15条",
    "zero_candidate": "零候选",
}
TASK_SCHEMA = "bid-intel-annotation-task/1"


def _read_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"无法读取 JSON：{path.name}（{exc}）") from exc


def _write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _html_path(entry: dict[str, Any]) -> Path:
    html = [Path(row["path"]) for row in entry["files"]
            if Path(row["name"]).suffix.lower() in {".html", ".htm"}]
    if len(html) != 1:
        raise ValueError(f"公告 {entry.get('index')} 必须且只能对应一个 HTML")
    return html[0]


def _prediction_notice(result: ImportResult, notice_id: str) -> dict[str, Any]:
    codes = sorted({row.package_code for row in [*result.items, *result.participants]}) or ["default"]
    packages = []
    for code in codes:
        people = [person for person in result.participants if person.package_code == code]
        items = [row for row in result.items if row.package_code == code]
        packages.append({
            "package_id": code,
            "items": [{
                "item_id": f"item-{index + 1}",
                "product_name": row.model_dump(mode="json")["product_name"],
                "category": row.model_dump(mode="json")["category"],
                "brand": row.model_dump(mode="json")["brand"],
                "model": row.model_dump(mode="json")["model"],
                "quantity": row.model_dump(mode="json")["quantity"],
                "unit_price": row.model_dump(mode="json")["unit_price"],
                "total_price": row.model_dump(mode="json")["total_price"],
            } for index, row in enumerate(items)],
            "buyer": ({"entity_id": "buyer-1", "name": result.metadata.procurement_unit}
                      if result.metadata.procurement_unit else None),
            "winners": [{
                "entity_id": f"winner-{index + 1}",
                "name": person.organization_name,
                "award_amount": person.model_dump(mode="json")["award_amount"],
            } for index, person in enumerate(people) if person.outcome == "winner"],
            "bidders": [{
                "entity_id": f"bidder-{index + 1}",
                "name": person.organization_name,
                "outcome": person.outcome,
            } for index, person in enumerate(people)],
        })
    return {"notice_id": notice_id, "packages": packages}


def _gold_skeleton(prediction: dict[str, Any]) -> dict[str, Any]:
    return {
        "notice_id": prediction["notice_id"],
        "packages": [{
            "package_id": package["package_id"],
            "items": [],
            "buyer": None,
            "winners": [],
            "bidders": [],
        } for package in prediction["packages"]],
    }


def _categories(entry: dict[str, Any], result: ImportResult) -> set[str]:
    names = [row["name"].lower() for row in entry["files"]]
    has_attachment = any(Path(name).suffix not in {".html", ".htm"} for name in names)
    item_count = len(result.items)
    package_codes = {row.package_code for row in [*result.items, *result.participants]}
    warnings = "\n".join(result.warnings).casefold()
    categories = set()
    if has_attachment:
        categories.add("attachment")
    if any(Path(row.source_file).suffix.lower() in {".html", ".htm"} for row in result.items):
        categories.add("html_table")
    if not has_attachment:
        categories.add("no_zip")
    if "ocr" in warnings or "rapidocr" in warnings or "扫描" in warnings:
        categories.add("ocr")
    if len(package_codes) > 1:
        categories.add("multi_package")
    if item_count > 15:
        categories.add("high_candidate")
    if not result.items and not result.participants:
        categories.add("zero_candidate")
    return categories


def stratified_sample(records: list[dict[str, Any]], sample_size: int, seed: int) -> tuple[list[dict[str, Any]], dict[str, int]]:
    if sample_size < 1 or sample_size > len(records):
        raise ValueError(f"样本数必须在 1 到 {len(records)} 之间")
    counts = Counter(category for record in records for category in record["categories"])
    targets = {category: min(quota, counts[category]) for category, quota in QUOTAS.items()}
    rng = random.Random(seed)
    tie_break = {record["notice_id"]: rng.random() for record in records}
    selected: list[dict[str, Any]] = []
    remaining = list(records)
    covered = Counter()
    while remaining and len(selected) < sample_size:
        def score(record: dict[str, Any]) -> float:
            return sum(
                (targets[category] - covered[category]) / targets[category]
                for category in record["categories"]
                if targets.get(category, 0) > covered[category]
            )

        best_score = max(score(record) for record in remaining)
        if best_score > 0:
            choices = [record for record in remaining if score(record) == best_score]
            chosen = min(choices, key=lambda record: tie_break[record["notice_id"]])
        else:
            chosen = min(remaining, key=lambda record: tie_break[record["notice_id"]])
        selected.append(chosen)
        remaining.remove(chosen)
        covered.update(chosen["categories"])
    return selected, dict(covered)


def _balanced_unique_assignment(records: list[dict[str, Any]], seed: int) -> tuple[list[dict], list[dict]]:
    if len(records) % 2:
        raise ValueError("试标之外的样本数量必须可以平分给两位标注员")
    rng = random.Random(seed ^ 0xA17E)
    tie = {record["notice_id"]: rng.random() for record in records}
    ordered = sorted(records, key=lambda row: (-len(row["categories"]), tie[row["notice_id"]]))
    left: list[dict] = []
    right: list[dict] = []
    left_counts: Counter[str] = Counter()
    right_counts: Counter[str] = Counter()
    capacity = len(records) // 2
    for record in ordered:
        if len(left) >= capacity:
            right.append(record)
            right_counts.update(record["categories"])
            continue
        if len(right) >= capacity:
            left.append(record)
            left_counts.update(record["categories"])
            continue
        left_cost = sum(abs(left_counts[c] + (c in record["categories"]) - right_counts[c])
                        for c in QUOTAS)
        right_cost = sum(abs(left_counts[c] - right_counts[c] - (c in record["categories"]))
                         for c in QUOTAS)
        if left_cost < right_cost or (left_cost == right_cost and tie[record["notice_id"]] < 0.5):
            left.append(record)
            left_counts.update(record["categories"])
        else:
            right.append(record)
            right_counts.update(record["categories"])
    return left, right


def _cached_text(cache_dir: Path, content: bytes, suffix: str, options: dict, limits: dict) -> tuple[str, list[str]] | None:
    identity = json.dumps({"version": 1, "suffix": suffix, "options": options, "limits": limits}, sort_keys=True)
    digest = hashlib.sha256(identity.encode() + content).hexdigest()
    path = cache_dir / f"{digest}.json"
    if not path.is_file():
        return None
    try:
        value = _read_json(path)
    except ValueError:
        return None
    return str(value.get("text", "")), list(value.get("warnings", []))


def _source_payload(entry: dict[str, Any], job: dict[str, Any], shared_source_dir: Path,
                    cache_dir: Path, *, scope: str) -> tuple[list[dict], str, list[str], list[dict]]:
    from app.archive_files import DiskDocument, expand_paths
    from app.parsers import SourceDocument, parse_document

    source_files = []
    copied_dir = shared_source_dir / scope / entry["notice_id"]
    copied_dir.mkdir(parents=True, exist_ok=True)
    for file in entry["manifest"]["files"]:
        path = Path(file["path"])
        if _sha256(path) != file["sha256"]:
            raise ValueError(f"源文件已变化，拒绝打包：{path.name}")
        safe_name = Path(file["name"]).name
        destination = copied_dir / safe_name
        shutil.copyfile(path, destination)
        source_files.append({
            "name": safe_name,
            "relative_path": destination.relative_to(shared_source_dir.parent).as_posix(),
            "sha256": file["sha256"],
        })

    limits = job["limits"]
    for name, value in limits.items():
        if hasattr(settings, name):
            setattr(settings, name, value)
    parser_options = {}
    if job.get("ocr"):
        parser_options = {
            "ocr_enabled": True,
            "ocr_language": settings.ocr_language,
            "ocr_timeout_seconds": settings.ocr_timeout_seconds,
        }
    parsed_sections = []
    warnings = []
    parse_audit = []
    with tempfile.TemporaryDirectory(prefix="annotation-expand-") as temporary:
        documents, expansion_warnings = expand_paths(
            [DiskDocument(row["name"], Path(row["path"])) for row in entry["manifest"]["files"]],
            Path(temporary),
        )
        warnings.extend(expansion_warnings)
        for document in documents:
            content = document.content
            suffix = Path(document.filename).suffix
            cached = _cached_text(cache_dir, content, suffix, parser_options, limits)
            if cached is None:
                text, _, parse_warnings = parse_document(
                    SourceDocument("document" + suffix, content), **parser_options
                )
                cache_status = "reparsed"
            else:
                text, parse_warnings = cached
                cache_status = "cache_hit"
            parse_audit.append({"name": document.filename, "cache_status": cache_status})
            warnings.extend(parse_warnings)
            if text:
                parsed_sections.append(f"【{document.filename}】\n{text}")
    if not parsed_sections:
        warnings.append("自动解析未生成来源文字；请打开任务包 sources 文件夹中的原始公告和附件核对。")
    return source_files, "\n\n".join(parsed_sections), list(dict.fromkeys(warnings)), parse_audit


def _bundle(task_id: str, assignee: str, records: list[dict], sources: dict[str, dict], seed: int) -> dict:
    predictions = {"schema_version": "1.0", "status": "predicted",
                   "notices": [record["prediction"] for record in records]}
    gold = {"schema_version": "1.0", "status": "draft",
            "notices": [_gold_skeleton(record["prediction"]) for record in records]}
    return {
        "schema_version": TASK_SCHEMA,
        "task_id": task_id,
        "assignee": assignee,
        "seed": seed,
        "instructions": "逐条打开来源原文，独立填写空白 Gold。每条都要勾选‘本公告已对照原文核验’。不要照抄自动预测。",
        "gold": gold,
        "predictions": predictions,
        "sources": [sources[record["notice_id"]] for record in records],
    }


def _write_delivery_archive(
    output_dir: Path, bundle_name: str, source_scope: str, archive_name: str, quickstart: Path
) -> str:
    """Create one ready-to-send archive containing only one assignment's originals."""
    archive_path = output_dir / archive_name
    source_dir = output_dir / "sources" / source_scope
    with zipfile.ZipFile(archive_path, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=6) as archive:
        archive.write(output_dir / bundle_name, bundle_name)
        archive.write(quickstart, "START_HERE.txt")
        for path in sorted(source_dir.rglob("*")):
            if path.is_file():
                archive.write(path, path.relative_to(output_dir).as_posix())
    return archive_name


def build_task_package(job_dir: Path, output_dir: Path, *, sample_size: int = 24, pilot_size: int = 6,
                       seed: int = 20260926,
                       annotator_a: str = "标注员 A", annotator_b: str = "标注员 B") -> dict[str, Any]:
    job_dir = job_dir.expanduser().resolve(strict=True)
    output_dir = output_dir.expanduser().resolve()
    data_root = settings.resolved_database_path.parent.resolve()
    if output_dir != data_root and data_root not in output_dir.parents:
        raise ValueError(f"输出必须位于本机忽略提交的 .data 目录：{data_root}")
    if output_dir.exists() and any(output_dir.iterdir()):
        raise ValueError(f"输出目录已有文件，为保护现有标注拒绝覆盖：{output_dir}")

    job = _read_json(job_dir / "job.json")
    if job.get("status") not in {"done", "completed", "complete"}:
        raise ValueError(f"后台任务尚未完成（status={job.get('status')}）")
    manifest = _read_json(job_dir / "manifest.json")
    if not isinstance(manifest, list) or not manifest:
        raise ValueError("任务 manifest 为空")
    if sample_size < 2 or sample_size % 2:
        raise ValueError("样本数必须是 >=2 的偶数，便于平分给两位标注员")
    if pilot_size < 0 or pilot_size >= sample_size or (sample_size - pilot_size) % 2:
        raise ValueError("pilot 数量必须小于样本数，并保证剩余样本可平分")

    records = []
    result_dir = job_dir / "notices"
    for entry in manifest:
        result_path = result_dir / f"{entry['index']:05d}.result.json"
        status_path = result_dir / f"{entry['index']:05d}.json"
        if not result_path.is_file() or not status_path.is_file():
            raise ValueError(f"后台任务记录缺失：第 {entry['index'] + 1} 条")
        status = _read_json(status_path)
        if status.get("status") != "done":
            raise ValueError(f"后台任务包含未完成公告：第 {entry['index'] + 1} 条")
        result = ImportResult.model_validate(_read_json(result_path))
        html_path = _html_path(entry)
        notice_id = hashlib.sha256(html_path.read_bytes()).hexdigest()[:20]
        result.notice_id = int(result.notice_id or 0)
        prediction = _prediction_notice(result, notice_id)
        records.append({
            "notice_id": notice_id,
            "name": entry["name"],
            "manifest": entry,
            "result": result,
            "prediction": prediction,
            "categories": _categories(entry, result),
        })
    if len({record["notice_id"] for record in records}) != len(records):
        raise ValueError("发现重复公告 HTML 摘要，拒绝生成可能冲突的标注任务")
    if pilot_size > sample_size // 2:
        raise ValueError("为让两位标注员完成同一试标，pilot 数不能超过样本数的一半")

    chosen, _ = stratified_sample(records, sample_size, seed)
    pilot = chosen[:pilot_size]
    unique_a, unique_b = _balanced_unique_assignment(chosen[pilot_size:], seed)
    output_dir.mkdir(parents=True, exist_ok=True)
    source_root = output_dir / "sources"
    cache_dir = Path(job.get("cache_database", settings.resolved_database_path)).parent / "parse-cache"
    parser_options = ({
        "ocr_enabled": True,
        "ocr_language": settings.ocr_language,
        "ocr_timeout_seconds": settings.ocr_timeout_seconds,
    } if job.get("ocr") else {})
    pilot_ids = {row["notice_id"] for row in pilot}
    a_ids = {row["notice_id"] for row in unique_a}
    b_ids = {row["notice_id"] for row in unique_b}
    source_map: dict[str, dict] = {}
    for record in chosen:
        record["manifest"]["notice_id"] = record["notice_id"]
        scope = "pilot" if record["notice_id"] in pilot_ids else "annotator-a" if record["notice_id"] in a_ids else "annotator-b"
        copied, text, warnings, parse_audit = _source_payload(record, job, source_root, cache_dir, scope=scope)
        result_warnings = record["result"].warnings
        source_map[record["notice_id"]] = {
            "notice_id": record["notice_id"],
            "files": copied,
            "text": text,
            "warnings": list(dict.fromkeys([*result_warnings, *warnings])),
            "parse_audit": parse_audit,
        }

    _write_json(output_dir / "predictions.canonical.json", {
        "schema_version": "1.0", "status": "predicted",
        "notices": [record["prediction"] for record in chosen],
    })
    if pilot:
        _write_json(output_dir / "pilot-a.bundle.json", _bundle(
            "pilot-a", f"{annotator_a}：先完成共同试标，再等协调员分配正式批次", pilot, source_map, seed,
        ))
        _write_json(output_dir / "pilot-b.bundle.json", _bundle(
            "pilot-b", f"{annotator_b}：先完成共同试标，再等协调员分配正式批次", pilot, source_map, seed,
        ))
    _write_json(output_dir / "annotator-a.bundle.json", _bundle(
        "annotator-a", f"{annotator_a}：完成分给自己的公告", unique_a, source_map, seed,
    ))
    _write_json(output_dir / "annotator-b.bundle.json", _bundle(
        "annotator-b", f"{annotator_b}：完成分给自己的公告", unique_b, source_map, seed,
    ))

    delivery_archives = []
    quickstart = Path(__file__).resolve().parents[2] / "docs" / "ANNOTATION_QUICKSTART.txt"
    if not quickstart.is_file():
        raise ValueError(f"队友操作卡缺失：{quickstart}")
    if pilot:
        delivery_archives.extend([
            _write_delivery_archive(output_dir, "pilot-a.bundle.json", "pilot", "pilot-a.ready.zip", quickstart),
            _write_delivery_archive(output_dir, "pilot-b.bundle.json", "pilot", "pilot-b.ready.zip", quickstart),
        ])
    delivery_archives.extend([
        _write_delivery_archive(output_dir, "annotator-a.bundle.json", "annotator-a", "annotator-a.ready.zip", quickstart),
        _write_delivery_archive(output_dir, "annotator-b.bundle.json", "annotator-b", "annotator-b.ready.zip", quickstart),
    ])

    counts = Counter(category for record in chosen for category in record["categories"])
    unmet = {category: max(0, min(quota, sum(category in row["categories"] for row in records)) - counts[category])
             for category, quota in QUOTAS.items() if counts[category] < min(quota, sum(category in row["categories"] for row in records))}
    with (output_dir / "sample_manifest.csv").open("w", newline="", encoding="utf-8-sig") as stream:
        writer = csv.DictWriter(stream, fieldnames=["notice_id", "official_file", "assignment", "categories", "source_files"])
        writer.writeheader()
        for record in chosen:
            category_labels = [CATEGORY_LABELS[name] for name in QUOTAS if name in record["categories"]]
            assignment = "pilot:A+B" if record["notice_id"] in pilot_ids else "annotator-a" if record["notice_id"] in a_ids else "annotator-b" if record["notice_id"] in b_ids else "unassigned"
            writer.writerow({
                "notice_id": record["notice_id"],
                "official_file": record["name"],
                "assignment": assignment,
                "categories": ";".join(category_labels),
                "source_files": ";".join(file["name"] for file in source_map[record["notice_id"]]["files"]),
            })
    summary = {
        "sample_size": sample_size,
        "pilot_size": pilot_size,
        "seed": seed,
        "coverage": {CATEGORY_LABELS[key]: counts[key] for key in QUOTAS},
        "requested_coverage": {CATEGORY_LABELS[key]: min(QUOTAS[key], sum(key in row["categories"] for row in records)) for key in QUOTAS},
        "coverage_shortfall": {CATEGORY_LABELS[key]: value for key, value in unmet.items()},
        "source_bundle_files": sum(len(value["files"]) for value in source_map.values()),
        "files_per_assignee": {
            "annotator_a_unique": sum(len(source_map[row["notice_id"]]["files"]) for row in unique_a),
            "annotator_b_unique": sum(len(source_map[row["notice_id"]]["files"]) for row in unique_b),
            "shared_pilot": sum(len(source_map[row["notice_id"]]["files"]) for row in pilot),
        },
        "parse_cache": {
            "cache_hits": sum(row["cache_status"] == "cache_hit" for value in source_map.values() for row in value["parse_audit"]),
            "reparsed": sum(row["cache_status"] == "reparsed" for value in source_map.values() for row in value["parse_audit"]),
            "parser_options": parser_options,
            "identity_note": "cache key uses parser identity version 1 and does not include a code commit; parsed text is source-navigation aid only, verify against original files",
        },
        "source_text_missing_notices": sum(not source["text"] for source in source_map.values()),
        "annotation_order": f"两位标注员先分别完成 pilot-a/pilot-b 共 {pilot_size} 条；协调员讨论差异后，再各自完成 {len(unique_a)} 条不重叠正式任务。",
        "prediction_notice_ids_match_sample": True,
        "model_calls": 0,
        "ready_to_send_archives": delivery_archives,
    }
    _write_json(output_dir / "sample_summary.json", summary)
    readme = f"""人工标注任务包（{sample_size} 条）

发给队友时直接发送下列 ZIP；不要发送其他人的文件：
- A：先发 pilot-a.ready.zip；通过校准后再发 annotator-a.ready.zip。
- B：先发 pilot-b.ready.zip；通过校准后再发 annotator-b.ready.zip。
- 每个 ZIP 解压后会看到一个 `*.bundle.json` 和配套的 `sources/` 原件。

先做共同试标：
1. 标注员 A 导入 pilot-a.bundle.json；标注员 B 导入 pilot-b.bundle.json。两份是同一组 {pilot_size} 条公告，独立标注，不要互看答案。
2. 每条公告都对照原文和附件，填写完后勾选本公告的核验框。页面会自动保存；中途离开前点“下载进度备份”。
3. 两人分别导出已核验 Gold，把文件交协调员。协调员讨论不同答案并形成 pilot.adjudicated.json；不要把两份重叠试标直接合并。
4. 协调员确认口径后，A 导入 annotator-a.bundle.json，B 导入 annotator-b.bundle.json，完成各自互不重叠的正式样本。

协调员文件：predictions.canonical.json 是唯一基准预测；sample_manifest.csv 是分配清单；sample_summary.json 是分层和解析缓存统计。
原件按上述目录随任务发送；bundle 左侧显示已解析文本和原始文件名。扫描件、附件缺失、损坏或来源不清时打开原件核对；无法确认就不勾选，导出“已核验部分”并通知协调员。

不要提交或外传 backend/.data 中的整份官方语料。任务包只含抽样公告的原始文件、解析文本和自动预测。自动预测不是答案；本地评测也不是官方成绩。
"""
    (output_dir / "README.txt").write_text(readme, encoding="utf-8")
    return summary


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="从已完成的本地后台任务生成分层人工标注包及可直接分发的独立 ZIP；不调用模型。")
    parser.add_argument("--job-dir", type=Path, required=True, help="已完成后台任务目录")
    parser.add_argument("--output-dir", type=Path, required=True, help="必须放在 backend/.data 内的新目录")
    parser.add_argument("--sample-size", type=int, default=24, help="分层样本总数，默认 24")
    parser.add_argument("--pilot-size", type=int, default=6, help="两位标注员共同独立标注的试点数，默认 6")
    parser.add_argument("--seed", type=int, default=20260926, help="固定随机种子，默认 20260926")
    parser.add_argument("--annotator-a", default="标注员 A",
                        help="A 的名字，写在任务包和标注页顶部，便于队友确认拿到的是自己那份")
    parser.add_argument("--annotator-b", default="标注员 B", help="B 的名字")
    args = parser.parse_args(argv)
    try:
        summary = build_task_package(args.job_dir, args.output_dir,
                                     sample_size=args.sample_size, pilot_size=args.pilot_size, seed=args.seed,
                                     annotator_a=args.annotator_a, annotator_b=args.annotator_b)
    except (OSError, ValueError, ValidationError) as exc:
        print(f"Annotation task generation failed: {exc}", file=sys.stderr)
        return 2
    print(json.dumps({"output_dir": str(args.output_dir.resolve()), **summary}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
