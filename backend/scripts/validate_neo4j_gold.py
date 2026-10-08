"""Compare all five relationship queries between SQLite and a live Neo4j mirror.

The reviewed Gold is imported into a temporary SQLite database, exported into a
dataset-scoped Neo4j graph, and then checked with the existing independent Gold
oracle. The script never overwrites an existing SQLite database or report.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import uuid
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from app import analytics
from app.graph import create_driver, query_neo4j, sync_to_neo4j
from scripts import validate_gold_queries as gold_queries


class Capture:
    def __init__(self, backend: Callable[..., dict[str, Any]]):
        self.backend = backend
        self.calls: dict[str, Any] = {}

    def _run(self, name: str, *args: Any, **kwargs: Any) -> dict[str, Any]:
        result = self.backend(name, *args, **kwargs)
        key = json.dumps([name, args[1:], kwargs], ensure_ascii=False, sort_keys=True, default=str)
        self.calls[key] = result
        return result

    def buyer_awardees(self, path: Path, buyer_id: int, **kwargs: Any):
        return self._run("buyer_awardees", path, buyer_id, **kwargs)

    def buyer_bidders(self, path: Path, buyer_id: int, **kwargs: Any):
        return self._run("buyer_bidders", path, buyer_id, **kwargs)

    def supplier_co_bidders(self, path: Path, supplier_id: int, **kwargs: Any):
        return self._run("supplier_co_bidders", path, supplier_id, **kwargs)

    def common_award_buyers(self, path: Path, supplier_ids: list[int], **kwargs: Any):
        return self._run("common_award_buyers", path, supplier_ids, **kwargs)

    def common_bid_packages(self, path: Path, supplier_ids: list[int], **kwargs: Any):
        return self._run("common_bid_packages", path, supplier_ids, **kwargs)


class NeoBackend:
    def __init__(self, driver: Any, dataset: str, database: str):
        self.driver = driver
        self.dataset = dataset
        self.database = database

    def __call__(self, name: str, _path: Path, *args: Any, **kwargs: Any):
        scene = {
            "common_award_buyers": "common_buyers",
            "common_bid_packages": "common_projects",
        }.get(name, name)
        params = {"dataset": self.dataset, "database": self.database}
        if name in {"buyer_awardees", "buyer_bidders"}:
            params["buyer_id"] = args[0]
        elif name == "supplier_co_bidders":
            params["supplier_id"] = args[0]
        else:
            params["supplier_ids"] = args[0]
        if name in {"buyer_bidders", "supplier_co_bidders"}:
            params["top"] = kwargs.get("top", gold_queries.TOP)
            params["include_winners"] = kwargs.get("include_winners", True)
        return query_neo4j(self.driver, scene, **params)


def _run_checks(gold: dict[str, Any], imported: dict[str, Any], database: Path, capture: Capture, label: str):
    index = gold_queries.gold_indexes(gold)
    report = {
        "validation": f"reviewed Gold {label} relationship validation",
        "created_at_utc": datetime.now(UTC).isoformat(),
        "gold_sha256": gold["_sha256"],
        "database_path": str(database.resolve()),
        "scenes": {
            name: {"checked": 0, "failed": 0}
            for name in (
                "scene1_buyer_awardees", "scene2_buyer_bidders",
                "scene3_supplier_co_bidders", "scene4_common_award_buyers",
                "scene5_common_bid_projects",
            )
        },
        "mismatches": [],
    }
    original = gold_queries.analytics
    gold_queries.analytics = capture
    try:
        gold_queries.validate_scene_1(gold, index, imported, report)
        gold_queries.validate_scene_2(index, imported, report)
        gold_queries.validate_scene_3(index, imported, report)
        gold_queries.validate_scene_4(index, imported, report)
        gold_queries.validate_scene_5(index, imported, report)
    finally:
        gold_queries.analytics = original
    report["summary"] = {
        "checked": sum(value["checked"] for value in report["scenes"].values()),
        "failed": sum(value["failed"] for value in report["scenes"].values()),
    }
    return report


def run(
    gold_path: Path,
    *,
    uri: str,
    user: str,
    password: str,
    dataset: str,
    database: str,
    sqlite_path: Path,
    report_path: Path,
    cleanup_dataset: bool = False,
) -> dict[str, Any]:
    if sqlite_path.exists() or report_path.exists():
        raise FileExistsError("refusing to overwrite validation output")
    gold = gold_queries.load_gold(gold_path)
    imported = gold_queries.import_gold(gold, sqlite_path)
    driver = create_driver(uri, user, password)
    try:
        exported = sync_to_neo4j(sqlite_path, driver, dataset=dataset, database=database)
        sqlite_capture = Capture(lambda name, path, *args, **kwargs: getattr(analytics, name)(path, *args, **kwargs))
        neo_capture = Capture(NeoBackend(driver, dataset, database))
        sqlite_report = _run_checks(gold, imported, sqlite_path, sqlite_capture, "SQLite")
        neo_report = _run_checks(gold, imported, sqlite_path, neo_capture, "Neo4j")
        keys = sorted(set(sqlite_capture.calls) | set(neo_capture.calls))
        mismatches = [
            {"call": key, "sqlite": sqlite_capture.calls.get(key), "neo4j": neo_capture.calls.get(key)}
            for key in keys if sqlite_capture.calls.get(key) != neo_capture.calls.get(key)
        ]
        result = {
            "validation": "reviewed Gold SQLite versus live Neo4j",
            "created_at_utc": datetime.now(UTC).isoformat(),
            "gold_path": str(gold_path.resolve()),
            "gold_sha256": gold["_sha256"],
            "neo4j": {"uri": uri, "database": database, "dataset": dataset},
            "import": exported,
            "sqlite_validation": sqlite_report,
            "neo4j_validation": neo_report,
            "comparison": {"checked": len(keys), "mismatched": len(mismatches), "passed": len(keys) - len(mismatches), "mismatches": mismatches},
        }
        report_path.parent.mkdir(parents=True, exist_ok=True)
        report_path.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        return result
    finally:
        if cleanup_dataset:
            with driver.session(database=database) as session:
                session.run(
                    "MATCH (n:BidIntelNode {dataset:$dataset}) DETACH DELETE n",
                    dataset=dataset,
                ).consume()
                session.run(
                    "MATCH (d:BidIntelDataset {name:$dataset}) DELETE d",
                    dataset=dataset,
                ).consume()
        driver.close()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--gold", type=Path, required=True)
    parser.add_argument("--uri", default=os.getenv("BIDINTEL_TEST_NEO4J_URI", "bolt://127.0.0.1:7687"))
    parser.add_argument("--user", default=os.getenv("BIDINTEL_TEST_NEO4J_USER", "neo4j"))
    parser.add_argument("--password", default=os.getenv("BIDINTEL_TEST_NEO4J_PASSWORD", ""))
    parser.add_argument("--dataset", help="persist under this dataset name; default uses a temporary scope")
    parser.add_argument("--database", default="neo4j")
    parser.add_argument("--sqlite", type=Path, required=True)
    parser.add_argument("--report", type=Path, required=True)
    args = parser.parse_args()
    temporary_dataset = args.dataset is None
    dataset = args.dataset or f"gold-validation-{uuid.uuid4().hex}"
    result = run(
        args.gold,
        uri=args.uri,
        user=args.user,
        password=args.password,
        dataset=dataset,
        database=args.database,
        sqlite_path=args.sqlite,
        report_path=args.report,
        cleanup_dataset=temporary_dataset,
    )
    print(json.dumps(result["comparison"], ensure_ascii=False, indent=2))
    return 0 if result["comparison"]["mismatched"] == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
