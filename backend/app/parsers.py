from __future__ import annotations

import importlib
import importlib.util
import io
import re
import shutil
import struct
import subprocess
import tempfile
import unicodedata
import zipfile
import zlib
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from pathlib import Path, PurePosixPath

from bs4 import BeautifulSoup
from docx import Document
from openpyxl import load_workbook
from pypdf import PdfReader

from app.legacy_documents import DocumentConversionError, convert_doc, libreoffice_executable
from app.schemas import ItemCandidate, NoticeMetadata

MAX_ARCHIVE_DEPTH = 3
MAX_ARCHIVE_FILES = 2_000
MAX_EXPANDED_BYTES = 200 * 1024 * 1024

FIELD_ALIASES: dict[str, tuple[str, ...]] = {
    "package_code": ("采购包编号", "采购包号", "包编号", "包号", "标包编号", "标段编号", "标段号"),
    "product_name": (
        "标的名称",
        "标的物名称",
        "采购标的",
        "采购标的名称",
        "标项名称",
        "采购内容",
        "产品服务名称",
        "产品名称",
        "货物名称",
        "服务名称",
        "货物服务名称",
        "名称",
        "品名",
        "标的物",
    ),
    "category": ("品目名称", "品目", "采购品目", "类别"),
    "brand": ("品牌", "品牌如有", "产品供应商", "品牌产品供应商"),
    "model": ("规格型号", "规格型号如有", "规格", "型号"),
    "quantity": ("数量", "数量单位", "采购数量", "采购数量单位"),
    "unit_price": ("单价", "单价元"),
    "total_price": (
        "总价", "合价", "总金额", "成交金额", "中标金额", "中标成交金额", "金额",
    ),
}


@dataclass(frozen=True)
class SourceDocument:
    filename: str
    content: bytes


def _clean(value: object) -> str:
    return re.sub(r"\s+", " ", str(value) if value is not None else "").strip()


def _header_key(value: object) -> str:
    text = unicodedata.normalize("NFKC", _clean(value)).lower()
    text = re.sub(r"[\s（）()【】\[\]：:，,、/\\_-]", "", text)
    text = text.replace("人民币", "")
    text = re.sub(r"(?:亿元|万元|元)$", "", text)
    return text


def _column_map(header: Iterable[object]) -> dict[str, int]:
    cells = [_header_key(cell) for cell in header]
    result: dict[str, int] = {}
    for field, aliases in FIELD_ALIASES.items():
        normalized_aliases = [_header_key(alias) for alias in aliases]
        # Match whole labels, never prose containing a header word. Alias order
        # gives 标的名称 priority over the broader 标项名称 when both are present.
        for alias in normalized_aliases:
            for index, cell in enumerate(cells):
                if cell and cell == alias:
                    result[field] = index
                    break
            if field in result:
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


def _money(value: str, header: str = "") -> Decimal | None:
    """Accept a single RMB amount; a percentage or reference is not a price."""
    text = unicodedata.normalize("NFKC", value)
    text = re.sub(r"[\s,，￥¥()]|人民币", "", text)
    match = re.fullmatch(r"(-?\d+(?:\.\d+)?)(亿元|万元|元)?", text)
    if not match:
        return None
    unit = match.group(2) or ("亿元" if "亿元" in header else
                            "万元" if "万元" in header else "元")
    return Decimal(match.group(1)) * {"元": 1, "万元": 10_000, "亿元": 100_000_000}[unit]


def _item_header(columns: dict[str, int]) -> bool:
    return (len(set(columns.values())) >= 3
            and ("product_name" in columns or "category" in columns))


