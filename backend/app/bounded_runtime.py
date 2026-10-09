"""Host-local, process-safe admission and resource leases.

All Web workers, job owners and evaluators in one deployment must use the same
local runtime directory. OS locks release leases on process death. This is not a
distributed lock protocol and must not be placed on NFS/shared network storage.
"""
from __future__ import annotations

import asyncio
import json
import os
import threading
import time
from contextlib import asynccontextmanager, contextmanager
from contextvars import ContextVar
from dataclasses import asdict, dataclass
from pathlib import Path
from uuid import uuid4

from filelock import FileLock, Timeout


class SchedulingStopped(RuntimeError):
    """Cooperative stop: no new resource use or HTTP request may start."""


class ModelAdmissionError(RuntimeError):
    """A model request was rejected before transport because admission is full."""


class RuntimeConfigurationError(ValueError):
    """Different processes attempted to allocate different deployment limits."""


def atomic_json(path: Path, value) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    pending = path.with_name(path.name + '.' + uuid4().hex + '.pending')
    try:
        with FileLock(str(path) + '.write.lock', timeout=300):
            with pending.open('w', encoding='utf-8') as stream:
                json.dump(value, stream, ensure_ascii=False, indent=2, allow_nan=False)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(pending, path)
            if os.name != 'nt':
                descriptor = os.open(path.parent, os.O_RDONLY)
                try:
                    os.fsync(descriptor)
                finally:
                    os.close(descriptor)
    finally:
        pending.unlink(missing_ok=True)


def read_json(path: Path):
    path = Path(path)
    with FileLock(str(path) + '.write.lock', timeout=300):
        return json.loads(path.read_text(encoding='utf-8'))


@dataclass(frozen=True)
class DeploymentLimits:
    runtime_dir: Path
    document_workers: int = 1
    model_concurrency: int = 2
    model_wait_queue_capacity: int = 8
    model_wait_timeout_seconds: int = 15
    queue_capacity: int = 8

    def __post_init__(self):
        object.__setattr__(self, 'runtime_dir', Path(self.runtime_dir).resolve())
        for name in ('document_workers', 'model_concurrency', 'model_wait_queue_capacity',
                     'model_wait_timeout_seconds', 'queue_capacity'):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value < 1:
                raise ValueError(f'{name} must be a positive integer')

    @classmethod
    def from_settings(cls, settings):
        configured = getattr(settings, 'job_runtime_dir', '')
        root = Path(configured) if configured else settings.resolved_database_path.parent / 'runtime'
        return cls(
            root,
            document_workers=settings.document_workers,
            model_concurrency=settings.model_concurrency,
            model_wait_queue_capacity=settings.model_wait_queue_capacity,
            model_wait_timeout_seconds=settings.model_wait_timeout_seconds,
            queue_capacity=settings.job_queue_capacity,
        )

    def identity(self):
        return {name: value for name, value in asdict(self).items() if name != 'runtime_dir'}


def from_settings(settings):
    return DeploymentLimits.from_settings(settings)


class Permit:
    def __init__(self, lock: FileLock, slot_key: str):
        self.lock = lock
        self.slot_key = slot_key

    def release(self):
        try:
            self.lock.release()
        finally:
            with _process_slot_guard:
                _process_slot_held.discard(self.slot_key)

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.release()


