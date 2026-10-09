"""Durable local batch jobs with per-notice checkpoints and idempotent commits."""
from __future__ import annotations

import argparse
import csv
import hashlib
import io
import json
import multiprocessing
import os
import re
import subprocess
import sys
import tempfile
import time
from concurrent.futures import FIRST_COMPLETED, ProcessPoolExecutor, wait
from concurrent.futures.process import BrokenProcessPool
from pathlib import Path
from uuid import uuid4

from filelock import FileLock, Timeout

from app.bounded_runtime import (
    DeploymentLimiter,
    DeploymentLimits,
    SchedulingStopped,
    use_runtime,
)
from app.config import effective_settings, settings
from app.schemas import ImportResult, ItemCandidate
from app.storage import save_import


def write_json(path: Path, value) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    pending = path.with_name(path.name + '.' + uuid4().hex + '.pending')
    try:
        with pending.open('w', encoding='utf-8', newline='\n') as stream:
            json.dump(value, stream, ensure_ascii=False, indent=2, allow_nan=False)
            stream.write('\n')
            stream.flush()
            os.fsync(stream.fileno())
        with FileLock(str(path.with_name(path.name + '.write.lock')), timeout=300):
            pending.replace(path)
    finally:
        pending.unlink(missing_ok=True)


def write_json_array(path: Path, rows) -> None:
    """Atomically write an iterable as JSON without materializing it in memory."""
    path.parent.mkdir(parents=True, exist_ok=True)
    pending = path.with_name(path.name + '.' + uuid4().hex + '.pending')
    try:
        with pending.open('w', encoding='utf-8', newline='\n') as stream:
            stream.write('[')
            first = True
            for row in rows:
                if not first:
                    stream.write(',')
                json.dump(row, stream, ensure_ascii=False)
                first = False
            stream.write(']')
            stream.flush()
            os.fsync(stream.fileno())
        with FileLock(str(path.with_name(path.name + '.write.lock')), timeout=300):
            pending.replace(path)
    finally:
        pending.unlink(missing_ok=True)


def iter_json_array(path: Path, *, chunk_size: int = 65536):
    """Incrementally decode a JSON array so manifests do not become RAM queues."""
    decoder = json.JSONDecoder()
    with path.open('r', encoding='utf-8') as stream:
        buffer = ''
        eof = False
        position = 0
        started = False
        finished = False
        while not finished:
            if not eof and len(buffer) - position < chunk_size // 2:
                chunk = stream.read(chunk_size)
                eof = not chunk
                buffer = buffer[position:] + chunk
                position = 0
            while position < len(buffer) and buffer[position].isspace():
                position += 1
            if not started:
                if position >= len(buffer):
                    if eof:
                        raise ValueError('任务 manifest 为空')
                    continue
                if buffer[position] != '[':
                    raise ValueError('任务 manifest 格式无效')
                position += 1
                started = True
                continue
            while position < len(buffer) and buffer[position].isspace():
                position += 1
            if position < len(buffer) and buffer[position] == ',':
                position += 1
                continue
            if position < len(buffer) and buffer[position] == ']':
                position += 1
                finished = True
                continue
            try:
                value, end = decoder.raw_decode(buffer, position)
            except json.JSONDecodeError:
                if eof:
                    raise ValueError('任务 manifest 被截断')
                chunk = stream.read(chunk_size)
                eof = not chunk
                buffer = buffer[position:] + chunk
                position = 0
                continue
            position = end
            yield value
        if buffer[position:].strip() or stream.read().strip():
            raise ValueError('任务 manifest 尾部包含多余内容')


def read_json(path: Path):
    with FileLock(str(path.with_name(path.name + '.write.lock')), timeout=300):
        return json.loads(path.read_text(encoding='utf-8'))


def jobs_root() -> Path:
    return settings.resolved_database_path.parent / 'jobs'


def job_path(job_id: str) -> Path:
    if len(job_id) != 32 or any(c not in '0123456789abcdef' for c in job_id):
        raise ValueError('任务 ID 无效')
    path = jobs_root() / job_id
    if not (path / 'job.json').is_file():
        raise ValueError('任务不存在')
    return path


def file_hash(path: Path) -> str:
    with path.open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def _source_hashes_path(result_file: Path) -> Path:
    # Job progress scans *.json; keep this JSON sidecar out of that namespace.
    return result_file.with_suffix('.sources')


def _cached_source_hashes(result_file: Path) -> list[tuple[str, str, int]]:
    path = _source_hashes_path(result_file)
    if not path.is_file():
        return []
    try:
        values = read_json(path)
    except (OSError, ValueError, TypeError):
        return []
    if not isinstance(values, list):
        return []
    rows = []
    for value in values:
        if not isinstance(value, dict):
            continue
        name, digest, size = value.get('source_file'), value.get('sha256'), value.get('byte_size')
        if (isinstance(name, str) and name and isinstance(digest, str)
                and len(digest) == 64 and all(c in '0123456789abcdef' for c in digest)
                and isinstance(size, int) and not isinstance(size, bool) and size >= 0):
            rows.append((name, digest, size))
    return rows


