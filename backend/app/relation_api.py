"""Read-only API for reviewer-facing relationship clues."""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Query

from app.datasets import DatabasePath
from app.relation_clues import query_relation_clues

router = APIRouter(prefix="/api/v1/analytics/relation-clues", tags=["relation-clues"])


@router.get("")
@router.get("/")
def get_relation_clues(
    database: DatabasePath,
    kind: str = "all",
    include_winners: bool = False,
    limit: int = Query(default=100, ge=1, le=500),
    supplier_id: int | None = Query(default=None, ge=1),
    buyer_id: int | None = Query(default=None, ge=1),
    min_projects: int = Query(default=2, ge=2, le=100),
):
    try:
        return query_relation_clues(
            database,
            kind=kind,
            include_winners=include_winners,
            limit=limit,
            supplier_id=supplier_id,
            buyer_id=buyer_id,
            min_projects=min_projects,
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
