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
        # Treat each input/container/member as an isolation boundary. A damaged
        # nested archive must not discard good siblings from the notice.
        try:
            _visit(document, depth)
        except Exception as exc:  # noqa: BLE001 - one bad attachment is isolated
            warnings.append(
                f'{document.filename}: 附件容器展开失败（{type(exc).__name__}）'
            )

    def _visit(document: DiskDocument, depth: int):
        if document.path.stat().st_size > settings.job_max_member_mb * 1024**2:
            warnings.append(f'{document.filename}: 单文件超过后台任务限制，已跳过')
            return
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
        if kind not in {'zip', 'rar', '7z'}:
            leaves.append(document)
            return
        if depth >= settings.job_max_archive_depth:
            warnings.append(f'压缩嵌套超过后台任务层数限制，已跳过：{document.filename}')
            return
        if kind == '7z':
            try:
                with py7zr.SevenZipFile(document.path) as archive:
                    if archive.needs_password():
                        warnings.append(f'{document.filename}: 7z 已加密，无法展开')
                        return
                    members = [item for item in archive.list() if item.is_file]
            except Exception as exc:  # noqa: BLE001 - isolate an invalid 7z container
                warnings.append(
                    f'{document.filename}: 附件容器展开失败（{type(exc).__name__}）'
                )
                return
            for member in members:
                full = f'{document.filename}!/{member.filename}'
                if member.uncompressed > settings.job_max_member_mb * 1024**2:
                    warnings.append(f'{full}: 单附件超过后台任务展开限制，已跳过')
                    continue
                factory = _Factory(budget)
                try:
                    with py7zr.SevenZipFile(document.path) as archive:
                        archive.extract(targets=[member.filename], factory=factory)
                except Exception as exc:  # noqa: BLE001 - isolate one 7z member
                    warnings.append(
                        f'{full}: 压缩成员读取失败（{type(exc).__name__}）'
                    )
                    continue
                finally:
                    for _, _, writer in factory.entries:
                        writer.stream.close()
                for name, path, _ in factory.entries:
                    visit(DiskDocument(f'{document.filename}!/{name}', path), depth + 1)
            return
        if kind == 'rar' and not configure_rar():
            warnings.append(f'{document.filename}: RAR 解码工具不可用，已保留来源并跳过')
            return
        opener = zipfile.ZipFile if kind == 'zip' else rarfile.RarFile
        try:
            with opener(document.path) as archive:
                members = archive.infolist()
                if not members:
                    warnings.append(
                        f'{document.filename}: 压缩包没有可读取成员（容器为空或损坏）'
                    )
                    return
                seen = set()
                for member in members:
                    try:
                        if member.is_dir():
                            continue
                        name = (_zip_member_name(member, warnings) if kind == 'zip'
                                else member.filename)
                        name = name.replace(chr(92), '/')
                        full = f'{document.filename}!/{name}'
                        if name in seen:
                            warnings.append(f'{full}: 重名成员已跳过')
                            continue
                        seen.add(name)
                        if member.file_size > settings.job_max_member_mb * 1024**2:
                            warnings.append(f'{full}: 单附件超过后台任务展开限制，已跳过')
                            continue
                        target = budget.target()
                        with archive.open(member) as stream:
                            budget.copy(stream, target)
                    except Exception as exc:  # noqa: BLE001 - isolate one archive member
                        name = getattr(member, 'filename', '<unknown>')
                        warnings.append(
                            f'{document.filename}!/{name}: 压缩成员读取失败（{type(exc).__name__}）'
                        )
                        continue
                    visit(DiskDocument(full, target), depth + 1)
        except Exception as exc:  # noqa: BLE001 - isolate an invalid archive container
            warnings.append(
                f'{document.filename}: 附件容器展开失败（{type(exc).__name__}）'
            )
            return

    for file in files:
        visit(file, 0)
    return leaves, warnings