def create_job(source: Path, database: Path, dataset_id: str, *, mode='hybrid', ocr=True) -> dict:
    jobs_root().mkdir(parents=True, exist_ok=True)
    source = source.resolve(strict=True)
    html = sorted([*source.glob('*.html'), *source.glob('*.htm')])
    if not html:
        raise ValueError('目录中没有 HTML 公告')
    job_id = uuid4().hex
    root = jobs_root() / job_id
    # Hash potentially large archives before taking the global admission lock.
    # Pause/stop/resume operations must not wait behind large-file I/O.
    notices = []
    for index, path in enumerate(html):
        paths = [path] + [path.with_suffix(suffix) for suffix in ('.zip', '.rar', '.7z')
                          if path.with_suffix(suffix).is_file()]
        notices.append({'index': index, 'name': path.stem, 'files': [
            {'path': str(p), 'name': p.name, 'sha256': file_hash(p)} for p in paths]})
    model = effective_settings()
    model_fields = (
        'model_base_url', 'model_name', 'model_max_chars', 'model_max_output_tokens',
        'model_timeout_seconds', 'model_stream_total_seconds', 'model_disable_thinking',
        'ocr_language', 'ocr_timeout_seconds',
    )
    frozen_model = {name: getattr(model, name) for name in model_fields}
    secrets = {'model_api_key': model.model_api_key}
    runtime_limits = DeploymentLimits.from_settings(settings)
    jobs_root().mkdir(parents=True, exist_ok=True)
    job = {'id': job_id, 'dataset_id': dataset_id, 'database': str(database.resolve()),
           'cache_database': str(settings.resolved_database_path), 'source': str(source),
           'mode': mode, 'ocr': ocr, 'status': 'queued', 'created_at': time.time(),
           'notices_total': len(html), 'workers': max(1, min(settings.job_workers, 8)),
           'limits': {name: getattr(settings, name) for name in (
            'job_max_expanded_mb', 'job_max_member_mb', 'job_max_archive_depth',
            'job_max_archive_files', 'pdf_max_pages', 'pdf_max_ocr_pages', 'ocr_engine',
            'document_conversion_timeout_seconds', 'libreoffice_path')},
           'runtime_limits': runtime_limits.identity(),
           'model_name': frozen_model['model_name'],
           'model_configured': bool(frozen_model['model_base_url'] and secrets['model_api_key']
                                    and frozen_model['model_name']),
           'model_config_sha256': hashlib.sha256(json.dumps({
               **frozen_model,
               'model_api_key_sha256': hashlib.sha256(
                   secrets['model_api_key'].encode()).hexdigest(),
           }, sort_keys=True).encode()).hexdigest()}
    queue_lock = FileLock(str(jobs_root() / 'admission.lock'), timeout=300)
    with queue_lock:
        queued = 0
        for path in jobs_root().glob('*/job.json'):
            try:
                if read_json(path).get('status') == 'queued':
                    queued += 1
            except (OSError, ValueError, TypeError):
                continue
        if queued >= runtime_limits.queue_capacity:
            raise JobQueueFull('后台任务队列已满，请稍后重试')
        root.mkdir(parents=True, exist_ok=False)
        try:
            root.chmod(0o700)
        except OSError:
            pass
        write_json_array(root / 'manifest.json', notices)
        write_json(root / 'model-config.json', frozen_model)
        write_private_json(root / 'model-secret.json', secrets)
        write_json(root / 'job.json', job)
    return job


class JobQueueFull(ValueError):
    """Deployment admission is full; the caller may retry after queue drain."""


def write_private_json(path: Path, value) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    pending = path.with_name(path.name + '.' + uuid4().hex + '.pending')
    try:
        # Restrict the empty temporary file before writing the API credential.
        with pending.open('x', encoding='utf-8', newline='\n'):
            pass
        if os.name == 'nt':
            system_root = Path(os.environ.get('SystemRoot', r'C:\Windows'))
            whoami = system_root / 'System32' / 'whoami.exe'
            icacls = system_root / 'System32' / 'icacls.exe'
            identity = subprocess.run(
                [str(whoami), '/user', '/fo', 'csv', '/nh'],
                check=True, capture_output=True, text=True, encoding='utf-8', errors='replace',
            )
            row = next(csv.reader(io.StringIO(identity.stdout.strip())), [])
            sid = row[-1] if row else ''
            if not re.fullmatch(r'S-1-[0-9-]+', sid):
                raise RuntimeError('无法确认当前 Windows 用户 SID；拒绝保存任务 API Key')
            subprocess.run(
                [str(icacls), str(pending), '/inheritance:r', '/grant:r',
                 f'*{sid}:(F)', '*S-1-5-18:(F)', '*S-1-5-32-544:(F)'],
                check=True, capture_output=True, text=True, encoding='utf-8', errors='replace',
            )
        else:
            os.chmod(pending, 0o600)
        with pending.open('w', encoding='utf-8', newline='\n') as stream:
            json.dump(value, stream, ensure_ascii=False, indent=2, allow_nan=False)
            stream.write('\n')
            stream.flush()
            os.fsync(stream.fileno())
        with FileLock(str(path.with_name(path.name + '.write.lock')), timeout=300):
            pending.replace(path)
    finally:
        pending.unlink(missing_ok=True)


