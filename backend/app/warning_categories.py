"""Stable coarse categories for the parser's user-facing warning strings.

The wording stays readable in import results; this module gives reports and tests a
consistent way to distinguish source damage from parser limitations. In particular,
an exception raised by a parser is not proof that the original file is unrecoverable.
"""
from __future__ import annotations

from enum import StrEnum


class WarningCategory(StrEnum):
    SOURCE_UNAVAILABLE = "source_unavailable"
    SOURCE_CORRUPT = "source_corrupt"
    ARCHIVE_FAILURE = "archive_failure"
    ARCHIVE_MEMBER_FAILURE = "archive_member_failure"
    PARSER_FAILURE = "parser_failure"
    UNSUPPORTED_FORMAT = "unsupported_format"
    OCR_NO_TEXT = "ocr_no_text"
    OCR_FAILURE = "ocr_failure"
    OCR_SKIPPED = "ocr_skipped"
    OCR_REVIEW = "ocr_review"
    PARTIAL_FALLBACK = "partial_fallback"
    FORMAT_MISMATCH = "format_mismatch"
    ROLE_SKIPPED = "role_skipped"
    OTHER = "other"


def classify_warning(warning: str) -> WarningCategory:
    """Classify one existing warning without inferring more than its evidence says."""
    value = warning.casefold()

    if "源附件不可用" in warning:
        return WarningCategory.SOURCE_UNAVAILABLE
    if any(marker in value for marker in (
        "gzip 内容损坏", "zip/ooxml 内容损坏", "ole 文档损坏", "压缩包损坏或格式无效",
    )):
        return WarningCategory.SOURCE_CORRUPT
    if "附件容器展开失败" in warning or "压缩包没有可读取成员" in warning:
        return WarningCategory.ARCHIVE_FAILURE
    if "压缩成员读取失败" in warning:
        return WarningCategory.ARCHIVE_MEMBER_FAILURE
    if "暂不支持附件格式" in warning:
        return WarningCategory.UNSUPPORTED_FORMAT
    if "解析失败：" in warning or "旧版 word 转换失败" in value:
        return WarningCategory.PARSER_FAILURE
    if "pdf pypdf 流解析失败" in value and (
        "备用解析也失败" in warning or "备用解析失败" in warning
    ):
        return WarningCategory.PARSER_FAILURE
    if "未识别到可信文本" in warning:
        return WarningCategory.OCR_NO_TEXT
    if "ocr" in value and any(marker in warning for marker in (
        "失败", "超时", "未安装", "缺少", "语言参数无效",
    )):
        return WarningCategory.OCR_FAILURE
    if "ocr" in value and any(marker in value for marker in (
        "未启用", "已启用但未找到", "每份 pdf 最多 ocr",
    )):
        return WarningCategory.OCR_SKIPPED
    if "rapidocr 已识别" in value or "本地 rapidocr 已识别" in value:
        return WarningCategory.OCR_REVIEW
    if any(marker in value for marker in (
        "pdf 表格解析失败", "pdfplumber 备用解析成功", "pypdf 文本读取失败",
    )) or "仅提取文本" in warning:
        return WarningCategory.PARTIAL_FALLBACK
    if "实际为" in warning or "扩展名" in warning and "按内容" in warning:
        return WarningCategory.FORMAT_MISMATCH
    if "参考采购材料" in warning or "保留来源" in warning and "不抽取" in warning:
        return WarningCategory.ROLE_SKIPPED
    return WarningCategory.OTHER