class DeploymentLimiter:
    def __init__(self, limits: DeploymentLimits):
        self.limits = limits
        self.root = limits.runtime_dir
        self.root.mkdir(parents=True, exist_ok=True)
        # Never silently multiply quotas by letting a second process create
        # more slot files. Configuration changes require a quiescent deployment
        # and an explicit fresh runtime directory.
        with FileLock(str(self.root / 'configuration.lock'), timeout=30):
            path = self.root / 'limits.json'
            identity = limits.identity()
            if path.exists():
                if read_json(path) != identity:
                    raise RuntimeConfigurationError('runtime limits differ; stop owners before '
                                                    'selecting a new runtime directory')
            else:
                atomic_json(path, identity)

    def try_acquire(self, resource: str) -> Permit | None:
        count = {'document': self.limits.document_workers,
                 'model': self.limits.model_concurrency,
                 'model-waiter': self.limits.model_wait_queue_capacity,
                 'queue': self.limits.queue_capacity}.get(resource)
        if count is None:
            raise ValueError(f'unknown resource: {resource}')
        for index in range(count):
            path = self.root / f'{resource}-{index}.lock'
            slot_key = str(path)
            # FileLock is re-entrant within a process. Without this local set,
            # concurrent asyncio tasks on one event-loop thread could all
            # reacquire slot 0 and accidentally serialize behind one permit.
            with _process_slot_guard:
                if slot_key in _process_slot_held:
                    continue
                _process_slot_held.add(slot_key)
            lock = FileLock(slot_key, timeout=0, thread_local=False)
            try:
                lock.acquire()
            except Timeout:
                with _process_slot_guard:
                    _process_slot_held.discard(slot_key)
                continue
            return Permit(lock, slot_key)
        return None

    @staticmethod
    def check_stop(stop_path):
        if stop_path is not None and Path(stop_path).exists():
            raise SchedulingStopped('用户请求暂停；不再发送新请求')

    @contextmanager
    def slot(self, resource: str, *, stop_path=None, timeout=None):
        started = time.monotonic()
        permit = None
        while permit is None:
            self.check_stop(stop_path)
            permit = self.try_acquire(resource)
            if permit is None:
                if timeout is not None and time.monotonic() - started >= timeout:
                    raise TimeoutError(f'{resource} admission timed out')
                time.sleep(0.05)
        try:
            self.check_stop(stop_path)
            yield permit
        finally:
            permit.release()

    def document_slot(self, *, stop_path=None, timeout=None):
        return self.slot('document', stop_path=stop_path, timeout=timeout)

    @contextmanager
    def model_slot_sync(self, *, stop_path=None, timeout=None):
        permit = self.try_acquire('model')
        waiter = None
        if permit is None:
            waiter = self.try_acquire('model-waiter')
            if waiter is None:
                raise ModelAdmissionError('模型请求等待队列已满，请稍后重试')
            started = time.monotonic()
            wait_seconds = min(
                self.limits.model_wait_timeout_seconds,
                timeout if timeout is not None else self.limits.model_wait_timeout_seconds,
            )
            try:
                while permit is None:
                    self.check_stop(stop_path)
                    permit = self.try_acquire('model')
                    if permit is None:
                        if time.monotonic() - started >= wait_seconds:
                            raise ModelAdmissionError('等待模型并发槽位超时，请稍后重试')
                        time.sleep(0.05)
            finally:
                waiter.release()
        try:
            self.check_stop(stop_path)
            yield permit
        finally:
            permit.release()

    @asynccontextmanager
    async def model_slot(self, *, stop_path=None, timeout=None):
        permit = self.try_acquire('model')
        waiter = None
        if permit is None:
            waiter = self.try_acquire('model-waiter')
            if waiter is None:
                raise ModelAdmissionError('模型请求等待队列已满，请稍后重试')
            started = time.monotonic()
            wait_seconds = min(
                self.limits.model_wait_timeout_seconds,
                timeout if timeout is not None else self.limits.model_wait_timeout_seconds,
            )
            try:
                while permit is None:
                    self.check_stop(stop_path)
                    permit = self.try_acquire('model')
                    if permit is None:
                        if time.monotonic() - started >= wait_seconds:
                            raise ModelAdmissionError('等待模型并发槽位超时，请稍后重试')
                        await asyncio.sleep(0.05)
            finally:
                waiter.release()
        try:
            self.check_stop(stop_path)
            yield permit
        finally:
            permit.release()


_runtime: ContextVar[tuple[DeploymentLimiter, Path | None] | None] = ContextVar(
    'bounded_runtime', default=None,
)
_process_slot_guard = threading.Lock()
_process_slot_held: set[str] = set()


@contextmanager
def use_runtime(limits: DeploymentLimits, *, stop_path=None):
    limiter = DeploymentLimiter(limits)
    token = _runtime.set((limiter, Path(stop_path) if stop_path is not None else None))
    try:
        yield limiter
    finally:
        _runtime.reset(token)


def current_runtime():
    value = _runtime.get()
    if value is not None:
        return value
    from app.config import settings
    return DeploymentLimiter(DeploymentLimits.from_settings(settings)), None
