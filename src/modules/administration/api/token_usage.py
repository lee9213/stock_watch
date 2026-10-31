"""Token 使用量统计 API（对齐 AlphaLom /token_usage）。"""

from __future__ import annotations

from datetime import datetime, timedelta

from fastapi import APIRouter, Query
from sqlalchemy.orm import Session

from src.platform.persistence.database import get_db
from src.platform.persistence.models import LlmTokenUsage
from fastapi import Depends

router = APIRouter()


def _apply_filters(query, model: str | None, scene: str | None, start_date: str | None, end_date: str | None):
    if isinstance(model, str) and model.strip():
        query = query.filter(LlmTokenUsage.model == model.strip())
    if isinstance(scene, str) and scene.strip():
        query = query.filter(LlmTokenUsage.scene == scene.strip())
    if isinstance(start_date, str) and start_date.strip():
        try:
            start_dt = datetime.strptime(start_date.strip(), "%Y-%m-%d")
            query = query.filter(LlmTokenUsage.created_at >= start_dt)
        except ValueError:
            pass
    if isinstance(end_date, str) and end_date.strip():
        try:
            end_dt = datetime.strptime(end_date.strip(), "%Y-%m-%d") + timedelta(days=1)
            query = query.filter(LlmTokenUsage.created_at < end_dt)
        except ValueError:
            pass
    return query


@router.get("/summary")
def token_usage_summary(
    model: str | None = Query(default=None),
    scene: str | None = Query(default=None),
    start_date: str | None = Query(default=None),
    end_date: str | None = Query(default=None),
    db: Session = Depends(get_db),
):
    query = _apply_filters(db.query(LlmTokenUsage), model, scene, start_date, end_date)
    rows = query.all()
    total_calls = len(rows)
    total_prompt = sum(int(r.prompt_tokens or 0) for r in rows)
    total_completion = sum(int(r.completion_tokens or 0) for r in rows)
    total_tokens = sum(int(r.total_tokens or 0) for r in rows)

    by_model: dict[str, dict] = {}
    by_scene: dict[str, dict] = {}
    daily: dict[str, dict] = {}
    for row in rows:
        model_key = row.model or "(unknown)"
        bucket = by_model.setdefault(
            model_key,
            {
                "model": model_key,
                "calls": 0,
                "total_tokens": 0,
                "prompt_tokens": 0,
                "completion_tokens": 0,
            },
        )
        bucket["calls"] += 1
        bucket["total_tokens"] += int(row.total_tokens or 0)
        bucket["prompt_tokens"] += int(row.prompt_tokens or 0)
        bucket["completion_tokens"] += int(row.completion_tokens or 0)

        scene_key = row.scene or "(未分类)"
        scene_bucket = by_scene.setdefault(scene_key, {"scene": scene_key, "calls": 0, "total_tokens": 0})
        scene_bucket["calls"] += 1
        scene_bucket["total_tokens"] += int(row.total_tokens or 0)

        day = row.created_at.strftime("%Y-%m-%d") if row.created_at else "unknown"
        day_bucket = daily.setdefault(
            day,
            {
                "date": day,
                "calls": 0,
                "total_tokens": 0,
                "prompt_tokens": 0,
                "completion_tokens": 0,
            },
        )
        day_bucket["calls"] += 1
        day_bucket["total_tokens"] += int(row.total_tokens or 0)
        day_bucket["prompt_tokens"] += int(row.prompt_tokens or 0)
        day_bucket["completion_tokens"] += int(row.completion_tokens or 0)

    return {
        "total_calls": total_calls,
        "total_prompt_tokens": total_prompt,
        "total_completion_tokens": total_completion,
        "total_tokens": total_tokens,
        "by_model": sorted(by_model.values(), key=lambda item: -item["total_tokens"]),
        "by_scene": sorted(by_scene.values(), key=lambda item: -item["total_tokens"]),
        "daily": sorted(daily.values(), key=lambda item: item["date"]),
    }


@router.get("/records")
def token_usage_records(
    model: str | None = Query(default=None),
    scene: str | None = Query(default=None),
    start_date: str | None = Query(default=None),
    end_date: str | None = Query(default=None),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=200),
    db: Session = Depends(get_db),
):
    query = _apply_filters(db.query(LlmTokenUsage), model, scene, start_date, end_date)
    total = query.count()
    rows = (
        query.order_by(LlmTokenUsage.created_at.desc(), LlmTokenUsage.id.desc())
        .offset((page - 1) * page_size)
        .limit(page_size)
        .all()
    )
    return {
        "total": total,
        "page": page,
        "page_size": page_size,
        "records": [row.to_dict() for row in rows],
    }
