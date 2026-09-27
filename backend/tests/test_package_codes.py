import pytest

from app.package_codes import normalize_package_code
from app.schemas import ModelItemPayload, ModelParticipantPayload


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        (None, "default"),
        ("", "default"),
        ("default", "default"),
        ("unknown", "default"),
        ("合同包1", "1"),
        ("合同包1(项目名称)", "1"),
        ("包号：01", "1"),
        ("包1", "1"),
        ("A包", "A"),
        ("A标段", "A"),
        ("第1包", "1"),
        ("第一包", "1"),
        ("1号包", "1"),
        ("合同包十一", "11"),
        ("A-02", "A-02"),
        ("包名：二", "2"),
        ("标段名称：第一标段", "1"),
        ("豫政采(2)20260817-2", "2"),
        ("豫政采(2)20260817-5", "5"),
        ("分包名称：", "default"),
        ("包名：见附件", "default"),
        ("包名：详见招标文件", "default"),
    ],
)
def test_normalize_package_code_has_one_canonical_form(raw, expected):
    assert normalize_package_code(raw) == expected


def test_model_payloads_normalize_before_hybrid_merge():
    item = ModelItemPayload(package_code="合同包1", product_name="设备", source_evidence="设备")
    participant = ModelParticipantPayload(
        package_code="包号：01", organization_name="供应商", source_evidence="供应商",
    )
    assert item.package_code == "1"
    assert participant.package_code == "1"
