import hashlib
from pathlib import Path

from app.schemas import ImportResult, ItemCandidate, NoticeMetadata, ParticipantCandidate
from app.storage import (
    SCHEMA,
    connect,
    count_notices,
    get_notice_detail,
    initialize,
    save_import,
    search_items,
)


def test_initialize_upgrades_legacy_evidence_columns(tmp_path: Path):
    database = tmp_path / "legacy.db"
    with connect(database) as connection:
        connection.executescript(SCHEMA.replace("    source_evidence TEXT,\n", ""))

    initialize(database)
    initialize(database)

    with connect(database) as connection:
        for table in ("bid_participations", "awards", "procurement_items"):
            columns = {
                row["name"] for row in connection.execute(f"PRAGMA table_info({table})")
            }
            assert "source_evidence" in columns


def test_import_save_and_search(tmp_path: Path):
    database = tmp_path / "sample.db"
    result = ImportResult(
        notice_id=0,
        source_files=["notice.html"],
        items_found=1,
        metadata=NoticeMetadata(project_name="终端采购项目", procurement_unit="某医院"),
        participants=[
            ParticipantCandidate(
                organization_name="中标供应商甲",
                outcome="winner",
                award_amount=120000,
                source_file="notice.html",
                source_location="正文第 3 段",
                source_evidence="中标供应商甲以 120000 元中标",
            ),
            ParticipantCandidate(
                organization_name="投标供应商乙",
                outcome="nonwinner",
                source_file="notice.html",
                source_location="评分表第 2 行",
                source_evidence="投标供应商乙",
            ),
        ],
        items=[
            ItemCandidate(
                product_name="自助终端",
                category="信息化设备",
                brand="长城医疗",
                model="MBS200K-3",
                source_file="notice.html",
                source_location="table:2/row:3",
                source_evidence="自助终端 | MBS200K-3",
            )
        ],
    )
    saved = save_import(database, result)
    assert saved.notice_id == 1
    assert count_notices(database) == 1
    rows = search_items(database, brand="长城")
    assert rows[0]["project_name"] == "终端采购项目"
    assert rows[0]["source_evidence"] == "自助终端 | MBS200K-3"
    from app.analytics import buyer_awardees

    awardees = buyer_awardees(database, 1)
    assert awardees["awardees"][0]["name"] == "中标供应商甲"
    assert awardees["awardees"][0]["award_amount_total"] == "120000"


def test_notice_detail_returns_saved_evidence_hashes_and_explicit_capability_gaps(tmp_path: Path):
    database = tmp_path / "detail.db"
    raw_source = b"<html>source bytes</html>"
    saved = save_import(
        database,
        ImportResult(
            notice_id=0,
            source_files=["notice.html", "attachment.pdf", "historical.pdf"],
            items_found=1,
            metadata=NoticeMetadata(project_name="示例项目", procurement_unit="采购中心"),
            items=[ItemCandidate(
                package_code="1", product_name="打印机", source_file="attachment.pdf",
                source_location="page:2/table:1/row:3", source_evidence="打印机 | 型号 X",
            )],
            participants=[
                ParticipantCandidate(
                    package_code="1", organization_name="中标供应商", outcome="winner",
                    award_amount=100, source_file="notice.html", source_location="table:2/row:3",
                    source_evidence="中标供应商 | 100 元",
                ),
                ParticipantCandidate(
                    package_code="1", organization_name="投标供应商", outcome="nonwinner",
                    source_file="notice.html", source_location="table:2/row:4",
                ),
            ],
            warnings=["PDF 第 2 页 OCR 提示"],
        ),
        source_documents=[("notice.html", raw_source)],
        source_hashes=[
            ("attachment.pdf", hashlib.sha256(b"one").hexdigest(), 3),
            ("attachment.pdf", hashlib.sha256(b"two").hexdigest(), 3),
        ],
    )

    detail = get_notice_detail(database, saved.notice_id)

    assert detail is not None
    assert detail["metadata"]["procurement_unit"] == "采购中心"
    assert detail["metadata_evidence_status"] == "missing"
    assert detail["warnings"] == ["PDF 第 2 页 OCR 提示"]
    assert detail["items"][0]["package_code"] == "1"
    assert detail["items"][0]["source_location"] == "page:2/table:1/row:3"
    assert detail["items"][0]["evidence_status"] == "available"
    assert detail["items"][0]["source_hash_status"] == "ambiguous"
    assert detail["items"][0]["source_sha256"] is None
    assert detail["bidders"][0]["evidence_status"] == "available"
    assert detail["bidders"][1]["evidence_status"] == "missing"
    assert detail["awards"][0]["organization_name"] == "中标供应商"
    assert detail["awards"][0]["source_evidence"] == "中标供应商 | 100 元"
    attachments = [row for row in detail["source_files"] if row["source_file"] == "attachment.pdf"]
    assert {row["sha256"] for row in attachments} == {
        hashlib.sha256(b"one").hexdigest(), hashlib.sha256(b"two").hexdigest(),
    }
    historical = next(row for row in detail["source_files"] if row["source_file"] == "historical.pdf")
    assert historical["sha256"] is None
    assert historical["hash_status"] == "not_saved"
    notice_source = next(row for row in detail["source_files"] if row["source_file"] == "notice.html")
    assert notice_source["sha256"] == hashlib.sha256(raw_source).hexdigest()
    assert notice_source["file_available"] is False
    assert detail["capabilities"]["explicit_empty_status"] == "not_saved"
