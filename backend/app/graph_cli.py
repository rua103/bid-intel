"""Command line entry point for optional Neo4j export and scene queries."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path

from app.graph import create_driver, query_neo4j, sync_to_neo4j


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="bid-intel-graph")
    parser.add_argument("--database", default=os.getenv("BID_INTEL_DATABASE", ".data/bidintel.db"))
    parser.add_argument("--uri", default=os.getenv("NEO4J_URI", "bolt://127.0.0.1:7687"))
    parser.add_argument("--user", default=os.getenv("NEO4J_USER", "neo4j"))
    parser.add_argument("--password", default=os.getenv("NEO4J_PASSWORD", ""))
    parser.add_argument("--neo4j-database", default=os.getenv("NEO4J_DATABASE", "neo4j"))
    parser.add_argument("--dataset", default=None, help="stable dataset scope; defaults to absolute SQLite path")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("export", help="replace only this dataset in Neo4j")
    scene = commands.add_parser("scene", help="run one of the five parameterized scenes")
    scene.add_argument("name", choices=("buyer_awardees", "buyer_bidders", "supplier_co_bidders", "common_buyers", "common_projects"))
    scene.add_argument("--buyer-id", type=int)
    scene.add_argument("--supplier-id", type=int)
    scene.add_argument("--supplier-ids", default="", help="comma-separated IDs for scenes 4/5")
    scene.add_argument("--top", type=int, default=5)
    scene.add_argument("--exclude-winners", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    database = Path(args.database)
    if not database.is_absolute():
        database = (Path.cwd() / database).resolve()
    dataset = args.dataset or str(database)
    driver = create_driver(args.uri, args.user, args.password)
    try:
        if args.command == "export":
            result = sync_to_neo4j(database, driver, dataset=dataset, database=args.neo4j_database)
        else:
            supplier_ids = [int(value) for value in args.supplier_ids.split(",") if value.strip()]
            result = query_neo4j(
                driver, args.name, dataset=dataset, database=args.neo4j_database,
                buyer_id=args.buyer_id, supplier_id=args.supplier_id,
                supplier_ids=supplier_ids, top=args.top,
                include_winners=not args.exclude_winners,
            )
        print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
        return 0
    finally:
        driver.close()


if __name__ == "__main__":
    raise SystemExit(main())
