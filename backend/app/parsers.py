from __future__ import annotations

import io
import re
import zipfile
from collections.abc import Iterable
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from pathlib import PurePosixPath

from bs4 import BeautifulSoup
from docx import Document
from openpyxl import load_workbook
from pypdf import PdfReader

from app.schemas import ItemCandidate, NoticeMetadata

MAX_ARCHIVE_DEPTH = 3
MAX_ARCHIVE_FILES = 2_000
MAX_EXPANDED_BYTES = 200 * 1024 * 1024

FIELD_ALIASES: dict[str, tuple[str, ...]] = {
    "product_name": (
        "采购标的",
        "产品服务名称",
        "产品名称",
        "货物名称",
        "服务名称",
        "品名",
        "标的物",
    ),
    "category": ("品目名称", "品目", "采购品目", "类别"),
    "brand": ("品牌", "产品供应商", "品牌产品供应商"),
    "model": ("规格型号", "规格", "型号"),
    "quantity": ("数量", "采购数量"),
    "unit_price": ("单价", "单价元"),
    "total_price": ("总价", "总价元", "合价", "总金额", "成交金额"),
}


@dataclass(frozen=True)
class SourceDocument:
    filename: str
    content: bytes


def _clean(value: object) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()


def _header_key(value: object) -> str:
    text = _clean(value).lower()
    text = re.sub(r"[\s（）()【】\[\]：:，,、/\\_-]", "", text)
    text = text.replace("人民币", "").replace("元", "元")
    return text


def _column_map(header: Iterable[object]) -> dict[str, int]:
    cells = [_header_key(cell) for cell in header]
    result: dict[str, int] = {}
    for field, aliases in FIELD_ALIASES.items():
        normalized_aliases = {_header_key(alias) for alias in aliases}
        for index, cell in enumerate(cells):
            if cell in normalized_aliases or any(
                alias and alias in cell for alias in normalized_aliases
            ):
                result[field] = index
                break
    return result


def _decimal(value: str) -> Decimal | None:
    if not value:
        return None
    text = value.replace(",", "").replace("，", "").replace("￥", "").replace("¥", "").strip()
    match = re.search(r"-?\d+(?:\.\d+)?", text)
    if not match:
        return None
    try:
        return Decimal(match.group(0))
    except InvalidOperation:
        return None


def _quantity(value: str) -> tuple[Decimal | None, str | None]:
    if not value:
        return None, None
    match = re.search(r"(-?\d+(?:[,.，]\d+)*)\s*(.*)", value)
    if not match:
        return _decimal(value), None
    return _decimal(match.group(1)), match.group(2).strip() or None


def parse_item_tables(
    rows: list[list[str]], *, source_file: str, table_index: int
) -> list[ItemCandidate]:
    """Map a tabular procurement-item section to the seven contest fields."""
    candidates: list[ItemCandidate] = []
    header_index: int | None = None
    columns: dict[str, int] = {}
    for index, row in enumerate(rows[:10]):
        current = _column_map(row)
        if len(current) >= 3 and ("product_name" in current or "category" in current):
            header_index, columns = index, current
            break
    if header_index is None:
        return candidates

    for row_index, row in enumerate(rows[header_index + 1 :], start=header_index + 2):
        values = [_clean(cell) for cell in row]
        if not any(values):
            continue

        def get_value(
            field: str, row: list[str] = values, column_map: dict[str, int] = columns
        ) -> str:
            col = column_map.get(field)
            return row[col] if col is not None and col < len(row) else ""

        product_name = get_value("product_name")
        category = get_value("category")
        quantity, quantity_unit = _quantity(get_value("quantity"))
        candidate = ItemCandidate(
            product_name=product_name or None,
            category=category or None,
            brand=get_value("brand") or None,
            model=get_value("model") or None,
            quantity=quantity,
            quantity_unit=quantity_unit,
            unit_price=_decimal(get_value("unit_price")),
            total_price=_decimal(get_value("total_price")),
            source_file=source_file,
            source_location=f"table:{table_index}/row:{row_index}",
            source_evidence=" | ".join(value for value in values if value),
        )
        # Ignore repeated header/footer rows and fully empty rows.
        if any(
            (
                candidate.product_name,
                candidate.brand,
                candidate.model,
                candidate.total_price is not None,
            )
        ):
            candidates.append(candidate)
    return candidates


def _html_tables(content: bytes, filename: str) -> tuple[str, list[ItemCandidate]]:
    soup = BeautifulSoup(content, "html.parser")
    for tag in soup(["script", "style", "noscript"]):
        tag.decompose()
    text = _clean(soup.get_text(" ", strip=True))
    items: list[ItemCandidate] = []
    for table_index, table in enumerate(soup.find_all("table"), start=1):
        rows = [
            [_clean(cell.get_text(" ", strip=True)) for cell in row.find_all(["th", "td"])]
            for row in table.find_all("tr")
        ]
        items.extend(parse_item_tables(rows, source_file=filename, table_index=table_index))
    return text, items


def _docx_tables(content: bytes, filename: str) -> tuple[str, list[ItemCandidate]]:
    document = Document(io.BytesIO(content))
    chunks = [paragraph.text for paragraph in document.paragraphs if paragraph.text.strip()]
    items: list[ItemCandidate] = []
    for table_index, table in enumerate(document.tables, start=1):
        rows = [[cell.text for cell in row.cells] for row in table.rows]
        chunks.extend(" ".join(_clean(cell) for cell in row) for row in rows)
        items.extend(parse_item_tables(rows, source_file=filename, table_index=table_index))
    return _clean(" ".join(chunks)), items


