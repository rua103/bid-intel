import json
from copy import deepcopy
from pathlib import Path

import pytest

from app.annotation_merge_cli import main


def gold_dataset(notice_id: str, *, package_id: str = "default", annotated: bool = True) -> dict:
    package = {
        "package_id": package_id,
        "items": [],
        "buyer": {"entity_id": "buyer-1", "name": "采购单位"} if annotated else None,
        "winners": [],
        "bidders": [],
    }
    return {
        "schema_version": "1.0",
        "status": "reviewed",
        "notices": [{"notice_id": notice_id, "packages": [package]}],
    }


def predictions_for(*gold_values: dict) -> dict:
    value = {
        "schema_version": "1.0",
        "status": "predicted",
        "notices": [],
    }
    for gold in gold_values:
        notice = deepcopy(gold["notices"][0])
        for package in notice["packages"]:
            package["buyer"] = None
        value["notices"].append(notice)
    return value


def write_json(path: Path, value: dict) -> Path:
    path.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")
    return path


def cli_args(tmp_path: Path, *gold_paths: Path, predictions: Path) -> list[str]:
    return [
        "--gold-files",
        *(str(path) for path in gold_paths),
        "--predictions",
        str(predictions),
        "--output",
        str(tmp_path / "gold.merged.json"),
    ]


def test_merges_reviewed_gold_and_checks_against_canonical_predictions(tmp_path, capsys):
    first = gold_dataset("notice-b")
    second = gold_dataset("notice-a")
    part_b = write_json(tmp_path / "part-b.json", first)
    part_a = write_json(tmp_path / "part-a.json", second)
    canonical = write_json(
        tmp_path / "predictions.json", predictions_for(first, second)
    )
    output = tmp_path / "gold.merged.json"

    assert main(cli_args(tmp_path, part_b, part_a, predictions=canonical)) == 0

    merged = json.loads(output.read_text(encoding="utf-8"))
    assert merged["status"] == "reviewed"
    assert [row["notice_id"] for row in merged["notices"]] == ["notice-a", "notice-b"]
    assert "Validated Gold notice IDs" in capsys.readouterr().out


def test_duplicate_notices_fail_and_optional_report_never_selects_a_source(tmp_path, capsys):
    first = write_json(tmp_path / "worker-a.json", gold_dataset("same-notice"))
    second = write_json(tmp_path / "worker-b.json", gold_dataset("same-notice"))
    canonical = write_json(
        tmp_path / "predictions.json", predictions_for(gold_dataset("same-notice"))
    )
    output = tmp_path / "gold.merged.json"
    report = tmp_path / "conflicts.json"
    args = cli_args(tmp_path, first, second, predictions=canonical)
    args.extend(["--conflicts-report", str(report)])

    assert main(args) == 2
    error = capsys.readouterr().err
    assert "same-notice" in error
    assert str(first) in error and str(second) in error
    assert not output.exists()
    assert json.loads(report.read_text(encoding="utf-8")) == {
        "conflicts": [
            {
                "notice_id": "same-notice",
                "sources": sorted([str(first.resolve()), str(second.resolve())]),
                "resolution": "unresolved; no source selected",
            }
        ]
    }


@pytest.mark.parametrize(
    ("gold_values", "prediction_values", "message"),
    [
        (
            [gold_dataset("notice-a")],
            [gold_dataset("notice-b")],
            "缺少 gold 公告",
        ),
    ],
)
def test_gold_notice_absent_from_predictions_fails_without_output(
    tmp_path, capsys, gold_values, prediction_values, message
):
    gold_path = write_json(tmp_path / "gold.json", gold_values[0])
    canonical = write_json(
        tmp_path / "predictions.json", predictions_for(*prediction_values)
    )
    output = tmp_path / "gold.merged.json"

    assert main(cli_args(tmp_path, gold_path, predictions=canonical)) == 2
    assert message in capsys.readouterr().err
    assert not output.exists()


def test_package_id_differences_are_preserved_and_reported(tmp_path, capsys):
    gold = gold_dataset("notice-a", package_id="source-package")
    prediction = gold_dataset("notice-a", package_id="system-package")
    gold_path = write_json(tmp_path / "gold.json", gold)
    canonical = write_json(tmp_path / "predictions.json", predictions_for(prediction))

    assert main(cli_args(tmp_path, gold_path, predictions=canonical)) == 0

    merged = json.loads((tmp_path / "gold.merged.json").read_text(encoding="utf-8"))
    assert merged["notices"][0]["packages"][0]["package_id"] == "source-package"
    output = capsys.readouterr().out
    assert "Package structure differs in 1 notice" in output
    assert "Gold 独有=['source-package']" in output
    assert "预测独有=['system-package']" in output


@pytest.mark.parametrize(
    "mutate",
    [
        lambda value: value.update(status="draft"),
        lambda value: value.update(status="predicted"),
        lambda value: value.update(schema_version="2.0"),
        lambda value: value.update(extra="unexpected"),
    ],
)
def test_invalid_or_unreviewed_gold_is_rejected(tmp_path, capsys, mutate):
    gold = gold_dataset("notice-a")
    mutate(gold)
    gold_path = write_json(tmp_path / "gold.json", gold)
    canonical = write_json(
        tmp_path / "predictions.json", predictions_for(gold_dataset("notice-a"))
    )

    assert main(cli_args(tmp_path, gold_path, predictions=canonical)) == 2
    assert "Gold" in capsys.readouterr().err
    assert not (tmp_path / "gold.merged.json").exists()


def test_empty_gold_is_rejected_and_existing_output_is_never_truncated(tmp_path, capsys):
    empty_gold = write_json(
        tmp_path / "empty.json", gold_dataset("notice-a", annotated=False)
    )
    canonical = write_json(
        tmp_path / "predictions.json", predictions_for(gold_dataset("notice-a"))
    )
    existing_output = tmp_path / "gold.merged.json"
    existing_output.write_text("preserve me", encoding="utf-8")

    assert main(cli_args(tmp_path, empty_gold, predictions=canonical)) == 2
    error = capsys.readouterr().err
    assert "没有任何人工标注记录" in error
    assert existing_output.read_text(encoding="utf-8") == "preserve me"


def test_prediction_file_must_have_predicted_status(tmp_path, capsys):
    gold_path = write_json(tmp_path / "gold.json", gold_dataset("notice-a"))
    invalid_predictions = predictions_for(gold_dataset("notice-a"))
    invalid_predictions["status"] = "reviewed"
    canonical = write_json(tmp_path / "predictions.json", invalid_predictions)

    assert main(cli_args(tmp_path, gold_path, predictions=canonical)) == 2
    assert "预测 JSON 无效" in capsys.readouterr().err
    assert not (tmp_path / "gold.merged.json").exists()


def test_worker_subset_can_be_checked_against_full_canonical_predictions(tmp_path, capsys):
    worker_gold = write_json(tmp_path / "worker.json", gold_dataset("notice-a"))
    canonical = write_json(
        tmp_path / "predictions.json",
        predictions_for(gold_dataset("notice-a"), gold_dataset("notice-b")),
    )

    assert main(cli_args(tmp_path, worker_gold, predictions=canonical)) == 0
    assert (tmp_path / "gold.merged.json").is_file()

    complete_args = cli_args(tmp_path, worker_gold, predictions=canonical)
    complete_args.extend(["--output", str(tmp_path / "complete.json"), "--require-complete"])
    assert main(complete_args) == 2
    assert "多出公告" in capsys.readouterr().err
    assert not (tmp_path / "complete.json").exists()
