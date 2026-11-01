"""强信号新闻推送：分析出「高关联 + 明确方向」的新闻时主动通知。

事件驱动链路原本只把结果写进库，等用户打开页面才看得到。这里在分析成功后
把真正够强的信号（关联度高、方向明确）直接推到已配置的通知渠道。
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections import deque

from src.platform.notifications.notifier import NotifierManager
from src.platform.notifications.notify_policy import NotifyPolicy
from src.platform.persistence.database import SessionLocal
from src.platform.persistence.models import AppSettings, MarketNews, NotifyChannel

logger = logging.getLogger(__name__)

# 关联度达到此值且方向绝对值达到 STRONG_BULLISH_ABS 才算「强信号」。
STRONG_RELATION_LEVEL = 7
STRONG_BULLISH_ABS = 6
# 开关与渠道：news_signal_alert_enabled=true/false；渠道留空用默认渠道。
ENABLED_KEY = "news_signal_alert_enabled"
CHANNEL_IDS_KEY = "news_signal_alert_channel_ids"
# 限流：每小时最多 N 条、两条之间至少间隔 N 秒，避免批量分析时刷屏。
MAX_PER_HOUR = 6
MIN_INTERVAL_SEC = 30

_sent_at: deque[float] = deque(maxlen=64)
_last_sent = 0.0
# 4 个分析 worker 会并发进来：限流检查与「占位」之间隔着 await notify，
# 不加锁就会同时通过检查、把「每小时最多 6 条 / 间隔 30s」一起突破。
# 推送本身是低频动作（≤6 条/小时），整段串行没有性能问题。
_push_lock = asyncio.Lock()


def is_strong_signal(relation_level, bullish_level) -> bool:
    try:
        relation = int(relation_level or 0)
        bullish = int(bullish_level or 0)
    except (TypeError, ValueError):
        return False
    return relation >= STRONG_RELATION_LEVEL and abs(bullish) >= STRONG_BULLISH_ABS


def _setting(db, key: str, default: str = "") -> str:
    row = db.query(AppSettings).filter(AppSettings.key == key).first()
    if row is None or row.value is None:
        return default
    return str(row.value)


def _channels(db) -> list[NotifyChannel]:
    raw = _setting(db, CHANNEL_IDS_KEY, "").strip()
    ids = [int(part) for part in raw.replace(" ", "").split(",") if part.isdigit()]
    query = db.query(NotifyChannel).filter(NotifyChannel.enabled.is_(True))
    if ids:
        return query.filter(NotifyChannel.id.in_(ids)).all()
    return query.filter(NotifyChannel.is_default.is_(True)).all()


def _build_notifier(channels: list[NotifyChannel]) -> NotifierManager | None:
    if not channels:
        return None
    try:
        from src.platform.runtime.config import Settings

        settings = Settings()
        policy = NotifyPolicy(
            timezone=settings.app_timezone or "UTC",
            quiet_hours=settings.notify_quiet_hours or "",
        )
    except Exception:  # 配置异常不应阻断推送
        policy = None
    manager = NotifierManager(policy=policy)
    for channel in channels:
        manager.add_channel(channel.type, channel.config or {})
    return manager


def _format(row: MarketNews) -> tuple[str, str]:
    bullish = int(row.bullish_level or 0)
    direction = "利好" if bullish > 0 else "利空"
    sign = f"+{bullish}" if bullish > 0 else str(bullish)
    stocks = row.relations_stocks or []
    names = "、".join(
        f"{item.get('name') or item.get('code')}({item.get('code')})"
        for item in stocks[:3]
        if isinstance(item, dict)
    )
    title = f"事件驱动 · {direction}信号"
    lines = [
        f"【{direction} {sign} · 关联度 {int(row.relation_level or 0)}】",
        row.digest or "",
        f"关联标的：{names or '—'}",
    ]
    reason = (row.bullish_level_reason or row.relation_reason or "").strip()
    if reason:
        lines.append(f"依据：{reason}")
    if row.url:
        lines.append(f"来源：{row.url}")
    return title, "\n\n".join(line for line in lines if line)


def _rate_limited(now: float) -> bool:
    while _sent_at and now - _sent_at[0] > 3600:
        _sent_at.popleft()
    if len(_sent_at) >= MAX_PER_HOUR:
        return True
    return now - _last_sent < MIN_INTERVAL_SEC


async def push_if_strong(news_id: int) -> bool:
    """分析成功后调用：够强就推一条。任何异常只记日志，不影响分析流程。"""
    async with _push_lock:
        return await _push_if_strong_locked(news_id)


async def _push_if_strong_locked(news_id: int) -> bool:
    global _last_sent
    db = SessionLocal()
    try:
        row = db.get(MarketNews, int(news_id))
        if row is None or int(row.analyzed or 0) != 1:
            return False
        if not is_strong_signal(row.relation_level, row.bullish_level):
            return False
        if _setting(db, ENABLED_KEY, "true").strip().lower() != "true":
            return False
        notifier = _build_notifier(_channels(db))
        if notifier is None:
            logger.debug("强信号新闻未推送：未配置通知渠道 id=%s", news_id)
            return False
        bullish = int(row.bullish_level or 0)
        title, content = _format(row)
    finally:
        db.close()

    now = time.monotonic()
    if _rate_limited(now):
        logger.debug("强信号新闻未推送：触发限流 id=%s", news_id)
        return False

    try:
        result = await notifier.notify(title, content)
        ok = bool((result or {}).get("success", True))
    except Exception as exc:
        logger.warning(
            "强信号新闻推送失败 id=%s: %s: %s", news_id, type(exc).__name__, str(exc)[:200]
        )
        return False
    if ok:
        _sent_at.append(now)
        _last_sent = now
        logger.info("强信号新闻已推送 id=%s bullish=%s", news_id, bullish)
    return ok