def _xlsx_tables(content: bytes, filename: str) -> tuple[str, list[ItemCandidate]]:
    workbook = load_workbook(io.BytesIO(content), read_only=True, data_only=True)
    chunks: list[str] = []
    items: list[ItemCandidate] = []
    table_index = 0
    for sheet in workbook.worksheets:
        rows = [[_clean(cell) for cell in row] for row in sheet.iter_rows(values_only=True)]
        rows = [row for row in rows if any(row)]
        chunks.extend(" ".join(row) for row in rows)
        parsed = parse_item_tables(rows, source_file=filename, table_index=table_index + 1)
        if parsed:
            table_index += 1
            items.extend(parsed)
    return _clean(" ".join(chunks)), items


def _pdf_text(content: bytes, filename: str) -> tuple[str, list[ItemCandidate]]:
    reader = PdfReader(io.BytesIO(content))
    page_text: list[str] = []
    items: list[ItemCandidate] = []
    for page_number, page in enumerate(reader.pages, start=1):
        text = page.extract_text() or ""
        page_text.append(f"[page:{page_number}] {text}")
        # PDF tables do not retain reliable column boundaries. Keep this text for
        # the model adapter and require review instead of inventing item rows.
    return _clean(" ".join(page_text)), items


def parse_document(document: SourceDocument) -> tuple[str, list[ItemCandidate], list[str]]:
    suffix = PurePosixPath(document.filename.replace("\\", "/")).suffix.lower()
    warnings: list[str] = []
    try:
        if suffix in {".html", ".htm"}:
            text, items = _html_tables(document.content, document.filename)
        elif suffix == ".docx":
            text, items = _docx_tables(document.content, document.filename)
        elif suffix == ".xlsx":
            text, items = _xlsx_tables(document.content, document.filename)
        elif suffix == ".pdf":
            text, items = _pdf_text(document.content, document.filename)
            if text and not items:
                warnings.append(
                    f"{document.filename}: PDF 文本已读取；复杂表格/扫描页需 OCR 或模型解析后核验"
                )
        elif suffix == ".txt":
            text, items = document.content.decode("utf-8", errors="replace"), []
        else:
            return "", [], [f"暂不支持附件格式：{document.filename}"]
    except Exception as exc:  # noqa: BLE001 - isolate individual corrupt attachments
        return "", [], [f"解析失败：{document.filename}（{type(exc).__name__}）"]
    return _clean(text), items, warnings


def expand_uploads(files: list[SourceDocument]) -> tuple[list[SourceDocument], list[str]]:
    """Expand nested ZIPs in memory with count/size/depth guards."""
    expanded: list[SourceDocument] = []
    warnings: list[str] = []
    total_bytes = 0

    def visit(document: SourceDocument, depth: int) -> None:
        nonlocal total_bytes
        suffix = PurePosixPath(document.filename.replace("\\", "/")).suffix.lower()
        if suffix != ".zip":
            total_bytes += len(document.content)
            if total_bytes > MAX_EXPANDED_BYTES:
                raise ValueError("解压后数据超过 200 MB 限制")
            expanded.append(document)
            return
        if depth >= MAX_ARCHIVE_DEPTH:
            warnings.append(f"压缩包嵌套超过 {MAX_ARCHIVE_DEPTH} 层：{document.filename}")
            return
        try:
            with zipfile.ZipFile(io.BytesIO(document.content)) as archive:
                members = [info for info in archive.infolist() if not info.is_dir()]
                if len(expanded) + len(members) > MAX_ARCHIVE_FILES:
                    raise ValueError("压缩包文件数量超过限制")
                for info in members:
                    # Do not write archive paths to disk; names are kept only as provenance.
                    if info.file_size > MAX_EXPANDED_BYTES:
                        warnings.append(f"跳过超大压缩文件：{info.filename}")
                        continue
                    nested = SourceDocument(
                        filename=f"{document.filename}!/{info.filename}",
                        content=archive.read(info),
                    )
                    visit(nested, depth + 1)
        except zipfile.BadZipFile:
            warnings.append(f"压缩包损坏或格式无效：{document.filename}")

    for uploaded in files:
        visit(uploaded, 0)
    return expanded, warnings


def extract_metadata(text: str) -> NoticeMetadata:
    """Conservative label-based extraction; ambiguous values remain blank."""
    normalized = re.sub(r"\s+", " ", text)
    labels = (
        "项目名称",
        "项目编号",
        "采购计划编号",
        "采购单位",
        "采购人",
        "采购机构",
        "采购预算",
        "预算金额",
        "项目预算",
        "总中标/成交金额",
        "中标金额",
        "成交金额",
        "项目成交金额",
    )
    next_label = "|".join(re.escape(label) for label in labels)

    def find(labels: tuple[str, ...]) -> str | None:
        label_pattern = "|".join(re.escape(label) for label in labels)
        match = re.search(
            rf"(?:{label_pattern})\s*[：:]\s*(.{{2,120}}?)(?=\s*(?:{next_label})\s*[：:]|[；;。]|$)",
            normalized,
        )
        if not match:
            return None
        value = match.group(1).strip(" ：:，,。")
        return value or None

    budget_text = find(("采购预算", "预算金额", "项目预算"))
    award_text = find(("总中标/成交金额", "中标金额", "成交金额", "项目成交金额"))
    return NoticeMetadata(
        project_name=find(("项目名称",)),
        project_number=find(("项目编号", "采购计划编号")),
        procurement_unit=find(("采购单位", "采购人", "采购机构")),
        project_budget=_decimal(budget_text or ""),
        announced_total_award=_decimal(award_text or ""),
    )
