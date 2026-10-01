"""Select the online relationship-query backend and keep Neo4j current."""

from __future__ import annotations

import hashlib
import logging
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from app import analytics
from app.config import settings
from app.graph import create_driver, query_neo4j, sync_to_neo4j

logger = logging.getLogger(__name__)

_driver_guard = threading.Lock()
_drivers: dict[tuple[str, str, str], Any] = {}
_sync_guard = threading.Lock()
_sync_locks: dict[tuple[str, str, str, int], threading.Lock] = {}
_synced_revisions: dict[tuple[str, str, str, int], tuple[Any, ...]] = {}


@dataclass(frozen=True)
class AnalyticsResult:
    payload: dict[str, Any]
    backend: str
    fell_back: bool = False


def _configured_driver() -> Any:
    key = (settings.neo4j_uri, settings.neo4j_user, settings.neo4j_password)
    with _driver_guard:
        driver = _drivers.get(key)
        if driver is None:
            driver = create_driver(*key)
            _drivers[key] = driver
        return driver


def _invalidate_configured_driver(driver: Any | None = None) -> None:
    key = (settings.neo4j_uri, settings.neo4j_user, settings.neo4j_password)
    with _driver_guard:
        cached = _drivers.get(key)
        if cached is None or (driver is not None and cached is not driver):
            return
        _drivers.pop(key, None)
    try:
        cached.close()
    except Exception:
        logger.debug("Error closing unavailable Neo4j driver", exc_info=True)


def close_neo4j_drivers() -> None:
    """Close cached drivers when the API process shuts down."""
    with _driver_guard:
        drivers = list(_drivers.values())
        _drivers.clear()
    with _sync_guard:
        _sync_locks.clear()
        _synced_revisions.clear()
    for driver in drivers:
        try:
            driver.close()
        except Exception:
            logger.debug("Error closing Neo4j driver", exc_info=True)


def _dataset_scope(path: Path) -> str:
    resolved = path.resolve()
    if resolved == settings.resolved_database_path.resolve():
        return "bid-intel:default"
    datasets = settings.resolved_database_path.parent / "datasets"
    try:
        relative = resolved.relative_to(datasets.resolve())
    except ValueError:
        digest = hashlib.sha256(str(resolved).encode("utf-8")).hexdigest()[:24]
        return f"bid-intel:path-{digest}"
    return f"bid-intel:{relative.stem}"


def _sqlite_revision(path: Path) -> tuple[Any, ...]:
    revision = []
    for candidate in (path, path.with_name(path.name + "-wal")):
        try:
            stat = candidate.stat()
            revision.append((stat.st_mtime_ns, stat.st_size))
        except FileNotFoundError:
            revision.append(None)
    return tuple(revision)


def _ensure_exported(path: Path, driver: Any, dataset: str) -> None:
    key = (str(path.resolve()), settings.neo4j_database, dataset, id(driver))
    with _sync_guard:
        lock = _sync_locks.setdefault(key, threading.Lock())
    with lock:
        revision = _sqlite_revision(path)
        with _sync_guard:
            if _synced_revisions.get(key) == revision:
                return
        sync_to_neo4j(path, driver, dataset=dataset, database=settings.neo4j_database)
        # Save the pre-export revision so a write concurrent with the snapshot
        # forces another refresh on the next request.
        with _sync_guard:
            _synced_revisions[key] = revision


def _sqlite_query(path: Path, scene: str, parameters: dict[str, Any]) -> dict[str, Any]:
    if scene == "buyer_awardees":
        return analytics.buyer_awardees(path, parameters["buyer_id"])
    if scene == "buyer_bidders":
        return analytics.buyer_bidders(
            path,
            parameters["buyer_id"],
            include_winners=parameters.get("include_winners", False),
            top=parameters.get("top", 5),
        )
    if scene == "supplier_co_bidders":
        return analytics.supplier_co_bidders(
            path,
            parameters["supplier_id"],
            include_winners=parameters.get("include_winners", False),
            top=parameters.get("top", 5),
        )
    if scene == "common_buyers":
        return analytics.common_award_buyers(path, parameters["supplier_ids"])
    if scene == "common_projects":
        return analytics.common_bid_packages(path, parameters["supplier_ids"])
    raise ValueError(f"未知场景：{scene}")


def query_analytics(path: Path, scene: str, **parameters: Any) -> AnalyticsResult:
    """Run one of the five relationship queries with SQLite fallback."""
    if settings.analytics_backend.lower() != "neo4j":
        return AnalyticsResult(_sqlite_query(path, scene, parameters), "sqlite")

    driver = None
    try:
        driver = _configured_driver()
        dataset = _dataset_scope(path)
        _ensure_exported(path, driver, dataset)
        neo4j_scene = {
            "common_buyers": "common_buyers",
            "common_projects": "common_projects",
        }.get(scene, scene)
        payload = query_neo4j(
            driver,
            neo4j_scene,
            dataset=dataset,
            database=settings.neo4j_database,
            **parameters,
        )
        return AnalyticsResult(payload, "neo4j")
    except ValueError:
        raise
    except RuntimeError as exc:
        _invalidate_configured_driver(driver)
        logger.warning("Neo4j analytics unavailable; using SQLite fallback: %s", exc)
        return AnalyticsResult(_sqlite_query(path, scene, parameters), "sqlite", True)
