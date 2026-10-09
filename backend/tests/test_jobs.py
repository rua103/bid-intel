import io
import json
import multiprocessing
import os
import subprocess
import threading
import time
import zipfile
from concurrent.futures import Future, ThreadPoolExecutor
from concurrent.futures.process import BrokenProcessPool
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import httpx
import py7zr
import pytest
from fastapi.testclient import TestClient
from filelock import FileLock

from app import jobs
from app.archive_files import DiskDocument, expand_paths
from app.config import Settings, settings
from app.datasets import DatasetCreate, create_dataset, resolve_database
from app.main import app
from app.storage import connect, count_notices, get_notice_detail

HTML = '<meta charset="utf-8"><table><tr><th>名称</th><th>数量</th><th>单价</th></tr><tr><td>电脑</td><td>2</td><td>100</td></tr></table>'


def prepare(tmp_path, *, mode='hybrid'):
    source = tmp_path / 'source'
    source.mkdir()
    (source / 'notice.html').write_text(HTML, encoding='utf-8')
    dataset = create_dataset(DatasetCreate(name='test'))
    database = resolve_database(dataset['id'])
    job = jobs.create_job(source, database, dataset['id'], ocr=False, mode=mode)
    return jobs.job_path(job['id']), database


def _hold_file_lock(path, ready, release):
    with FileLock(path):
        ready.set()
        release.wait(15)


@contextmanager
def _local_model_server(delay=0.25):
    state = {"active": 0, "maximum": 0, "requests": 0}
    guard = threading.Lock()

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_args):
            pass

        def do_POST(self):
            self.rfile.read(int(self.headers["Content-Length"]))
            with guard:
                state["active"] += 1
                state["maximum"] = max(state["maximum"], state["active"])
                state["requests"] += 1
            try:
                time.sleep(delay)
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.end_headers()
                frames = [
                    {"choices": [{"delta": {"content": json.dumps({
                        "metadata": {}, "participants": [], "items": [],
                    })}}]},
                    {"choices": [{"delta": {}, "finish_reason": "stop"}]},
                    {"choices": [], "usage": {"prompt_tokens": 9, "completion_tokens": 2}},
                ]
                for frame in frames:
                    self.wfile.write(f"data: {json.dumps(frame)}\n\n".encode())
                self.wfile.write(b"data: [DONE]\n\n")
                self.wfile.flush()
            finally:
                with guard:
                    state["active"] -= 1

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}/v1", state
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def test_new_batch_jobs_default_to_model_led_hybrid(tmp_path):
    source = tmp_path / 'source'
    source.mkdir()
    (source / 'notice.html').write_text(HTML, encoding='utf-8')
    dataset = create_dataset(DatasetCreate(name='default hybrid'))
    job = jobs.create_job(source, resolve_database(dataset['id']), dataset['id'], ocr=False)
    assert job['mode'] == 'hybrid'


def test_job_recovery_after_commit_before_checkpoint_does_not_duplicate(tmp_path):
    root, database = prepare(tmp_path)
    entry = jobs.read_json(root / 'manifest.json')[0]
    result = jobs.process_notice(str(root), entry)
    assert result['status'] == 'done' and count_notices(database) == 1
    with connect(database) as connection:
        before = tuple(connection.execute(
            'SELECT (SELECT COUNT(*) FROM procurement_items), '
            '(SELECT COUNT(*) FROM import_receipts)'
        ).fetchone())
    (root / 'notices/00000.json').unlink()
    resumed = jobs.process_notice(str(root), entry)
    assert resumed['notice_id'] == result['notice_id']
    assert count_notices(database) == 1
    with connect(database) as connection:
        after = tuple(connection.execute(
            'SELECT (SELECT COUNT(*) FROM procurement_items), '
            '(SELECT COUNT(*) FROM import_receipts)'
        ).fetchone())
    assert after == before == (1, 1)
    assert jobs.snapshot(root)['done'] == 1


def test_pause_and_resume_use_document_checkpoint(tmp_path):
    root, database = prepare(tmp_path)
    entry = jobs.read_json(root / 'manifest.json')[0]
    (root / 'stop').touch()
    assert jobs.process_notice(str(root), entry)['status'] == 'pending'
    assert count_notices(database) == 0
    (root / 'stop').unlink()
    assert jobs.process_notice(str(root), entry)['status'] == 'done'
    assert count_notices(database) == 1
    notice_id = jobs.read_json(root / 'notices' / '00000.json')['notice_id']
    detail = get_notice_detail(database, notice_id)
    source = next(row for row in detail['source_files'] if row['source_file'] == 'notice.html')
    assert source['sha256'] == jobs.file_hash(tmp_path / 'source' / 'notice.html')


