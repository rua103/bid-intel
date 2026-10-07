import json

from app.config import Settings
from app.model_adapter import _brand_is_supplier_labelled, extract_unstructured_items
from app.parsers import parse_item_tables


def test_product_supplier_is_not_brand_and_h3c_brand_is_preserved():
    rows = [
        ["包号", "采购标的", "品牌", "产品供应商", "规格型号", "数量"],
        ["包1", "交换机", "H3C", "中移建设有限公司", "S5130", "2台"],
    ]
    item = parse_item_tables(rows, source_file="notice.xlsx", table_index=1)[0]
    assert item.brand == "H3C"
    assert "中移建设有限公司" in item.source_evidence


def test_supplier_only_table_keeps_brand_empty_and_source_evidence():
    rows = [
        ["包号", "采购标的", "产品供应商", "规格型号", "数量"],
        ["包1", "交换机", "中移建设有限公司", "S5130", "2台"],
    ]
    item = parse_item_tables(rows, source_file="notice.xlsx", table_index=1)[0]
    assert item.brand is None
    assert "中移建设有限公司" in item.source_evidence
    assert item.extraction_method.endswith("supplier_column_unmapped")


def test_brand_and_winner_same_name_remain_semantically_distinct():
    rows = [
        ["采购标的", "品牌名称", "供应商名称", "规格型号"],
        ["服务器", "联想", "联想（北京）有限公司", "SR650"],
    ]
    item = parse_item_tables(rows, source_file="notice.xlsx", table_index=1)[0]
    assert item.brand == "联想"
    assert "联想（北京）有限公司" in item.source_evidence


def test_multi_package_multi_company_rows_keep_brand_and_supplier_separate():
    rows = [
        ["采购包编号", "采购标的", "制造商品牌", "产品供应商", "规格型号", "数量"],
        ["包1", "交换机", "H3C", "中移建设有限公司", "S5130", "2台"],
        ["包2", "路由器", "华为", "某某系统集成有限公司", "AR6300", "1台"],
    ]
    items = parse_item_tables(rows, source_file="notice.xlsx", table_index=1)
    assert [(item.package_code, item.brand) for item in items] == [
        ("1", "H3C"), ("2", "华为"),
    ]
    assert all("有限公司" in item.source_evidence for item in items)


def test_model_route_does_not_accept_supplier_as_brand(stub_model_stream):
    source = "包1 交换机 品牌为空 产品供应商：中移建设有限公司 规格型号S5130"
    stub_model_stream(json.dumps({
        "metadata": {}, "participants": [], "items": [{
            "product_name": "交换机", "brand": "中移建设有限公司", "model": "S5130",
            "source_evidence": "交换机 产品供应商：中移建设有限公司 规格型号S5130",
        }],
    }, ensure_ascii=False))
    settings = Settings(model_base_url="https://model.example/v1", model_api_key="key", model_name="qwen-test")
    _, items, _, warnings = extract_unstructured_items(
        filename="notice.html", text=source, settings=settings,
    )
    assert items == [] or items[0].brand is None
    assert items == [] or any("产品供应商" in warning for warning in warnings)


def test_model_supplier_guard_preserves_brand_prefix_and_explicit_brand():
    assert not _brand_is_supplier_labelled("供应商名称：联想（北京）有限公司；品牌：联想", "联想")
    assert not _brand_is_supplier_labelled("产品供应商：联想（北京）有限公司", "联想")
    assert not _brand_is_supplier_labelled("供应商：华为；品牌：华为", "华为")
    assert _brand_is_supplier_labelled("产品供应商：中移建设有限公司；型号：S5130", "中移建设有限公司")
