from app.config import Settings
from app.ingestion import _deduplicate_participants, extract_notice
from app.parsers import SourceDocument
from app.schemas import ImportResult, ItemCandidate, NoticeMetadata, ParticipantCandidate
from app.storage import connect, save_import


def test_rules_never_calls_configured_model(monkeypatch):
    def forbidden(**kwargs):
        raise AssertionError("rules must not call model")
    monkeypatch.setattr("app.ingestion.extract_unstructured_items", forbidden)
    result = extract_notice([SourceDocument("a.txt", b"hello")], extraction_mode="rules",
                            model_settings=Settings(model_base_url="https://test.invalid",
                                                    model_api_key="test", model_name="deepseek-test"))
    assert result.items == []


def test_multiple_packages_and_consortium_do_not_duplicate_amounts(tmp_path):
    participants = [ParticipantCandidate(
        package_code=code, organization_name="甲乙联合体", consortium_members=["甲公司", "乙公司"],
        outcome="winner", award_amount=amount, source_file="a", source_location=code,
    ) for code, amount in [("包1", 100), ("包2", 200)]]
    assert len(_deduplicate_participants(participants, [])) == 2
    items = [ItemCandidate(package_code=code, product_name="电脑", source_file="a", source_location=code)
             for code in ["包1", "包2"]]
    db = tmp_path / "packages.db"
    save_import(db, ImportResult(notice_id=0, source_files=["a"], items_found=2, items=items,
                                 participants=participants,
                                 metadata=NoticeMetadata(announced_total_award=300)))
    with connect(db) as conn:
        assert conn.execute("SELECT count(*) FROM packages").fetchone()[0] == 2
        assert conn.execute("SELECT count(*) FROM awards").fetchone()[0] == 2
        assert conn.execute("SELECT sum(cast(award_amount as real)) FROM awards").fetchone()[0] == 300
        assert conn.execute("SELECT count(*) FROM organizations").fetchone()[0] == 1
        assert conn.execute("SELECT count(*) FROM packages WHERE package_award_total IS NULL").fetchone()[0] == 2

