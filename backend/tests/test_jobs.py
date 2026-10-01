import io
import json
import multiprocessing
import time
import zipfile
from concurrent.futures import ThreadPoolExecutor
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
from app.storage import connect, count_notices

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


def test_changed_source_fails_without_inserting(tmp_path):
    root, database = prepare(tmp_path)
    entry = jobs.read_json(root / 'manifest.json')[0]
    Path(entry['files'][0]['path']).write_text('changed', encoding='utf-8')
    result = jobs.process_notice(str(root), entry)
    assert result['status'] == 'failed' and '不一致' in result['error']
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
    root, _ = prepare(tmp_path)
    job = jobs.read_json(root / 'job.json')
    job.update(status='running', heartbeat=0)
    jobs.write_json(root / 'job.json', job)
    assert jobs.snapshot(root)['status'] == 'interrupted'


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
    with connect(database) as connection:
        assert connection.execute('SELECT COUNT(*) FROM procurement_items').fetchone()[0] == 1
        assert connection.execute('SELECT COUNT(*) FROM import_receipts').fetchone()[0] == 1


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


@pytest.mark.parametrize('failure', ['timeout', '429', 'invalid-json'])
def test_model_failure_is_isolated_retryable_and_does_not_import_failed_notice(
    tmp_path, monkeypatch, failure,
):
    source = tmp_path / 'source'
    source.mkdir()
    (source / '01-fail.html').write_text(HTML, encoding='utf-8')
    (source / '02-good.html').write_text(HTML.replace('电脑', '扫描仪'), encoding='utf-8')
    dataset = create_dataset(DatasetCreate(name=f'model failure {failure}'))
    database = resolve_database(dataset['id'])
    job = jobs.create_job(source, database, dataset['id'], ocr=False, mode='model')
    root = jobs.job_path(job['id'])
    persisted = jobs.read_json(root / 'job.json')
    persisted['workers'] = 1
    jobs.write_json(root / 'job.json', persisted)
    model_settings = Settings(
        model_base_url='https://model.example/v1',
        model_api_key='synthetic-key',
        model_name='qwen-test',
    )
    monkeypatch.setattr(jobs, 'effective_settings', lambda: model_settings)
    monkeypatch.setattr(jobs, 'ProcessPoolExecutor', ThreadPoolExecutor)
    keep_failing = [True]
    calls = []

    class Response:
        def __init__(self, content, status_code=200):
            self.content = content
            self.status_code = status_code

        def raise_for_status(self):
            if self.status_code >= 400:
                request = httpx.Request('POST', 'https://model.example/v1/chat/completions')
                response = httpx.Response(self.status_code, request=request)
                raise httpx.HTTPStatusError('synthetic HTTP failure', request=request,
                                            response=response)

        def iter_lines(self):
            frame = json.dumps({'choices': [{'delta': {'content': self.content}}]})
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
            if failure == '429':
                return Stream(Response('{}', status_code=429))
            return Stream(Response('not valid JSON'))
        return Stream(Response('{}'))

    monkeypatch.setattr('app.model_adapter.httpx.stream', fake_stream)
    jobs.run_job(root)

    first = jobs.snapshot(root)
    assert first['status'] == 'completed_with_errors'
    assert first['failed'] == 1 and first['done'] == 1
    assert len(calls) == 2
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
