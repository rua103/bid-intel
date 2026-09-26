import io
import struct
import subprocess
import sys
import zipfile
import zlib
from pathlib import Path

import pytest
import xlwt
from fastapi.testclient import TestClient
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.cidfonts import UnicodeCIDFont
from reportlab.pdfgen.canvas import Canvas
from reportlab.platypus import SimpleDocTemplate, Table, TableStyle

from app import legacy_documents
from app.ingestion import group_notice_documents
from app.main import app
from app.parsers import SourceDocument, expand_uploads, parse_document

ROWS = [
    ['采购标的', '品目名称', '品牌', '规格型号', '数量', '单价', '总价'],
    ['打印机', '办公设备', '示例牌', 'X-100', '2台', '1000', '2000'],
]


def assert_item(items, filename):
    assert len(items) == 1
    item = items[0]
    assert [item.product_name, item.category, item.brand, item.model] == ROWS[1][:4]
    assert (item.quantity, item.unit_price, item.total_price) == (2, 1000, 2000)
    assert item.source_file == filename
    assert '打印机' in item.source_evidence


def test_real_binary_xls_seven_fields_and_zero_price():
    workbook = xlwt.Workbook()
    sheet = workbook.add_sheet('报价明细')
    for r, row in enumerate(ROWS):
        for c, value in enumerate(row):
            sheet.write(r, c, value)
    zero = workbook.add_sheet('免费项目')
    for r, row in enumerate([ROWS[0], ['免费服务', '', '', '', 1, 0, 0]]):
        for c, value in enumerate(row):
            zero.write(r, c, value)
    buffer = io.BytesIO()
    workbook.save(buffer)
    assert buffer.getvalue().startswith(bytes.fromhex('d0cf11e0'))
    text, items, warnings = parse_document(SourceDocument('报价.xls', buffer.getvalue()))
    assert not warnings
    assert '打印机' in text
    assert_item(items[:1], '报价.xls')
    assert items[1].unit_price == items[1].total_price == 0


def test_real_chinese_pdf_table_seven_fields():
    pdfmetrics.registerFont(UnicodeCIDFont('STSong-Light'))
    table = Table(ROWS, colWidths=[80, 70, 65, 65, 50, 50, 50])
    table.setStyle(TableStyle([
        ('FONTNAME', (0, 0), (-1, -1), 'STSong-Light'),
        ('GRID', (0, 0), (-1, -1), 0.5, '#000000'),
    ]))
    buffer = io.BytesIO()
    SimpleDocTemplate(buffer).build([table])
    text, items, warnings = parse_document(SourceDocument('报价.pdf', buffer.getvalue()))
    assert not warnings
    assert '打印机' in text
    assert_item(items, '报价.pdf')
    assert items[0].source_location.startswith('page:1/')


def test_real_pdf_scan_page_is_rendered_before_ocr():
    buffer = io.BytesIO()
    canvas = Canvas(buffer)
    canvas.rect(20, 20, 100, 100)
    canvas.showPage()
    canvas.save()
    pages = []

    def fake_ocr(content, filename):
        assert content.startswith(b'\x89PNG\r\n\x1a\n')
        pages.append(filename)
        return '扫描页文字'

    text, items, warnings = parse_document(
        SourceDocument('扫描.pdf', buffer.getvalue()), ocr_enabled=True, ocr_engine=fake_ocr,
    )
    assert '扫描页文字' in text
    assert not items and pages == ['扫描.pdf.page1.png']
    assert not any('未安装' in warning or '缺少' in warning for warning in warnings)


def test_real_binary_doc_fixture(monkeypatch):
    if not legacy_documents.libreoffice_executable():
        pytest.skip('真实 DOC 转换需要 LibreOffice')
    fixture = Path(__file__).parent / 'fixtures/legacy-quotation.doc'
    text, items, warnings = parse_document(SourceDocument('报价.doc', fixture.read_bytes()))
    assert not warnings
    assert '打印机' in text
    assert_item(items, '报价.doc')

    def forbidden(*args, **kwargs):
        raise AssertionError('cached content must not run LibreOffice again')

    monkeypatch.setattr(legacy_documents, '_run_conversion', forbidden)
    text, items, warnings = parse_document(SourceDocument('伪扩展名.docx', fixture.read_bytes()))
    assert_item(items, '伪扩展名.docx')
    assert any('实际为 DOC' in row for row in warnings)


def test_doc_dependency_and_timeout_diagnostics(monkeypatch):
    monkeypatch.setattr(legacy_documents, 'libreoffice_executable', lambda: None)
    document = SourceDocument('报价.doc', b'legacy')
    assert 'LIBREOFFICE_PATH' in parse_document(document)[2][0]
    monkeypatch.setattr(legacy_documents, 'libreoffice_executable', lambda: 'soffice')

    def timeout(*args, **kwargs):
        raise subprocess.TimeoutExpired(args[0], 1)

    monkeypatch.setattr(legacy_documents, '_run_conversion', timeout)
    assert '转换超时' in parse_document(document)[2][0]


