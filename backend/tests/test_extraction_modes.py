import json
import sys

import pytest

from app import experiments
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


def test_report_refuses_to_overwrite_before_spending_model_calls(tmp_path, monkeypatch, capsys):
    """The guard must run first: it used to sit after the comparison, so a re-run paid
    for every model call and only then threw FileExistsError, discarding all results."""
    output = tmp_path / "report.json"
    output.write_text("previous run\n", encoding="utf-8")

    def forbidden(*args, **kwargs):
        raise AssertionError("must refuse before any model call")
    monkeypatch.setattr(experiments, "compare_extraction_modes", forbidden)
    # A manifest that does not exist: reaching the loader at all would raise
    # FileNotFoundError instead of the clean SystemExit the guard owes us.
    monkeypatch.setattr(
        sys, "argv",
        ["experiments", str(tmp_path / "missing.json"), "--output", str(output)],
    )

    with pytest.raises(SystemExit) as exit_info:
        experiments.main()

    assert exit_info.value.code == 2
    assert "已存在" in capsys.readouterr().err
    assert output.read_text(encoding="utf-8") == "previous run\n"


def test_force_swaps_in_the_report_only_once_it_is_complete(tmp_path, monkeypatch):
    output = tmp_path / "report.json"
    output.write_text("previous run\n", encoding="utf-8")
    during: dict[str, str] = {}

    def fake_compare(notices, **kwargs):
        # The previous report stays readable until the replacement is fully written.
        during["text"] = output.read_text(encoding="utf-8")
        return {"mode": "stub"}

    monkeypatch.setattr(experiments, "load_manifest_notices", lambda path, limit: [])
    monkeypatch.setattr(experiments, "compare_extraction_modes", fake_compare)
    monkeypatch.setattr(
        sys, "argv",
        ["experiments", "manifest.json", "--output", str(output), "--force"],
    )
    experiments.main()

    assert during["text"] == "previous run\n"
    assert json.loads(output.read_text(encoding="utf-8")) == {"mode": "stub"}
    assert [entry.name for entry in tmp_path.iterdir()] == ["report.json"]

