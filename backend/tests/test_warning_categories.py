import pytest

from app.warning_categories import WarningCategory, classify_warning


@pytest.mark.parametrize(("warning", "expected"), [
    ("a.pdf: 源附件不可用：实际是验证码下载页", WarningCategory.SOURCE_UNAVAILABLE),
    ("a.zip: ZIP/OOXML 内容损坏，无法读取", WarningCategory.SOURCE_CORRUPT),
    ("a.rar: 附件容器展开失败（BadRarFile）", WarningCategory.ARCHIVE_FAILURE),
    ("a.rar!/quote.pdf: 压缩成员读取失败（BadRarFile）", WarningCategory.ARCHIVE_MEMBER_FAILURE),
    # A parser exception alone cannot tell us whether the bytes are damaged.
    ("a.pdf: 解析失败：a.pdf（PdfStreamError）", WarningCategory.PARSER_FAILURE),
    ("a.dwg: 暂不支持附件格式", WarningCategory.UNSUPPORTED_FORMAT),
    ("a.pdf: OCR 未识别到可信文本", WarningCategory.OCR_NO_TEXT),
    ("a.png: RapidOCR 未安装，请安装后端 ocr 依赖", WarningCategory.OCR_FAILURE),
    ("a.pdf: 扫描页无可提取文本，OCR 未启用", WarningCategory.OCR_SKIPPED),
    ("a.png: 本地 RapidOCR 已识别，文本和表格需人工核验", WarningCategory.OCR_REVIEW),
    ("a.pdf: PDF 表格解析失败（TypeError），保留文本结果", WarningCategory.PARTIAL_FALLBACK),
    ("a.docx: 实际为 DOC，已按内容解析", WarningCategory.FORMAT_MISMATCH),
    ("a.docx: 参考采购材料，保留来源；不抽取成交标的", WarningCategory.ROLE_SKIPPED),
])
def test_warning_categories_keep_source_damage_and_parser_failures_separate(warning, expected):
    assert classify_warning(warning) == expected
