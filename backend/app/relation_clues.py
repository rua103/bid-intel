"""Explainable relationship clues derived from the stored SQLite evidence.

This module deliberately sits beside the existing five analytics queries.  It
does not change extraction, package normalization, or evaluation data.  Every
clue carries the records used to construct it (notice, project, package and
source evidence) so that a reviewer can jump back to the existing notice
detail panel.

SQLite remains the source of truth for this first version.  The functions only
read rows and never call a model or use model confidence values.
"""

from __future__ import annotations

import json
from collections import defaultdict
from collections.abc import Iterable
from pathlib import Path
from typing import Any

from app.storage import connect, initialize

RELATION_KINDS = {
    "common_bidding",
    "repeat_cooperation",
    "supplier_distribution",
    "buyer_network",
}

_KIND_ALIASES = {
    "all": "all",
    "common": "common_bidding",
    "common-bidding": "common_bidding",
    "common_bidding": "common_bidding",
    "co-bidding": "common_bidding",
    "repeat": "repeat_cooperation",
    "repeat-cooperation": "repeat_cooperation",
    "repeat_cooperation": "repeat_cooperation",
    "distribution": "supplier_distribution",
    "supplier-distribution": "supplier_distribution",
    "supplier_distribution": "supplier_distribution",
    "network": "buyer_network",
    "buyer-network": "buyer_network",
    "buyer_network": "buyer_network",
}

_TIME_BASIS = "公告导入时间：notices.created_at（当前数据模型未保存公告发布时间）"

DEFINITIONS: dict[str, dict[str, Any]] = {
    "common_bidding": {
        "label": "供应商共同投标",
        "description": "同一采购包中同时记录的两家主体关系；项目数按 project_id 去重。",
        "project_deduplication": "按 project_id 去重",
        "package_deduplication": "按 package_id 保留并展示",
    },
    "repeat_cooperation": {
        "label": "跨项目重复合作",
        "description": "同一对主体在至少两个不同项目中共同出现；项目数按 project_id 去重。",
        "project_deduplication": "按 project_id 去重，至少 2 个项目才形成线索",
        "package_deduplication": "按 package_id 保留并展示",
    },
    "supplier_distribution": {
        "label": "供应商参与/中标项目分布",
        "description": "按供应商汇总参与项目数与有 awards 记录的中标项目数。",
        "project_deduplication": "参与和中标分别按 project_id 去重",
        "package_deduplication": "包数按 package_id 去重",
    },
    "buyer_network": {
        "label": "采购单位与供应商关系网络",
        "description": "采购单位与供应商的参与、成交项目/包计数摘要。",
        "project_deduplication": "参与和中标分别按 project_id 去重",
        "package_deduplication": "包数按 package_id 去重",
    },
}


def normalize_kind(kind: str | None) -> str:
    value = (kind or "all").strip().casefold()
    normalized = _KIND_ALIASES.get(value)
    if normalized is None:
        choices = ", ".join(sorted(RELATION_KINDS | {"all"}))
        raise ValueError(f"未知关系线索类型：{kind}；可选值：{choices}")
    return normalized


def _json_list(value: str | None) -> list[Any]:
    if not value:
        return []
    try:
        parsed = json.loads(value)
    except (TypeError, json.JSONDecodeError):
        return []
    return parsed if isinstance(parsed, list) else []


def _distinct(values: Iterable[Any]) -> list[Any]:
    seen: set[Any] = set()
    result: list[Any] = []
    for value in values:
        if value in seen:
            continue
        seen.add(value)
        result.append(value)
    return result


