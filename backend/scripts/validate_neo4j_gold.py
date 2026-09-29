"""Run the five relationship validations against a live Neo4j mirror.

The reviewed Gold is imported into a fresh SQLite validation database first.
That database is the source for ``sync_to_neo4j`` and for the SQLite side of
the comparison.  Every validation call made by the existing Gold checker is
captured once for SQLite and once for Neo4j, so the report can show a direct
逐项 comparison rather than only two aggregate scores.

Run from ``backend``::

    BIDINTEL_TEST_NEO4J_URI=bolt://127.0.0.1:7687 \
    BIDINTEL_TEST_NEO4J_PASSWORD=... \
    python -m scripts.validate_neo4j_gold --gold <reviewed-gold.json>
"""

from __future__ import annotations

import argparse
import itertools
import json
import os
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from app import analytics
from app.graph import create_driver, query_neo4j, sync_to_neo4j
from scripts import validate_gold_queries as gold_queries

BACKEND_DIR = Path(__file__).resolve().parents[1]
DEFAULT_OUTPUT_DIR = BACKEND_DIR / ".data" / "query-validation"
DEFAULT_DATASET = "gold-24-neo4j-20260929"
TOP = gold_queries.TOP


def _call_key(name: str, args: tuple[Any, ...], kwargs: dict[str, Any]) -> str:
    return json.dumps([name, args, kwargs], ensure_ascii=False, sort_keys=True, default=str)


def _canonical(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(key): _canonical(item) for key, item in sorted(value.items(), key=lambda pair: str(pair[0]))}
    if isinstance(value, list):
        return [_canonical(item) for item in value]
    if isinstance(value, tuple):
        return [_canonical(item) for item in value]
    return value


def _scene_name(method: str) -> str:
    return {
        "buyer_awardees": "scene1_buyer_awardees",
        "buyer_bidders": "scene2_buyer_bidders",
        "supplier_co_bidders": "scene3_supplier_co_bidders",
        "common_award_buyers": "scene4_common_award_buyers",
        "common_bid_packages": "scene5_common_bid_projects",
    }[method]


def _party_selection_coverage(calls: dict[str, Any]) -> dict[str, dict[str, int]]:
    coverage = {
        "scene4_common_award_buyers": {"two_party": 0, "three_party": 0},
        "scene5_common_bid_projects": {"two_party": 0, "three_party": 0},
    }
    for key in calls:
        method, args, _kwargs = json.loads(key)
        scene = _scene_name(method)
        if scene not in coverage:
            continue
        count = len(args[0]) if args and isinstance(args[0], list) else 0
        if count == 2:
            coverage[scene]["two_party"] += 1
        elif count == 3:
            coverage[scene]["three_party"] += 1
    return coverage


def _scene_samples(sqlite_calls: dict[str, Any], neo4j_calls: dict[str, Any]) -> dict[str, Any]:
    def result_row_count(value: Any) -> int:
        if isinstance(value, list):
            return len(value) + sum(result_row_count(item) for item in value)
        if isinstance(value, dict):
            return sum(result_row_count(item) for item in value.values())
        return 0

    candidates: dict[str, list[tuple[str, Any, Any]]] = {}
    for key in sorted(set(sqlite_calls) | set(neo4j_calls)):
        method = json.loads(key)[0]
        scene = _scene_name(method)
        sqlite_result = sqlite_calls.get(key)
        neo4j_result = neo4j_calls.get(key)
        candidates.setdefault(scene, []).append((key, sqlite_result, neo4j_result))

    samples: dict[str, Any] = {}
    for scene, options in candidates.items():
        key, sqlite_result, neo4j_result = next(
            (
                option
                for option in options
                if result_row_count(option[1]) > 0 and result_row_count(option[2]) > 0
            ),
            options[0],
        )
        samples[scene] = {
            "call": key,
            "sqlite": sqlite_result,
            "neo4j": neo4j_result,
            "matches": sqlite_result == neo4j_result,
        }
    return samples