class JobStopped(Exception):
    pass


class RetryableModelError(RuntimeError):
    """A model request failed before the notice could be committed."""

    def __init__(self, warnings: list[str]):
        self.warnings = warnings
        super().__init__('；'.join(warnings))


def _cached_result(path: Path) -> ImportResult | None:
    if not path.exists():
        return None
    try:
        return ImportResult.model_validate(read_json(path))
    except (OSError, ValueError, TypeError):
        # Result files are an optimization/checkpoint, not the source of truth.
        # Rebuild a damaged result; save_import's receipt prevents duplicate rows
        # if the prior worker committed just before the file was damaged.
        return None


def _retryable_model_warnings(result: ImportResult) -> list[str]:
    return [warning for warning in result.warnings
            if '模型抽取失败' in warning or '模型返回空内容' in warning]


def _read_progress_entry(path: Path) -> dict | None:
    try:
        value = read_json(path)
    except (OSError, ValueError, TypeError):
        return None
    if (not isinstance(value, dict)
            or not isinstance(value.get('status'), str)
            or value.get('status') not in {'pending', 'running', 'done', 'failed'}
            or not isinstance(value.get('name'), str)):
        return None
    if 'items' in value and (
        isinstance(value['items'], bool)
        or not isinstance(value['items'], int)
        or value['items'] < 0
    ):
        return None
    return value


def _recover_notice_telemetry(checkpoint: Path, row: dict) -> dict:
    try:
        telemetry = read_json(checkpoint.with_suffix('.telemetry.json'))
    except (OSError, ValueError, TypeError):
        return row
    calls = telemetry.get('model_calls') if isinstance(telemetry, dict) else None
    if not isinstance(calls, list) or not all(isinstance(call, dict) for call in calls):
        return row
    row.update(
        model_calls=calls,
        historical_cached_requests=len(telemetry.get('historical_model_calls') or []),
        provider_kv_tokens=telemetry.get('provider_kv_tokens', 0),
        api_usage={
            'requests': len(calls),
            'successful_responses': sum(call.get('transport') == 'success' for call in calls),
            'prompt_tokens': sum(call.get('prompt_tokens') or 0 for call in calls) or None,
            'completion_tokens': sum(call.get('completion_tokens') or 0 for call in calls) or None,
        },
    )
    return row


def read_notice_progress(root: Path) -> list[dict]:
    entries = []
    for path in sorted((root / 'notices').glob('*.json')):
        if path.name.endswith(('.result.json', '.telemetry.json')):
            continue
        entry = _read_progress_entry(path)
        if entry is None:
            # Surface the damaged checkpoint without letting it break list/report
            # requests. run_job treats it as pending and replaces it on recovery.
            entries.append({'index': path.stem, 'name': path.stem, 'status': 'failed',
                            'error': '公告进度文件损坏；续跑时将重新处理'})
        else:
            entries.append(entry)
    return entries


def process_notice(root_string: str, entry: dict) -> dict:
    root = Path(root_string)
    (root / 'notices').mkdir(parents=True, exist_ok=True)
    lease = root / 'notices' / f"{entry['index']:05d}.work.lock"
    with FileLock(str(lease), timeout=1200):
        return _process_notice(root_string, entry)


