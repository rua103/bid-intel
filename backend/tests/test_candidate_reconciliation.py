import json
from itertools import permutations

import pytest

from app.candidate_reconciliation import (
    PackageAlias,
    discover_package_aliases,
    reconcile_candidates,
)
from app.schemas import ItemCandidate, NoticeMetadata


def item(source="notice.html", **changes):
    values = {
        "package_code": "1", "product_name": "打印机", "model": "PX-1", "quantity": 2,
        "unit_price": 100, "total_price": 200, "source_file": source,
        "source_location": "table:1/row:1", "source_evidence": "包号:1 打印机 PX-1 2 100 200",
    }
    values.update(changes)
    return ItemCandidate(**values)


def reconcile(rows, **context):
    context.setdefault("source_links", [("notice.html", "quote.pdf")])
    return reconcile_candidates(rows, **context)


def test_no_source_correspondence_no_merge_even_same_archive():
    rows = [item("archive.zip!/a.pdf"), item("archive.zip!/b.pdf")]
    assert len(reconcile_candidates(rows)) == 2


def test_equal_independently_extracted_project_ids_link_documents():
    rows = [item(), item("quote.pdf")]
    context = {filename: NoticeMetadata(project_number="PROJECT-17")
               for filename in ("notice.html", "quote.pdf")}
    assert len(reconcile_candidates(rows, source_metadata=context)) == 1


def test_blank_project_ids_do_not_link():
    assert len(reconcile_candidates([item(), item("quote.pdf")], source_metadata={
        "notice.html": NoticeMetadata(), "quote.pdf": NoticeMetadata(),
    })) == 2


def test_name_alone_does_not_merge():
    rows = [item(model=None, quantity=None, unit_price=None, total_price=None),
            item("quote.pdf", model=None, quantity=None, unit_price=None, total_price=None)]
    assert len(reconcile(rows)) == 2


@pytest.mark.parametrize("changes", [
    {"package_code": "2"}, {"model": "PX-2"}, {"quantity": 3},
    {"unit_price": 101}, {"total_price": 201}, {"category": "其他设备"},
    {"brand": "不同品牌"}, {"quantity_unit": "套"},
])
def test_conflicting_populated_fields_remain(changes):
    left = item(category="打印设备", brand="原品牌", quantity_unit="台")
    right = ItemCandidate.model_validate({**left.model_dump(), "source_file": "quote.pdf", **changes})
    assert len(reconcile([left, right])) == 2


def test_different_same_source_rows_are_never_collapsed():
    assert len(reconcile([item(), item(source_location="table:1/row:2")])) == 2


def test_two_identical_instances_from_each_source_preserve_multiplicity_and_warn():
    rows = [item(source, source_location=f"table:1/row:{row}")
            for source in ("notice.html", "quote.pdf") for row in (1, 2)]
    warnings = []
    assert len(reconcile(rows, warnings=warnings)) == 4
    assert any("一对一唯一" in warning for warning in warnings)


def test_missing_package_cannot_bridge_two_known_packages():
    rows = [item(), item("quote.pdf", package_code="default"),
            item("third.pdf", package_code="2", source_evidence="包号:2 打印机 PX-1 2 100 200")]
    links = [(a.source_file, b.source_file) for a, b in permutations(rows, 2)]
    assert len(reconcile(rows, source_links=links)) == 3


def test_missing_package_assigns_only_after_unique_strong_evidenced_correspondence():
    rows = [item(), item("quote.pdf", package_code="default", category="设备")]
    merged = reconcile(rows)
    assert len(merged) == 1
    assert merged[0].package_code == "1"
    assert merged[0].category == "设备"
    sources = json.loads(merged[0].source_evidence)["sources"]
    assert {source["source_file"] for source in sources} == {"notice.html", "quote.pdf"}
    assert {source["package_code"] for source in sources} == {"1", "default"}


def test_single_known_package_does_not_assign_unrelated_default():
    rows = [item(), item("quote.pdf", package_code="default", model="PX-2")]
    assert {row.package_code for row in reconcile(rows)} == {"1", "default"}


def test_package_code_without_raw_label_does_not_assign_default():
    rows = [item(source_evidence="打印机 PX-1 2 100 200"),
            item("quote.pdf", package_code="default")]
    warnings = []
    assert len(reconcile(rows, warnings=warnings)) == 2
    assert any("包号" in warning for warning in warnings)


def test_equal_claimed_known_package_still_needs_one_raw_package_evidence():
    rows = [item(source_evidence="打印机 PX-1 2 100 200"),
            item("quote.pdf", source_evidence="打印机 PX-1 2 100 200")]
    assert len(reconcile(rows)) == 2


def test_multiple_document_package_labels_do_not_assign_a_row_missing_local_evidence():
    rows = [item(source_evidence="打印机 PX-1 2 100 200"),
            item("quote.pdf", package_code="default")]
    assert len(reconcile(rows, source_texts={"notice.html": "采购包:1 采购包:2"})) == 2


def test_enumerated_document_packages_are_not_a_single_package_label():
    rows = [item(source_evidence="打印机 PX-1 2 100 200"),
            item("quote.pdf", package_code="default")]
    assert len(reconcile(rows, source_texts={"notice.html": "结果公告（采购包1、2、3）"})) == 2


def test_only_leaf_filename_package_evidence_is_used():
    rows = [item("包1.zip!/采购结果.pdf", source_evidence="打印机 PX-1 2 100 200"),
            item("quote.pdf", package_code="default")]
    assert len(reconcile(rows, source_links=[("包1.zip!/采购结果.pdf", "quote.pdf")])) == 2


