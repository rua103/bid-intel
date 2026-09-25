from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

import pytest
from fastapi.testclient import TestClient

from app.config import settings
from app.datasets import resolve_database
from app.main import app
from app.schemas import ImportResult, ItemCandidate, NoticeMetadata, ParticipantCandidate
from app.storage import save_import


def seed(path, title, amount):
    save_import(path, ImportResult(
        notice_id=0, source_files=['a.html'], items_found=1,
        metadata=NoticeMetadata(project_name=title, procurement_unit='同名采购中心'),
        items=[ItemCandidate(product_name=title, quantity=1, total_price=amount,
                             source_file='a.html', source_location='row:1')],
        participants=[ParticipantCandidate(
            organization_name=name, outcome='winner', award_amount=amount,
            source_file='a.html', source_location='row:1',
        ) for name in ['同名供应商甲', '同名供应商乙']],
    ))


def test_datasets_keep_all_queries_separate_and_survive_restart():
    original = settings.resolved_database_path
    with TestClient(app) as client:
        seed(original, '开发项目', 100)
        created = client.post('/api/v1/datasets', json={'name': ' 正式数据 '})
        assert created.status_code == 201
        dataset = created.json()
        headers = {'X-Dataset-ID': dataset['id']}
        assert dataset['name'] == '正式数据'
        assert client.get('/api/v1/items', headers=headers).json() == []
        assert client.get('/api/v1/health', headers=headers).json()['notices_imported'] == 0
        seed(resolve_database(dataset['id']), '正式项目', 500)
        assert settings.resolved_database_path == original

        for scope, title, amount in [({}, '开发项目', '100'), (headers, '正式项目', '500')]:
            items = client.get('/api/v1/items', headers=scope).json()
            assert len(items) == 1 and items[0]['product_name'] == title
            orgs = client.get('/api/v1/organizations', headers=scope).json()
            ids = {row['canonical_name']: row['id'] for row in orgs}
            buyer = ids['同名采购中心']
            suppliers = [ids['同名供应商甲'], ids['同名供应商乙']]
            awardees = client.get(f'/api/v1/analytics/buyers/{buyer}/awardees',
                                 headers=scope).json()
            assert {row['award_amount_total'] for row in awardees['awardees']} == {amount}
            routes = [
                (f'/api/v1/analytics/buyers/{buyer}/bidders', None),
                (f'/api/v1/analytics/suppliers/{suppliers[0]}/co-bidders', None),
                ('/api/v1/analytics/common-buyers', {'organization_ids': suppliers}),
                ('/api/v1/analytics/common-projects', {'organization_ids': suppliers}),
                ('/api/v1/graph', None),
            ]
            other = '正式项目' if title == '开发项目' else '开发项目'
            for route, payload in routes:
                response = (client.get(route, headers=scope) if payload is None
                            else client.post(route, headers=scope, json=payload))
                assert response.status_code == 200, response.text
                assert other not in response.text
                assert response.json()
            graph = client.get('/api/v1/graph', headers=scope).json()
            assert graph['project_count'] == 1
            assert any(row['name'] == title for row in graph['nodes'])
    with TestClient(app) as restarted:
        listed = restarted.get('/api/v1/datasets').json()
        assert {row['id']: row['notices_imported'] for row in listed} == {
            'default': 1, dataset['id']: 1,
        }
        assert restarted.get('/api/v1/items', headers=headers).json()[0]['product_name'] == '正式项目'


def test_concurrent_imports_pin_dataset_before_background_processing(monkeypatch):
    from app import main

    barrier = Barrier(2)
    original_import = main.import_notice

    def blocked_import(files, path):
        barrier.wait(timeout=10)
        return original_import(files, path, extraction_mode='rules')

    monkeypatch.setattr(main, 'import_notice', blocked_import)
    with TestClient(app) as client:
        dataset = client.post('/api/v1/datasets', json={'name': '隔离测试'}).json()['id']

        def upload(dataset_id, title):
            return client.post('/api/v1/notices/import', headers={'X-Dataset-ID': dataset_id},
                               files=[('files', ('a.html', f'<p>项目名称：{title}</p>'.encode()))])

        with ThreadPoolExecutor(max_workers=2) as executor:
            futures = [executor.submit(upload, scope, title)
                       for scope, title in [('default', '开发项目'), (dataset, '正式项目')]]
            results = [future.result() for future in futures]
        assert all(result.status_code == 200 for result in results)
        assert client.get('/api/v1/health').json()['notices_imported'] == 1
        assert client.get('/api/v1/health', headers={'X-Dataset-ID': dataset}).json()[
            'notices_imported'] == 1
        from app.storage import connect
        for scope, title in [('default', '开发项目'), (dataset, '正式项目')]:
            with connect(resolve_database(scope)) as connection:
                assert connection.execute('SELECT project_name FROM notices').fetchone()[0] == title


def test_batch_import_also_uses_dataset():
    with TestClient(app) as client:
        dataset = client.post('/api/v1/datasets', json={'name': '批量测试'}).json()['id']
        headers = {'X-Dataset-ID': dataset}
        response = client.post('/api/v1/notices/import-batch', headers=headers,
                               files=[('files', ('a.html', b'<p>notice</p>'))])
        assert response.status_code == 200
        assert response.json()['notices_imported'] == 1
        assert client.get('/api/v1/health').json()['notices_imported'] == 0
        assert client.get('/api/v1/health', headers=headers).json()['notices_imported'] == 1


@pytest.mark.parametrize('dataset_id,status', [('../outside', 422), ('', 422), ('f' * 32, 404)])
def test_invalid_or_unknown_dataset_never_falls_back(dataset_id, status):
    with TestClient(app) as client:
        response = client.get('/api/v1/items', headers={'X-Dataset-ID': dataset_id})
        assert response.status_code == status


def test_dataset_name_validation_and_parser_capabilities():
    with TestClient(app) as client:
        assert client.post('/api/v1/datasets', json={'name': ' '}).status_code == 422
        capabilities = client.get('/api/v1/parser-capabilities').json()
        assert capabilities['legacy_xls'] and capabilities['pdf_tables'] and capabilities['pdf_render']