def _process_notice(root_string: str, entry: dict) -> dict:
    from app import ingestion
    from app.archive_files import DiskDocument, expand_paths
    from app.parsers import SourceDocument, parse_document_with_participants
    from app.schemas import ParticipantCandidate

    root = Path(root_string)
    job = read_json(root / 'job.json')
    settings.database_path = job['cache_database']
    for name, value in job['limits'].items():
        setattr(settings, name, value)
    current_limits = DeploymentLimits.from_settings(settings)
    if job.get('runtime_limits') and job['runtime_limits'] != current_limits.identity():
        raise RuntimeError('部署并发配置与任务创建时不同；请恢复原配置后续跑')
    try:
        frozen_model = read_json(root / 'model-config.json')
        frozen_secret = read_json(root / 'model-secret.json')
    except FileNotFoundError:
        # Compatibility for jobs created before model configuration was frozen.
        frozen = effective_settings()
        frozen_model = {name: getattr(frozen, name) for name in (
            'model_base_url', 'model_name', 'model_max_chars', 'model_max_output_tokens',
            'model_timeout_seconds', 'model_stream_total_seconds', 'model_disable_thinking')}
        frozen_secret = {'model_api_key': frozen.model_api_key}
    job['frozen_model_settings'] = {**frozen_model, **frozen_secret}
    checkpoint = root / 'notices' / f"{entry['index']:05d}.json"
    result_file = checkpoint.with_suffix('.result.json')
    previous_attempt = _read_progress_entry(checkpoint) if checkpoint.exists() else None
    if previous_attempt and previous_attempt.get('status') != 'done':
        previous_attempt = _recover_notice_telemetry(checkpoint, previous_attempt)
        write_json(checkpoint, previous_attempt)
    checkpoint.with_suffix('.telemetry.json').unlink(missing_ok=True)
    history = list((previous_attempt or {}).get('attempt_history') or [])
    if previous_attempt and previous_attempt.get('attempt'):
        history.append({key: previous_attempt[key] for key in (
            'attempt', 'status', 'error', 'warnings', 'model_calls', 'api_usage',
            'started_at', 'finished_at', 'seconds',
        ) if key in previous_attempt})
    started = time.time()
    status = {'index': entry['index'], 'name': entry['name'], 'status': 'running',
              'attempt': int((previous_attempt or {}).get('attempt', 0)) + 1,
              'attempt_history': history, 'started_at': started,
              'current_file': None, 'documents_done': 0}
    write_json(checkpoint, status)
    cache = Path(job['cache_database']).parent / 'parse-cache'
    cache.mkdir(parents=True, exist_ok=True)
    original_parse = ingestion.parse_document_with_participants
    from app import parsers as parser_module

    parser_code_hashes = {}
    for parser_path in (Path(ingestion.__file__), Path(parser_module.__file__)):
        try:
            with parser_path.open('rb') as code:
                parser_code_hashes[parser_path.name] = hashlib.file_digest(code, 'sha256').hexdigest()
        except OSError:
            parser_code_hashes[parser_path.name] = 'unavailable'
    limiter = DeploymentLimiter(current_limits)
    stop_path = root / 'stop'

    def cached_parse(document, **options):
        with limiter.document_slot(stop_path=stop_path):
            return parse_under_lease(document, options)

    def parse_under_lease(document, options):
        if stop_path is not None and Path(stop_path).exists():
            raise JobStopped()
        status.update(current_file=document.filename, worker_pid=os.getpid())
        write_json(checkpoint, status)
        content = document.content
        suffix = Path(document.filename).suffix
        # Bump this whenever parser output semantics change. Reusing an older
        # cached result would silently preserve stale package codes/items.
        identity = json.dumps({'version': 4, 'parser_sha256': parser_code_hashes,
                               'suffix': suffix, 'options': options,
                               'limits': job['limits']}, sort_keys=True)
        digest = hashlib.sha256(identity.encode() + content).hexdigest()
        path = cache / (digest + '.json')
        with FileLock(str(path.with_suffix('.lock')), timeout=1200):
            stored = None
            if path.exists():
                try:
                    stored = read_json(path)
                    if (not isinstance(stored, dict) or not isinstance(stored.get('text'), str)
                            or not isinstance(stored.get('items'), list)
                            or not isinstance(stored.get('participants'), list)
                            or not isinstance(stored.get('warnings'), list)
                            or not all(isinstance(warning, str) for warning in stored['warnings'])):
                        stored = None
                    else:
                        for row in stored['items']:
                            ItemCandidate.model_validate(row)
                        for row in stored['participants']:
                            ParticipantCandidate.model_validate(row)
                except (OSError, ValueError, TypeError):
                    stored = None
            if stored is None:
                text, items, participants, warnings = parse_document_with_participants(
                    SourceDocument('document' + suffix, content), **options,
                )
                stored = {'text': text, 'items': [row.model_dump(mode='json') for row in items],
                          'participants': [row.model_dump(mode='json') for row in participants],
                          'warnings': warnings}
                if not any(any(word in w for word in ('失败', '超时', '未安装', '缺少')) for w in warnings):
                    write_json(path, stored)
        items = [ItemCandidate.model_validate(row).model_copy(update={'source_file': document.filename})
                 for row in stored['items']]
        participants = [
            ParticipantCandidate.model_validate(row).model_copy(update={'source_file': document.filename})
            for row in stored['participants']
        ]
        warnings = [w.replace('document' + suffix, document.filename) for w in stored['warnings']]
        status['documents_done'] += 1
        write_json(checkpoint, status)
        return stored['text'], items, participants, warnings

    try:
        if (root / 'stop').exists():
            raise JobStopped()
        for file in entry['files']:
            if file_hash(Path(file['path'])) != file['sha256']:
                raise ValueError('源文件与任务创建时不一致，请创建新任务')
        with use_runtime(current_limits, stop_path=root / 'stop'):
            result = (_cached_result(result_file)
                      if result_file.exists() and not entry.get('replace_existing') else None)
            if result is None:
                (root / 'notices').mkdir(exist_ok=True)
                with FileLock(str(checkpoint.with_suffix('.lock')), timeout=1200):
                    result = (_cached_result(result_file)
                              if result_file.exists() and not entry.get('replace_existing') else None)
                    if result is None and result_file.exists():
                        result_file.unlink(missing_ok=True)
                if result is None:
                    result = _build_result(root, entry, job, checkpoint, result_file, status, cached_parse,
                                           ingestion, expand_paths, DiskDocument, limiter, stop_path)
            retryable_warnings = _retryable_model_warnings(result)
            if retryable_warnings:
                result_file.unlink(missing_ok=True)
                raise RetryableModelError(retryable_warnings)
            # Catch source replacement during archive expansion or parsing, not
            # only changes that happened before the first read.
            for file in entry['files']:
                if file_hash(Path(file['path'])) != file['sha256']:
                    raise ValueError('处理期间源文件发生变化；未导入结果，请创建新任务')
            source_key = job['id'] + ':' + str(entry['index'])
            saved = save_import(Path(job['database']), result, source_key=source_key,
                                replace_existing=bool(entry.get('replace_existing')),
                                source_hashes=_cached_source_hashes(result_file))
            status.update(status='done', notice_id=saved.notice_id, items=result.items_found,
                          participants=len(result.participants), warnings=result.warnings,
                          source_files=len(result.source_files))
    except (JobStopped, SchedulingStopped):
        status.update(status='pending', error='用户请求暂停；在途公告已结束或续跑将复用缓存')
    except RetryableModelError as exc:
        status.update(status='failed', error=f'模型抽取可重试失败：{exc}', warnings=exc.warnings)
    except Exception as exc:  # noqa: BLE001 - per-notice isolation
        message = str(exc)
        # Provider response text can echo credentials or request headers.
        if frozen_secret.get('model_api_key'):
            message = message.replace(frozen_secret['model_api_key'], '[已隐藏]')
        status.update(status='failed', error=f'{type(exc).__name__}: {message}')
    finally:
        ingestion.parse_document_with_participants = original_parse
    status.update(finished_at=time.time(), seconds=round(time.time() - started, 3), current_file=None)
    write_json(checkpoint, status)
    return status