def test_pause_after_first_attachment_stops_before_second_parse(tmp_path, monkeypatch):
    root, database = prepare(tmp_path, mode='rules')
    archive_path = tmp_path / 'source' / 'attachments.zip'
    with zipfile.ZipFile(archive_path, 'w') as archive:
        archive.writestr('first.txt', 'first synthetic attachment')
        archive.writestr('second.txt', 'second synthetic attachment')
    entry = jobs.read_json(root / 'manifest.json')[0]
    entry['files'] = [{
        'name': archive_path.name, 'path': str(archive_path),
        'sha256': jobs.file_hash(archive_path),
    }]
    from app import parsers
    original_parse = parsers.parse_document_with_participants
    parsed = []

    def pause_after_parse(document, **options):
        parsed.append(document.filename)
        result = original_parse(document, **options)
        (root / 'stop').touch()
        return result

    monkeypatch.setattr(parsers, 'parse_document_with_participants', pause_after_parse)

    assert jobs.process_notice(str(root), entry)['status'] == 'pending'
    assert len(parsed) == 1
    assert count_notices(database) == 0
    (root / 'stop').unlink()
    monkeypatch.setattr(parsers, 'parse_document_with_participants', original_parse)
    assert jobs.process_notice(str(root), entry)['status'] == 'done'
    assert count_notices(database) == 1


def test_restart_retains_interrupted_request_telemetry_in_attempt_history(tmp_path):
    root, database = prepare(tmp_path, mode='rules')
    checkpoint = root / 'notices' / '00000.json'
    jobs.write_json(checkpoint, {
        'index': 0, 'name': 'notice', 'status': 'running', 'attempt': 2,
        'attempt_history': [{'attempt': 1, 'status': 'pending'}],
    })
    jobs.write_json(checkpoint.with_suffix('.telemetry.json'), {
        'model_calls': [{
            'call_id': 'synthetic-interrupted-request', 'transport': 'failed',
            'prompt_tokens': 9, 'completion_tokens': None,
        }],
        'historical_model_calls': [], 'provider_kv_tokens': 0,
    })

    recovered = jobs.process_notice(str(root), jobs.read_json(root / 'manifest.json')[0])

    assert recovered['status'] == 'done' and recovered['attempt'] == 3
    assert recovered['attempt_history'][-1]['attempt'] == 2
    assert recovered['attempt_history'][-1]['api_usage']['requests'] == 1
    assert recovered['attempt_history'][-1]['api_usage']['prompt_tokens'] == 9
    assert recovered['attempt_history'][-1]['model_calls'][0]['call_id'] == (
        'synthetic-interrupted-request'
    )
    assert recovered['api_usage']['requests'] == 0
    assert count_notices(database) == 1


@pytest.mark.parametrize('damage', ['truncated', 'schema', 'warnings'])
def test_damaged_production_parse_cache_is_rebuilt_without_duplicate_import(tmp_path, damage):
    root, database = prepare(tmp_path, mode='rules')
    entry = jobs.read_json(root / 'manifest.json')[0]
    assert jobs.process_notice(str(root), entry)['status'] == 'done'
    cache_root = Path(jobs.read_json(root / 'job.json')['cache_database']).parent / 'parse-cache'
    cache_path = next(cache_root.glob('*.json'))
    if damage == 'truncated':
        cache_path.write_text('{"text":', encoding='utf-8')
    else:
        cached = jobs.read_json(cache_path)
        if damage == 'schema':
            cached['items'] = [{'quantity': {'invalid': True}}]
        else:
            cached['warnings'] = [None]
        jobs.write_json(cache_path, cached)
    (root / 'notices' / '00000.result.json').unlink()

    recovered = jobs.process_notice(str(root), entry)

    assert recovered['status'] == 'done'
    assert count_notices(database) == 1
    repaired = jobs.read_json(cache_path)
    assert repaired['warnings'] == []
    assert repaired['items'][0]['product_name'] == '电脑'
    with connect(database) as connection:
        assert connection.execute('SELECT COUNT(*) FROM import_receipts').fetchone()[0] == 1


def test_changed_source_fails_without_inserting(tmp_path):
    root, database = prepare(tmp_path)
    entry = jobs.read_json(root / 'manifest.json')[0]
    Path(entry['files'][0]['path']).write_text('changed', encoding='utf-8')
    result = jobs.process_notice(str(root), entry)
    assert result['status'] == 'failed' and '不一致' in result['error']
    assert count_notices(database) == 0


