"""机会链路任务的命名互斥与状态（刷新 / 后验评估）。

改造前三处各自维护状态、彼此不知情：
- ``research/api/recommendations.py`` 用模块级 ``_refresh_state``（后台线程刷新）
- ``research/context_scheduler.py`` 用 ``self._refreshing``（定时刷新）
- ``refresh_opportunities_once()`` 完全不设防

结果：定时任务与手动刷新撞在同一分钟时会重复拉一次行情、重复计算候选池，
并竞争同一批 snapshot 行的写入（SQLite 写锁 → 5s busy_timeout 后报错）。

后验评估本来也有同样的问题：API 手动触发与夜间定时任务可以同时跑，
两边各自查"已评估"集合都不含对方刚插入的行，于是同时 INSERT 撞唯一约束，
整批 rollback——统计接口看到的是半成品。所以这里做成**按任务名分锁**：
同一个名字互斥（刷新 ↔ 刷新、评估 ↔ 评估），不同名字之间互不影响，
避免评估跑几分钟把定时刷新整个挡住。

状态同时旁路写进 ``app_settings``，重启后/多进程下至少能读到
"上次跑到什么时候、成功还是失败"。
"""

from __future__ import annotations

import json
import logging
import threading
from datetime import datetime, timezone

logger = logging.getLogger(__name__)

# 任务名 → app_settings 里的 key
_PERSIST_KEYS = {
    "refresh": "opportunity_refresh_state",
    "evaluate": "opportunity_evaluate_state",
}

_locks: dict[str, threading.Lock] = {}
_states: dict[str, dict] = {}
_loaded: set[str] = set()


def _empty_state() -> dict:
    return {
        "running": False,
        "source": "",
        "started_at": "",
        "finished_at": "",
        "last_error": "",
        "last_snapshot_date": "",
        "last_count": 0,
        "runs": 0,
    }


def _lock_for(name: str) -> threading.Lock:
    lock = _locks.get(name)
    if lock is None:
        lock = threading.Lock()
        _locks[name] = lock
    return lock


def _state_for(name: str) -> dict:
    state = _states.get(name)
    if state is None:
        state = _empty_state()
        _states[name] = state
    return state


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _persist(name: str, payload: dict) -> None:
    """把状态旁路写进 app_settings，失败只记日志，不影响主流程。"""
    key = _PERSIST_KEYS.get(name)
    if not key:
        return
    try:
        from src.platform.persistence.database import SessionLocal
        from src.platform.persistence.models import AppSettings

        db = SessionLocal()
        try:
            row = db.query(AppSettings).filter(AppSettings.key == key).first()
            value = json.dumps(payload, ensure_ascii=False)
            if row is None:
                db.add(
                    AppSettings(
                        key=key,
                        value=value,
                        description="机会链路任务最近一次运行状态（自动维护）",
                    )
                )
            else:
                row.value = value
            db.commit()
        finally:
            db.close()
    except Exception as exc:  # noqa: BLE001 - 观测性旁路不允许打断主流程
        logger.debug("写入机会任务状态失败(%s): %s", name, exc)


def _load_persisted(name: str) -> None:
    key = _PERSIST_KEYS.get(name)
    if not key:
        return
    try:
        from src.platform.persistence.database import SessionLocal
        from src.platform.persistence.models import AppSettings

        db = SessionLocal()
        try:
            row = db.query(AppSettings).filter(AppSettings.key == key).first()
            if row is None or not row.value:
                return
            data = json.loads(row.value)
        finally:
            db.close()
    except Exception:
        return
    if not isinstance(data, dict):
        return
    with _lock_for(name):
        state = _state_for(name)
        for field in ("finished_at", "last_error", "last_snapshot_date", "last_count", "runs"):
            if field in data and not state.get(field):
                state[field] = data[field]


def _ensure_loaded(name: str) -> None:
    if name in _loaded:
        return
    _loaded.add(name)
    _load_persisted(name)


def try_begin(source: str = "api", *, name: str = "refresh") -> tuple[bool, dict]:
    """尝试占用该任务的执行权。返回 (是否拿到, 当前状态快照)。"""
    _ensure_loaded(name)
    with _lock_for(name):
        state = _state_for(name)
        if state.get("running"):
            return False, dict(state)
        state.update(
            running=True,
            source=source,
            started_at=_now_iso(),
            finished_at="",
            last_error="",
            runs=int(state.get("runs") or 0) + 1,
        )
        current = dict(state)
    _persist(name, {**current, "running": False})
    return True, current


def finish(
    *,
    snapshot_date: str = "",
    count: int = 0,
    error: str = "",
    name: str = "refresh",
) -> dict:
    with _lock_for(name):
        state = _state_for(name)
        state.update(
            running=False,
            finished_at=_now_iso(),
            last_error=str(error or ""),
            last_snapshot_date=str(snapshot_date or state.get("last_snapshot_date") or ""),
            last_count=int(count or 0),
        )
        current = dict(state)
    _persist(name, current)
    return current


def is_running(*, name: str = "refresh") -> bool:
    with _lock_for(name):
        return bool(_state_for(name).get("running"))


def snapshot(*, name: str = "refresh") -> dict:
    _ensure_loaded(name)
    with _lock_for(name):
        return dict(_state_for(name))


def reset_for_tests() -> None:
    for name in list(_PERSIST_KEYS):
        _loaded.add(name)
        with _lock_for(name):
            state = _state_for(name)
            state.clear()
            state.update(_empty_state())
