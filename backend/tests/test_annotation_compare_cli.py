from copy import deepcopy

import pytest

from app.annotation_compare_cli import compare_pilot_files


def pilot_dataset(*, brand="Acme", notice_id="notice-a"):
    return {
        "schema_version": "1.0",
        "status": "reviewed",
        "notices": [{
            "notice_id": notice_id,
            "packages": [{
                "package_id": "default",
                "items": [{
                    "item_id": "item-1",
                    "product_name": "服务器",
                    "category": "设备",
                    "brand": brand,
                    "model": "X1",
                    "quantity": 2,
                    "unit_price": 100,
                    "total_price": 200,
                }],
                "buyer": {"entity_id": "buyer-1", "name": "采购单位"},
                "winners": [],
                "bidders": [],
            }],
        }],
    }


def write_json(path, value):
    path.write_text(__import__("json").dumps(value, ensure_ascii=False), encoding="utf-8")
    return path


def test_pilot_comparison_ignores_ids_but_lists_real_field_disagreements(tmp_path):
    left = pilot_dataset()
    right = deepcopy(left)
    right["notices"][0]["packages"][0]["items"][0]["item_id"] = "another-local-id"
    same_a = write_json(tmp_path / "same-a.json", left)
    same_b = write_json(tmp_path / "same-b.json", right)

    same = compare_pilot_files(same_a, same_b)
    assert same["disagreement_count"] == 0
    assert same["agreement_metrics"]["field_micro"]["accuracy"] == 1

    different = write_json(tmp_path / "different.json", pilot_dataset(brand="Other"))
    comparison = compare_pilot_files(same_a, different)
    assert comparison["disagreement_count"] == 1
    item_difference = next(
        row for row in comparison["disagreements"][0]["differences"] if row["role"] == "items"
    )
    assert item_difference["annotator_a"][0]["brand"] == "Acme"
    assert item_difference["annotator_b"][0]["brand"] == "Other"


@pytest.mark.parametrize(
    ("mutate", "message"),
    [
        (lambda value: value.update(status="draft"), "reviewed 状态"),
        (lambda value: value["notices"][0].update(notice_id="notice-b"), "公告 ID 不一致"),
    ],
)
def test_pilot_comparison_rejects_unreviewed_or_misaligned_sets(tmp_path, mutate, message):
    first = write_json(tmp_path / "a.json", pilot_dataset())
    altered = pilot_dataset()
    mutate(altered)
    second = write_json(tmp_path / "b.json", altered)
    with pytest.raises(ValueError, match=message):
        compare_pilot_files(first, second)


def test_package_id_differences_are_reported_instead_of_blocking_pilot_review(tmp_path):
    left = pilot_dataset()
    right = deepcopy(left)
    right["notices"][0]["packages"][0]["package_id"] = "package-from-source"
    first = write_json(tmp_path / "a.json", left)
    second = write_json(tmp_path / "b.json", right)

    report = compare_pilot_files(first, second)

    assert report["disagreement_count"] == 2
    presence = [
        row for row in report["disagreements"]
        if row["differences"][0]["role"] == "package_presence"
    ]
    assert len(presence) == 2
    assert {(row["differences"][0]["annotator_a"], row["differences"][0]["annotator_b"]) for row in presence} == {
        (True, False), (False, True),
    }
    assert "按原文分别报告" in report["disclaimer"]