def _build_result(root, entry, job, checkpoint, result_file, status, cached_parse,
                  ingestion, expand_paths, DiskDocument, limiter, stop_path):

    with FileLock(str(checkpoint.with_suffix('.lock')), timeout=1200):
        if result_file.exists() and not entry.get('replace_existing'):
            result = _cached_result(result_file)
            if result is not None:
                return result
            result_file.unlink(missing_ok=True)
        with tempfile.TemporaryDirectory(prefix='expand-', dir=root) as temporary:
            # Expansion and parser work share one deployment-wide CPU permit;
            # the permit is released before model calls so requests can overlap.
            with limiter.document_slot(stop_path=stop_path):
                expanded, warnings = expand_paths([DiskDocument(f['name'], Path(f['path']))
                                                   for f in entry['files']], Path(temporary))
            status['documents_total'] = len(expanded)
            ingestion.parse_document_with_participants = cached_parse
            frozen = job['frozen_model_settings']
            model_settings = settings.model_copy(update={
                **frozen, 'ocr_enabled': job['ocr'], 'extraction_mode': job['mode']})
            if job['mode'] == 'rules':
                model_settings = model_settings.model_copy(update={
                    'model_base_url': '', 'model_api_key': '', 'model_name': ''})
            from app.job_cache import instrumented_notice_cache

            telemetry_path = checkpoint.with_suffix('.telemetry.json')
            with instrumented_notice_cache(
                Path(job['cache_database']).parent / 'bounded-cache',
                job['mode'], model_settings, job['limits'],
                stop_path=stop_path, telemetry_path=telemetry_path,
                cache_parsing=False,
            ) as cache_stats:
                try:
                    result = ingestion._ingest_expanded(
                        expanded, warnings, extraction_mode=job['mode'],
                        model_settings=model_settings,
                    )
                finally:
                    status.update(
                        model_calls=cache_stats['model_calls'],
                        historical_cached_requests=len(cache_stats['historical_model_calls']),
                        provider_kv_tokens=cache_stats['provider_kv_tokens'],
                        model_cache_hits=cache_stats['model_cache_hits'],
                        model_cache_misses=cache_stats['model_cache_misses'],
                        api_usage={
                            'requests': len(cache_stats['model_calls']),
                            'successful_responses': sum(
                                call.get('transport') == 'success'
                                for call in cache_stats['model_calls']),
                            'prompt_tokens': sum(
                                call['prompt_tokens'] for call in cache_stats['model_calls']
                                if call.get('prompt_tokens') is not None) or None,
                            'completion_tokens': sum(
                                call['completion_tokens'] for call in cache_stats['model_calls']
                                if call.get('completion_tokens') is not None) or None,
                        },
                    )
                    write_json(checkpoint, status)
            if _retryable_model_warnings(result):
                raise RetryableModelError(_retryable_model_warnings(result))
            source_hashes = [
                {'source_file': document.filename,
                 'sha256': document.source_sha256 or file_hash(document.path),
                 'byte_size': document.source_size
                 if document.source_size is not None else document.path.stat().st_size}
                for document in expanded
            ]
            write_json(_source_hashes_path(result_file), source_hashes)
            write_json(result_file, result.model_dump(mode='json'))
            return result
