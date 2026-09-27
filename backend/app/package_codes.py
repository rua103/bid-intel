"""Deterministic package-id extraction and normalization."""

from __future__ import annotations

import re
import unicodedata

DEFAULT_PACKAGE_CODE = "default"
_PACKAGE_ID = r"[A-Za-z0-9一二三四五六七八九十百千万零〇]+(?:[-_/][A-Za-z0-9一二三四五六七八九十百千万零〇]+)*"
_LABEL_PREFIX = r"(?:第\s*)?(?:合同|采购|标包|标段)?\s*包"
_LABEL_SUFFIX = r"(?:标包|标段|包)"
_PREFIX_RE = re.compile(
    rf"^{_LABEL_PREFIX}\s*(?:编号|号)?\s*[:：]?\s*({_PACKAGE_ID})", re.IGNORECASE,
)
_ORDINAL_RE = re.compile(rf"^第\s*({_PACKAGE_ID})\s*号?\s*包", re.IGNORECASE)
_REVERSE_RE = re.compile(
    rf"^({_PACKAGE_ID})\s*(?:号\s*)?{_LABEL_SUFFIX}", re.IGNORECASE,
)
_NAMED_RE = re.compile(rf"^包名\s*[:：]?\s*({_PACKAGE_ID})", re.IGNORECASE)
_NAMED_VALUE_RE = re.compile(
    r"^(?:分包名称|标段名称|采购包名称|包名称|包名)\s*[:：]\s*(.+)$",
    re.IGNORECASE,
)
_ORDINAL_SECTION_RE = re.compile(rf"^第\s*({_PACKAGE_ID})\s*标段$", re.IGNORECASE)
_TENDER_SUFFIX_RE = re.compile(
    rf"^.+[（(]\s*{_PACKAGE_ID}\s*[）)][^\s()（）]*[-_/]({_PACKAGE_ID})$",
    re.IGNORECASE,
)
_EMPTY_LABELS = {
    "包名", "分包名称", "标段名称", "采购包名称", "包名称", "包号", "标包号", "标段号",
}
_PLACEHOLDER_RE = re.compile(r"^(?:详?见(?:附件|招标文件|采购文件|文件)|附件)$")

_SIMPLE_CHINESE_NUMBERS = {
    "零": 0,
    "〇": 0,
    "一": 1,
    "二": 2,
    "两": 2,
    "三": 3,
    "四": 4,
    "五": 5,
    "六": 6,
    "七": 7,
    "八": 8,
    "九": 9,
}


def _chinese_number(value: str) -> str:
    if value.isdigit():
        return str(int(value))
    if value in _SIMPLE_CHINESE_NUMBERS:
        return str(_SIMPLE_CHINESE_NUMBERS[value])
    if not value or any(char not in "零〇一二两三四五六七八九十百千万" for char in value):
        return value
    units = {"十": 10, "百": 100, "千": 1000, "万": 10000}
    total = 0
    section = 0
    digit = 0
    for char in value:
        if char in _SIMPLE_CHINESE_NUMBERS:
            digit = _SIMPLE_CHINESE_NUMBERS[char]
            continue
        unit = units[char]
        if unit >= 10_000:
            section += digit
            total += section * unit
            section = 0
            digit = 0
        else:
            section += (digit or 1) * unit
            digit = 0
    return str(total + section + digit)


def _clean(value: str) -> str:
    return " ".join(unicodedata.normalize("NFKC", value).split()).strip(
        " ：:，,。;；()（）[]【】"
    )


def normalize_package_code(value: str | None) -> str:
    """Return one stable id for a package label.

    The function only strips an explicit package wrapper. Unknown non-empty
    identifiers are preserved, while empty/default placeholders remain
    ``default``. No semantic inference or model call belongs here.
    """
    text = _clean(value or "")
    if not text or text.casefold() in {"default", "unknown", "none", "null"}:
        return DEFAULT_PACKAGE_CODE
    if text in _EMPTY_LABELS or _PLACEHOLDER_RE.fullmatch(text):
        return DEFAULT_PACKAGE_CODE

    for pattern in (_NAMED_RE, _ORDINAL_RE, _PREFIX_RE, _REVERSE_RE):
        match = pattern.match(text)
        if match:
            return _chinese_number(match.group(1))

    # A named package is still a package label; strip only the explicit field
    # wrapper. Empty/placeholder values must not become package IDs.
    named_value = _NAMED_VALUE_RE.match(text)
    if named_value:
        candidate = named_value.group(1).strip()
        if not candidate or _PLACEHOLDER_RE.fullmatch(candidate):
            return DEFAULT_PACKAGE_CODE
        ordinal = _ORDINAL_SECTION_RE.fullmatch(candidate)
        if ordinal:
            return _chinese_number(ordinal.group(1))
        return candidate

    # Several provincial tender systems append the package number to an
    # identifier such as ``豫政采(2)20260817-2``. The trailing component is
    # the package, while the parenthesized component belongs to the project.
    match = _TENDER_SUFFIX_RE.match(text)
    if match:
        return _chinese_number(match.group(1))

    # A bare package id is already canonical. Keep separators used by real
    # tender systems, but do not treat arbitrary prose as a package number.
    if re.fullmatch(_PACKAGE_ID, text, re.IGNORECASE):
        return _chinese_number(text)
    return text