def _fetch_records(path: Path) -> tuple[list[dict[str, Any]], list[dict[str, Any]], dict[int, dict[str, Any]]]:
    """Read all relationship rows and notice metadata in one read-only pass."""
    initialize(path)
    with connect(path) as connection:
        bid_rows = [
            dict(row)
            for row in connection.execute(
                """
                SELECT b.id AS record_id, 'bid' AS record_type,
                       b.package_id, p.project_id, x.notice_id,
                       p.package_code, p.package_name,
                       x.project_name, x.project_number,
                       x.buyer_organization_id,
                       buyer.canonical_name AS buyer_name,
                       b.organization_id AS supplier_id,
                       supplier.canonical_name AS supplier_name,
                       b.outcome, b.source_file, b.source_location,
                       b.source_evidence, n.created_at
                FROM bid_participations b
                JOIN packages p ON p.id = b.package_id
                JOIN projects x ON x.id = p.project_id
                JOIN notices n ON n.id = x.notice_id
                LEFT JOIN organizations buyer ON buyer.id = x.buyer_organization_id
                JOIN organizations supplier ON supplier.id = b.organization_id
                ORDER BY x.id, p.id, b.organization_id, b.id
                """
            ).fetchall()
        ]
        award_rows = [
            dict(row)
            for row in connection.execute(
                """
                SELECT a.id AS record_id, 'award' AS record_type,
                       a.package_id, p.project_id, x.notice_id,
                       p.package_code, p.package_name,
                       x.project_name, x.project_number,
                       x.buyer_organization_id,
                       buyer.canonical_name AS buyer_name,
                       a.organization_id AS supplier_id,
                       supplier.canonical_name AS supplier_name,
                       'winner' AS outcome, a.source_file, a.source_location,
                       a.source_evidence, n.created_at, a.award_amount
                FROM awards a
                JOIN packages p ON p.id = a.package_id
                JOIN projects x ON x.id = p.project_id
                JOIN notices n ON n.id = x.notice_id
                LEFT JOIN organizations buyer ON buyer.id = x.buyer_organization_id
                JOIN organizations supplier ON supplier.id = a.organization_id
                ORDER BY x.id, p.id, a.organization_id, a.id
                """
            ).fetchall()
        ]
        notice_rows = {
            int(row["id"]): dict(row)
            for row in connection.execute(
                """
                SELECT id, project_name, project_number, procurement_unit,
                       warnings_json, created_at
                FROM notices ORDER BY id
                """
            ).fetchall()
        }
    return bid_rows, award_rows, notice_rows


def _scope(include_winners: bool, *, kind: str) -> dict[str, Any]:
    outcomes = ["winner", "nonwinner", "unknown"] if include_winners else ["nonwinner"]
    return {
        "include_winners": include_winners,
        "participation_outcomes": outcomes,
        "participation_rule": (
            "包含 winner、nonwinner、unknown 的全部已记录投标主体"
            if include_winners
            else "仅统计 outcome=nonwinner；unknown 不推定为未中标"
        ),
        "award_rule": "中标项目/包仅按 awards 记录统计，保留多个中标方",
        "project_deduplication": DEFINITIONS[kind]["project_deduplication"],
        "package_deduplication": DEFINITIONS[kind]["package_deduplication"],
        "default_package_rule": "保留 package_code=default，并逐条标示包号待核查",
        "time_basis": _TIME_BASIS,
    }


def _record_selected(row: dict[str, Any], include_winners: bool) -> bool:
    return include_winners or row.get("outcome") == "nonwinner"


def _warning_categories(notice_rows: Iterable[dict[str, Any]], evidence_rows: Iterable[dict[str, Any]]) -> list[str]:
    """Return stable reviewer-facing warning text, without legal conclusions."""
    categories: list[str] = []
    notices = list(notice_rows)
    for notice in notices:
        for raw in _json_list(notice.get("warnings_json")):
            text = str(raw)
            lowered = text.casefold()
            if "ocr" in lowered or "文字识别" in text or "扫描" in text:
                categories.append("OCR 提示：关联公告包含 OCR/文字识别告警，需人工核验")
            if (
                "附件" in text
                and any(token in text for token in ("解析", "失败", "不支持", "不可用", "无法"))
            ) or "attachment" in lowered:
                categories.append("附件解析提示：关联公告包含附件解析失败或不支持格式的告警")
    rows = list(evidence_rows)
    if rows and any(
        not (str(row.get("source_evidence") or "").strip())
        or not (str(row.get("source_file") or "").strip())
        or not (str(row.get("source_location") or "").strip())
        for row in rows
    ):
        categories.append("证据不足：关联投标或中标记录未保存完整原文证据")
    if any(str(row.get("package_code") or "default").strip().casefold() == "default" for row in rows):
        categories.append("default 包号提示：原文未提供可唯一识别包号，包范围待核查")
    return _distinct(categories)