def snapshot(root: Path) -> dict:
    job = read_json(root / 'job.json')
    entries = read_notice_progress(root)
    job.update(done=sum(e['status'] == 'done' for e in entries),
               failed=sum(e['status'] == 'failed' for e in entries),
               items=sum(e.get('items', 0) for e in entries),
               active=[e for e in entries if e['status'] == 'running'],
               errors=[{'name': e['name'], 'error': e.get('error')} for e in entries if e['status']=='failed'])
    if job['status'] == 'running' and time.time() - job.get('heartbeat', 0) > 30:
        job['status'] = 'interrupted'
    return job


def job_state_lock(root: Path) -> FileLock:
    return FileLock(str(root / 'state.lock'), timeout=300)


def _finalize_job_run(root: Path, job: dict, state: dict) -> None:
    """Commit the run outcome without losing a concurrent resume request."""
    resume_path = root / 'resume-request.json'
    with job_state_lock(root):
        if (root / 'cancel').exists():
            final_status = 'stopped'
        elif (root / 'stop').exists():
            final_status = 'paused'
        elif state['failed']:
            final_status = 'completed_with_errors'
        elif state['done'] < job['notices_total']:
            final_status = 'interrupted'
        else:
            final_status = 'completed'
        try:
            resume_request = read_json(resume_path)
        except (OSError, ValueError, TypeError):
            resume_request = None
        if resume_request and final_status != 'completed':
            job.update(
                status='queued',
                queued_at=time.time(),
                retry_failed_request=bool(resume_request.get('retry_failed')),
                resume_requested_at=resume_request.get('requested_at'),
            )
            job.pop('finished_at', None)
        else:
            job.update(status=final_status, finished_at=time.time())
            job.pop('retry_failed_request', None)
        resume_path.unlink(missing_ok=True)
        job.pop('error', None)
        write_json(root / 'job.json', job)


def _run_job(root: Path, *, retry_failed=False, wait_for_lock=False):
    lock = FileLock(str(root / 'run.lock'), timeout=-1 if wait_for_lock else 0)
    with lock:
        job = read_json(root / 'job.json')
        current_limits = DeploymentLimits.from_settings(settings)
        if job.get('runtime_limits') and job['runtime_limits'] != current_limits.identity():
            job.update(status='failed', finished_at=time.time(),
                       error='部署并发配置与任务创建时不同；请恢复原配置后续跑')
            write_json(root / 'job.json', job)
            return
        if (root / 'stop').exists():
            if wait_for_lock:
                (root / 'stop').unlink()
            else:
                job.update(status='paused', finished_at=time.time())
                write_json(root / 'job.json', job)
                return
        def pending_entries():
            for entry in iter_json_array(root / 'manifest.json'):
                path = root / 'notices' / f"{entry['index']:05d}.json"
                prior = _read_progress_entry(path) if path.exists() else {}
                if prior is None:
                    prior = {}
                if prior.get('status') == 'done' or prior.get('status') == 'failed' and not retry_failed:
                    continue
                if prior.get('status') == 'running':
                    prior.update(status='pending',
                                 error='检测到上次进程未完成；已保存的结果收据会防止重复入库')
                    write_json(path, prior)
                yield entry

        pending = iter(pending_entries())
        exhausted = False
        job.update(status='running', heartbeat=time.time())
        write_json(root / 'job.json', job)
        max_active = max(1, min(job['workers'], settings.job_workers,
                                current_limits.queue_capacity))
        limiter = DeploymentLimiter(current_limits)
        with ProcessPoolExecutor(
            max_workers=max_active,
            mp_context=multiprocessing.get_context('spawn'),
        ) as pool:
            active = {}
            try:
                while not exhausted or active:
                    while not exhausted and len(active) < max_active and not (root / 'stop').exists():
                        permit = limiter.try_acquire('queue')
                        if permit is None:
                            break
                        try:
                            entry = next(pending)
                        except StopIteration:
                            permit.release()
                            exhausted = True
                            break
                        try:
                            future = pool.submit(process_notice, str(root), entry)
                        except BaseException:
                            permit.release()
                            raise
                        active[future] = (entry, permit)
                    if not active:
                        if not exhausted and not (root / 'stop').exists():
                            time.sleep(0.1)
                            job['heartbeat'] = time.time()
                            write_json(root / 'job.json', job)
                            continue
                        break
                    done, _ = wait(active, timeout=2, return_when=FIRST_COMPLETED)
                    for future in done:
                        entry, permit = active.pop(future)
                        permit.release()
                        try:
                            future.result()
                        except Exception as exc:  # noqa: BLE001
                            checkpoint = root / 'notices' / f"{entry['index']:05d}.json"
                            saved = _read_progress_entry(checkpoint)
                            # A sibling may have committed and checkpointed before
                            # this process pool broke. Never replace that success.
                            if saved and saved.get('status') == 'done':
                                continue
                            saved = _recover_notice_telemetry(checkpoint, saved or {})
                            if isinstance(exc, BrokenProcessPool):
                                (root / 'stop').touch(exist_ok=True)
                                state = {**saved, 'index': entry['index'], 'name': entry['name'],
                                         'status': 'pending',
                                         'error': '文档 worker 意外退出；续跑将复用成功检查点'}
                            elif future.cancelled():
                                state = {**saved, 'index': entry['index'], 'name': entry['name'],
                                         'status': 'pending', 'error': '任务已取消，续跑将重新调度'}
                            else:
                                state = {**saved, 'index': entry['index'], 'name': entry['name'],
                                         'status': 'failed', 'error': type(exc).__name__}
                            write_json(root / 'notices' / f"{entry['index']:05d}.json", state)
                    job['heartbeat'] = time.time()
                    write_json(root / 'job.json', job)
            except (KeyboardInterrupt, BrokenProcessPool):
                (root / 'stop').touch(exist_ok=True)
                for future in active:
                    future.cancel()
                # Running workers are allowed to finish the current operation;
                # future results/checkpoints are drained by the normal loop.
                while active:
                    done, _ = wait(active, timeout=2, return_when=FIRST_COMPLETED)
                    for future in done:
                        entry, permit = active.pop(future)
                        permit.release()
                        try:
                            future.result()
                        except Exception as exc:  # noqa: BLE001
                            checkpoint = root / 'notices' / f"{entry['index']:05d}.json"
                            saved = _read_progress_entry(checkpoint)
                            if saved and saved.get('status') == 'done':
                                continue
                            saved = _recover_notice_telemetry(checkpoint, saved or {})
                            if isinstance(exc, BrokenProcessPool):
                                state = {**saved, 'index': entry['index'], 'name': entry['name'],
                                         'status': 'pending',
                                         'error': '文档 worker 意外退出；续跑将复用成功检查点'}
                                write_json(root / 'notices' / f"{entry['index']:05d}.json", state)
                                continue
                            state = {**saved, 'index': entry['index'], 'name': entry['name'],
                                     'status': 'pending' if future.cancelled() else 'failed',
                                     'error': ('任务已取消，续跑将重新调度' if future.cancelled()
                                               else type(exc).__name__)}
                            write_json(root / 'notices' / f"{entry['index']:05d}.json", state)
        state = snapshot(root)
        _finalize_job_run(root, job, state)