def test_source_changed_during_parse_fails_before_import(tmp_path, monkeypatch):
    root, database = prepare(tmp_path, mode='rules')
    entry = jobs.read_json(root / 'manifest.json')[0]
    source_path = Path(entry['files'][0]['path'])
    from app import parsers

    original = parsers.parse_document_with_participants

    def parse_then_change(source, **options):
        result = original(source, **options)
        source_path.write_text('changed during parse', encoding='utf-8')
        return result

    monkeypatch.setattr(parsers, 'parse_document_with_participants', parse_then_change)
    result = jobs.process_notice(str(root), entry)

    assert result['status'] == 'failed'
    assert '处理期间源文件发生变化' in result['error']
    assert count_notices(database) == 0


def test_api_dataset_scope_and_directory_guard(tmp_path, monkeypatch):
    root, _ = prepare(tmp_path)
    job = jobs.read_json(root / 'job.json')
    monkeypatch.setattr(settings, 'intake_root', str(tmp_path / 'source'))
    with TestClient(app) as client:
        assert client.get('/api/v1/jobs/' + job['id']).status_code == 404
        headers = {'X-Dataset-ID': job['dataset_id']}
        assert client.get('/api/v1/jobs/' + job['id'], headers=headers).status_code == 200
        assert client.post('/api/v1/jobs', json={'source_directory': str(tmp_path)},
                           headers=headers).status_code == 400
        assert client.post('/api/v1/jobs/' + job['id'] + '/pause', headers=headers).status_code == 202
        assert (root / 'stop').exists()
        assert client.post('/api/v1/jobs/' + job['id'] + '/stop', headers=headers).status_code == 202
        assert (root / 'cancel').exists()
        assert jobs.read_json(root / 'job.json')['status'] == 'stopped'


def test_nested_7z_zip_uses_safe_disk_paths(tmp_path):
    zipped = io.BytesIO()
    with zipfile.ZipFile(zipped, 'w') as archive:
        archive.writestr('../../escape.txt', '正文')
    path = tmp_path / 'outer.7z'
    with py7zr.SevenZipFile(path, 'w') as archive:
        archive.writestr(zipped.getvalue(), '报价.zip')
    expanded = tmp_path / 'expanded'
    docs, warnings = expand_paths([DiskDocument('outer.7z', path)], expanded)
    assert not warnings and len(docs) == 1
    assert docs[0].path.is_relative_to(expanded)
    assert not (tmp_path / 'escape.txt').exists()
    assert docs[0].content.decode() == '正文'
    assert docs[0].filename.endswith('报价.zip!/../../escape.txt')


