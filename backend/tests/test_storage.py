from pathlib import Path

from app.schemas import ImportResult, ItemCandidate, NoticeMetadata, ParticipantCandidate
from app.storage import SCHEMA, connect, count_notices, initialize, save_import, search_items


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
