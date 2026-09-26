from pathlib import Path
from typing import Literal

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from app.datasets import DatabasePath
from app.jobs import create_job, job_path, jobs_root, launch, read_json, snapshot

router = APIRouter(prefix='/api/v1/jobs', tags=['batch jobs'])


class JobCreate(BaseModel):
    source_directory: str
    mode: Literal['rules', 'hybrid', 'model'] = 'rules'
    ocr: bool = True


def checked_job(job_id: str, database: Path) -> Path:
    try:
        root = job_path(job_id)
        if Path(read_json(root / 'job.json')['database']).resolve() != database.resolve():
            raise ValueError('当前数据集下没有此任务')
        return root
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.get('')
def list_jobs(database: DatabasePath):
    return [snapshot(path.parent) for path in sorted(jobs_root().glob('*/job.json'))
            if Path(read_json(path)['database']).resolve() == database.resolve()]


@router.post('', status_code=202)
def start_job(payload: JobCreate, database: DatabasePath):
    from app.config import settings
    # Server paths are confined to the configured intake root. Never provide an
    # unauthenticated arbitrary-file reader through the batch API.
    allowed = Path(settings.intake_root).resolve() if settings.intake_root else Path(__file__).resolve().parents[3]
    try:
        source = Path(payload.source_directory).resolve(strict=True)
        if not source.is_relative_to(allowed) or not source.is_dir():
            raise ValueError('数据目录必须位于配置的 INTAKE_ROOT 内')
        dataset_id = 'default' if database.resolve() == settings.resolved_database_path.resolve() else database.stem
        job = create_job(source, database, dataset_id, mode=payload.mode, ocr=payload.ocr)
        launch(job_path(job['id']))
        return job
    except (ValueError, OSError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get('/{job_id}')
def get_job(job_id: str, database: DatabasePath):
    return snapshot(checked_job(job_id, database))


@router.get('/{job_id}/report')
def get_report(job_id: str, database: DatabasePath):
    root = checked_job(job_id, database)
    return {'job': snapshot(root), 'notices': [read_json(path) for path in
            sorted((root / 'notices').glob('*.json')) if not path.name.endswith('.result.json')]}


@router.post('/{job_id}/pause', status_code=202)
def pause_job(job_id: str, database: DatabasePath):
    root = checked_job(job_id, database)
    (root / 'stop').touch()
    return {'message': '已请求暂停，将在当前文件处理后保存进度'}


@router.post('/{job_id}/resume', status_code=202)
def resume_job(job_id: str, database: DatabasePath, retry_failed: bool = False):
    root = checked_job(job_id, database)
    launch(root, retry_failed=retry_failed)
    return {'message': '已提交续跑；完成的公告不会重复入库'}
