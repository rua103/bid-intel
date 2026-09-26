"""Durable local batch jobs with per-notice checkpoints and idempotent commits."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
import tempfile
import time
from concurrent.futures import FIRST_COMPLETED, ProcessPoolExecutor, wait
from pathlib import Path
from uuid import uuid4

from filelock import FileLock, Timeout

from app.config import effective_settings, settings
from app.schemas import ImportResult, ItemCandidate
from app.storage import save_import


def write_json(path: Path, value) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    pending = path.with_name(path.name + '.' + uuid4().hex + '.pending')
    pending.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding='utf-8')
    with FileLock(str(path.with_name(path.name + '.write.lock')), timeout=300):
        pending.replace(path)


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


def create_job(source: Path, database: Path, dataset_id: str, *, mode='rules', ocr=True) -> dict:
    source = source.resolve(strict=True)
    html = sorted([*source.glob('*.html'), *source.glob('*.htm')])
    if not html:
        raise ValueError('目录中没有 HTML 公告')
    job_id = uuid4().hex
    root = jobs_root() / job_id
    notices = []
    for index, path in enumerate(html):
        paths = [path] + [path.with_suffix(suffix) for suffix in ('.zip', '.rar', '.7z')
                          if path.with_suffix(suffix).is_file()]
        notices.append({'index': index, 'name': path.stem, 'files': [
            {'path': str(p), 'name': p.name, 'sha256': file_hash(p)} for p in paths]})
    job = {'id': job_id, 'dataset_id': dataset_id, 'database': str(database.resolve()),
           'cache_database': str(settings.resolved_database_path), 'source': str(source),
           'mode': mode, 'ocr': ocr, 'status': 'queued', 'created_at': time.time(),
           'notices_total': len(notices), 'workers': max(1, min(settings.job_workers, 8)),
           'limits': {name: getattr(settings, name) for name in (
               'job_max_expanded_mb', 'job_max_member_mb', 'job_max_archive_depth',
               'job_max_archive_files', 'pdf_max_pages', 'pdf_max_ocr_pages', 'ocr_engine')}}
    write_json(root / 'manifest.json', notices)
    write_json(root / 'job.json', job)
    return job


class JobStopped(Exception):
    pass


def process_notice(root_string: str, entry: dict) -> dict:
    from app import ingestion
    from app.archive_files import DiskDocument, expand_paths
    from app.parsers import SourceDocument, parse_document

    root = Path(root_string)
    job = read_json(root / 'job.json')
    settings.database_path = job['cache_database']
    for name, value in job['limits'].items():
        setattr(settings, name, value)
    checkpoint = root / 'notices' / f"{entry['index']:05d}.json"
    result_file = checkpoint.with_suffix('.result.json')
    started = time.time()
    status = {'index': entry['index'], 'name': entry['name'], 'status': 'running',
              'started_at': started, 'current_file': None, 'documents_done': 0}
    write_json(checkpoint, status)
    cache = Path(job['cache_database']).parent / 'parse-cache'
    cache.mkdir(parents=True, exist_ok=True)
    original_parse = ingestion.parse_document

    def cached_parse(document, **options):
        if (root / 'stop').exists():
            raise JobStopped()
        status.update(current_file=document.filename, worker_pid=os.getpid())
        write_json(checkpoint, status)
        content = document.content
        suffix = Path(document.filename).suffix
        identity = json.dumps({'version': 1, 'suffix': suffix, 'options': options,
                               'limits': job['limits']}, sort_keys=True)
        digest = hashlib.sha256(identity.encode() + content).hexdigest()
        path = cache / (digest + '.json')
        with FileLock(str(path.with_suffix('.lock')), timeout=1200):
            stored = None
            if path.exists():
                try:
                    stored = read_json(path)
                except ValueError:
                    pass
            if stored is None:
                text, items, warnings = parse_document(SourceDocument('document' + suffix, content), **options)
                stored = {'text': text, 'items': [row.model_dump(mode='json') for row in items],
                          'warnings': warnings}
                if not any(any(word in w for word in ('失败', '超时', '未安装', '缺少')) for w in warnings):
                    write_json(path, stored)
        items = [ItemCandidate.model_validate(row).model_copy(update={'source_file': document.filename})
                 for row in stored['items']]
        warnings = [w.replace('document' + suffix, document.filename) for w in stored['warnings']]
        status['documents_done'] += 1
        write_json(checkpoint, status)
        return stored['text'], items, warnings

    try:
        if result_file.exists() and not entry.get('replace_existing'):
            result = ImportResult.model_validate(read_json(result_file))
        else:
            (root / 'notices').mkdir(exist_ok=True)
            with FileLock(str(checkpoint.with_suffix('.lock')), timeout=1200):
                if result_file.exists() and not entry.get('replace_existing'):
                    result = ImportResult.model_validate(read_json(result_file))
                else:
                    result = None
            if result is None:
                result = _build_result(root, entry, job, checkpoint, result_file, status, cached_parse,
                                       ingestion, expand_paths, DiskDocument)
        source_key = job['id'] + ':' + str(entry['index'])
        saved = save_import(Path(job['database']), result, source_key=source_key,
                            replace_existing=bool(entry.get('replace_existing')))
        status.update(status='done', notice_id=saved.notice_id, items=result.items_found,
                      participants=len(result.participants), warnings=result.warnings,
                      source_files=len(result.source_files))
    except JobStopped:
        status.update(status='pending', error='用户请求暂停，续跑将复用解析缓存')
    except Exception as exc:  # noqa: BLE001 - per-notice isolation
        status.update(status='failed', error=f'{type(exc).__name__}: {exc}')
    finally:
        ingestion.parse_document = original_parse
    status.update(finished_at=time.time(), seconds=round(time.time() - started, 3), current_file=None)
    write_json(checkpoint, status)
    return status


def _build_result(root, entry, job, checkpoint, result_file, status, cached_parse,
                  ingestion, expand_paths, DiskDocument):

    for file in entry['files']:
        if file_hash(Path(file['path'])) != file['sha256']:
            raise ValueError('源文件与任务创建时不一致，请创建新任务')
    with FileLock(str(checkpoint.with_suffix('.lock')), timeout=1200):
        if result_file.exists() and not entry.get('replace_existing'):
            return ImportResult.model_validate(read_json(result_file))
        with tempfile.TemporaryDirectory(prefix='expand-', dir=root) as temporary:
            expanded, warnings = expand_paths([DiskDocument(f['name'], Path(f['path']))
                                               for f in entry['files']], Path(temporary))
            status['documents_total'] = len(expanded)
            ingestion.parse_document = cached_parse
            model_settings = effective_settings().model_copy(update={
                'ocr_enabled': job['ocr'], 'extraction_mode': job['mode']})
            if job['mode'] == 'rules':
                model_settings = model_settings.model_copy(update={
                    'model_base_url': '', 'model_api_key': '', 'model_name': ''})
            result = ingestion._ingest_expanded(expanded, warnings,
                extraction_mode=job['mode'], model_settings=model_settings)
            write_json(result_file, result.model_dump(mode='json'))
            return result
def snapshot(root: Path) -> dict:
    job = read_json(root / 'job.json')
    entries = [read_json(p) for p in sorted((root / 'notices').glob('*.json'))
               if not p.name.endswith('.result.json')]
    job.update(done=sum(e['status'] == 'done' for e in entries),
               failed=sum(e['status'] == 'failed' for e in entries),
               items=sum(e.get('items', 0) for e in entries),
               active=[e for e in entries if e['status'] == 'running'],
               errors=[{'name': e['name'], 'error': e.get('error')} for e in entries if e['status']=='failed'])
    if job['status'] == 'running' and time.time() - job.get('heartbeat', 0) > 30:
        job['status'] = 'interrupted'
    return job


def run_job(root: Path, *, retry_failed=False):
    with FileLock(str(root / 'run.lock'), timeout=0):
        job = read_json(root / 'job.json')
        if (root / 'stop').exists():
            (root / 'stop').unlink()
        pending = []
        for entry in read_json(root / 'manifest.json'):
            path = root / 'notices' / f"{entry['index']:05d}.json"
            prior = read_json(path) if path.exists() else {}
            if prior.get('status') == 'done' or prior.get('status') == 'failed' and not retry_failed:
                continue
            pending.append(entry)
        job.update(status='running', heartbeat=time.time())
        write_json(root / 'job.json', job)
        with ProcessPoolExecutor(max_workers=job['workers']) as pool:
            active = {}
            while pending or active:
                while pending and len(active) < job['workers'] and not (root / 'stop').exists():
                    entry = pending.pop(0)
                    active[pool.submit(process_notice, str(root), entry)] = entry
                if not active:
                    break
                done, _ = wait(active, timeout=2, return_when=FIRST_COMPLETED)
                for future in done:
                    entry = active.pop(future)
                    try:
                        future.result()
                    except Exception as exc:  # noqa: BLE001
                        write_json(root / 'notices' / f"{entry['index']:05d}.json",
                                   {'index':entry['index'], 'name':entry['name'],
                                    'status':'failed', 'error':type(exc).__name__})
                job['heartbeat'] = time.time()
                write_json(root / 'job.json', job)
        state = snapshot(root)
        job.update(status='paused' if (root / 'stop').exists() else
                   'completed_with_errors' if state['failed'] else 'completed', finished_at=time.time())
        write_json(root / 'job.json', job)


def launch(root: Path, *, retry_failed=False):
    command = [sys.executable, '-m', 'app.jobs', 'run', str(root)]
    if retry_failed:
        command.append('--retry-failed')
    with (root / 'worker.log').open('ab') as log:
        subprocess.Popen(command, cwd=Path(__file__).resolve().parents[1],
                         stdout=log, stderr=log, stdin=subprocess.DEVNULL,
                         creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0),
                         start_new_session=os.name != 'nt')


def main():
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest='action', required=True)
    create = sub.add_parser('create')
    create.add_argument('source', type=Path)
    create.add_argument('--name', required=True)
    create.add_argument('--mode', choices=['rules', 'hybrid', 'model'], default='rules')
    create.add_argument('--no-ocr', action='store_true')
    create.add_argument('--start', action='store_true')
    run = sub.add_parser('run')
    run.add_argument('root', type=Path)
    run.add_argument('--retry-failed', action='store_true')
    status = sub.add_parser('status')
    status.add_argument('id')
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
            run_job(args.root, retry_failed=args.retry_failed)
        except Timeout:
            print('任务已在运行')
    else:
        print(json.dumps(snapshot(job_path(args.id)), ensure_ascii=False))


if __name__ == '__main__':
    main()
