from __future__ import annotations

import csv
import json
from pathlib import Path

from app import gold_route_evaluation
from app.evaluation import GoldDataset, PredictionDataset, evaluate_dataset
from app.route_evaluation_report import build_report, duplicate_candidates, render_markdown


def _item(name: str = "电脑") -> dict[str, object]:
    return {
        "item_id": f"item-{name}",
        "product_name": name,
        "category": None,
        "brand": None,
        "model": None,
        "unit_price": None,
        "quantity": None,
        "total_price": None,
    }


def _package(package_id: str, *, item_name: str = "电脑") -> dict[str, object]:
    return {
        "package_id": package_id,
        "items": [_item(item_name)],
        "buyer": {"entity_id": f"buyer-{package_id}", "name": "采购单位"},
        "winners": [
            {
                "entity_id": f"winner-{package_id}",
                "name": "中标公司",
                "award_amount": None,
            }
        ],
        "bidders": [
            {
                "entity_id": f"bidder-{package_id}",
                "name": "中标公司",
                "outcome": "winner",
            }
        ],
    }


def _gold(notice_id: str = "n1") -> dict[str, object]:
    return {
        "schema_version": "1.0",
        "status": "reviewed",
        "notices": [
            {
                "notice_id": notice_id,
                "packages": [_package("p1"), _package("p2", item_name="打印机")],
            }
        ],
    }


def _prediction(*, duplicate: bool = True, package_id: str = "p1") -> dict[str, object]:
    item_rows = [_item("电脑")]
    if duplicate:
        item_rows.append({**_item("电脑"), "item_id": "item-duplicate"})
    package = _package(package_id)
    package["items"] = item_rows
    # p3 is deliberately an extra package for package-set diagnostics.
    return {
        "schema_version": "1.0",
        "status": "predicted",
        "notices": [{"notice_id": "n1", "packages": [package, _package("p3")]}],
    }


def _write_run(tmp_path: Path) -> tuple[Path, Path, Path]:
    gold_path = tmp_path / "gold.json"
    manifest_path = tmp_path / "manifest.csv"
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    gold_data = _gold()
    gold_path.write_text(json.dumps(gold_data, ensure_ascii=False), encoding="utf-8")
    with manifest_path.open("w", encoding="utf-8", newline="") as stream:
        writer = csv.DictWriter(
            stream,
            fieldnames=["notice_id", "official_file", "assignment", "categories", "source_files"],
        )
        writer.writeheader()
        writer.writerow(
            {
                "notice_id": "n1",
                "official_file": "n1",
                "assignment": "pilot:A+B",
                "categories": "有附件;OCR提示",
                "source_files": "n1.html;n1.zip",
            }
        )
    gold = GoldDataset.model_validate(gold_data)
    predictions = _prediction()
    prediction_model = PredictionDataset.model_validate(predictions)
    report = evaluate_dataset(gold, prediction_model).model_dump(mode="json")
    for mode in ("rules", "hybrid", "model"):
        (run_dir / f"predictions-{mode}.json").write_text(
            json.dumps(predictions, ensure_ascii=False),
            encoding="utf-8",
        )
        (run_dir / f"evaluation-{mode}.json").write_text(
            json.dumps(report, ensure_ascii=False),
            encoding="utf-8",
        )
    import hashlib

    sha = lambda path: hashlib.sha256(path.read_bytes()).hexdigest()
    progress = {
        "status": "completed",
        "identity": {
            "scope": "attachments",
            "gold_sha256": sha(gold_path),
            "manifest_sha256": sha(manifest_path),
            "notice_ids": ["n1"],
            "code_sha256": {"ingestion.py": "test-code-hash"},
        },
        "completed": {
            mode: {
                "n1": {
                    "status": "done",
                    "notice_id": "n1",
                    "warnings": ["OCR page warning", "附件展开失败"],
                    "errors": [],
                }
            }
            for mode in ("rules", "hybrid", "model")
        },
    }
    (run_dir / "progress.json").write_text(
        json.dumps(progress, ensure_ascii=False),
        encoding="utf-8",
    )
    summary = {
        "modes": {
            mode: {
                "completed_notices": 1,
                "failed_or_incomplete_notices": 0,
                "elapsed_seconds_sum": 2.0,
                "elapsed_seconds_median": 2.0,
                "requests": 1 if mode != "rules" else 0,
                "prompt_tokens": 10 if mode != "rules" else None,
                "completion_tokens": 5 if mode != "rules" else None,
                "source_documents_parsed": 2,
            }
            for mode in ("rules", "hybrid", "model")
        },
    }
    (run_dir / "summary.json").write_text(
        json.dumps(summary, ensure_ascii=False),
        encoding="utf-8",
    )
    return gold_path, manifest_path, run_dir


