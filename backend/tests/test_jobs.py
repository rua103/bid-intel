import io
import zipfile
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import py7zr
import pytest
from fastapi.testclient import TestClient

from app import jobs
from app.archive_files import DiskDocument, expand_paths
from app.config import settings
from app.datasets import DatasetCreate, create_dataset, resolve_database
from app.main import app
from app.storage import connect, count_notices

HTML = '<meta charset="utf-8"><table><tr><th>名称</th><th>数量</th><th>单价</th></tr><tr><td>电脑</td><td>2</td><td>100</td></tr></table>'


def prepare(tmp_path):
    source = tmp_path / 'source'
    source.mkdir()
    (source / 'notice.html').write_text(HTML, encoding='utf-8')
    dataset = create_dataset(DatasetCreate(name='test'))
    database = resolve_database(dataset['id'])
    job = jobs.create_job(source, database, dataset['id'], ocr=False)
    return jobs.job_path(job['id']), database


def test_job_recovery_after_commit_before_checkpoint_does_not_duplicate(tmp_path):
    root, database = prepare(tmp_path)
    entry = jobs.read_json(root / 'manifest.json')[0]
    result = jobs.process_notice(str(root), entry)
    assert result['status'] == 'done' and count_notices(database) == 1
    (root / 'notices/00000.json').unlink()
    resumed = jobs.process_notice(str(root), entry)
    assert resumed['notice_id'] == result['notice_id']
    assert count_notices(database) == 1
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
    with pytest.raises(ValueError, match='展开大小'):
        expand_paths([DiskDocument('large.zip', path)], tmp_path / 'expanded')


def test_stale_job_is_reported_as_interrupted(tmp_path):
    root, _ = prepare(tmp_path)
    job = jobs.read_json(root / 'job.json')
    job.update(status='running', heartbeat=0)
    jobs.write_json(root / 'job.json', job)
    assert jobs.snapshot(root)['status'] == 'interrupted'


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
