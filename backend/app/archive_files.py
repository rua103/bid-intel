"""Bounded disk-backed expansion. Original member names are provenance, never paths."""
from __future__ import annotations

import io
import shutil
import zipfile
from dataclasses import dataclass
from pathlib import Path

import py7zr
import rarfile
from py7zr.io import Py7zIO, WriterFactory

from app.config import settings
from app.file_formats import inspect_content
from app.parsers import _zip_member_name


@dataclass(frozen=True)
class DiskDocument:
    filename: str
    path: Path

    @property
    def content(self) -> bytes:
        return self.path.read_bytes()


def configure_rar() -> bool:
    bundled = Path(__file__).resolve().parents[1] / '.data/tools/7zip/full/7z.exe'
    if bundled.is_file():
        rarfile.SEVENZIP_TOOL = str(bundled)
    if shutil.which('tar'):
        rarfile.BSDTAR_TOOL = shutil.which('tar')
    try:
        rarfile.tool_setup(force=True)
        return True
    except rarfile.RarCannotExec:
        return False


class Budget:
    def __init__(self, root: Path):
        self.root = root
        root.mkdir(parents=True, exist_ok=True)
        self.total = 0
        self.count = 0

    def target(self) -> Path:
        self.count += 1
        if self.count > settings.job_max_archive_files:
            raise ValueError('压缩包成员数超过后台任务限制')
        return self.root / f'{self.count:06d}.payload'

    def account(self, size: int, member_size: int) -> None:
        if member_size > settings.job_max_member_mb * 1024**2:
            raise ValueError('单个附件超过后台任务展开限制')
        self.total += size
        if self.total > settings.job_max_expanded_mb * 1024**2:
            raise ValueError('公告展开大小超过后台任务限制')

    def copy(self, source, target: Path) -> None:
        size = 0
        with target.open('wb') as output:
            while chunk := source.read(1024**2):
                size += len(chunk)
                self.account(len(chunk), size)
                output.write(chunk)


class _Writer(Py7zIO):
    def __init__(self, path: Path, budget: Budget):
        self.stream = path.open('w+b')
        self.budget = budget
        self.length = 0

    def write(self, data):
        self.budget.account(len(data), self.length + len(data))
        self.length += len(data)
        return self.stream.write(data)

    def read(self, size=None):
        return self.stream.read(size)

    def seek(self, offset, whence=0):
        return self.stream.seek(offset, whence)

    def flush(self):
        self.stream.flush()

    def size(self):
        return self.length


class _Factory(WriterFactory):
    def __init__(self, budget: Budget):
        self.budget = budget
        self.entries = []

    def create(self, filename):
        target = self.budget.target()
        writer = _Writer(target, self.budget)
        self.entries.append((filename, target, writer))
        return writer


def expand_paths(files: list[DiskDocument], directory: Path) -> tuple[list[DiskDocument], list[str]]:
    budget = Budget(directory)
    leaves, warnings = [], []

    def visit(document: DiskDocument, depth: int):
        if document.path.stat().st_size > settings.job_max_member_mb * 1024**2:
            raise ValueError(f'单文件超过后台任务限制：{document.filename}')
        detected = inspect_content(document.content)
        warnings.extend(f'{document.filename}: {w}' for w in detected.warnings)
        if detected.error:
            warnings.append(f'{document.filename}: {detected.error}')
            return
        if detected.warnings:  # gzip decoded payload
            target = budget.target()
            budget.copy(io.BytesIO(detected.content), target)
            document = DiskDocument(document.filename, target)
        kind = detected.format
        del detected
        if kind not in {'zip', 'rar', '7z'} or document.filename.lower().endswith('.gbq7'):
            leaves.append(document)
            return
        if depth >= settings.job_max_archive_depth:
            raise ValueError(f'压缩嵌套超过后台任务层数限制：{document.filename}')
        if kind == '7z':
            factory = _Factory(budget)
            try:
                with py7zr.SevenZipFile(document.path) as archive:
                    if archive.needs_password():
                        warnings.append(f'{document.filename}: 7z 已加密，无法展开')
                        return
                    archive.extractall(factory=factory)
            finally:
                for _, _, writer in factory.entries:
                    writer.stream.close()
            for name, path, _ in factory.entries:
                visit(DiskDocument(f'{document.filename}!/{name}', path), depth + 1)
            return
        if kind == 'rar' and not configure_rar():
            raise ValueError('RAR 需要 bsdtar、unrar 或 7z 解码程序')
        opener = zipfile.ZipFile if kind == 'zip' else rarfile.RarFile
        with opener(document.path) as archive:
            seen = set()
            for member in archive.infolist():
                if member.is_dir():
                    continue
                name = (_zip_member_name(member, warnings) if kind == 'zip' else member.filename)
                name = name.replace(chr(92), '/')
                full = f'{document.filename}!/{name}'
                if name in seen:
                    warnings.append(f'{full}: 重名成员已跳过')
                    continue
                seen.add(name)
                if member.file_size > settings.job_max_member_mb * 1024**2:
                    raise ValueError(f'单附件过大：{full}')
                target = budget.target()
                try:
                    with archive.open(member) as stream:
                        budget.copy(stream, target)
                except (RuntimeError, zipfile.BadZipFile, rarfile.Error) as exc:
                    warnings.append(f'{full}: 压缩成员读取失败（{type(exc).__name__}）')
                    continue
                visit(DiskDocument(full, target), depth + 1)

    for file in files:
        visit(file, 0)
    return leaves, warnings
