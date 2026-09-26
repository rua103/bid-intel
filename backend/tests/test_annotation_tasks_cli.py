import hashlib
import json
import zipfile
from pathlib import Path

import pytest

from app.annotation_tasks_cli import build_task_package, stratified_sample
from app.config import settings


def test_stratified_sample_is_deterministic_and_reports_actual_available_coverage():
    rows = [
        {"notice_id": "a", "categories": {"multi_package", "attachment"}},
        {"notice_id": "b", "categories": {"ocr", "zero_candidate"}},
        {"notice_id": "c", "categories": {"html_table", "high_candidate"}},
        {"notice_id": "d", "categories": {"attachment"}},
    ]

    first, _ = stratified_sample(rows, 3, 7)
    second, _ = stratified_sample(rows, 3, 7)

    assert [row["notice_id"] for row in first] == [row["notice_id"] for row in second]
    assert len(first) == 3


def write_job(root: Path, *, count: int = 4) -> Path:
    job_dir = root / "jobs" / "job-1"
    notices = job_dir / "notices"
    notices.mkdir(parents=True)
    manifest = []
    for index in range(count):
        html = root / "source" / f"notice-{index}.html"
        html.parent.mkdir(parents=True, exist_ok=True)
        html.write_text(f"<html><body><p>Notice {index}</p></body></html>", encoding="utf-8")
        manifest.append({
            "index": index,
            "name": html.stem,
            "files": [{"path": str(html), "name": html.name,
                       "sha256": hashlib.sha256(html.read_bytes()).hexdigest()}],
        })
        (notices / f"{index:05d}.json").write_text(json.dumps({"status": "done"}), encoding="utf-8")
        (notices / f"{index:05d}.result.json").write_text(json.dumps({
            "notice_id": 0,
            "source_files": [html.name],
            "items_found": 0,
            "items": [],
            "metadata": {
                "project_name": None,
                "project_number": None,
                "procurement_unit": None,
                "project_budget": None,
                "announced_total_award": None,
            },
            "participants": [],
            "warnings": [],
        }), encoding="utf-8")
    (job_dir / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    (job_dir / "job.json").write_text(json.dumps({
        "status": "done",
        "ocr": False,
        "cache_database": str(root / "cache.db"),
        "limits": {
            "job_max_expanded_mb": 10,
            "job_max_member_mb": 10,
            "job_max_archive_depth": 4,
            "job_max_archive_files": 100,
            "pdf_max_pages": 10,
            "pdf_max_ocr_pages": 10,
            "ocr_engine": "rapidocr",
        },
    }), encoding="utf-8")
    return job_dir


def test_builds_isolated_worker_bundles_with_matching_prediction_ids(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "database_path", str(tmp_path / ".data" / "bidintel.db"))
    job_dir = write_job(tmp_path)
    output = tmp_path / ".data" / "annotation-tasks"

    summary = build_task_package(job_dir, output, sample_size=4, pilot_size=0, seed=9)

    canonical = json.loads((output / "predictions.canonical.json").read_text(encoding="utf-8"))
    bundle_a = json.loads((output / "annotator-a.bundle.json").read_text(encoding="utf-8"))
    bundle_b = json.loads((output / "annotator-b.bundle.json").read_text(encoding="utf-8"))
    assert len(canonical["notices"]) == 4
    assert len(bundle_a["gold"]["notices"]) == 2
    assert len(bundle_b["gold"]["notices"]) == 2
    assert set(bundle_a["gold"]["notices"][0]) == {"notice_id", "packages"}
    assert {row["notice_id"] for row in bundle_a["gold"]["notices"]}.isdisjoint(
        {row["notice_id"] for row in bundle_b["gold"]["notices"]}
    )
    assert summary["model_calls"] == 0
    assert (output / "README.txt").is_file()
    assert set(summary["ready_to_send_archives"]) == {
        "annotator-a.ready.zip", "annotator-b.ready.zip",
    }
    for bundle in (bundle_a, bundle_b):
        notice_id = bundle["gold"]["notices"][0]["notice_id"]
        source = next(row for row in bundle["sources"] if row["notice_id"] == notice_id)
        assert "Notice" in source["text"]
        assert Path(output, source["files"][0]["relative_path"]).is_file()
    with zipfile.ZipFile(output / "annotator-a.ready.zip") as archive:
        names = archive.namelist()
        assert "annotator-a.bundle.json" in names
        assert "START_HERE.txt" in names
        assert any(name.startswith("sources/annotator-a/") for name in names)
        assert not any(name.startswith("sources/annotator-b/") for name in names)

    with pytest.raises(ValueError, match="拒绝覆盖"):
        build_task_package(job_dir, output, sample_size=4, pilot_size=0, seed=9)


def test_refuses_output_outside_backend_data_and_incomplete_jobs(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "database_path", str(tmp_path / ".data" / "bidintel.db"))
    job_dir = write_job(tmp_path)
    with pytest.raises(ValueError, match="必须位于"):
        build_task_package(job_dir, tmp_path / "outside", sample_size=4, pilot_size=0)

    job = json.loads((job_dir / "job.json").read_text(encoding="utf-8"))
    job["status"] = "running"
    (job_dir / "job.json").write_text(json.dumps(job), encoding="utf-8")
    with pytest.raises(ValueError, match="尚未完成"):
        build_task_package(job_dir, tmp_path / ".data" / "another", sample_size=4, pilot_size=0)