def _evidence_links(rows: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    links: list[dict[str, Any]] = []
    seen: set[tuple[str, int]] = set()
    for row in rows:
        key = (str(row.get("record_type")), int(row["record_id"]))
        if key in seen:
            continue
        seen.add(key)
        evidence = str(row.get("source_evidence") or "").strip()
        links.append(
            {
                "record_type": row.get("record_type"),
                "record_id": int(row["record_id"]),
                "notice_id": int(row["notice_id"]),
                "project_id": int(row["project_id"]),
                "package_id": int(row["package_id"]),
                "package_code": row.get("package_code") or "default",
                "organization_id": int(row["supplier_id"]),
                "organization_name": row.get("supplier_name"),
                "source_file": row.get("source_file"),
                "source_location": row.get("source_location"),
                "source_evidence": row.get("source_evidence"),
                "evidence_status": "available" if evidence else "missing",
                "notice_url": f"/api/v1/notices/{int(row['notice_id'])}",
            }
        )
    links.sort(key=lambda value: (value["notice_id"], value["project_id"], value["package_id"], value["record_type"], value["record_id"]))
    return links


def _references(
    rows: Iterable[dict[str, Any]],
    notice_rows: dict[int, dict[str, Any]],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]], dict[str, Any]]:
    rows = list(rows)
    notice_ids = sorted({int(row["notice_id"]) for row in rows})
    first_by_notice: dict[int, dict[str, Any]] = {}
    first_by_project: dict[int, dict[str, Any]] = {}
    first_by_package: dict[int, dict[str, Any]] = {}
    for row in rows:
        first_by_notice.setdefault(int(row["notice_id"]), row)
        first_by_project.setdefault(int(row["project_id"]), row)
        first_by_package.setdefault(int(row["package_id"]), row)
    notices = []
    for notice_id in notice_ids:
        row = first_by_notice[notice_id]
        notice = notice_rows.get(notice_id, {})
        notices.append(
            {
                "notice_id": notice_id,
                "project_name": notice.get("project_name") or row.get("project_name"),
                "project_number": notice.get("project_number") or row.get("project_number"),
                "procurement_unit": notice.get("procurement_unit") or row.get("buyer_name"),
                "created_at": notice.get("created_at") or row.get("created_at"),
                "notice_url": f"/api/v1/notices/{notice_id}",
            }
        )
    projects = [
        {
            "project_id": project_id,
            "notice_id": int(row["notice_id"]),
            "project_name": row.get("project_name"),
            "project_number": row.get("project_number"),
            "buyer_organization_id": row.get("buyer_organization_id"),
            "buyer_name": row.get("buyer_name"),
        }
        for project_id, row in sorted(first_by_project.items())
    ]
    packages = [
        {
            "package_id": package_id,
            "project_id": int(row["project_id"]),
            "notice_id": int(row["notice_id"]),
            "package_code": row.get("package_code") or "default",
            "package_name": row.get("package_name"),
        }
        for package_id, row in sorted(first_by_package.items())
    ]
    times = [
        str(notice_rows.get(notice_id, {}).get("created_at") or first_by_notice[notice_id].get("created_at"))
        for notice_id in notice_ids
    ]
    times = [value for value in times if value and value != "None"]
    time_range = {
        "start": min(times) if times else None,
        "end": max(times) if times else None,
        "basis": _TIME_BASIS,
    }
    return notices, projects, packages, time_range


