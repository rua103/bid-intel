from __future__ import annotations

import json
import sqlite3
from decimal import Decimal
from pathlib import Path
from typing import Any

from app.schemas import ImportResult

SCHEMA = """
PRAGMA foreign_keys = ON;
CREATE TABLE IF NOT EXISTS notices (
    id INTEGER PRIMARY KEY,
    project_name TEXT,
    project_number TEXT,
    procurement_unit TEXT,
    project_budget TEXT,
    announced_total_award TEXT,
    source_files_json TEXT NOT NULL,
    warnings_json TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS organizations (
    id INTEGER PRIMARY KEY,
    canonical_name TEXT NOT NULL,
    normalized_name TEXT NOT NULL UNIQUE,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS organization_aliases (
    id INTEGER PRIMARY KEY,
    organization_id INTEGER NOT NULL REFERENCES organizations(id),
    raw_name TEXT NOT NULL,
    source_notice_id INTEGER REFERENCES notices(id),
    UNIQUE(organization_id, raw_name, source_notice_id)
);
CREATE TABLE IF NOT EXISTS projects (
    id INTEGER PRIMARY KEY,
    notice_id INTEGER NOT NULL UNIQUE REFERENCES notices(id) ON DELETE CASCADE,
    project_name TEXT,
    project_number TEXT,
    buyer_organization_id INTEGER REFERENCES organizations(id),
    project_budget TEXT,
    announced_total_award TEXT,
    amount_type TEXT
);
CREATE TABLE IF NOT EXISTS packages (
    id INTEGER PRIMARY KEY,
    project_id INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    package_code TEXT NOT NULL DEFAULT 'default',
    package_name TEXT,
    package_budget TEXT,
    package_award_total TEXT,
    UNIQUE(project_id, package_code)
);
CREATE TABLE IF NOT EXISTS bid_participations (
    id INTEGER PRIMARY KEY,
    package_id INTEGER NOT NULL REFERENCES packages(id) ON DELETE CASCADE,
    organization_id INTEGER NOT NULL REFERENCES organizations(id),
    raw_name TEXT NOT NULL,
    outcome TEXT NOT NULL DEFAULT 'unknown',
    consortium_members_json TEXT NOT NULL DEFAULT '[]',
    source_file TEXT,
    source_location TEXT,
    source_evidence TEXT,
    UNIQUE(package_id, organization_id)
);
CREATE TABLE IF NOT EXISTS awards (
    id INTEGER PRIMARY KEY,
    package_id INTEGER NOT NULL REFERENCES packages(id) ON DELETE CASCADE,
    organization_id INTEGER NOT NULL REFERENCES organizations(id),
    raw_name TEXT NOT NULL,
    award_amount TEXT,
    source_file TEXT,
    source_location TEXT,
    source_evidence TEXT,
    UNIQUE(package_id, organization_id)
);
CREATE TABLE IF NOT EXISTS procurement_items (
    id INTEGER PRIMARY KEY,
    notice_id INTEGER NOT NULL REFERENCES notices(id) ON DELETE CASCADE,
    package_id INTEGER REFERENCES packages(id) ON DELETE SET NULL,
    product_name TEXT,
    category TEXT,
    brand TEXT,
    model TEXT,
    quantity TEXT,
    quantity_unit TEXT,
    unit_price TEXT,
    total_price TEXT,
    source_file TEXT NOT NULL,
    source_location TEXT NOT NULL,
    source_evidence TEXT,
    extraction_method TEXT NOT NULL,
    confidence REAL NOT NULL DEFAULT 0.0
);
CREATE INDEX IF NOT EXISTS idx_items_product_name ON procurement_items(product_name);
CREATE INDEX IF NOT EXISTS idx_items_brand ON procurement_items(brand);
CREATE INDEX IF NOT EXISTS idx_items_category ON procurement_items(category);
CREATE INDEX IF NOT EXISTS idx_items_model ON procurement_items(model);
CREATE INDEX IF NOT EXISTS idx_bids_org ON bid_participations(organization_id, package_id);
CREATE INDEX IF NOT EXISTS idx_awards_org ON awards(organization_id, package_id);
"""


class _ClosingConnection(sqlite3.Connection):
    """Make ``with connect(...)`` release Windows file handles after commit."""

    def __exit__(self, exc_type, exc_value, traceback):
        result = super().__exit__(exc_type, exc_value, traceback)
        self.close()
        return result