def test_distinct_names_only_merge_for_evidenced_project_generic_name():
    generic = item(product_name="校园设备项目")
    detailed = item("quote.pdf")
    context = {"notice.html": NoticeMetadata(project_name="校园设备项目")}
    merged = reconcile([generic, detailed], source_metadata=context)
    assert len(merged) == 1
    assert merged[0].product_name == "校园设备项目"
    assert {source["product_name"] for source in json.loads(merged[0].source_evidence)["sources"]} == {
        "校园设备项目", "打印机",
    }
    assert len(reconcile([generic, detailed])) == 2


def test_generic_name_still_requires_model_and_quantity_not_price_only():
    rows = [item(product_name="校园设备项目", model=None), item("quote.pdf", model=None)]
    assert len(reconcile(rows, source_metadata={
        "notice.html": NoticeMetadata(project_name="校园设备项目"),
    })) == 2


def test_numbered_quoted_detail_name_replaces_generic_and_preserves_original_name():
    context = {"notice.html": NoticeMetadata(project_name="校园设备项目")}
    rows = [item(product_name="校园设备项目"),
            item("quote.pdf", source_evidence="1 打印机 PX-1 2台 单价100 总价200")]
    merged = reconcile(rows, source_metadata=context)
    assert merged[0].product_name == "打印机"
    assert json.loads(merged[0].source_evidence)["sources"][0]["product_name"] in {
        "校园设备项目", "打印机",
    }


def test_name_refinement_reorders_stably_on_the_first_pass():
    context = {"notice.html": NoticeMetadata(project_name="校园设备项目")}
    rows = [item(product_name="校园设备项目", source_location="model_text_evidence"),
            item(product_name="扫描服务", source_location="model_text_evidence"),
            item("quote.pdf", source_evidence="1 打印机 PX-1 2台 单价100 总价200")]
    merged = reconcile(rows, source_metadata=context)
    assert reconcile(merged, source_metadata=context) == merged


def test_other_documents_project_name_does_not_make_a_row_generic():
    rows = [item(product_name="校园设备项目"), item("quote.pdf")]
    assert len(reconcile(rows, source_metadata={
        "third.pdf": NoticeMetadata(project_name="校园设备项目"),
    })) == 2


@pytest.mark.parametrize("model,brand", [("定制", None), ("自制", None), ("品牌", "品牌")])
def test_generic_models_do_not_identify_a_specific_product(model, brand):
    rows = [item(product_name="校园设备项目", model=model, brand=brand),
            item("quote.pdf", model=model, brand=brand)]
    assert len(reconcile(rows, source_metadata={
        "notice.html": NoticeMetadata(project_name="校园设备项目"),
    })) == 2


def test_unrelated_product_names_never_merge_even_equal_numbers():
    assert len(reconcile([item(), item("quote.pdf", product_name="扫描仪")])) == 2


def test_explicit_filename_reference_supports_source_correspondence():
    assert len(reconcile_candidates([item(), item("quote.pdf")],
                                   source_texts={"notice.html": "详细结果见附件quote.pdf"})) == 1


def test_suffix_only_is_not_package_alias():
    assert len(reconcile([item(package_code="A"),
                          item("quote.pdf", package_code="LONG-A")])) == 2


def test_explicit_parenthetical_alias_merges_and_retains_original_codes():
    texts = {"quote.pdf": "包号:A（采购包编号:LONG-A） 报价明细表"}
    rows = [item(package_code="A"), item("quote.pdf", package_code="LONG-A")]
    merged = reconcile(rows, source_texts=texts)
    assert len(merged) == 1
    assert merged[0].package_code == "A"
    evidence = json.loads(merged[0].source_evidence)
    assert evidence["source_context"]["quote.pdf"]["package_aliases"]
    assert {source["package_code"] for source in evidence["sources"]} == {"A", "LONG-A"}


def test_unverified_or_cooccurring_alias_is_rejected():
    rows = [item(package_code="A"), item("quote.pdf", package_code="LONG-A")]
    alias = PackageAlias("quote.pdf", "A", "LONG-A", "包号:A（LONG-A）")
    assert len(reconcile(rows, package_aliases=[alias])) == 2
    assert not discover_package_aliases({"quote.pdf": "包号:A 项目编号:LONG-A"})


def test_ambiguous_aliases_preserve_original_packages():
    rows = [item(package_code="A"), item("quote.pdf", package_code="LONG-A")]
    texts = {"quote.pdf": "包号:A（LONG-A） 包号:B（LONG-A）"}
    warnings = []
    assert len(reconcile(rows, source_texts=texts, warnings=warnings)) == 2
    assert any("别名对应歧义" in warning for warning in warnings)


def test_order_invariant_and_idempotent_with_all_original_sources_preserved():
    rows = [item(), item("quote.pdf"), item("third.pdf", brand="品牌")]
    links = [(a.source_file, b.source_file) for a, b in permutations(rows, 2)]
    expected = reconcile(rows, source_links=links)
    for permutation in permutations(rows):
        assert reconcile(permutation, source_links=links) == expected
    assert reconcile(expected, source_links=links) == expected
    assert len(json.loads(expected[0].source_evidence)["sources"]) == 3


def test_second_pass_never_consumes_a_different_original_row_of_merged_file():
    merged = reconcile([item(), item("quote.pdf")])
    another = item("quote.pdf", source_location="table:1/row:2")
    assert len(reconcile([*merged, another])) == 2


def test_previously_unseen_third_source_preserves_existing_provenance_on_merge():
    merged = reconcile([item(), item("quote.pdf")])
    links = [("notice.html", "third.pdf"), ("quote.pdf", "third.pdf")]
    rows = reconcile([*merged, item("third.pdf")], source_links=links)
    assert len(rows) == 1
    assert len(json.loads(rows[0].source_evidence)["sources"]) == 3
    assert ["notice.html", "quote.pdf"] in json.loads(rows[0].source_evidence)["source_links"]