def run_job(root: Path, *, retry_failed=False, wait_for_lock=False,
            _coordinator_owned=False):
    """Run one requested job under the single deployment owner lock."""
    if _coordinator_owned:
        return _run_job(root, retry_failed=retry_failed, wait_for_lock=wait_for_lock)
    with FileLock(str(_coordinator_lock_path()), timeout=-1):
        return _run_job(root, retry_failed=retry_failed, wait_for_lock=wait_for_lock)


def _coordinator_lock_path() -> Path:
    runtime_dir = DeploymentLimits.from_settings(settings).runtime_dir
    runtime_dir.mkdir(parents=True, exist_ok=True)
    return runtime_dir / 'jobs-coordinator.lock'


def _job_owner_active(root: Path) -> bool:
    """Probe the per-job process lock without relying on a stale status field."""
    lock = FileLock(str(root / 'run.lock'), timeout=0)
    try:
        lock.acquire()
    except Timeout:
        return True
    lock.release()
    return False


def _recover_orphaned_jobs() -> None:
    """Requeue jobs left running by a dead coordinator; checkpoints drive resume."""
    with FileLock(str(jobs_root() / 'admission.lock'), timeout=300):
        _recover_orphaned_jobs_locked()


def _recover_orphaned_jobs_locked() -> None:
    limits = DeploymentLimits.from_settings(settings)
    queued = 0
    for path in jobs_root().glob('*/job.json'):
        try:
            job = read_json(path)
            if job.get('status') == 'queued':
                queued += 1
        except (OSError, ValueError, TypeError):
            continue
    for path in jobs_root().glob('*/job.json'):
        if queued >= limits.queue_capacity:
            break
        root = path.parent
        if (root / 'stop').exists():
            continue
        try:
            job = read_json(path)
            if job.get('status') != 'running' or _job_owner_active(root):
                continue
            job.update(status='queued', queued_at=time.time(),
                       recovered_at=time.time(),
                       recovery_note='服务重启后从公告检查点恢复')
            write_json(path, job)
            queued += 1
        except (OSError, ValueError, TypeError, Timeout):
            continue