def parse_item_tables(
    rows: list[list[str]], *, source_file: str, table_index: int
) -> list[ItemCandidate]:
    """Map a tabular procurement-item section to the seven contest fields."""
    candidates: list[ItemCandidate] = []
    header_index: int | None = None
    columns: dict[str, int] = {}
    for index, row in enumerate(rows[:10]):
        current = _column_map(row)
        # A flattened heading in one merged cell can contain many field words.
        # It is not a usable table header unless those fields occupy separate columns.
        if _item_header(current):
            header_index, columns = index, current
            break
    if header_index is None:
        return candidates
    header = rows[header_index]
    if any("供应商" in cell for cell in header) and not (
        {"brand", "model", "quantity", "unit_price"} & columns.keys()
    ):
        # 包号/采购内容/供应商/中标金额 describes a package award, not
        # item quantities and prices. Do not add its total beside detailed items.
        return candidates

    for row_index, row in enumerate(rows[header_index + 1 :], start=header_index + 2):
        values = [_clean(cell) for cell in row]
        if not any(values):
            continue
        repeated = _column_map(values)
        if _item_header(repeated):
            columns, header = repeated, values
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
            unit_price=_money(get_value("unit_price"),
                              header[columns["unit_price"]] if "unit_price" in columns else ""),
            total_price=_money(get_value("total_price"),
                               header[columns["total_price"]] if "total_price" in columns else ""),
            source_file=source_file,
            source_location=f"table:{table_index}/row:{row_index}",
            source_evidence=" | ".join(value for value in values if value),
            **extra,
        )
        if _is_item_data_row(values, columns, candidate):
            candidates.append(candidate)
    return candidates


_NON_ITEM_ROW = re.compile(
    r"^(?:[一二三四五六七八九十百\d]+\s*[、)）]|"
    r"(?:联系人|联系方式|联系电话|联系地址|收费标准|采购代理机构)[：:]|"
    r"(?:中标情况|成交情况|评审专家名单|序号|包号|合计|总计|备注|说明)(?:$|[：:]))"
)
_REFERENCE_VALUE = re.compile(r"^(?:详?见|具体见|以).*(?:附件|文件|清单|响应|为准).*")


def _is_item_data_row(
    values: list[str], columns: dict[str, int], candidate: ItemCandidate
) -> bool:
    """Reject headings and narrative rows that happen to sit below an item header."""
    identity = candidate.product_name or candidate.category
    if not identity:
        return False
    if not _clean(identity) or _NON_ITEM_ROW.match(_clean(identity)):
        return False
    if _REFERENCE_VALUE.fullmatch(identity) or _header_key(identity) in {
        _header_key(alias) for field in ("product_name", "category")
        for alias in FIELD_ALIASES[field]
    }:
        return False

    # A real item row should populate at least two separately mapped cells (for
    # example name + quantity). This excludes merged section titles and contact
    # paragraphs, while allowing a sparse name/category row.
    populated_columns = {
        column for column in set(columns.values())
        if column < len(values) and values[column]
    }
    if len(populated_columns) < 2:
        return False

    # The same source cell must never be reused as both the item identity and its
    # supporting detail; this also guards against malformed merged-cell tables.
    identity_field = "product_name" if candidate.product_name else "category"
    identity_column = columns.get(identity_field)
    return identity_column is not None and bool(populated_columns - {identity_column})


def _html_tables(content: bytes, filename: str) -> tuple[str, list[ItemCandidate]]:
    soup = BeautifulSoup(content, "html.parser")
    for tag in soup(["script", "style", "noscript"]):
        tag.decompose()
    items: list[ItemCandidate] = []
    for table_index, table in enumerate(soup.find_all("table"), start=1):
        rows = [
            ["" if cell.find("table") else _clean(cell.get_text(" ", strip=True))
             for cell in row.find_all(["th", "td"], recursive=False)]
            for row in table.find_all("tr") if row.find_parent("table") is table
        ]
        items.extend(parse_item_tables(rows, source_file=filename, table_index=table_index))
    # Keep block and cell boundaries for metadata; a space-flattened document
    # cannot distinguish the buyer from the next row's administrative region.
    for tag in soup.find_all(["p", "div", "tr", "table", "section", "br",
                              "h1", "h2", "h3", "h4", "li", "title"]):
        tag.insert_before("\n")
        tag.insert_after("\n")
    for cell in soup.find_all(["td", "th"]):
        # Cells frequently contain <p>/<div> wrappers; these are not new rows.
        if not cell.find("table"):
            value = _clean(cell.get_text(" ", strip=True))
            cell.clear()
            cell.append(value)
        cell.insert_after("\t")
    text = "\n".join(
        re.sub(r"[^\S\t]+", " ", line).strip(" ")
        for line in soup.get_text("").splitlines() if line.strip()
    )
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
    workbook.close()
    return _clean(" ".join(chunks)), items