def test_duplicate_candidates_and_package_alignment_are_explicit(tmp_path):
    assert duplicate_candidates(_prediction())["items"]["extra_rows"] == 1
    gold_path, manifest_path, run_dir = _write_run(tmp_path)
    report = build_report(
        gold_path=gold_path,
        manifest_path=manifest_path,
        run_dir=run_dir,
        dataset_role="tuning",
        stage="prep",
    )
    route = report["modes"]["model"]
    assert route["package_alignment"]["exact_notice_count"] == 0
    assert route["package_alignment"]["matched_package_count"] == 1
    assert route["package_alignment"]["missing_package_count"] == 1
    assert route["package_alignment"]["extra_package_count"] == 1
    assert route["duplicates"]["items"]["extra_rows"] == 1
    assert route["coverage"]["notices_with_attachments"] == 1
    assert route["coverage"]["notices_with_ocr_warning"] == 1
    assert report["recommendation"]["status"] == "pending_freeze_and_independent_holdout"


def test_final_stage_requires_disjoint_reviewed_tuning_gold_and_recommends(tmp_path):
    gold_path, manifest_path, run_dir = _write_run(tmp_path)
    tuning_path = tmp_path / "tuning.json"
    tuning_path.write_text(
        json.dumps(
            {
                "schema_version": "1.0",
                "status": "reviewed",
                "notices": [{"notice_id": "tuning-notice", "packages": []}],
            }
        ),
        encoding="utf-8",
    )
    report = build_report(
        gold_path=gold_path,
        manifest_path=manifest_path,
        run_dir=run_dir,
        dataset_role="holdout",
        stage="final",
        tuning_gold_path=tuning_path,
    )
    assert report["recommendation"]["status"] == "determined"
    assert report["recommendation"]["route"] in {"model", "hybrid"}
    markdown = render_markdown(report)
    assert "团队本地 Gold 验证" in markdown
    assert "68.56" not in markdown


def test_route_runner_can_select_notice_ids_for_targeted_replay(tmp_path, monkeypatch):
    notice_id = "n1" + "0" * 18
    source_root = tmp_path / "sources"
    source_root.mkdir()
    (source_root / "n1.html").write_text("<html>公告</html>", encoding="utf-8")
    manifest_path = tmp_path / "manifest.csv"
    with manifest_path.open("w", encoding="utf-8", newline="") as stream:
        writer = csv.DictWriter(
            stream,
            fieldnames=["notice_id", "official_file", "assignment", "categories", "source_files"],
        )
        writer.writeheader()
        writer.writerow(
            {
                "notice_id": notice_id,
                "official_file": "n1",
                "assignment": "annotator-a",
                "categories": "",
                "source_files": "n1.html",
            }
        )
    gold = GoldDataset.model_validate(_gold(notice_id))
    monkeypatch.setattr(gold_route_evaluation, "_sha256_file", lambda path: notice_id)
    records = gold_route_evaluation._load_sources(
        gold,
        manifest_path,
        source_root,
        pilot_only=False,
        limit=None,
        notice_ids={notice_id},
    )
    assert [row["notice_id"] for row in records] == [notice_id]