def test_disk_expansion_enforces_actual_cumulative_budget(tmp_path, monkeypatch):
    path = tmp_path / 'large.zip'
    with zipfile.ZipFile(path, 'w', compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr('one.txt', b'a' * 700000)
        archive.writestr('two.txt', b'b' * 700000)
    monkeypatch.setattr(settings, 'job_max_expanded_mb', 1)
    documents, warnings = expand_paths([DiskDocument('large.zip', path)], tmp_path / 'expanded')
    assert [document.filename for document in documents] == ['large.zip!/one.txt']
    assert any('two.txt' in warning and '读取失败' in warning for warning in warnings)


def test_member_over_limit_is_skipped_without_losing_valid_sibling(tmp_path, monkeypatch):
    path = tmp_path / 'attachments.zip'
    with zipfile.ZipFile(path, 'w', compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr('oversize.txt', b'x' * (1024 * 1024 + 1))
        archive.writestr('valid.txt', '有效附件')
    monkeypatch.setattr(settings, 'job_max_member_mb', 1)

    documents, warnings = expand_paths([DiskDocument('attachments.zip', path)],
                                       tmp_path / 'expanded')

    assert [document.filename for document in documents] == ['attachments.zip!/valid.txt']
    assert any('oversize.txt' in warning and '超过' in warning for warning in warnings)
    assert documents[0].content.decode() == '有效附件'


def test_corrupt_rar_isolated_from_valid_attachment_sibling(tmp_path):
    bad_rar = tmp_path / 'broken.rar'
    bad_rar.write_bytes(b'Rar!\x1a\x07\x00not a valid RAR archive')
    good_text = tmp_path / 'valid.txt'
    good_text.write_text('valid content', encoding='utf-8')

    documents, warnings = expand_paths([
        DiskDocument('broken.rar', bad_rar), DiskDocument('valid.txt', good_text),
    ], tmp_path / 'expanded')

    assert [document.filename for document in documents] == ['valid.txt']
    assert any('broken.rar' in warning for warning in warnings)


def test_corrupt_rar_does_not_fail_notice_job(tmp_path):
    source = tmp_path / 'source'
    source.mkdir()
    (source / 'notice.html').write_text(HTML, encoding='utf-8')
    (source / 'notice.rar').write_bytes(b'Rar!\x1a\x07\x00not a valid RAR archive')
    dataset = create_dataset(DatasetCreate(name='bad attachment isolation'))
    database = resolve_database(dataset['id'])
    job = jobs.create_job(source, database, dataset['id'], ocr=False, mode='rules')
    root = jobs.job_path(job['id'])

    result = jobs.process_notice(str(root), jobs.read_json(root / 'manifest.json')[0])

    assert result['status'] == 'done'
    assert count_notices(database) == 1
    assert any('notice.rar' in warning for warning in result['warnings'])


def test_stale_job_is_reported_as_interrupted(tmp_path):
    root, database = prepare(tmp_path, mode='rules')
    job = jobs.read_json(root / 'job.json')
    job.update(status='running', heartbeat=0)
    jobs.write_json(root / 'job.json', job)
    assert jobs.snapshot(root)['status'] == 'interrupted'
    queued = jobs.queue_job(root)
    assert queued['status'] == 'queued'
    jobs.run_job(root)
    assert jobs.snapshot(root)['status'] == 'completed'
    assert jobs.snapshot(root)['done'] == 1
    assert count_notices(database) == 1


def test_corrupt_progress_is_reported_and_reprocessed_on_resume(tmp_path):
    source = tmp_path / 'source'
    source.mkdir()
    (source / '01-corrupt.html').write_text(HTML, encoding='utf-8')
    (source / '02-running.html').write_text(HTML.replace('电脑', '扫描仪'), encoding='utf-8')
    dataset = create_dataset(DatasetCreate(name='corrupt progress recovery'))
    database = resolve_database(dataset['id'])
    job = jobs.create_job(source, database, dataset['id'], ocr=False, mode='rules')
    root = jobs.job_path(job['id'])
    checkpoint = root / 'notices' / '00000.json'
    checkpoint.parent.mkdir(parents=True, exist_ok=True)
    checkpoint.write_text('{"status":', encoding='utf-8')
    jobs.write_json(root / 'notices' / '00001.json', {
        'index': 1, 'name': '02-running', 'status': 'running',
    })
    job_data = jobs.read_json(root / 'job.json')
    job_data.update(status='running', heartbeat=0)
    jobs.write_json(root / 'job.json', job_data)

    before = jobs.snapshot(root)
    assert before['status'] == 'interrupted'
    assert before['failed'] == 1
    assert '损坏' in before['errors'][0]['error']
    headers = {'X-Dataset-ID': job_data['dataset_id']}
    with TestClient(app) as client:
        listing = client.get('/api/v1/jobs', headers=headers)
        report = client.get(f"/api/v1/jobs/{job_data['id']}/report", headers=headers)
    assert listing.status_code == report.status_code == 200
    assert next(row for row in listing.json() if row['id'] == job_data['id'])['failed'] == 1
    assert report.json()['job']['failed'] == 1
    jobs.run_job(root)

    after = jobs.snapshot(root)
    assert after['status'] == 'completed'
    assert after['done'] == 2 and after['failed'] == 0
    assert count_notices(database) == 2


@pytest.mark.parametrize('broken_value', [
    'null', '[]', '"not-an-entry"', '{}',
    '{"status": [], "name": "notice"}',
    '{"status": "done"}',
    '{"status": "done", "name": "notice", "items": null}',
    '{"status": "done", "name": "notice", "items": "1"}',
])
def test_structurally_invalid_progress_is_reported_and_reprocessed(tmp_path, broken_value):
    root, database = prepare(tmp_path, mode='rules')
    checkpoint = root / 'notices' / '00000.json'
    checkpoint.parent.mkdir(parents=True, exist_ok=True)
    checkpoint.write_text(broken_value, encoding='utf-8')
    job_data = jobs.read_json(root / 'job.json')
    job_data.update(status='running', heartbeat=0)
    jobs.write_json(root / 'job.json', job_data)

    snapshot = jobs.snapshot(root)
    assert snapshot['failed'] == 1
    assert '损坏' in snapshot['errors'][0]['error']
    headers = {'X-Dataset-ID': job_data['dataset_id']}
    with TestClient(app) as client:
        assert client.get('/api/v1/jobs', headers=headers).status_code == 200
        assert client.get(f"/api/v1/jobs/{job_data['id']}", headers=headers).status_code == 200
        assert client.get(f"/api/v1/jobs/{job_data['id']}/report", headers=headers).status_code == 200

    jobs.run_job(root)
    assert jobs.snapshot(root)['status'] == 'completed'
    assert jobs.snapshot(root)['done'] == 1
    assert count_notices(database) == 1
    jobs.run_job(root)
    with connect(database) as connection:
        assert connection.execute('SELECT COUNT(*) FROM procurement_items').fetchone()[0] == 1
        assert connection.execute('SELECT COUNT(*) FROM import_receipts').fetchone()[0] == 1


def test_resume_waits_for_previous_runner_lock_instead_of_losing_request(tmp_path):
    root, database = prepare(tmp_path, mode='rules')
    (root / 'stop').touch()
    lock_path = str(root / 'run.lock')
    context = multiprocessing.get_context('spawn')
    ready = context.Event()
    release = context.Event()
    holder = context.Process(target=_hold_file_lock, args=(lock_path, ready, release))
    holder.start()
    assert ready.wait(10)

    try:
        with ThreadPoolExecutor(max_workers=1) as pool:
            resumed = pool.submit(jobs.run_job, root, wait_for_lock=True)
            time.sleep(0.2)
            assert not resumed.done()
            release.set()
            resumed.result(timeout=30)
    finally:
        release.set()
        holder.join(timeout=10)

    assert holder.exitcode == 0
    assert not (root / 'stop').exists()
    assert jobs.snapshot(root)['status'] == 'completed'
    assert count_notices(database) == 1


def test_corrupt_result_checkpoint_is_rebuilt_without_duplicate_receipt(tmp_path):
    root, database = prepare(tmp_path, mode='rules')
    entry = jobs.read_json(root / 'manifest.json')[0]
    first = jobs.process_notice(str(root), entry)
    assert first['status'] == 'done'
    with connect(database) as connection:
        before = tuple(connection.execute(
            'SELECT (SELECT COUNT(*) FROM procurement_items), '
            '(SELECT COUNT(*) FROM import_receipts)'
        ).fetchone())

    (root / 'notices' / '00000.result.json').write_text('{broken', encoding='utf-8')
    resumed = jobs.process_notice(str(root), entry)

    assert resumed['status'] == 'done'
    assert resumed['notice_id'] == first['notice_id']
    assert count_notices(database) == 1
    with connect(database) as connection:
        after = tuple(connection.execute(
            'SELECT (SELECT COUNT(*) FROM procurement_items), '
            '(SELECT COUNT(*) FROM import_receipts)'
        ).fetchone())
    assert after == before == (1, 1)


def test_failed_notice_retries_only_when_requested_and_commits_once(tmp_path):
    root, database = prepare(tmp_path, mode='rules')
    entry = jobs.read_json(root / 'manifest.json')[0]
    original = Path(entry['files'][0]['path']).read_text(encoding='utf-8')
    Path(entry['files'][0]['path']).write_text('temporary source mismatch', encoding='utf-8')

    jobs.run_job(root)
    assert jobs.snapshot(root)['failed'] == 1
    assert count_notices(database) == 0
    Path(entry['files'][0]['path']).write_text(original, encoding='utf-8')
    jobs.run_job(root)
    assert jobs.snapshot(root)['failed'] == 1
    assert count_notices(database) == 0

    jobs.run_job(root, retry_failed=True)
    assert jobs.snapshot(root)['status'] == 'completed'
    assert jobs.snapshot(root)['done'] == 1
    assert count_notices(database) == 1
    checkpoint = jobs.read_json(root / 'notices' / '00000.json')
    assert checkpoint['attempt'] == 2
    assert checkpoint['attempt_history'][0]['status'] == 'failed'
    with connect(database) as connection:
        assert connection.execute('SELECT COUNT(*) FROM procurement_items').fetchone()[0] == 1
        assert connection.execute('SELECT COUNT(*) FROM import_receipts').fetchone()[0] == 1


def test_resume_requested_while_active_owner_is_finalizing_is_not_lost(tmp_path, monkeypatch):
    root, _ = prepare(tmp_path, mode='rules')
    job = jobs.read_json(root / 'job.json')
    job.update(status='running', heartbeat=time.time())
    jobs.write_json(root / 'job.json', job)
    (root / 'stop').touch()
    monkeypatch.setattr(jobs, '_job_owner_active', lambda _root: True)

    queued = jobs.queue_job(root, retry_failed=True)

    assert queued['status'] == 'running'
    assert not (root / 'stop').exists()
    assert jobs.read_json(root / 'resume-request.json')['retry_failed'] is True

    jobs._finalize_job_run(root, queued, jobs.snapshot(root))

    finalized = jobs.read_json(root / 'job.json')
    assert finalized['status'] == 'queued'
    assert finalized['retry_failed_request'] is True
    assert not (root / 'resume-request.json').exists()


def test_full_job_queue_does_not_leave_an_orphan_directory(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, 'job_queue_capacity', 1)
    root, database = prepare(tmp_path, mode='rules')
    source = Path(jobs.read_json(root / 'job.json')['source'])
    before = {path.name for path in jobs.jobs_root().iterdir() if path.is_dir()}

    with pytest.raises(jobs.JobQueueFull):
        jobs.create_job(source, database, jobs.read_json(root / 'job.json')['dataset_id'],
                        mode='rules', ocr=False)

    after = {path.name for path in jobs.jobs_root().iterdir() if path.is_dir()}
    assert after == before


def test_private_job_secret_has_restricted_file_permissions(tmp_path):
    path = tmp_path / 'job' / 'model-secret.json'
    jobs.write_private_json(path, {'model_api_key': 'synthetic-secret'})

    assert jobs.read_json(path) == {'model_api_key': 'synthetic-secret'}
    if os.name == 'nt':
        acl = subprocess.run(
            ['icacls', str(path)], check=True, stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT, text=True, encoding='utf-8', errors='replace',
        ).stdout.casefold()
        assert 'authenticated users' not in acl
        assert 'builtin\\users' not in acl
    else:
        assert path.stat().st_mode & 0o777 == 0o600


def test_two_jobs_run_concurrently_without_crossing_dataset_boundaries(tmp_path):
    roots_and_databases = []
    for index in range(2):
        source = tmp_path / f'source-{index}'
        source.mkdir()
        (source / 'notice.html').write_text(HTML.replace('电脑', f'设备{index}'), encoding='utf-8')
        dataset = create_dataset(DatasetCreate(name=f'parallel {index}'))
        database = resolve_database(dataset['id'])
        job = jobs.create_job(source, database, dataset['id'], ocr=False, mode='rules')
        root = jobs.job_path(job['id'])
        persisted = jobs.read_json(root / 'job.json')
        persisted['workers'] = 1
        jobs.write_json(root / 'job.json', persisted)
        roots_and_databases.append((root, database))

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(jobs.run_job, root) for root, _ in roots_and_databases]
        for future in futures:
            future.result()

    for root, database in roots_and_databases:
        assert jobs.snapshot(root)['status'] == 'completed'
        assert jobs.snapshot(root)['done'] == 1
        assert count_notices(database) == 1


def test_production_pool_overlaps_model_io_with_separate_document_limit(
    tmp_path, monkeypatch,
):
    source = tmp_path / "source"
    source.mkdir()
    for index in range(4):
        (source / f"notice-{index}.html").write_text(
            f"<html><body>采购项目 {index}。公告内容：合成文档。</body></html>",
            encoding="utf-8",
        )
    dataset = create_dataset(DatasetCreate(name="bounded production overlap"))
    database = resolve_database(dataset["id"])

    with _local_model_server() as (base_url, state):
        model_settings = Settings(
            database_path=settings.database_path,
            model_base_url=base_url,
            model_api_key="offline-synthetic-key",
            model_name="qwen-plus",
            job_workers=2,
            document_workers=1,
            model_concurrency=2,
            job_queue_capacity=4,
        )
        monkeypatch.setattr(jobs, "effective_settings", lambda: model_settings)
        job = jobs.create_job(source, database, dataset["id"], ocr=False, mode="model")
        root = jobs.job_path(job["id"])
        jobs.run_job(root)

    snapshot = jobs.snapshot(root)
    assert snapshot["status"] == "completed"
    assert snapshot["done"] == 4
    assert state["requests"] == 4
    assert state["maximum"] == 2
    assert count_notices(database) == 4
    rows = jobs.read_notice_progress(root)
    assert sum(row.get("api_usage", {}).get("requests", 0) for row in rows) == 4
    assert all(row.get("api_usage", {}).get("completion_tokens") == 2 for row in rows)


def test_broken_worker_keeps_completed_sibling_and_recovers_pending_notice(
    tmp_path, monkeypatch,
):
    source = tmp_path / "source"
    source.mkdir()
    (source / "notice-0.html").write_text(HTML, encoding="utf-8")
    (source / "notice-1.html").write_text(HTML.replace("电脑", "显示器"), encoding="utf-8")
    dataset = create_dataset(DatasetCreate(name="worker crash recovery"))
    database = resolve_database(dataset["id"])
    job = jobs.create_job(source, database, dataset["id"], ocr=False, mode="rules")
    root = jobs.job_path(job["id"])
    original_pool = jobs.ProcessPoolExecutor

    class OneCrashPool:
        def __init__(self, *args, **kwargs):
            self.submitted = 0

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            self.shutdown(wait=True)
            return False

        def submit(self, function, *args):
            future = Future()
            self.submitted += 1
            if self.submitted == 1:
                future.set_result(function(*args))
            else:
                checkpoint = root / 'notices' / '00001.json'
                jobs.write_json(checkpoint, {
                    'index': 1, 'name': 'notice-1', 'status': 'running', 'attempt': 1,
                })
                jobs.write_json(checkpoint.with_suffix('.telemetry.json'), {
                    'model_calls': [{'call_id': 'crash-request', 'transport': 'failed'}],
                    'historical_model_calls': [], 'provider_kv_tokens': 0,
                })
                future.set_exception(BrokenProcessPool("synthetic child crash"))
            return future

        def shutdown(self, *, wait, cancel_futures=False):
            assert wait

    monkeypatch.setattr(jobs, "ProcessPoolExecutor", OneCrashPool)
    jobs.run_job(root)
    assert jobs.read_notice_progress(root)[0]["status"] == "done"
    assert jobs.read_notice_progress(root)[1]["status"] == "pending"
    assert jobs.read_notice_progress(root)[1]['attempt'] == 1
    assert jobs.read_notice_progress(root)[1]['api_usage']['requests'] == 1
    assert (root / "stop").exists()
    assert count_notices(database) == 1

    monkeypatch.setattr(jobs, "ProcessPoolExecutor", original_pool)
    jobs.queue_job(root)
    jobs.run_job(root)
    assert jobs.snapshot(root)["status"] == "completed"
    assert jobs.snapshot(root)["done"] == 2
    assert count_notices(database) == 2
    recovered = jobs.read_notice_progress(root)[1]
    assert recovered['attempt'] == 2
    assert recovered['attempt_history'][-1]['api_usage']['requests'] == 1


@pytest.mark.parametrize('failure', [
    '401', '429', 'timeout', 'invalid-json', 'truncated-json',
    'unsupported-chat-template', 'finish-reason-length',
])
def test_model_failure_is_isolated_retryable_and_does_not_import_failed_notice(
    tmp_path, monkeypatch, failure,
):
    source = tmp_path / 'source'
    source.mkdir()
    (source / '01-fail.html').write_text(HTML, encoding='utf-8')
    (source / '02-good.html').write_text(HTML.replace('电脑', '扫描仪'), encoding='utf-8')
    model_settings = Settings(
        model_base_url='https://model.example/v1',
        model_api_key='synthetic-key',
        model_name='qwen-test',
    )
    monkeypatch.setattr(jobs, 'effective_settings', lambda: model_settings)
    dataset = create_dataset(DatasetCreate(name=f'model failure {failure}'))
    database = resolve_database(dataset['id'])
    job = jobs.create_job(source, database, dataset['id'], ocr=False, mode='model')
    root = jobs.job_path(job['id'])
    persisted = jobs.read_json(root / 'job.json')
    persisted['workers'] = 1
    jobs.write_json(root / 'job.json', persisted)
    def thread_pool(*, max_workers, mp_context):
        return ThreadPoolExecutor(max_workers=max_workers)

    monkeypatch.setattr(jobs, 'ProcessPoolExecutor', thread_pool)
    keep_failing = [True]
    calls = []

    class Response:
        def __init__(self, content, status_code=200, finish_reason=None):
            self.content = content
            self.status_code = status_code
            self.finish_reason = finish_reason

        def raise_for_status(self):
            if self.status_code >= 400:
                request = httpx.Request('POST', 'https://model.example/v1/chat/completions')
                response = httpx.Response(
                    self.status_code,
                    text='Unsupported parameter: chat_template_kwargs',
                    request=request,
                )
                raise httpx.HTTPStatusError('synthetic HTTP failure', request=request,
                                            response=response)

        def iter_lines(self):
            choice = {'delta': {'content': self.content}}
            if self.finish_reason:
                choice['finish_reason'] = self.finish_reason
            frame = json.dumps({'choices': [choice]})
            return iter([f'data: {frame}', 'data: [DONE]'])

    class Stream:
        def __init__(self, response):
            self.response = response

        def __enter__(self):
            return self.response

        def __exit__(self, *args):
            return False

    def fake_stream(method, url, **kwargs):
        call = kwargs['json']
        user_message = call['messages'][1]['content']
        calls.append(user_message)
        if '01-fail.html' in user_message and keep_failing[0]:
            if failure == 'timeout':
                raise httpx.ReadTimeout('synthetic timeout')
            if failure == '401':
                return Stream(Response('{}', status_code=401))
            if failure == '429':
                return Stream(Response('{}', status_code=429))
            if failure == 'truncated-json':
                return Stream(Response('{"metadata":{},"items":['))
            if failure == 'unsupported-chat-template':
                assert call['chat_template_kwargs'] == {'enable_thinking': False}
                return Stream(Response('{}', status_code=400))
            if failure == 'finish-reason-length':
                return Stream(Response('{}', finish_reason='length'))
            return Stream(Response('not valid JSON'))
        return Stream(Response('{}'))

    monkeypatch.setattr('app.model_adapter.httpx.stream', fake_stream)
    jobs.run_job(root)

    first = jobs.snapshot(root)
    assert first['status'] == 'completed_with_errors'
    assert first['failed'] == 1 and first['done'] == 1
    assert len(calls) == 2
    failed_checkpoint = jobs.read_json(root / 'notices' / '00000.json')
    warning_text = ' '.join(failed_checkpoint.get('warnings', []))
    expected_warning = {
        '401': 'HTTP 401',
        '429': 'HTTP 429',
        'timeout': '超时',
        'invalid-json': '非法 JSON',
        'truncated-json': 'JSON 不完整或被截断',
        'unsupported-chat-template': 'chat_template_kwargs',
        'finish-reason-length': 'finish_reason=length',
    }[failure]
    assert failed_checkpoint['status'] == 'failed'
    assert expected_warning in warning_text
    assert not (root / 'notices' / '00000.result.json').exists()
    with connect(database) as connection:
        notices = connection.execute('SELECT source_files_json FROM notices').fetchall()
        assert [json.loads(row['source_files_json']) for row in notices] == [['02-good.html']]
        assert connection.execute('SELECT COUNT(*) FROM import_receipts').fetchone()[0] == 1
        assert connection.execute('SELECT COUNT(*) FROM procurement_items').fetchone()[0] == 0

    keep_failing[0] = False
    jobs.run_job(root, retry_failed=True)
    second = jobs.snapshot(root)
    assert second['status'] == 'completed'
    assert second['failed'] == 0 and second['done'] == 2
    with connect(database) as connection:
        rows = connection.execute('SELECT source_files_json FROM notices ORDER BY id').fetchall()
        assert [json.loads(row['source_files_json']) for row in rows] == [
            ['02-good.html'], ['01-fail.html'],
        ]
        assert connection.execute('SELECT COUNT(*) FROM import_receipts').fetchone()[0] == 2
        assert connection.execute('SELECT COUNT(*) FROM procurement_items').fetchone()[0] == 0


def test_status_readers_and_atomic_writers_share_windows_safe_lock(tmp_path):
    path = tmp_path / 'progress.json'
    jobs.write_json(path, {'version': 0})
    errors = []

    def writer():
        try:
            for index in range(150):
                jobs.write_json(path, {'version': index})
        except OSError as exc:  # pragma: no cover - captured for assertion
            errors.append(exc)

    with ThreadPoolExecutor(max_workers=5) as pool:
        futures = [pool.submit(writer)]
        futures.extend(pool.submit(lambda: [jobs.read_json(path) for _ in range(500)])
                       for _ in range(4))
        for future in futures:
            future.result()
    assert not errors
    assert jobs.read_json(path)['version'] == 149


def test_rules_job_never_calls_model(tmp_path, monkeypatch):
    root, _ = prepare(tmp_path)
    def forbidden(**kwargs):
        raise AssertionError('model must not be called')
    monkeypatch.setattr('app.ingestion.extract_unstructured_items', forbidden)
    result = jobs.process_notice(str(root), jobs.read_json(root / 'manifest.json')[0])
    assert result['status'] == 'done'


def test_background_rules_job_caches_and_imports_review_table_participants(tmp_path):
    source = tmp_path / 'source'
    source.mkdir()
    html = """<meta charset="utf-8"><table>
      <tr><th>供应商</th><th>资格性审查</th><th>符合性审查</th><th>综合得分</th><th>推荐排名</th></tr>
      <tr><td>甲设备有限公司</td><td>通过</td><td>通过</td><td>95</td><td>1</td></tr>
      <tr><td>乙设备有限公司</td><td>通过</td><td>通过</td><td>88</td><td>2</td></tr>
    </table>"""
    (source / 'notice.html').write_text(html, encoding='utf-8')
    dataset = create_dataset(DatasetCreate(name='participant job test'))
    database = resolve_database(dataset['id'])
    job = jobs.create_job(source, database, dataset['id'], ocr=False)
    root = jobs.job_path(job['id'])

    result = jobs.process_notice(str(root), jobs.read_json(root / 'manifest.json')[0])

    assert result['status'] == 'done'
    with connect(database) as connection:
        participants = connection.execute(
            """SELECT raw_name, outcome FROM bid_participations ORDER BY raw_name"""
        ).fetchall()
    assert [(row['raw_name'], row['outcome']) for row in participants] == [
        ('乙设备有限公司', 'unknown'), ('甲设备有限公司', 'unknown'),
    ]