def _xls_tables(content: bytes, filename: str) -> tuple[str, list[ItemCandidate]]:
    import xlrd

    workbook = xlrd.open_workbook(file_contents=content, on_demand=True)
    chunks: list[str] = []
    items: list[ItemCandidate] = []
    try:
        for index, sheet in enumerate(workbook.sheets(), start=1):
            # Keep numeric zero (e.g. a free item).
            rows = [[str(cell.value) if cell.ctype == xlrd.XL_CELL_NUMBER
                     else _clean(cell.value) for cell in row] for row in sheet.get_rows()]
            chunks.extend('\t'.join(row) for row in rows if any(row))
            parsed = parse_item_tables(rows, source_file=filename, table_index=index)
            for item in parsed:
                item.extraction_method = 'xls_table_header_mapping'
            items.extend(parsed)
        return '\n'.join(chunks), items
    finally:
        workbook.release_resources()


def parser_capabilities() -> dict[str, bool]:
    return {
        "pdf_text": True,
        "pdf_tables": importlib.util.find_spec("pdfplumber") is not None,
        "pdf_render": importlib.util.find_spec("pypdfium2") is not None,
        "image_ocr": shutil.which("tesseract") is not None,
        "legacy_doc": libreoffice_executable() is not None,
        "legacy_xls": importlib.util.find_spec("xlrd") is not None,
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
        warnings.append(f"{filename}: 缺少 pdfplumber，请重装后端依赖；本次 PDF 仅提取文本")
    if scan_pages:
        if not ocr_enabled:
            warnings.append(f"{filename}: 第 {','.join(map(str, scan_pages))} 页无可提取文本，OCR 未启用")
        elif not importlib.util.find_spec("pypdfium2"):
            warnings.append(f"{filename}: 缺少 pypdfium2 渲染组件，请重装后端依赖")
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
        elif suffix in {".doc", ".xls"}:
            # Some public attachments use Office suffixes for HTML or OOXML.
            leading = document.content.lstrip(b'\xef\xbb\xbf \t\r\n')[:1024].lower()
            if re.search(br'<(?:!doctype\s+html|html|table|head|body)\b', leading):
                text, items = _html_tables(document.content, document.filename)
                warnings.append(f"{document.filename}: 实际为 HTML，已按内容解析")
            elif document.content.startswith(b'PK\x03\x04'):
                parser = _docx_tables if suffix == '.doc' else _xlsx_tables
                text, items = parser(document.content, document.filename)
                warnings.append(f"{document.filename}: 实际为 OOXML，已按内容解析")
            elif suffix == '.xls':
                text, items = _xls_tables(document.content, document.filename)
            else:
                text, items = _docx_tables(convert_doc(document.content), document.filename)
                for item in items:
                    item.extraction_method = 'doc_converted_table_header_mapping'
                if text and not items:
                    warnings.append(f"{document.filename}: DOC 文本已读取，未映射出标的表格，请核验布局或使用模型")
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
    except DocumentConversionError as exc:
        return "", [], [f"解析失败：{document.filename}（{exc}）"]
    except Exception as exc:  # noqa: BLE001 - isolate individual corrupt attachments
        return "", [], [f"解析失败：{document.filename}（{type(exc).__name__}）"]
    if not text.strip() and not items and not warnings:
        warnings.append(f"{document.filename}: 未解析出文本或表格，请检查空文件、加密或扫描内容")
    return text.strip(), items, warnings


def _zip_member_name(info: zipfile.ZipInfo, warnings: list[str]) -> str:
    if info.flag_bits & 0x800:
        return info.filename
    raw = info.orig_filename.encode('cp437')
    # Info-ZIP Unicode Path overrides a legacy name only when its CRC matches.
    extra = info.extra
    while len(extra) >= 4:
        kind, size = struct.unpack_from('<HH', extra)
        payload, extra = extra[4:4 + size], extra[4 + size:]
        if kind == 0x7075 and len(payload) >= 5 and payload[0] == 1:
            if struct.unpack_from('<I', payload, 1)[0] == zlib.crc32(raw):
                try:
                    return payload[5:].decode('utf-8')
                except UnicodeDecodeError:
                    pass
            warnings.append(f"ZIP Unicode 文件名校验失败，尝试原始编码：{info.filename}")
    for encoding in ('utf-8', 'gb18030'):
        try:
            name = raw.decode(encoding)
        except UnicodeDecodeError:
            continue
        if name != info.filename:
            warnings.append(f"ZIP 文件名按 {encoding} 解码：{name}")
        return name
    warnings.append(f"ZIP 文件名编码无法确定，保留原名，请核验附件归属：{info.filename}")
    return info.filename


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
                seen_names: set[str] = set()
                for info in members:
                    name = _zip_member_name(info, warnings).replace('\\', '/')
                    if name in seen_names:
                        warnings.append(f"ZIP 解码后文件名重复，跳过后续同名文件：{document.filename}!/{name}")
                        continue
                    seen_names.add(name)
                    # Do not write archive paths to disk; names are kept only as provenance.
                    if info.file_size > MAX_EXPANDED_BYTES:
                        warnings.append(f"跳过超大压缩文件：{name}")
                        continue
                    try:
                        content = archive.read(info)
                    except (zipfile.BadZipFile, RuntimeError, NotImplementedError, OSError):
                        warnings.append(f"ZIP 成员读取失败（损坏、加密或压缩算法不支持）：{name}")
                        continue
                    nested = SourceDocument(
                        filename=f"{document.filename}!/{name}",
                        content=content,
                    )
                    visit(nested, depth + 1)
        except (zipfile.BadZipFile, UnicodeDecodeError):
            warnings.append(f"压缩包损坏或格式无效：{document.filename}")

    for uploaded in files:
        visit(uploaded, 0)
    return expanded, warnings


METADATA_LABELS = {
    "project_name": ("采购项目名称", "项目名称"),
    "project_number": ("采购项目编号", "项目编号", "采购计划编号"),
    "procurement_unit": ("采购单位名称", "采购单位", "采购人名称", "采购人", "采购机构"),
    "project_budget": ("采购预算金额", "采购预算", "预算金额", "项目预算金额", "项目预算"),
    "announced_total_award": (
        "总中标金额", "总成交金额", "总中标/成交金额", "中标（成交）金额",
        "中标/成交金额", "中标金额", "成交金额", "项目成交金额",
    ),
}


def extract_metadata(text: str) -> NoticeMetadata:
    """Prefer exact table label/value cells, then bounded colon-labelled lines."""
    label_fields = {label: field for field, labels in METADATA_LABELS.items() for label in labels}
    cell_fields = {_header_key(label): field for label, field in label_fields.items()}
    values: dict[str, object] = {}

    def accept(field: str, raw: str, label: str) -> None:
        value = _clean(raw).strip(" ：:，,。")
        if not value or field in values or _REFERENCE_VALUE.fullmatch(value):
            return
        if field in {"project_budget", "announced_total_award"}:
            amount = _money(value, label)
            if amount is not None:
                values[field] = amount
        elif field == "project_number":
            # Codes may contain parentheses but cannot include a title suffix.
            code = re.split(r"[）)](?=.*(?:公告|采购项目))", value, maxsplit=1)[0]
            if len(code) <= 120:
                values[field] = code
        elif len(value) <= 160:
            values[field] = value

    lines = text.splitlines()
    for line in lines:
        # HTML extraction preserves empty cells and tabs, including 4-column
        # rows containing two key/value pairs. Never join adjacent row values.
        cells = line.rstrip("\t ").split("\t")
        for index in range(0, len(cells) - 1, 2):
            label, value = cells[index:index + 2]
            field = cell_fields.get(_header_key(label))
            if field and _header_key(value) not in cell_fields:
                accept(field, value, label)

    pattern = "|".join(re.escape(label) for label in sorted(label_fields, key=len, reverse=True))
    labelled = re.compile(rf"(?P<label>{pattern})\s*[：:]\s*")
    for line in lines:
        # A label on its own (e.g. 项目名称) is not a value. No whitespace-only
        # fallback: structural table rows were handled above.
        matches = list(labelled.finditer(line))
        for index, match in enumerate(matches):
            end = matches[index + 1].start() if index + 1 < len(matches) else len(line)
            raw = line[match.end():end]
            raw = re.split(r"[\t；;。]|\s+[\u4e00-\u9fff]{2,16}\s*[：:]|"
                           r"\s*[一二三四五六七八九十]+\s*[、）)]|"
                           r"\s+\d+\s*[、）)]", raw, maxsplit=1)[0]
            accept(label_fields[match["label"]], raw, match["label"])
    return NoticeMetadata(**values)
