"""系统调度任务执行历史：轻量落库，供查询与排障。"""

from __future__ import annotations

import logging
from contextlib import contextmanager
from datetime import timedelta
from typing import Any, Iterator

from src.platform.persistence.database import SessionLocal
from src.platform.persistence.json_safe import to_jsonable
from src.platform.persistence.models import SchedulerJobRun
from src.platform.scheduling.timezone import utc_now

logger = logging.getLogger(__name__)


@contextmanager
def track_job_run(
    scheduler_name: str,
    job_id: str,
    *,
    trigger_source: str = "schedule",
    persist_idle: bool = True,
) -> Iterator[dict[str, Any]]:
    """记录一次 job 执行。

    用法::

        with track_job_run("news_flow", "news_flow_ingest") as summary:
            counts = ingest...
            summary.update(counts)
            if sum(counts.values()) == 0:
                summary["_idle"] = True

    - 调用方往 ``summary`` 写入结果摘要。
    - ``summary["_idle"]=True`` 表示空跑；``persist_idle=False`` 时不落库。
    - 未捕获异常会记 ``failed`` 并原样抛出。
    """
    started = utc_now()
    summary: dict[str, Any] = {}
    status = "success"
    error = ""
    try:
        yield summary
        if summary.pop("_idle", False):
            status = "idle"
    except Exception as exc:
        status = "failed"
        error = str(exc)[:2000]
        raise
    finally:
        finished = utc_now()
        duration_ms = max(0, int((finished - started).total_seconds() * 1000))
        if status == "idle" and not persist_idle:
            return
        try:
            db = SessionLocal()
            try:
                db.add(
                    SchedulerJobRun(
                        scheduler_name=(scheduler_name or "").strip() or "unknown",
                        job_id=(job_id or "").strip() or "unknown",
                        trigger_source=(trigger_source or "schedule").strip() or "schedule",
                        status=status,
                        summary=to_jsonable(summary) if summary else {},
                        error=error,
                        duration_ms=duration_ms,
                        started_at=started,
                        finished_at=finished,
                    )
                )
                db.commit()
            finally:
                db.close()
        except Exception as persist_err:
            logger.debug(
                "写入调度历史失败 scheduler=%s job=%s: %s",
                scheduler_name,
                job_id,
                persist_err,
            )


def list_job_runs(
    *,
    scheduler_name: str | None = None,
    job_id: str | None = None,
    status: str | None = None,
    include_idle: bool = False,
    limit: int = 50,
    offset: int = 0,
) -> list[SchedulerJobRun]:
    db = SessionLocal()
    try:
        q = db.query(SchedulerJobRun)
        if scheduler_name:
            q = q.filter(SchedulerJobRun.scheduler_name == scheduler_name.strip())
        if job_id:
            q = q.filter(SchedulerJobRun.job_id == job_id.strip())
        if status:
            q = q.filter(SchedulerJobRun.status == status.strip())
        elif not include_idle:
            q = q.filter(SchedulerJobRun.status != "idle")
        rows = (
            q.order_by(SchedulerJobRun.created_at.desc(), SchedulerJobRun.id.desc())
            .offset(max(0, int(offset)))
            .limit(max(1, min(200, int(limit))))
            .all()
        )
        # 脱离 session 前取属性
        for row in rows:
            _ = (
                row.id,
                row.scheduler_name,
                row.job_id,
                row.trigger_source,
                row.status,
                row.summary,
                row.error,
                row.duration_ms,
                row.started_at,
                row.finished_at,
                row.created_at,
            )
        db.expunge_all()
        return rows
    finally:
        db.close()


def cleanup_job_runs(*, retention_days: int = 30) -> int:
    """清理过期调度历史，返回删除行数。"""
    days = max(7, int(retention_days))
    cutoff = utc_now() - timedelta(days=days)
    db = SessionLocal()
    try:
        deleted = (
            db.query(SchedulerJobRun)
            .filter(SchedulerJobRun.created_at < cutoff)
            .delete(synchronize_session=False)
        )
        db.commit()
        return int(deleted or 0)
    except Exception as exc:
        db.rollback()
        logger.debug("清理调度历史失败: %s", exc)
        return 0
    finally:
        db.close()


def serialize_job_run(row: SchedulerJobRun) -> dict[str, Any]:
    return {
        "id": row.id,
        "scheduler_name": row.scheduler_name,
        "job_id": row.job_id,
        "trigger_source": row.trigger_source or "schedule",
        "status": row.status,
        "summary": row.summary if isinstance(row.summary, dict) else {},
        "error": row.error or "",
        "duration_ms": int(row.duration_ms or 0),
        "started_at": row.started_at.isoformat() if row.started_at else None,
        "finished_at": row.finished_at.isoformat() if row.finished_at else None,
        "created_at": row.created_at.isoformat() if row.created_at else None,
    }


def list_registered_jobs() -> list[dict[str, Any]]:
    """当前进程已注册调度器上的 job 清单（下次触发时间等）。"""
    from src.platform.scheduling import scheduler_registry

    out: list[dict[str, Any]] = []
    for name, sched in scheduler_registry.get_all().items():
        running = bool(getattr(sched, "running", False))
        try:
            jobs = list(sched.get_jobs()) if hasattr(sched, "get_jobs") else []
        except Exception:
            jobs = []
        for job in jobs:
            next_run = getattr(job, "next_run_time", None)
            out.append(
                {
                    "scheduler_name": name,
                    "job_id": getattr(job, "id", "") or "",
                    "running": running,
                    "next_run_time": next_run.isoformat() if next_run is not None else None,
                    "trigger": str(getattr(job, "trigger", "") or ""),
                }
            )
    out.sort(key=lambda x: (x["scheduler_name"], x["job_id"]))
    return out