def _base_clue(
    kind: str,
    rows: Iterable[dict[str, Any]],
    notice_rows: dict[int, dict[str, Any]],
    *,
    include_winners: bool,
    metrics: dict[str, Any],
    entities: list[dict[str, Any]],
    clue_id: str,
    title: str,
    evidence_rows: Iterable[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    rows = list(rows)
    evidence_rows = list(rows if evidence_rows is None else evidence_rows)
    notices, projects, packages, time_range = _references(rows, notice_rows)
    warnings = _warning_categories(
        (notice_rows.get(int(row["notice_id"]), {}) for row in rows), evidence_rows
    )
    return {
        "id": clue_id,
        "type": kind,
        "label": "待核查线索",
        "title": title,
        "entities": entities,
        "metrics": metrics,
        "notices": notices,
        "projects": projects,
        "packages": packages,
        "time_range": time_range,
        "warnings": warnings,
        "evidence_links": _evidence_links(evidence_rows),
        "scope": _scope(include_winners, kind=kind),
    }


def _organization_entity(row: dict[str, Any], *, key: str = "supplier") -> dict[str, Any]:
    return {
        "organization_id": int(row[f"{key}_id"]),
        "canonical_name": row[f"{key}_name"],
        "role": key,
    }


def common_bidding_clues(
    bid_rows: list[dict[str, Any]],
    notice_rows: dict[int, dict[str, Any]],
    *,
    include_winners: bool,
    limit: int,
    supplier_id: int | None = None,
) -> list[dict[str, Any]]:
    selected = [row for row in bid_rows if _record_selected(row, include_winners)]
    by_package: dict[int, list[dict[str, Any]]] = defaultdict(list)
    for row in selected:
        by_package[int(row["package_id"])].append(row)
    pairs: dict[tuple[int, int], list[dict[str, Any]]] = defaultdict(list)
    for rows in by_package.values():
        # A unique organization per package prevents duplicate records from
        # inflating the relationship count.
        unique = {int(row["supplier_id"]): row for row in rows}
        ids = sorted(unique)
        for index, left_id in enumerate(ids):
            for right_id in ids[index + 1 :]:
                if supplier_id is not None and supplier_id not in (left_id, right_id):
                    continue
                pairs[(left_id, right_id)].extend([unique[left_id], unique[right_id]])
    results: list[dict[str, Any]] = []
    for (left_id, right_id), rows in pairs.items():
        # A pair contributes at most one row per package.  Keep one evidence
        # row per endpoint in the clue while deriving counts from packages.
        pair_package_rows: dict[int, dict[int, dict[str, Any]]] = defaultdict(dict)
        for row in rows:
            pair_package_rows[int(row["package_id"])][int(row["supplier_id"])] = row
        rows = [row for package in pair_package_rows.values() for row in package.values()]
        project_ids = {int(row["project_id"]) for row in rows}
        package_ids = set(pair_package_rows)
        left = next(row for row in rows if int(row["supplier_id"]) == left_id)
        right = next(row for row in rows if int(row["supplier_id"]) == right_id)
        results.append(
            _base_clue(
                "common_bidding",
                rows,
                notice_rows,
                include_winners=include_winners,
                metrics={
                    "project_count": len(project_ids),
                    "package_count": len(package_ids),
                    "relationship_rule": "同一 package_id 中同时存在两家主体的投标记录",
                },
                entities=[_organization_entity(left), _organization_entity(right)],
                clue_id=f"common-bidding:{left_id}-{right_id}",
                title=f"{left['supplier_name']} 与 {right['supplier_name']} 共同投标 {len(project_ids)} 个项目",
            )
        )
    results.sort(key=lambda clue: (-clue["metrics"]["project_count"], -clue["metrics"]["package_count"], clue["id"]))
    return results[:limit]


def repeat_cooperation_clues(
    bid_rows: list[dict[str, Any]],
    notice_rows: dict[int, dict[str, Any]],
    *,
    include_winners: bool,
    limit: int,
    supplier_id: int | None = None,
    min_projects: int = 2,
) -> list[dict[str, Any]]:
    # Build the full pair map before filtering, otherwise a low API limit could
    # hide a qualifying repeated pair behind one-off pairs.
    all_clues = common_bidding_clues(
        bid_rows,
        notice_rows,
        include_winners=include_winners,
        limit=1_000_000,
        supplier_id=supplier_id,
    )
    results = [
        clue
        for clue in all_clues
        if clue["metrics"]["project_count"] >= min_projects
    ]
    for clue in results:
        clue["type"] = "repeat_cooperation"
        clue["id"] = clue["id"].replace("common-bidding:", "repeat-cooperation:", 1)
        clue["title"] = clue["title"].replace("共同投标", "跨项目重复共同投标")
        clue["scope"] = _scope(include_winners, kind="repeat_cooperation")
    return results[:limit]


def supplier_distribution_clues(
    bid_rows: list[dict[str, Any]],
    award_rows: list[dict[str, Any]],
    notice_rows: dict[int, dict[str, Any]],
    *,
    include_winners: bool,
    limit: int,
    supplier_id: int | None = None,
) -> list[dict[str, Any]]:
    selected_bids = [row for row in bid_rows if _record_selected(row, include_winners)]
    by_supplier: dict[int, list[dict[str, Any]]] = defaultdict(list)
    for row in selected_bids:
        by_supplier[int(row["supplier_id"])].append(row)
    for row in award_rows:
        by_supplier.setdefault(int(row["supplier_id"]), [])
    if supplier_id is not None:
        by_supplier = {supplier_id: by_supplier.get(supplier_id, [])}
    results: list[dict[str, Any]] = []
    for organization_id, rows in sorted(by_supplier.items(), key=lambda value: value[0]):
        awards = [row for row in award_rows if int(row["supplier_id"]) == organization_id]
        reference_rows = [*rows, *awards]
        if not reference_rows:
            continue
        source = reference_rows[0]
        participation_projects = {int(row["project_id"]) for row in rows}
        award_projects = {int(row["project_id"]) for row in awards}
        participation_packages = {int(row["package_id"]) for row in rows}
        award_packages = {int(row["package_id"]) for row in awards}
        results.append(
            _base_clue(
                "supplier_distribution",
                reference_rows,
                notice_rows,
                include_winners=include_winners,
                metrics={
                    "participation_project_count": len(participation_projects),
                    "award_project_count": len(award_projects),
                    "participation_package_count": len(participation_packages),
                    "award_package_count": len(award_packages),
                    "distribution_rule": "参与来自筛选后的 bid_participations；中标来自 awards",
                },
                entities=[_organization_entity(source)],
                clue_id=f"supplier-distribution:{organization_id}",
                title=f"{source['supplier_name']} 参与 {len(participation_projects)} 个项目，中标 {len(award_projects)} 个项目",
                evidence_rows=reference_rows,
            )
        )
    results.sort(key=lambda clue: (-clue["metrics"]["participation_project_count"], clue["id"]))
    return results[:limit]


def buyer_network_clues(
    bid_rows: list[dict[str, Any]],
    award_rows: list[dict[str, Any]],
    notice_rows: dict[int, dict[str, Any]],
    *,
    include_winners: bool,
    limit: int,
    buyer_id: int | None = None,
) -> list[dict[str, Any]]:
    selected_bids = [row for row in bid_rows if _record_selected(row, include_winners)]
    edges: dict[tuple[int, int], list[dict[str, Any]]] = defaultdict(list)
    for row in selected_bids:
        if row.get("buyer_organization_id") is None:
            continue
        if buyer_id is not None and int(row["buyer_organization_id"]) != buyer_id:
            continue
        edges[(int(row["buyer_organization_id"]), int(row["supplier_id"]))].append(row)
    for row in award_rows:
        if row.get("buyer_organization_id") is None:
            continue
        if buyer_id is not None and int(row["buyer_organization_id"]) != buyer_id:
            continue
        edges.setdefault((int(row["buyer_organization_id"]), int(row["supplier_id"])), []).append(row)
    results: list[dict[str, Any]] = []
    for (buyer_org_id, supplier_org_id), rows in sorted(edges.items()):
        participation = [row for row in rows if row.get("record_type") == "bid"]
        awards = [row for row in rows if row.get("record_type") == "award"]
        source = rows[0]
        participation_projects = {int(row["project_id"]) for row in participation}
        award_projects = {int(row["project_id"]) for row in awards}
        participation_packages = {int(row["package_id"]) for row in participation}
        award_packages = {int(row["package_id"]) for row in awards}
        results.append(
            _base_clue(
                "buyer_network",
                rows,
                notice_rows,
                include_winners=include_winners,
                metrics={
                    "participation_project_count": len(participation_projects),
                    "award_project_count": len(award_projects),
                    "participation_package_count": len(participation_packages),
                    "award_package_count": len(award_packages),
                    "relationship_rule": "采购单位与供应商在同一项目/包存在投标或中标记录",
                },
                entities=[
                    {
                        "organization_id": buyer_org_id,
                        "canonical_name": source.get("buyer_name"),
                        "role": "buyer",
                    },
                    _organization_entity(source),
                ],
                clue_id=f"buyer-network:{buyer_org_id}-{supplier_org_id}",
                title=f"{source.get('buyer_name') or '采购单位'} 与 {source['supplier_name']} 的关系摘要",
                evidence_rows=rows,
            )
        )
    results.sort(
        key=lambda clue: (
            -clue["metrics"]["participation_project_count"],
            -clue["metrics"]["award_project_count"],
            clue["id"],
        )
    )
    return results[:limit]


def query_relation_clues(
    path: Path,
    *,
    kind: str = "all",
    include_winners: bool = True,
    limit: int = 100,
    supplier_id: int | None = None,
    buyer_id: int | None = None,
    min_projects: int = 2,
) -> dict[str, Any]:
    """Return one or all explainable relationship clue collections."""
    normalized = normalize_kind(kind)
    limit = max(1, min(int(limit), 500))
    min_projects = max(2, min(int(min_projects), 100))
    bid_rows, award_rows, notice_rows = _fetch_records(path)
    builders = {
        "common_bidding": lambda: common_bidding_clues(
            bid_rows,
            notice_rows,
            include_winners=include_winners,
            limit=limit,
            supplier_id=supplier_id,
        ),
        "repeat_cooperation": lambda: repeat_cooperation_clues(
            bid_rows,
            notice_rows,
            include_winners=include_winners,
            limit=limit,
            supplier_id=supplier_id,
            min_projects=min_projects,
        ),
        "supplier_distribution": lambda: supplier_distribution_clues(
            bid_rows,
            award_rows,
            notice_rows,
            include_winners=include_winners,
            limit=limit,
            supplier_id=supplier_id,
        ),
        "buyer_network": lambda: buyer_network_clues(
            bid_rows,
            award_rows,
            notice_rows,
            include_winners=include_winners,
            limit=limit,
            buyer_id=buyer_id,
        ),
    }
    kinds = sorted(RELATION_KINDS) if normalized == "all" else [normalized]
    # Keep the API order stable for deterministic UI and tests.
    kinds = [kind_name for kind_name in ("common_bidding", "repeat_cooperation", "supplier_distribution", "buyer_network") if kind_name in kinds]
    clues: list[dict[str, Any]] = []
    counts: dict[str, int] = {}
    for kind_name in kinds:
        rows = builders[kind_name]()
        counts[kind_name] = len(rows)
        clues.extend(rows)
    return {
        "kind": normalized,
        "backend": "sqlite",
        "dataset": path.name,
        "clues": clues,
        "counts": counts,
        "definitions": {
            kind_name: {
                **DEFINITIONS[kind_name],
                "scope": _scope(include_winners, kind=kind_name),
            }
            for kind_name in kinds
        },
        "filters": {
            "supplier_id": supplier_id,
            "buyer_id": buyer_id,
            "min_projects": min_projects,
        },
    }
