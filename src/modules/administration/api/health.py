"""系统自检 API。"""

from fastapi import APIRouter, Query
from pydantic import BaseModel, Field

from src.modules.administration.selfcheck import list_selfcheck_items, run_selfcheck
from src.platform.runtime.config import Settings
from src.platform.scheduling.job_manager import (
    list_managed_jobs,
    update_agent_job,
    update_system_job,
)
from src.platform.scheduling.job_runs import (
    list_job_runs,
    list_registered_jobs,
    serialize_job_run,
)
from src.platform.scheduling.schedule_parser import parse_schedule, preview_schedule
from src.web.errors import api_error

router = APIRouter()


class SchedulerJobUpdate(BaseModel):
    """更新调度表达式或启停。"""

    schedule: str | None = Field(None, description="cron「分 时 日 月 周」或 interval:5m")
    enabled: bool | None = None


@router.get("/selfcheck")
async def selfcheck(
    notify_send: bool = Query(False, description="是否真实发送通知测试(默认只校验配置不发送)"),
    list_only: bool = Query(False, alias="list", description="只列出待检项不探测(前端先渲染列表)"),
    keys: str | None = Query(None, description="逗号分隔,只探测这些 key(前端逐项更新进度)"),
):
    """一键体检 数据源 / AI / 通知。

    - `?list=1`:只返回待检项身份 `{items:[{category,key,name}]}`,不探测。
    - `?keys=ds:1,ai:2`:只探测这些项(逐项进度)。
    - 无参:探测全部。
    """
    if list_only:
        return {"items": list_selfcheck_items()}
    key_list = [k for k in keys.split(",") if k] if keys else None
    return await run_selfcheck(notify_send=notify_send, keys=key_list)


@router.get("/scheduler-jobs")
async def scheduler_jobs(
    managed: bool = Query(True, description="true=可管理任务目录+运行时状态；false=仅注册表原始列表"),
    include_agents: bool = Query(True, description="是否包含 Agent 任务"),
):
    """调度任务列表。"""
    if managed:
        return {"items": list_managed_jobs(include_agents=include_agents)}
    return {"items": list_registered_jobs()}


@router.patch("/scheduler-jobs/{scheduler_name}/{job_id}")
async def patch_scheduler_job(
    scheduler_name: str,
    job_id: str,
    body: SchedulerJobUpdate,
):
    """修改系统 Job 调度或启停；Agent 任务走同接口但内部转 Agent 配置。"""
    if body.schedule is None and body.enabled is None:
        raise api_error(400, "scheduler_job_noop", "请提供 schedule 或 enabled")

    if scheduler_name == "agent":
        try:
            item = update_agent_job(job_id, schedule=body.schedule, enabled=body.enabled)
        except KeyError:
            raise api_error(404, "agent_not_found", f"Agent {job_id} 不存在") from None
        except ValueError as exc:
            raise api_error(400, "agent_schedule_invalid", str(exc)) from exc
        return item

    try:
        return update_system_job(
            scheduler_name,
            job_id,
            schedule=body.schedule,
            enabled=body.enabled,
        )
    except KeyError:
        raise api_error(404, "scheduler_job_not_found", f"任务不存在: {scheduler_name}/{job_id}") from None
    except PermissionError:
        raise api_error(400, "scheduler_job_not_editable", "该任务不允许修改") from None
    except ValueError as exc:
        raise api_error(400, "scheduler_schedule_invalid", f"调度表达式无效: {exc}") from exc
    except RuntimeError as exc:
        raise api_error(500, "scheduler_update_failed", str(exc)) from exc


@router.get("/scheduler-jobs/preview")
async def preview_scheduler_schedule(
    schedule: str = Query(..., description="cron 或 interval:5m"),
    count: int = Query(5, ge=1, le=20),
):
    """预览调度表达式的未来触发时间。"""
    tz = Settings().app_timezone or "Asia/Shanghai"
    try:
        parse_schedule(schedule, timezone=tz)
        runs = preview_schedule(schedule, count=count, timezone=tz)
    except ValueError as exc:
        raise api_error(400, "scheduler_schedule_invalid", str(exc)) from exc
    return {
        "schedule": schedule,
        "timezone": tz,
        "runs": [dt.isoformat() for dt in runs],
    }


@router.get("/scheduler-runs")
async def scheduler_runs(
    scheduler: str | None = Query(None, description="调度器名，如 news_flow / context / paper_trading"),
    job_id: str | None = Query(None, description="任务 id"),
    status: str | None = Query(None, description="success / failed / idle"),
    include_idle: bool = Query(False, description="是否包含空跑记录"),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
):
    """系统调度历史（新闻/机会/模拟盘/后验等）。"""
    rows = list_job_runs(
        scheduler_name=scheduler,
        job_id=job_id,
        status=status,
        include_idle=include_idle,
        limit=limit,
        offset=offset,
    )
    return {"items": [serialize_job_run(r) for r in rows], "limit": limit, "offset": offset}
