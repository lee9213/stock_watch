"""调度历史落库与查询。"""

from __future__ import annotations

import pytest

from src.platform.persistence.database import SessionLocal
from src.platform.persistence.models import SchedulerJobRun
from src.platform.scheduling.job_runs import (
    list_job_runs,
    serialize_job_run,
    track_job_run,
)


@pytest.fixture(autouse=True)
def _clean_runs():
    db = SessionLocal()
    try:
        db.query(SchedulerJobRun).delete()
        db.commit()
    finally:
        db.close()
    yield


def test_track_job_run_success_and_list():
    with track_job_run("context", "context_maintenance_refresh_opportunities") as summary:
        summary["count"] = 3
        summary["snapshot_date"] = "2026-10-27"

    rows = list_job_runs(scheduler_name="context", limit=10)
    assert len(rows) == 1
    assert rows[0].status == "success"
    assert rows[0].summary.get("count") == 3
    payload = serialize_job_run(rows[0])
    assert payload["job_id"] == "context_maintenance_refresh_opportunities"


def test_track_job_run_skip_idle_when_disabled():
    with track_job_run("paper_trading", "paper_trading_scan", persist_idle=False) as summary:
        summary["opened"] = 0
        summary["closed"] = 0
        summary["_idle"] = True

    assert list_job_runs(include_idle=True, limit=10) == []


def test_track_job_run_failed():
    with pytest.raises(RuntimeError):
        with track_job_run("news_flow", "news_flow_ingest"):
            raise RuntimeError("boom")

    rows = list_job_runs(status="failed", limit=10)
    assert len(rows) == 1
    assert "boom" in (rows[0].error or "")
