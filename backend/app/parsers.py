from __future__ import annotations

import importlib
import importlib.util
import io
import re
import shutil
import subprocess
import tempfile
import zipfile
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from pathlib import Path, PurePosixPath

from bs4 import BeautifulSoup
from docx import Document
from openpyxl import load_workbook
from pypdf import PdfReader

from app.schemas import ItemCandidate, NoticeMetadata

MAX_ARCHIVE_DEPTH = 3
MAX_ARCHIVE_FILES = 2_000
MAX_EXPANDED_BYTES = 200 * 1024 * 1024

FIELD_ALIASES: dict[str, tuple[str, ...]] = {
    "package_code": ("采购包编号", "采购包号", "包编号", "包号", "标包编号", "标段编号", "标段号"),
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
        extra = {}
        if "package_code" in ItemCandidate.model_fields:
            extra["package_code"] = get_value("package_code") or "default"
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
            **extra,
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


def parser_capabilities() -> dict[str, bool]:
    return {
        "pdf_text": True,
        "pdf_tables": importlib.util.find_spec("pdfplumber") is not None,
        "pdf_render": importlib.util.find_spec("pypdfium2") is not None,
        "image_ocr": shutil.which("tesseract") is not None,
    }


def _ocr_image(
    content: bytes, filename: str, *, language: str, timeout: int,
    engine: Callable[[bytes, str], str] | None = None,
) -> tuple[str, list[str]]:
    if engine is not None:
        return engine(content, filename), []
    executable = shutil.which("tesseract")
    if executable is None:
        return "", [f"{filename}: OCR 已启用但未找到 Tesseract，可安装并配置 chi_sim 语言包"]
    if not re.fullmatch(r"[A-Za-z0-9_+\-]+", language):
        return "", [f"{filename}: OCR 语言参数无效"]
    with tempfile.TemporaryDirectory(prefix="bidintel-ocr-") as directory:
        path = Path(directory) / ("page" + (Path(filename).suffix or ".png"))
        path.write_bytes(content)
        try:
            result = subprocess.run(
                [executable, str(path), "stdout", "-l", language],
                capture_output=True, timeout=max(1, min(timeout, 120)), check=False,
            )
        except subprocess.TimeoutExpired:
            return "", [f"{filename}: OCR 超时，已跳过该图像"]
        if result.returncode:
            return "", [f"{filename}: Tesseract OCR 失败，请检查图像与语言包"]
        return result.stdout.decode("utf-8", errors="replace"), []


def _pdf_text(
    content: bytes, filename: str, *, ocr_enabled: bool = False,
    ocr_language: str = "chi_sim+eng", ocr_timeout_seconds: int = 30,
    ocr_engine: Callable[[bytes, str], str] | None = None,
) -> tuple[str, list[ItemCandidate], list[str]]:
    reader = PdfReader(io.BytesIO(content))
    page_text: list[str] = []
    items: list[ItemCandidate] = []
    warnings: list[str] = []
    scan_pages = []
    for page_number, page in enumerate(reader.pages[:100], start=1):
        text = page.extract_text() or ""
        if text.strip():
            page_text.append(f"[page:{page_number}] {text}")
        else:
            scan_pages.append(page_number)
    if len(reader.pages) > 100:
        warnings.append(f"{filename}: PDF 超过 100 页，只解析前 100 页")
    if importlib.util.find_spec("pdfplumber"):
        try:
            with importlib.import_module("pdfplumber").open(io.BytesIO(content)) as pdf:
                for page_number, page in enumerate(pdf.pages[:100], start=1):
                    for table_index, table in enumerate(page.extract_tables() or [], start=1):
                        parsed = parse_item_tables(table, source_file=filename, table_index=table_index)
                        for item in parsed:
                            item.source_location = f"page:{page_number}/{item.source_location}"
                            item.extraction_method = "pdfplumber_table_header_mapping"
                        items.extend(parsed)
        except Exception as exc:  # noqa: BLE001
            warnings.append(f"{filename}: PDF 表格解析失败（{type(exc).__name__}），保留文本结果")
    else:
        warnings.append(f"{filename}: 未安装可选 pdfplumber，PDF 仅提取文本")
    if scan_pages:
        if not ocr_enabled:
            warnings.append(f"{filename}: 第 {','.join(map(str, scan_pages))} 页无可提取文本，OCR 未启用")
        elif not importlib.util.find_spec("pypdfium2"):
            warnings.append(f"{filename}: 扫描页 OCR 需要可选 pypdfium2 渲染组件")
        else:
            renderer = importlib.import_module("pypdfium2").PdfDocument(content)
            try:
                for page_number in scan_pages[:30]:
                    page = renderer[page_number - 1]
                    bitmap = page.render(scale=2)
                    buffer = io.BytesIO()
                    try:
                        bitmap.to_pil().save(buffer, format="PNG")
                        text, ocr_warnings = _ocr_image(
                            buffer.getvalue(), f"{filename}.page{page_number}.png",
                            language=ocr_language, timeout=ocr_timeout_seconds, engine=ocr_engine,
                        )
                        if text.strip():
                            page_text.append(f"[page:{page_number}/ocr] {text}")
                        warnings.extend(ocr_warnings)
                    finally:
                        bitmap.close()
                        page.close()
                if len(scan_pages) > 30:
                    warnings.append(f"{filename}: 每份 PDF 最多 OCR 30 页，其余扫描页未处理")
            finally:
                renderer.close()
    return _clean(" ".join(page_text)), items, warnings


def parse_document(
    document: SourceDocument, *, ocr_enabled: bool = False,
    ocr_language: str = "chi_sim+eng", ocr_timeout_seconds: int = 30,
    ocr_engine: Callable[[bytes, str], str] | None = None,
) -> tuple[str, list[ItemCandidate], list[str]]:
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
            text, items, warnings = _pdf_text(
                document.content, document.filename, ocr_enabled=ocr_enabled,
                ocr_language=ocr_language, ocr_timeout_seconds=ocr_timeout_seconds,
                ocr_engine=ocr_engine,
            )
            if text and not items:
                warnings.append(
                    f"{document.filename}: PDF 文本已读取；复杂表格/扫描页需 OCR 或模型解析后核验"
                )
        elif suffix == ".txt":
            text, items = document.content.decode("utf-8", errors="replace"), []
        elif suffix in {".png", ".jpg", ".jpeg", ".tif", ".tiff", ".bmp"}:
            if not ocr_enabled:
                return "", [], [f"{document.filename}: 图像附件需要 OCR，当前未启用"]
            text, warnings = _ocr_image(
                document.content, document.filename, language=ocr_language,
                timeout=ocr_timeout_seconds, engine=ocr_engine,
            )
            items = []
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