def run_coordinator(*, idle_seconds: float = 3.0) -> None:
    """Own the deployment job queue and serialize durable job-state transitions."""
    lock = FileLock(str(_coordinator_lock_path()), timeout=15)
    try:
        lock.acquire()
    except Timeout:
        return
    idle_since = time.monotonic()
    try:
        while True:
            _recover_orphaned_jobs()
            queued: list[tuple[float, Path]] = []
            for path in jobs_root().glob('*/job.json'):
                try:
                    job = read_json(path)
                except (OSError, ValueError, TypeError):
                    continue
                if job.get('status') == 'queued' and not (path.parent / 'stop').exists():
                    queued.append((float(job.get('created_at', 0)), path.parent))
            if not queued:
                if time.monotonic() - idle_since >= idle_seconds:
                    return
                time.sleep(0.2)
                continue
            _, root = min(queued, key=lambda item: (item[0], item[1].name))
            idle_since = time.monotonic()
            try:
                job = read_json(root / 'job.json')
                run_job(root, retry_failed=bool(job.get('retry_failed_request')),
                        _coordinator_owned=True)
            except Exception as exc:  # noqa: BLE001 - isolate one job from the queue
                job = read_json(root / 'job.json')
                try:
                    api_key = read_json(root / 'model-secret.json').get('model_api_key', '')
                except (OSError, ValueError, TypeError):
                    api_key = ''
                message = str(exc).replace(api_key, '[已隐藏]') if api_key else str(exc)
                job.update(status='failed', finished_at=time.time(),
                           error=f'{type(exc).__name__}: {message}')
                write_json(root / 'job.json', job)
    finally:
        lock.release()


def launch(root: Path, *, retry_failed=False):
    command = [sys.executable, '-m', 'app.jobs', 'serve']
    with (root / 'worker.log').open('ab') as log:
        subprocess.Popen(command, cwd=Path(__file__).resolve().parents[1],
                         stdout=log, stderr=log, stdin=subprocess.DEVNULL,
                         creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0),
                         start_new_session=os.name != 'nt')


def queue_job(root: Path, *, retry_failed=False) -> dict:
    """Requeue a stopped/interrupted job atomically for the single owner."""
    with FileLock(str(jobs_root() / 'admission.lock'), timeout=300), job_state_lock(root):
        job = read_json(root / 'job.json')
        if job.get('status') == 'running' and _job_owner_active(root):
            limits = DeploymentLimits.from_settings(settings)
            queued = 0
            for path in jobs_root().glob('*/job.json'):
                if path.parent == root:
                    continue
                try:
                    queued += read_json(path).get('status') == 'queued'
                except (OSError, ValueError, TypeError):
                    continue
            if queued >= limits.queue_capacity:
                raise JobQueueFull('后台任务队列已满，请稍后重试')
            # The active coordinator may already have observed the stop flag
            # and be finalizing. Record intent under the same state lock used
            # by _finalize_job_run, so either it consumes this request or we
            # see its terminal state and enqueue normally.
            write_json(root / 'resume-request.json', {
                'retry_failed': bool(retry_failed),
                'requested_at': time.time(),
            })
            (root / 'stop').unlink(missing_ok=True)
            (root / 'cancel').unlink(missing_ok=True)
            return job
        if job.get('status') == 'running':
            job.update(status='interrupted',
                       recovery_note='检测到协调器已退出；续跑将复用成功检查点')
            write_json(root / 'job.json', job)
        limits = DeploymentLimits.from_settings(settings)
        queued = 0
        for path in jobs_root().glob('*/job.json'):
            try:
                if path.parent != root and read_json(path).get('status') == 'queued':
                    queued += 1
            except (OSError, ValueError, TypeError):
                continue
        if queued >= limits.queue_capacity:
            raise JobQueueFull('后台任务队列已满，请稍后重试')
        (root / 'stop').unlink(missing_ok=True)
        (root / 'cancel').unlink(missing_ok=True)
        (root / 'resume-request.json').unlink(missing_ok=True)
        job.update(status='queued', queued_at=time.time(),
                   retry_failed_request=bool(retry_failed))
        write_json(root / 'job.json', job)
        return job


def main():
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest='action', required=True)
    create = sub.add_parser('create')
    create.add_argument('source', type=Path)
    create.add_argument('--name', required=True)
    create.add_argument('--mode', choices=['rules', 'hybrid', 'model'], default='hybrid')
    create.add_argument('--no-ocr', action='store_true')
    create.add_argument('--start', action='store_true')
    run = sub.add_parser('run')
    run.add_argument('root', type=Path)
    run.add_argument('--retry-failed', action='store_true')
    run.add_argument('--wait-for-lock', action='store_true')
    status = sub.add_parser('status')
    status.add_argument('id')
    sub.add_parser('serve')
    args = parser.parse_args()
    if args.action == 'create':
        from app.datasets import DatasetCreate, create_dataset, resolve_database
        dataset = create_dataset(DatasetCreate(name=args.name))
        job = create_job(args.source, resolve_database(dataset['id']), dataset['id'],
                         mode=args.mode, ocr=not args.no_ocr)
        if args.start:
            launch(job_path(job['id']))
        print(json.dumps(job, ensure_ascii=False))
    elif args.action == 'run':
        try:
            run_job(args.root, retry_failed=args.retry_failed,
                    wait_for_lock=args.wait_for_lock)
        except Timeout:
            print('任务已在运行')
    elif args.action == 'serve':
        run_coordinator()
    else:
        print(json.dumps(snapshot(job_path(args.id)), ensure_ascii=False))


if __name__ == '__main__':
    main()
