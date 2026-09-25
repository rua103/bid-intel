"""Per-request SQLite selection; the original database is always 'default'."""
from __future__ import annotations

import re
from pathlib import Path
from typing import Annotated
from uuid import uuid4

from fastapi import APIRouter, Depends, Header, HTTPException
from pydantic import BaseModel, StringConstraints

from app.config import settings
from app.storage import connect, count_notices, initialize

router = APIRouter(prefix='/api/v1/datasets', tags=['datasets'])
DATASET_ID = re.compile(r'[0-9a-f]{32}')


class DatasetCreate(BaseModel):
    name: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=100)]


def dataset_directory() -> Path:
    return settings.resolved_database_path.parent / 'datasets'


def resolve_database(
    dataset_id: Annotated[str, Header(alias='X-Dataset-ID')] = 'default',
) -> Path:
    if dataset_id == 'default':
        return settings.resolved_database_path
    if not DATASET_ID.fullmatch(dataset_id):
        raise HTTPException(status_code=422, detail='数据集 ID 格式无效')
    path = dataset_directory() / f'{dataset_id}.sqlite'
    if not path.is_file():
        raise HTTPException(status_code=404, detail='数据集不存在，请重新选择')
    return path


DatabasePath = Annotated[Path, Depends(resolve_database)]


@router.get('')
def list_datasets() -> list[dict]:
    result = [{'id': 'default', 'name': '默认数据集（原有数据）',
               'notices_imported': count_notices(settings.resolved_database_path)}]
    for path in sorted(dataset_directory().glob('*.sqlite')):
        if not DATASET_ID.fullmatch(path.stem):
            continue
        with connect(path) as connection:
            row = connection.execute('SELECT name FROM dataset_info WHERE id = 1').fetchone()
        result.append({'id': path.stem, 'name': row['name'],
                       'notices_imported': count_notices(path)})
    return result


@router.post('', status_code=201)
def create_dataset(payload: DatasetCreate) -> dict:
    dataset_id = uuid4().hex
    path = dataset_directory() / f'{dataset_id}.sqlite'
    pending = path.with_suffix('.pending')
    initialize(pending)
    with connect(pending) as connection:
        connection.execute('CREATE TABLE dataset_info (id INTEGER PRIMARY KEY, name TEXT NOT NULL)')
        connection.execute('INSERT INTO dataset_info(id, name) VALUES (1, ?)', (payload.name,))
    # List/resolve only see fully initialized files, even during concurrent creation.
    pending.replace(path)
    return {'id': dataset_id, 'name': payload.name, 'notices_imported': 0}
