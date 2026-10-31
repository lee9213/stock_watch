"""调度 Job 管理：配置落库与列表。"""

from __future__ import annotations

from src.platform.persistence.database import SessionLocal
from src.platform.persistence.models import SchedulerJobConfig
from src.platform.scheduling.job_manager import (
    list_managed_jobs,
    trigger_to_schedule_expr,
    update_system_job,
)
from apscheduler.triggers.interval import IntervalTrigger


def test_trigger_to_schedule_interval():
    assert trigger_to_schedule_expr(IntervalTrigger(seconds=60)) == "interval:1m"
    assert trigger_to_schedule_expr(IntervalTrigger(minutes=5)) == "interval:5m"


def test_update_system_job_persists(monkeypatch):
    db = SessionLocal()
    try:
        db.query(SchedulerJobConfig).filter(
            SchedulerJobConfig.scheduler_name == "news_flow",
            SchedulerJobConfig.job_id == "news_flow_analyze",
        ).delete()
        db.commit()
    finally:
        db.close()

    # 无运行中 scheduler 时仍应写库
    monkeypatch.setattr(
        "src.platform.scheduling.job_manager.scheduler_registry.get_all",
        lambda: {},
    )
    item = update_system_job(
        "news_flow",
        "news_flow_analyze",
        schedule="interval:4m",
        enabled=True,
    )
    assert item["schedule"] == "interval:4m"
    assert item["overridden"] is True

    jobs = list_managed_jobs(include_agents=False)
    hit = next(j for j in jobs if j["job_id"] == "news_flow_analyze")
    assert hit["schedule"] == "interval:4m"