class _Capture:
    """Expose the five analytics methods while recording their JSON results."""

    def __init__(self, backend: Callable[..., dict[str, Any]]):
        self.backend = backend
        self.calls: dict[str, Any] = {}

    def _run(self, name: str, *args: Any, **kwargs: Any) -> dict[str, Any]:
        result = self.backend(name, *args, **kwargs)
        self.calls[_call_key(name, args[1:], kwargs)] = _canonical(result)
        return result

    def buyer_awardees(self, path: Path, buyer_id: int, **kwargs: Any) -> dict[str, Any]:
        return self._run("buyer_awardees", path, buyer_id, **kwargs)

    def buyer_bidders(self, path: Path, buyer_id: int, **kwargs: Any) -> dict[str, Any]:
        return self._run("buyer_bidders", path, buyer_id, **kwargs)

    def supplier_co_bidders(self, path: Path, supplier_id: int, **kwargs: Any) -> dict[str, Any]:
        return self._run("supplier_co_bidders", path, supplier_id, **kwargs)

    def common_award_buyers(self, path: Path, supplier_ids: list[int], **kwargs: Any) -> dict[str, Any]:
        return self._run("common_award_buyers", path, supplier_ids, **kwargs)

    def common_bid_packages(self, path: Path, supplier_ids: list[int], **kwargs: Any) -> dict[str, Any]:
        return self._run("common_bid_packages", path, supplier_ids, **kwargs)


def _sqlite_backend(name: str, path: Path, *args: Any, **kwargs: Any) -> dict[str, Any]:
    return getattr(analytics, name)(path, *args, **kwargs)


class _Neo4jBackend:
    def __init__(self, driver: Any, dataset: str, database: str):
        self.driver = driver
        self.dataset = dataset
        self.database = database

    def __call__(self, name: str, _path: Path, *args: Any, **kwargs: Any) -> dict[str, Any]:
        scene = {
            "common_award_buyers": "common_buyers",
            "common_bid_packages": "common_projects",
        }.get(name, name)
        parameters: dict[str, Any] = {"dataset": self.dataset, "database": self.database}
        if name == "buyer_awardees":
            parameters["buyer_id"] = args[0]
        elif name == "buyer_bidders":
            parameters["buyer_id"] = args[0]
            parameters["top"] = kwargs.get("top", TOP)
            parameters["include_winners"] = kwargs.get("include_winners", False)
        elif name == "supplier_co_bidders":
            parameters["supplier_id"] = args[0]
            parameters["top"] = kwargs.get("top", TOP)
            parameters["include_winners"] = kwargs.get("include_winners", False)
        elif name in {"common_award_buyers", "common_bid_packages"}:
            parameters["supplier_ids"] = args[0]
        else:
            raise ValueError(f"unknown analytics method: {name}")
        return query_neo4j(self.driver, scene, **parameters)


def _validation_report(kind: str, gold: dict[str, Any], database: Path) -> dict[str, Any]:
    return {
        "validation": f"GAP 6a reviewed-gold {kind} relationship validation",
        "created_at_utc": datetime.now(UTC).isoformat(),
        "gold_status": gold["status"],
        "gold_sha256": gold["_sha256"],
        "database_path": str(database.resolve()),
        "scenes": {
            "scene1_buyer_awardees": {"checked": 0, "failed": 0},
            "scene2_buyer_bidders": {"checked": 0, "failed": 0},
            "scene3_supplier_co_bidders": {"checked": 0, "failed": 0},
            "scene4_common_award_buyers": {"checked": 0, "failed": 0},
            "scene5_common_bid_projects": {"checked": 0, "failed": 0},
        },
        "mismatches": [],
    }


def _run_gold_checks(
    gold: dict[str, Any], imported: dict[str, Any], database: Path, backend: _Capture, kind: str
) -> dict[str, Any]:
    index = gold_queries.gold_indexes(gold)
    report = _validation_report(kind, gold, database)
    original = gold_queries.analytics
    gold_queries.analytics = backend
    try:
        gold_queries.validate_scene_1(gold, index, imported, report)
        gold_queries.validate_scene_2(index, imported, report)
        gold_queries.validate_scene_3(index, imported, report)
        gold_queries.validate_scene_4(index, imported, report)
        gold_queries.validate_scene_5(index, imported, report)
    finally:
        gold_queries.analytics = original
    report["summary"] = {
        "checked": sum(row["checked"] for row in report["scenes"].values()),
        "failed": sum(row["failed"] for row in report["scenes"].values()),
    }
    return report


def _new_path(directory: Path, suffix: str) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    return directory / f"neo4j-gold-{stamp}-{os.getpid()}.{suffix}"