def test_conversion_process_really_times_out():
    with pytest.raises(subprocess.TimeoutExpired):
        legacy_documents._run_conversion([sys.executable, '-c', 'import time; time.sleep(30)'], 1)


@pytest.mark.parametrize('suffix', ['doc', 'xls'])
def test_html_disguised_as_office(suffix):
    html = '<meta charset="utf-8"><table>' + ''.join(
        '<tr>' + ''.join(f'<td>{value}</td>' for value in row) + '</tr>' for row in ROWS
    ) + '</table>'
    filename = f'报价.{suffix}'
    _, items, warnings = parse_document(SourceDocument(filename, html.encode()))
    assert_item(items, filename)
    assert '实际为 HTML' in warnings[0]


@pytest.mark.parametrize('suffix', ['xls', 'pdf'])
def test_corrupt_attachment_is_visible(suffix):
    text, items, warnings = parse_document(SourceDocument(f'坏文件.{suffix}', b'corrupt'))
    assert not text and not items
    assert '解析失败' in warnings[0]


class EncodedZipInfo(zipfile.ZipInfo):
    encoding = 'gb18030'

    def _encodeFilenameFlags(self):
        return self.filename.encode(self.encoding), self.flag_bits & ~0x800


def make_zip(entries):
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, 'w') as archive:
        for name, content in entries:
            archive.writestr(name, content)
    return buffer.getvalue()


def test_mixed_gbk_utf8_nested_zip_matches_each_notice():
    nested = make_zip([('报价.txt', '附件甲')])
    content = make_zip([
        (EncodedZipInfo('项目甲.html'), '<p>项目甲</p>'),
        ('项目甲附件.zip', nested),
        ('项目乙.html', '<p>项目乙</p>'),
        (EncodedZipInfo('项目乙附件.txt'), '附件乙'),
    ])
    expanded, warnings = expand_uploads([SourceDocument('数据.zip', content)])
    groups, orphans = group_notice_documents(expanded)
    assert not orphans and len(groups) == 2
    assert [len(group) for group in groups] == [2, 2]
    assert any('gb18030' in warning for warning in warnings)
    assert any('项目甲附件.zip!/报价.txt' in row.filename for row in groups[0])


def test_zip_unflagged_utf8_and_unicode_extra_crc():
    name = EncodedZipInfo('无标志中文.txt')
    name.encoding = 'utf-8'
    unicode_name = '正式名称.txt'
    other = EncodedZipInfo('legacy.txt')
    payload = b'\x01' + struct.pack('<I', zlib.crc32(b'legacy.txt')) + unicode_name.encode()
    other.extra = struct.pack('<HH', 0x7075, len(payload)) + payload
    content = make_zip([(name, 'a'), (other, 'b')])
    documents, _ = expand_uploads([SourceDocument('a.zip', content)])
    assert [row.filename for row in documents] == ['a.zip!/无标志中文.txt', 'a.zip!/正式名称.txt']


def test_zip_duplicate_decoded_names_and_invalid_unicode_crc_are_visible():
    duplicate = EncodedZipInfo('项目附件.txt')
    invalid = EncodedZipInfo('合法原名.txt')
    payload = b'\x01' + struct.pack('<I', 0) + '错误覆盖.txt'.encode()
    invalid.extra = struct.pack('<HH', 0x7075, len(payload)) + payload
    content = make_zip([(duplicate, 'first'), ('项目附件.txt', 'second'), (invalid, 'third')])
    documents, warnings = expand_uploads([SourceDocument('a.zip', content)])
    assert [row.filename for row in documents] == ['a.zip!/项目附件.txt', 'a.zip!/合法原名.txt']
    assert documents[0].content == b'first'
    assert any('重复' in warning for warning in warnings)
    assert any('校验失败' in warning for warning in warnings)


def test_corrupt_zip_without_notices_still_has_top_level_warning():
    with TestClient(app) as client:
        response = client.post('/api/v1/notices/import-batch',
                               files=[('files', ('bad.zip', b'corrupt'))])
    payload = response.json()
    assert response.status_code == 400
    assert '损坏' in payload['detail']


def test_bad_archive_beside_valid_notice_is_reported_without_losing_notice():
    with TestClient(app) as client:
        response = client.post('/api/v1/notices/import-batch', files=[
            ('files', ('bad.zip', b'corrupt')), ('files', ('a.html', b'<p>notice</p>')),
        ])
    assert response.status_code == 200
    assert response.json()['notices_imported'] == 1
    assert any('损坏' in warning for warning in response.json()['warnings'])
