"""Detect payload formats before dispatch; filenames remain unchanged for provenance."""
from __future__ import annotations

import gzip
import io
import json
import re
import zipfile
from dataclasses import dataclass, field

import olefile
from bs4 import BeautifulSoup

MAX_DECODED_BYTES = 200 * 1024 * 1024


@dataclass
class DetectedFile:
    content: bytes
    format: str | None = None
    warnings: list[str] = field(default_factory=list)
    error: str | None = None


def _download_error(content: bytes) -> str | None:
    # Only inspect complete small textual responses. A tender can legitimately
    # discuss CAPTCHA/access controls, so do not match keywords in arbitrary PDFs.
    if len(content) > 128 * 1024:
        return None
    text = content.decode('utf-8-sig', errors='replace').strip()
    if text.startswith('{'):
        try:
            payload = json.loads(text)
        except ValueError:
            return None
        if isinstance(payload, dict) and (
            payload.get('successFul') is False or payload.get('success') is False
            or ('message' in payload or 'msg' in payload)
            and payload.get('code') not in (None, 0, 200, '0', '200')
        ):
            return 'JSON 下载错误响应'
        return None
    if not re.search(r'<(?:!doctype|html|head|body|div|p|table)\b', text[:2048], re.IGNORECASE):
        return None
    soup = BeautifulSoup(text, 'html.parser')
    for tag in soup(['script', 'style']):
        tag.decompose()
    visible = soup.get_text(' ', strip=True)
    title = soup.title.get_text(' ', strip=True) if soup.title else ''
    if ('请输入验证码' in visible and ('再次下载请刷新页面' in visible
                                     or soup.find('input') and '文件下载' in visible)):
        return '验证码下载页'
    if '系统限制' in title or title.strip().lower() in ('access denied', '403 forbidden',
                                                       '404 not found'):
        return '访问限制或错误页面'
    if len(visible) < 1500 and '系统正在维护中' in visible:
        return '系统维护页面'
    return None


def inspect_content(content: bytes) -> DetectedFile:
    result = DetectedFile(content)
    if content.startswith(b'\x1f\x8b'):
        try:
            with gzip.GzipFile(fileobj=io.BytesIO(content)) as stream:
                content = stream.read(MAX_DECODED_BYTES + 1)
            if len(content) > MAX_DECODED_BYTES:
                result.error = 'gzip 展开内容超过 200 MB 限制'
                return result
        except (OSError, EOFError):
            result.error = 'gzip 内容损坏，无法读取'
            return result
        result.content = content
        result.warnings.append('已解开 gzip 内容封装')
    reason = _download_error(content)
    if reason:
        result.error = f'源附件不可用：实际是{reason}，请核对原始下载；未送入模型'
        return result
    leading = content.lstrip(b'\xef\xbb\xbf \t\r\n')[:2048]
    if b'%PDF-' in content[:1024] and not content.startswith((b'PK', b'Rar!', b'7z')):
        result.format = 'pdf'
    elif content.startswith((b'PK\x03\x04', b'PK\x05\x06')):
        try:
            with zipfile.ZipFile(io.BytesIO(content)) as archive:
                names = set(archive.namelist())
            result.format = ('docx' if 'word/document.xml' in names else
                             'xlsx' if 'xl/workbook.xml' in names else 'zip')
        except zipfile.BadZipFile:
            result.error = 'ZIP/OOXML 内容损坏，无法读取'
    elif content.startswith(bytes.fromhex('d0cf11e0a1b11ae1')):
        try:
            with olefile.OleFileIO(io.BytesIO(content)) as document:
                streams = {path[0] for path in document.listdir()}
            if 'EncryptedPackage' in streams:
                result.error = 'Office 文档已加密，需提供可读取的原件'
            elif 'WordDocument' in streams:
                result.format = 'doc'
            elif streams.intersection({'Workbook', 'Book'}):
                result.format = 'xls'
            else:
                result.error = '无法识别的 OLE 文档结构'
        except (OSError, ValueError):
            result.error = 'OLE 文档损坏，无法读取'
    elif content.startswith(b'{\\rtf'):
        result.format = 'rtf'
    elif content.startswith(b'Rar!\x1a\x07'):
        result.format = 'rar'
    elif content.startswith(bytes.fromhex('377abcaf271c')):
        result.format = '7z'
    elif content.startswith(b'\x89PNG\r\n\x1a\n'):
        result.format = 'png'
    elif content.startswith(b'\xff\xd8\xff'):
        result.format = 'jpg'
    elif content.startswith((b'II*\x00', b'MM\x00*')):
        result.format = 'tiff'
    elif content.startswith(b'BM'):
        result.format = 'bmp'
    elif re.search(br'<(?:!doctype\s+html|html|head|body|div|p|table)\b', leading, re.IGNORECASE):
        result.format = 'html'
    return result