def _db_path(path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


def connect(path: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(_db_path(path), factory=_ClosingConnection)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    return connection


def initialize(path: Path) -> None:
    with connect(path) as connection:
        connection.executescript(SCHEMA)
        for table in ("bid_participations", "awards", "procurement_items"):
            columns = {
                row["name"] for row in connection.execute(f"PRAGMA table_info({table})")
            }
            if "source_evidence" not in columns:
                connection.execute(f"ALTER TABLE {table} ADD COLUMN source_evidence TEXT")
        columns = {row["name"] for row in connection.execute("PRAGMA table_info(bid_participations)")}
        if "consortium_members_json" not in columns:
            connection.execute(
                "ALTER TABLE bid_participations ADD COLUMN consortium_members_json TEXT NOT NULL DEFAULT '[]'"
            )


def _decimal_text(value: Decimal | None) -> str | None:
    return str(value) if value is not None else None


def _normalized_entity_name(name: str) -> str:
    # Conservative normalization only. Do not fuzzy-merge branches or similar names
    # before the official reference data establishes that they are the same entity.
    return "".join(name.strip().split()).casefold()


def _upsert_organization(connection: sqlite3.Connection, raw_name: str, notice_id: int) -> int:
    normalized = _normalized_entity_name(raw_name)
    if not normalized:
        raise ValueError("organization name cannot be empty")
    connection.execute(
        "INSERT OR IGNORE INTO organizations(canonical_name, normalized_name) VALUES (?, ?)",
        (raw_name.strip(), normalized),
    )
    row = connection.execute(
        "SELECT id FROM organizations WHERE normalized_name = ?", (normalized,)
    ).fetchone()
    assert row is not None
    connection.execute(
        "INSERT OR IGNORE INTO organization_aliases(organization_id, raw_name, source_notice_id) "
        "VALUES (?, ?, ?)",
        (row["id"], raw_name.strip(), notice_id),
    )
    return int(row["id"])


def save_import(path: Path, result: ImportResult, source_text: str = "") -> ImportResult:
    initialize(path)
    with connect(path) as connection:
        cursor = connection.execute(
            """INSERT INTO notices (
                project_name, project_number, procurement_unit, project_budget,
                announced_total_award, source_files_json, warnings_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (
                result.metadata.project_name,
                result.metadata.project_number,
                result.metadata.procurement_unit,
                _decimal_text(result.metadata.project_budget),
                _decimal_text(result.metadata.announced_total_award),
                json.dumps(result.source_files, ensure_ascii=False),
                json.dumps(result.warnings, ensure_ascii=False),
            ),
        )
        notice_id = int(cursor.lastrowid)
        buyer_id = None
        if result.metadata.procurement_unit:
            buyer_id = _upsert_organization(connection, result.metadata.procurement_unit, notice_id)
        project_cursor = connection.execute(
            """INSERT INTO projects (
                notice_id, project_name, project_number, buyer_organization_id,
                project_budget, announced_total_award, amount_type
            ) VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (
                notice_id,
                result.metadata.project_name,
                result.metadata.project_number,
                buyer_id,
                _decimal_text(result.metadata.project_budget),
                _decimal_text(result.metadata.announced_total_award),
                "budget_and_award"
                if result.metadata.project_budget is not None
                and result.metadata.announced_total_award is not None
                else "budget"
                if result.metadata.project_budget is not None
                else "announced_total_award"
                if result.metadata.announced_total_award is not None
                else None,
            ),
        )
        project_id = int(project_cursor.lastrowid)
        codes = sorted({row.package_code for row in [*result.items, *result.participants]}) or ["default"]
        package_ids = {}
        for code in codes:
            package_cursor = connection.execute(
                """INSERT INTO packages(project_id, package_code, package_name, package_award_total)
                   VALUES (?, ?, NULL, ?)""",
                (project_id, code, _decimal_text(result.metadata.announced_total_award)
                 if len(codes) == 1 else None),
            )
            package_ids[code] = int(package_cursor.lastrowid)
        for participant in result.participants:
            package_id = package_ids[participant.package_code]
            organization_id = _upsert_organization(
                connection, participant.organization_name, notice_id
            )
            connection.execute(
                """INSERT OR IGNORE INTO bid_participations (
                    package_id, organization_id, raw_name, outcome,
                    source_file, source_location, source_evidence, consortium_members_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    package_id,
                    organization_id,
                    participant.organization_name,
                    participant.outcome,
                    participant.source_file,
                    participant.source_location,
                    participant.source_evidence,
                    json.dumps(participant.consortium_members, ensure_ascii=False),
                ),
            )
            if participant.outcome == "winner":
                connection.execute(
                    """INSERT OR IGNORE INTO awards (
                        package_id, organization_id, raw_name, award_amount,
                        source_file, source_location, source_evidence
                    ) VALUES (?, ?, ?, ?, ?, ?, ?)""",
                    (
                        package_id,
                        organization_id,
                        participant.organization_name,
                        _decimal_text(participant.award_amount),
                        participant.source_file,
                        participant.source_location,
                        participant.source_evidence,
                    ),
                )
        for item in result.items:
            package_id = package_ids[item.package_code]
            connection.execute(
                """INSERT INTO procurement_items (
                    notice_id, package_id, product_name, category, brand, model,
                    quantity, quantity_unit, unit_price, total_price, source_file,
                    source_location, source_evidence, extraction_method, confidence
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    notice_id,
                    package_id,
                    item.product_name,
                    item.category,
                    item.brand,
                    item.model,
                    _decimal_text(item.quantity),
                    item.quantity_unit,
                    _decimal_text(item.unit_price),
                    _decimal_text(item.total_price),
                    item.source_file,
                    item.source_location,
                    item.source_evidence,
                    item.extraction_method,
                    item.confidence,
                ),
            )
        return result.model_copy(update={"notice_id": notice_id})


def search_items(
    path: Path,
    *,
    query: str | None = None,
    category: str | None = None,
    brand: str | None = None,
    model: str | None = None,
    limit: int = 100,
) -> list[dict[str, Any]]:
    initialize(path)
    clauses: list[str] = []
    values: list[Any] = []
    for column, value in (
        ("product_name", query),
        ("category", category),
        ("brand", brand),
        ("model", model),
    ):
        if value:
            clauses.append(f"{column} LIKE ?")
            values.append(f"%{value}%")
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    sql = f"""SELECT i.*, n.project_name, n.project_number, n.procurement_unit
              FROM procurement_items i JOIN notices n ON n.id = i.notice_id
              {where} ORDER BY i.id DESC LIMIT ?"""
    values.append(max(1, min(limit, 500)))
    with connect(path) as connection:
        return [dict(row) for row in connection.execute(sql, values).fetchall()]


def count_notices(path: Path) -> int:
    initialize(path)
    with connect(path) as connection:
        return int(connection.execute("SELECT COUNT(*) FROM notices").fetchone()[0])