def run(
    gold_path: Path,
    *,
    uri: str,
    user: str,
    password: str,
    database: str = "neo4j",
    dataset: str = DEFAULT_DATASET,
    sqlite_path: Path | None = None,
) -> dict[str, Any]:
    gold = gold_queries.load_gold(gold_path)
    sqlite_path = sqlite_path or _new_path(DEFAULT_OUTPUT_DIR, "sqlite")
    if sqlite_path.exists():
        raise FileExistsError(f"refusing to overwrite existing DB: {sqlite_path}")
    imported = gold_queries.import_gold(gold, sqlite_path)
    driver = create_driver(uri, user, password)
    try:
        export = sync_to_neo4j(sqlite_path, driver, dataset=dataset, database=database)
        sqlite_capture = _Capture(_sqlite_backend)
        neo4j_capture = _Capture(_Neo4jBackend(driver, dataset, database))
        sqlite_report = _run_gold_checks(gold, imported, sqlite_path, sqlite_capture, "SQLite")
        neo4j_report = _run_gold_checks(gold, imported, sqlite_path, neo4j_capture, "Neo4j")
        keys = sorted(set(sqlite_capture.calls) | set(neo4j_capture.calls))
        mismatches = [
            {
                "call": key,
                "sqlite": sqlite_capture.calls.get(key),
                "neo4j": neo4j_capture.calls.get(key),
            }
            for key in keys
            if sqlite_capture.calls.get(key) != neo4j_capture.calls.get(key)
        ]
        return {
            "validation": "GAP 6a reviewed-gold SQLite versus live Neo4j",
            "created_at_utc": datetime.now(UTC).isoformat(),
            "gold_path": str(gold_path.resolve()),
            "gold_status": gold["status"],
            "gold_sha256": gold["_sha256"],
            "neo4j": {"uri": uri, "database": database, "dataset": dataset},
            "sqlite_database": str(sqlite_path.resolve()),
            "import": {**imported["actual_counts"], **export},
            "sqlite_validation": sqlite_report,
            "neo4j_validation": neo4j_report,
            "comparison": {
                "checked": len(keys),
                "mismatched": len(mismatches),
                "passed": len(keys) - len(mismatches),
                "mismatches": mismatches[:200],
                "scene_samples": _scene_samples(sqlite_capture.calls, neo4j_capture.calls),
            },
            "coverage": {
                "multi_package": any(len(project["packages"]) > 1 for project in gold_queries.gold_indexes(gold)["projects"].values()),
                "unknown": any(
                    bidder.get("outcome") == "unknown"
                    for notice in gold["notices"]
                    for package in notice.get("packages", [])
                    for bidder in package.get("bidders", [])
                ),
                "nonwinner": any(
                    bidder.get("outcome") == "nonwinner"
                    for notice in gold["notices"]
                    for package in notice.get("packages", [])
                    for bidder in package.get("bidders", [])
                ),
                "party_selection_calls": _party_selection_coverage(sqlite_capture.calls),
                "three_party_gold_combinations": sum(
                    1
                    for package in gold_queries.gold_indexes(gold)["packages"].values()
                    for _ in itertools.combinations(sorted(package["bidders"]), 3)
                ),
            },
        }
    finally:
        driver.close()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--gold", type=Path, required=True)
    parser.add_argument("--uri", default=os.getenv("BIDINTEL_TEST_NEO4J_URI", "bolt://127.0.0.1:7687"))
    parser.add_argument("--user", default=os.getenv("BIDINTEL_TEST_NEO4J_USER", "neo4j"))
    parser.add_argument("--password", default=os.getenv("BIDINTEL_TEST_NEO4J_PASSWORD", ""))
    parser.add_argument("--database", default=os.getenv("NEO4J_DATABASE", "neo4j"))
    parser.add_argument("--dataset", default=DEFAULT_DATASET)
    parser.add_argument("--sqlite", type=Path, default=None)
    parser.add_argument("--report", type=Path, default=None)
    args = parser.parse_args(argv)
    report = run(
        args.gold,
        uri=args.uri,
        user=args.user,
        password=args.password,
        database=args.database,
        dataset=args.dataset,
        sqlite_path=args.sqlite,
    )
    report_path = args.report or _new_path(DEFAULT_OUTPUT_DIR, "json")
    if report_path.exists():
        raise FileExistsError(f"refusing to overwrite existing report: {report_path}")
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report["comparison"]["mismatched"] == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
