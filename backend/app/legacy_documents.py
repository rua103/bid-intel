"""Convert legacy Word in a private, short-lived LibreOffice process."""
from __future__ import annotations

import hashlib
import io
import os
import shutil
import signal
import subprocess
import tempfile
import zipfile
from pathlib import Path

from filelock import FileLock, Timeout

from app.config import settings


class DocumentConversionError(ValueError):
    """Safe, actionable conversion diagnostic for the import report."""


def _run_conversion(command: list[str], timeout: int) -> int:
    process = subprocess.Popen(
        command, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0),
        start_new_session=os.name != 'nt',
    )
    try:
        return process.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        # soffice launches a child process. Stop our isolated tree before deleting
        # its profile; killing just the launcher leaves conversion running.
        if os.name == 'nt':
            subprocess.run(['taskkill', '/PID', str(process.pid), '/T', '/F'],
                           capture_output=True, check=False, timeout=10,
                           creationflags=subprocess.CREATE_NO_WINDOW)
        else:
            os.killpg(process.pid, signal.SIGKILL)
        process.wait(timeout=10)
        raise


def libreoffice_executable() -> str | None:
    if settings.libreoffice_path:
        configured = Path(settings.libreoffice_path).expanduser()
        return str(configured) if configured.is_file() else None
    executable = shutil.which('soffice') or shutil.which('libreoffice')
    if executable:
        return executable
    candidates = [
        Path(__file__).resolve().parents[1] / '.data/tools/libreoffice/program/soffice.exe',
        Path(os.environ.get('PROGRAMFILES', 'C:/Program Files'))
        / 'LibreOffice/program/soffice.exe',
        Path('/Applications/LibreOffice.app/Contents/MacOS/soffice'),
    ]
    return next((str(path) for path in candidates if path.is_file()), None)


def convert_doc(content: bytes) -> bytes:
    executable = libreoffice_executable()
    if not executable:
        raise DocumentConversionError('旧版 Word 需要 LibreOffice；安装后设置 LIBREOFFICE_PATH')
    executable_path = Path(executable).resolve()
    stamp = executable_path.stat() if executable_path.is_file() else None
    version = f'v2:{executable_path}:{stamp.st_mtime_ns if stamp else 0}'
    cache_root = settings.resolved_database_path.parent / 'document-cache'
    cache_root.mkdir(parents=True, exist_ok=True)
    key = hashlib.sha256(version.encode() + b'\0' + content).hexdigest()
    cached = cache_root / (key + '.docx')
    # The shared profile is private to this app. A cross-process lock prevents
    # LibreOffice forwarding a request to a busy process and reporting early success.
    try:
        with FileLock(str(cache_root / 'converter.lock'), timeout=300):
            if cached.is_file():
                data = cached.read_bytes()
                if _valid_docx(data):
                    return data
            data = _convert_uncached(content, executable, cache_root / 'profile')
            temporary = cached.with_suffix('.pending')
            temporary.write_bytes(data)
            temporary.replace(cached)
            return data
    except Timeout as exc:
        raise DocumentConversionError('旧版 Word 转换队列等待超过 300 秒，请稍后重试') from exc


def _valid_docx(content: bytes) -> bool:
    try:
        with zipfile.ZipFile(io.BytesIO(content)) as archive:
            return 'word/document.xml' in archive.namelist() and archive.testzip() is None
    except (zipfile.BadZipFile, OSError, RuntimeError, NotImplementedError):
        return False


def _convert_uncached(content: bytes, executable: str, profile: Path) -> bytes:
    with tempfile.TemporaryDirectory(prefix='bidintel-doc-') as directory:
        root = Path(directory)
        source = root / 'attachment.doc'
        source.write_bytes(content)
        output = root / 'output'
        output.mkdir()
        (profile / 'user').mkdir(parents=True, exist_ok=True)
        # Independent profile avoids reusing or blocking the user's office session.
        (profile / 'user/registrymodifications.xcu').write_text(
            '<?xml version="1.0" encoding="UTF-8"?>'
            '<oor:items xmlns:oor="http://openoffice.org/2001/registry">'
            '<item oor:path="/org.openoffice.Office.Common/Security/Scripting">'
            '<prop oor:name="MacroSecurityLevel" oor:op="fuse"><value>3</value></prop>'
            '</item></oor:items>', encoding='utf-8',
        )
        try:
            returncode = _run_conversion(
                [executable, f'-env:UserInstallation={profile.as_uri()}', '--headless',
                 '--nologo', '--nodefault', '--norestore', '--convert-to',
                 'docx:Office Open XML Text', '--outdir', str(output), str(source)],
                timeout=max(1, min(settings.document_conversion_timeout_seconds, 120)),
            )
        except subprocess.TimeoutExpired as exc:
            raise DocumentConversionError(
                f'旧版 Word 转换超时（配置 {settings.document_conversion_timeout_seconds} 秒，最高 120 秒）'
            ) from exc
        converted = output / 'attachment.docx'
        if returncode or not converted.is_file():
            raise DocumentConversionError('旧版 Word 转换失败，文件可能损坏、加密或格式不受支持')
        data = converted.read_bytes()
        if not _valid_docx(data):
            raise DocumentConversionError('旧版 Word 转换输出无效，未缓存，请核对原文件')
        return data
