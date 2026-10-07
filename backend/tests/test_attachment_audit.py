import hashlib
import json
import zipfile

from scripts import attachment_audit
from scripts.attachment_audit import build_ledger


def _write_result(root, sources, warnings=(), notice="notice-1"):
    results = root / "results"
    results.mkdir(exist_ok=True)
    (results / f"{notice}.result.json").write_text(json.dumps({
        "source_files": sources, "warnings": list(warnings),
    }), encoding="utf-8")
    return results


def test_same_name_different_hash_is_reported_without_collapsing_records(tmp_path):
    corpus = tmp_path / "corpus"
    corpus.mkdir()
    source = corpus / "quote.pdf"
    source.write_bytes(b"current bytes")
    result_dir = _write_result(tmp_path, ["quote.pdf", "quote.pdf"], notice="n")
    # Duplicate paths in a legacy result still yield one stable file row.
    ledger = build_ledger(result_dir, corpus)
    assert len(ledger) == 1
    assert ledger[0]["sha256"] == hashlib.sha256(b"current bytes").hexdigest()
    assert ledger[0]["record_id"]
    assert ledger[0]["status"] == "parsed_or_partial"


def test_html_zip_member_name_mismatch_and_orphan_are_tracked(tmp_path):
    corpus = tmp_path / "corpus"
    corpus.mkdir()
    html = corpus / "notice.html"
    html.write_text('<a href="download/OfficialName.pdf">附件</a>', encoding="utf-8")
    archive = corpus / "notice.zip"
    with zipfile.ZipFile(archive, "w") as output:
        output.writestr("renamed.pdf", b"pdf bytes")
    result_dir = _write_result(tmp_path, ["notice.html", "notice.zip!/renamed.pdf", "loose.pdf"])
    ledger = build_ledger(result_dir, corpus)
    by_source = {row["source_file"]: row for row in ledger}
    assert by_source["notice.zip!/renamed.pdf"]["archive_member"] == "renamed.pdf"
    assert by_source["notice.zip!/renamed.pdf"]["status"] == "parsed_or_partial"
    assert not by_source["notice.zip!/renamed.pdf"]["html_references"]
    assert by_source["notice.zip!/renamed.pdf"]["sha256"] == hashlib.sha256(b"pdf bytes").hexdigest()
    assert "orphan" in by_source["loose.pdf"]["warning_categories"]
    assert not by_source["loose.pdf"]["html_references"]


def test_warning_statuses_do_not_call_parser_failures_success(tmp_path):
    corpus = tmp_path / "corpus"
    corpus.mkdir()
    result_dir = _write_result(tmp_path, ["broken.pdf", "download.html", "ref.docx", "future.dwg"], [
        "解析失败：broken.pdf（PdfStreamError）",
        "download.html: 源附件不可用：实际为验证码页面",
        "ref.docx: 参考采购材料，保留来源；不抽取成交标的",
        "暂不支持附件格式：future.dwg",
    ])
    ledger = build_ledger(result_dir, corpus)
    states = {row["source_file"]: row["status"] for row in ledger}
    assert states["broken.pdf"] == "program_parse_failure"
    assert states["download.html"] == "download_error_response"
    assert states["ref.docx"] == "reference_material"
    assert states["future.dwg"] == "unsupported_format"


def test_recorded_hash_mismatch_is_retained_for_review(tmp_path):
    corpus = tmp_path / "corpus"
    corpus.mkdir()
    (corpus / "quote.pdf").write_bytes(b"changed source")
    recorded = hashlib.sha256(b"original source").hexdigest()
    results = _write_result(tmp_path, ["quote.pdf"], notice="n")
    result_path = results / "n.result.json"
    payload = json.loads(result_path.read_text(encoding="utf-8"))
    payload["source_hashes"] = [{"source_file": "quote.pdf", "sha256": recorded}]
    result_path.write_text(json.dumps(payload), encoding="utf-8")
    row = build_ledger(results, corpus)[0]
    assert row["sha256"] == recorded
    assert "same_name_different_hash" in row["warning_categories"]
    assert row["manual_disposition"] == "pending"


def test_unresolved_or_oversized_members_never_receive_container_hash(tmp_path, monkeypatch):
    corpus = tmp_path / "corpus"
    corpus.mkdir()
    with zipfile.ZipFile(corpus / "notice.zip", "w") as output:
        output.writestr("large.pdf", b"12345")
    monkeypatch.setattr(attachment_audit, "MAX_MEMBER_BYTES", 4)
    sources = ["notice.zip!/large.pdf", "notice.zip!/nested.zip!/leaf.pdf"]
    results = _write_result(tmp_path, sources)
    for row in build_ledger(results, corpus):
        assert row["sha256"] is None
        assert "source_hash_unavailable" in row["warning_categories"]
        assert row["manual_disposition"] == "pending"
