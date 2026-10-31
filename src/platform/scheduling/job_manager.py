"""系统调度 Job 管理：目录默认值、配置持久化、运行时热更新。"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger

from src.platform.persistence.database import SessionLocal
from src.platform.persistence.models import SchedulerJobConfig
from src.platform.runtime.config import Settings
from src.platform.scheduling import scheduler_registry
from src.platform.scheduling.schedule_parser import parse_schedule, preview_schedule
from src.platform.scheduling.timezone import utc_now

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class JobSpec:
    scheduler_name: str
    job_id: str
    label: str
    default_schedule: str
    editable: bool = True
    description: str = ""


# 可在页面管理的系统任务目录（默认表达式；库中有覆盖则以库为准）
SYSTEM_JOB_CATALOG: tuple[JobSpec, ...] = (
    JobSpec(
        "news_flow",
        "news_flow_ingest",
        "新闻采集(盘中)",
        "*/3 9-14 * * *",
        description="交易时段 09:00-14:57 每3分钟拉取同花顺/华尔街见闻等",
    ),
    JobSpec(
        "news_flow",
        "news_flow_ingest_after_hours",
        "新闻采集(盘后)",
        "0 15-23,0-8 * * *",
        description="15:00-次日08:00 每小时整点采集一次",
    ),
    JobSpec(
        "news_flow",
        "news_flow_analyze",
        "新闻分析(扫遗漏)",
        "interval:5m",
        description="每5分钟补扫入库超过5分钟仍未分析的新闻；主路径为本轮入库id直接入队",
    ),
    JobSpec("news_flow", "news_flow_digest", "新闻速览", "30 7-23 * * *", description="每小时整点半生成速览"),
    JobSpec("context", "context_maintenance_evaluate", "夜间后验评估", "30 4 * * *", description="策略/候选/因子全量评估"),
    JobSpec("context", "context_maintenance_evaluate_cn_close", "A股收盘复盘", "45 15 * * *", description="收盘后快速评估"),
    JobSpec("context", "context_maintenance_cleanup", "过期数据清理", "15 4 * * *"),
    JobSpec("context", "context_maintenance_trading_calendar", "交易日历刷新", "0 3 * * *"),
    JobSpec("context", "context_maintenance_refresh_opportunities_0915", "机会刷新 09:15", "15 9 * * *"),
    JobSpec("context", "context_maintenance_refresh_opportunities_1030", "机会刷新 10:30", "30 10 * * *"),
    JobSpec("context", "context_maintenance_refresh_opportunities_1330", "机会刷新 13:30", "30 13 * * *"),
    JobSpec("context", "context_maintenance_refresh_opportunities_1430", "机会刷新 14:30", "30 14 * * *"),
    JobSpec("context", "context_maintenance_refresh_opportunities_2200", "机会刷新 22:00", "0 22 * * *"),
    JobSpec("paper_trading", "paper_trading_scan", "模拟盘扫描", "interval:60s", description="交易时段自动开平仓"),
    JobSpec("price_alert", "price_alert_scan", "价格提醒扫描", "interval:60s", editable=True),
    JobSpec("price_alert", "price_alert_delivery", "价格提醒投递", "interval:10s", editable=True),
)

SCHEDULER_LABELS_ZH: dict[str, str] = {
    "news_flow": "新闻流",
    "context": "机会与复盘",
    "paper_trading": "模拟盘",
    "price_alert": "价格提醒",
    "agent": "智能体",
}

KIND_LABELS_ZH: dict[str, str] = {
    "system": "系统",
    "agent": "智能体",
    "runtime": "运行时",
}

AGENT_NAME_LABELS_ZH: dict[str, str] = {
    "daily_report": "收盘复盘",
    "premarket_outlook": "盘前分析",
    "intraday_monitor": "盘中监测",
    "tradingagents": "深度分析",
}

# Agent 调度器上挂的维护类任务（非 Agent 本体）
AGENT_SCHEDULER_JOB_LABELS_ZH: dict[str, str] = {
    "mcp_log_retention": "MCP 调用日志清理",
    "prune_mcp_logs": "MCP 调用日志清理",
}


def _catalog_map() -> dict[tuple[str, str], JobSpec]:
    return {(s.scheduler_name, s.job_id): s for s in SYSTEM_JOB_CATALOG}


def resolve_job_label_zh(
    scheduler_name: str,
    job_id: str,
    *,
    fallback: str | None = None,
) -> str:
    """统一解析中文任务名（目录 / Agent / 运行时命名规则）。"""
    spec = _catalog_map().get((scheduler_name, job_id))
    if spec:
        return spec.label
    if scheduler_name == "agent":
        return (
            AGENT_SCHEDULER_JOB_LABELS_ZH.get(job_id)
            or AGENT_NAME_LABELS_ZH.get(job_id)
            or fallback
            or job_id
        )
    if job_id in AGENT_SCHEDULER_JOB_LABELS_ZH:
        return AGENT_SCHEDULER_JOB_LABELS_ZH[job_id]
    if job_id.startswith("paper_trading_premarket_"):
        market = job_id.rsplit("_", 1)[-1]
        return f"模拟盘盘前通知（{market}）"
    if job_id.startswith("paper_trading_summary_"):
        # paper_trading_summary_CN_1530
        parts = job_id.split("_")
        market = parts[3] if len(parts) >= 4 else ""
        return f"模拟盘日终摘要（{market}）" if market else "模拟盘日终摘要"
    text = (fallback or "").strip()
    if text and not text.isascii():
        return text
    return text or job_id


def _with_zh_fields(row: dict[str, Any]) -> dict[str, Any]:
    sname = str(row.get("scheduler_name") or "")
    jid = str(row.get("job_id") or "")
    kind = str(row.get("kind") or "system")
    row["label"] = resolve_job_label_zh(sname, jid, fallback=str(row.get("label") or ""))
    row["kind_label"] = KIND_LABELS_ZH.get(kind, kind)
    row["scheduler_label"] = SCHEDULER_LABELS_ZH.get(sname, sname)
    return row


def trigger_to_schedule_expr(trigger: object) -> str:
    """尽量把 APScheduler trigger 还原成可编辑表达式。"""
    if isinstance(trigger, IntervalTrigger):
        seconds = int(getattr(trigger, "interval", None).total_seconds()) if getattr(trigger, "interval", None) else 0
        if seconds <= 0:
            return "interval:60s"
        if seconds % 3600 == 0:
            return f"interval:{seconds // 3600}h"
        if seconds % 60 == 0:
            return f"interval:{seconds // 60}m"
        return f"interval:{seconds}s"
    if isinstance(trigger, CronTrigger):
        fields = {f.name: str(f) for f in trigger.fields}
        # APScheduler numeric dow → 展示仍用字段原样；编辑时走 parse_schedule 归一化
        return (
            f"{fields.get('minute', '*')} {fields.get('hour', '*')} "
            f"{fields.get('day', '*')} {fields.get('month', '*')} {fields.get('day_of_week', '*')}"
        )
    return str(trigger or "")


def _load_configs() -> dict[tuple[str, str], SchedulerJobConfig]:
    db = SessionLocal()
    try:
        rows = db.query(SchedulerJobConfig).all()
        out: dict[tuple[str, str], SchedulerJobConfig] = {}
        for row in rows:
            key = (row.scheduler_name, row.job_id)
            out[key] = row
            db.expunge(row)
        return out
    finally:
        db.close()


def _upsert_config(
    *,
    scheduler_name: str,
    job_id: str,
    schedule_expr: str | None = None,
    enabled: bool | None = None,
    label: str | None = None,
) -> SchedulerJobConfig:
    db = SessionLocal()
    try:
        row = (
            db.query(SchedulerJobConfig)
            .filter(
                SchedulerJobConfig.scheduler_name == scheduler_name,
                SchedulerJobConfig.job_id == job_id,
            )
            .first()
        )
        spec = _catalog_map().get((scheduler_name, job_id))
        if not row:
            row = SchedulerJobConfig(
                scheduler_name=scheduler_name,
                job_id=job_id,
                schedule_expr=(schedule_expr or (spec.default_schedule if spec else "")),
                enabled=True if enabled is None else bool(enabled),
                label=label or (spec.label if spec else job_id),
            )
            db.add(row)
        else:
            if schedule_expr is not None:
                row.schedule_expr = schedule_expr
            if enabled is not None:
                row.enabled = bool(enabled)
            if label is not None:
                row.label = label
            row.updated_at = utc_now()
        db.commit()
        db.refresh(row)
        db.expunge(row)
        return row
    finally:
        db.close()


def _job_timezone(job, fallback: str) -> str:
    trigger = getattr(job, "trigger", None)
    tz = getattr(trigger, "timezone", None) if trigger is not None else None
    if tz is None:
        return fallback
    return getattr(tz, "key", None) or str(tz) or fallback


def apply_configs_to_scheduler(scheduler_name: str, scheduler: object, *, timezone: str | None = None) -> int:
    """在 scheduler.start() 之后调用：按库配置 reschedule / pause / resume。"""
    tz = timezone or Settings().app_timezone or "Asia/Shanghai"
    configs = _load_configs()
    applied = 0
    if not hasattr(scheduler, "get_job"):
        return 0
    for (sname, job_id), cfg in configs.items():
        if sname != scheduler_name:
            continue
        job = scheduler.get_job(job_id)
        if job is None:
            continue
        try:
            expr = (cfg.schedule_expr or "").strip()
            if expr:
                job_tz = _job_timezone(job, tz)
                trigger = parse_schedule(expr, timezone=job_tz)
                scheduler.reschedule_job(job_id, trigger=trigger)
            if cfg.enabled:
                if hasattr(scheduler, "resume_job"):
                    try:
                        scheduler.resume_job(job_id)
                    except Exception:
                        pass
            else:
                if hasattr(scheduler, "pause_job"):
                    scheduler.pause_job(job_id)
            applied += 1
        except Exception as exc:
            logger.warning(
                "应用调度配置失败 %s/%s: %s", scheduler_name, job_id, exc
            )
    if applied:
        logger.info("已应用调度配置: scheduler=%s count=%s", scheduler_name, applied)
    return applied


def list_managed_jobs(*, include_agents: bool = True) -> list[dict[str, Any]]:
    """合并目录默认值、库覆盖、运行时 APScheduler 状态。"""
    tz = Settings().app_timezone or "Asia/Shanghai"
    configs = _load_configs()
    catalog = _catalog_map()
    regs = scheduler_registry.get_all()
    items: dict[tuple[str, str], dict[str, Any]] = {}

    # 1) 目录任务
    for spec in SYSTEM_JOB_CATALOG:
        key = (spec.scheduler_name, spec.job_id)
        cfg = configs.get(key)
        schedule = (cfg.schedule_expr if cfg and cfg.schedule_expr else spec.default_schedule)
        enabled = True if cfg is None else bool(cfg.enabled)
        items[key] = {
            "scheduler_name": spec.scheduler_name,
            "job_id": spec.job_id,
            "label": (cfg.label if cfg and cfg.label else spec.label),
            "description": spec.description,
            "schedule": schedule,
            "default_schedule": spec.default_schedule,
            "enabled": enabled,
            "editable": spec.editable,
            "kind": "system",
            "running": False,
            "paused": False,
            "next_run_time": None,
            "trigger": "",
            "overridden": cfg is not None,
        }

    # 2) 运行时状态覆盖
    for sname, sched in regs.items():
        running = bool(getattr(sched, "running", False))
        try:
            jobs = list(sched.get_jobs()) if hasattr(sched, "get_jobs") else []
        except Exception:
            jobs = []
        for job in jobs:
            job_id = str(getattr(job, "id", "") or "")
            if not job_id:
                continue
            key = (sname, job_id)
            next_run = getattr(job, "next_run_time", None)
            trigger = getattr(job, "trigger", None)
            trigger_expr = trigger_to_schedule_expr(trigger) if trigger is not None else ""
            paused = bool(getattr(job, "next_run_time", True) is None and running)
            # APScheduler: paused jobs have next_run_time None
            try:
                from apscheduler.job import Job

                if isinstance(job, Job):
                    paused = job.next_run_time is None and running
            except Exception:
                pass

            if key in items:
                row = items[key]
                row["running"] = running
                row["paused"] = paused and not row["enabled"]
                row["next_run_time"] = next_run.isoformat() if next_run else None
                row["trigger"] = str(trigger or "")
                if not row.get("overridden") and trigger_expr:
                    # 未覆盖时用运行时表达式展示更准
                    row["schedule"] = trigger_expr
            else:
                # 未在目录中的运行时 job（如模拟盘盘前/摘要、Agent）
                cfg = configs.get(key)
                is_agent = sname == "agent"
                raw_name = str(getattr(job, "name", None) or "")
                items[key] = {
                    "scheduler_name": sname,
                    "job_id": job_id,
                    "label": resolve_job_label_zh(sname, job_id, fallback=raw_name or None),
                    "description": "智能体定时任务" if is_agent else "系统运行时任务",
                    "schedule": (cfg.schedule_expr if cfg and cfg.schedule_expr else trigger_expr),
                    "default_schedule": trigger_expr,
                    "enabled": True if cfg is None else bool(cfg.enabled),
                    "editable": bool(is_agent or (cfg is not None)),
                    "kind": "agent" if is_agent else "runtime",
                    "running": running,
                    "paused": next_run is None and running,
                    "next_run_time": next_run.isoformat() if next_run else None,
                    "trigger": str(trigger or ""),
                    "overridden": cfg is not None,
                }

    # 3) Agent：即使当前未注册到 scheduler，也从 agent 表补全（可选）
    if include_agents:
        try:
            from src.platform.persistence.models import AgentConfig

            db = SessionLocal()
            try:
                agents = (
                    db.query(AgentConfig)
                    .filter(AgentConfig.schedule.isnot(None), AgentConfig.schedule != "")
                    .all()
                )
                for agent in agents:
                    key = ("agent", agent.name)
                    zh_name = resolve_job_label_zh(
                        "agent",
                        agent.name,
                        fallback=agent.display_name or agent.name,
                    )
                    if key in items:
                        items[key]["label"] = zh_name
                        items[key]["schedule"] = agent.schedule or items[key]["schedule"]
                        items[key]["enabled"] = bool(agent.enabled)
                        items[key]["editable"] = True
                        items[key]["kind"] = "agent"
                        items[key]["description"] = "智能体定时任务"
                        continue
                    items[key] = {
                        "scheduler_name": "agent",
                        "job_id": agent.name,
                        "label": zh_name,
                        "description": "智能体定时任务",
                        "schedule": agent.schedule or "",
                        "default_schedule": agent.schedule or "",
                        "enabled": bool(agent.enabled),
                        "editable": True,
                        "kind": "agent",
                        "running": "agent" in regs and bool(getattr(regs["agent"], "running", False)),
                        "paused": not bool(agent.enabled),
                        "next_run_time": None,
                        "trigger": "",
                        "overridden": False,
                    }
            finally:
                db.close()
        except Exception as exc:
            logger.debug("补充 Agent 调度列表失败: %s", exc)

    out = [_with_zh_fields(row) for row in items.values()]
    out.sort(key=lambda x: (x["kind"], x["scheduler_name"], x["job_id"]))
    # 附带预览下一次（失败忽略）
    for row in out:
        try:
            if row.get("schedule") and row.get("enabled"):
                runs = preview_schedule(row["schedule"], count=1, timezone=tz)
                if runs and not row.get("next_run_time"):
                    row["next_run_time"] = runs[0].isoformat()
        except Exception:
            pass
    return out


def update_system_job(
    scheduler_name: str,
    job_id: str,
    *,
    schedule: str | None = None,
    enabled: bool | None = None,
) -> dict[str, Any]:
    """更新系统/运行时 job 配置并热应用到对应 scheduler。"""
    if scheduler_name == "agent":
        raise ValueError("agent_jobs_use_agent_api")

    catalog = _catalog_map()
    key = (scheduler_name, job_id)
    spec = catalog.get(key)
    regs = scheduler_registry.get_all()
    sched = regs.get(scheduler_name)
    live_job = sched.get_job(job_id) if sched is not None and hasattr(sched, "get_job") else None

    if spec is None and live_job is None:
        raise KeyError(f"unknown_job:{scheduler_name}/{job_id}")
    if spec is not None and not spec.editable:
        raise PermissionError("job_not_editable")

    expr = schedule.strip() if isinstance(schedule, str) else None
    if expr is not None:
        # 校验
        parse_schedule(expr, timezone=Settings().app_timezone or "Asia/Shanghai")

    cfg = _upsert_config(
        scheduler_name=scheduler_name,
        job_id=job_id,
        schedule_expr=expr,
        enabled=enabled,
        label=spec.label if spec else None,
    )

    # 热更新
    if sched is not None and live_job is not None:
        tz = _job_timezone(live_job, Settings().app_timezone or "Asia/Shanghai")
        try:
            if expr:
                sched.reschedule_job(job_id, trigger=parse_schedule(expr, timezone=tz))
            if enabled is False:
                sched.pause_job(job_id)
            elif enabled is True:
                try:
                    sched.resume_job(job_id)
                except Exception:
                    pass
        except Exception as exc:
            logger.exception("热更新调度失败 %s/%s", scheduler_name, job_id)
            raise RuntimeError(str(exc)) from exc

    items = [x for x in list_managed_jobs(include_agents=False) if x["scheduler_name"] == scheduler_name and x["job_id"] == job_id]
    return items[0] if items else {
        "scheduler_name": scheduler_name,
        "job_id": job_id,
        "schedule": cfg.schedule_expr,
        "enabled": bool(cfg.enabled),
        "label": cfg.label,
        "kind": "system",
        "editable": True,
    }


def update_agent_job(agent_name: str, *, schedule: str | None = None, enabled: bool | None = None) -> dict[str, Any]:
    """通过 AgentConfig + request_scheduler_reload 更新 Agent 调度。"""
    from src.modules.automation.scheduling_policy import request_scheduler_reload
    from src.platform.persistence.models import AgentConfig

    db = SessionLocal()
    try:
        agent = db.query(AgentConfig).filter(AgentConfig.name == agent_name).first()
        if not agent:
            raise KeyError(f"agent_not_found:{agent_name}")
        if schedule is not None:
            expr = schedule.strip()
            parse_schedule(expr, timezone=Settings().app_timezone or "Asia/Shanghai")
            agent.schedule = expr
        if enabled is not None:
            agent.enabled = bool(enabled)
        db.commit()
        request_scheduler_reload()
    finally:
        db.close()

    items = [x for x in list_managed_jobs(include_agents=True) if x["kind"] == "agent" and x["job_id"] == agent_name]
    if items:
        return items[0]
    return {"scheduler_name": "agent", "job_id": agent_name, "kind": "agent", "schedule": schedule, "enabled": enabled}
