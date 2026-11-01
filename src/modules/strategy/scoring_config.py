"""机会链路阈值集中配置。

改造前这些数字散落在 ``entry_candidates`` / ``strategy_engine`` 两处硬编码，
调参时容易改一处漏一处、也无法在不改代码的情况下临时放宽/收紧。
这里统一收口：**默认值与改造前逐项一致**（保证零行为变化），需要调参用环境
变量覆盖即可。

命名约定：``PW_`` 前缀 = PanWatch 机会链路参数。
"""

from __future__ import annotations

import os


def _num(key: str, default: float) -> float:
    raw = os.environ.get(key)
    if raw is None or not str(raw).strip():
        return default
    try:
        return float(str(raw).strip())
    except ValueError:
        return default


def _int(key: str, default: int) -> int:
    return int(_num(key, float(default)))


# ---------------------------------------------------------------- 动作基准分
ACTION_BASE_SCORE: dict[str, float] = {
    "buy": 78.0,
    "add": 72.0,
    "hold": 58.0,
    "watch": 52.0,
    "alert": 45.0,
    "reduce": 30.0,
    "sell": 20.0,
    "avoid": 15.0,
}

# ---------------------------------------------------------------- 候选层门槛
# 涨幅过热：涨停幅度 - 该值
OVERHEAT_DELTA_PCT = _num("PW_OVERHEAT_DELTA_PCT", 0.5)
# 当日大跌硬阈值（%）：跌幅达到后不再给 buy/add
CRASH_CHANGE_PCT = _num("PW_CRASH_CHANGE_PCT", -7.0)
# A 股买入最低成交额（元），低于视为薄票
MIN_TURNOVER_FOR_BUY = _num("PW_MIN_TURNOVER_FOR_BUY", 80_000_000.0)
# 日 K 形态必须叠加当日盘口确认
INTRADAY_CONFIRM_TAGS = frozenset({"momentum", "volume_breakout", "pullback"})
INTRADAY_CONFIRM_CHANGE_PCT = _num("PW_INTRADAY_CONFIRM_CHANGE_PCT", 1.5)
INTRADAY_CONFIRM_VOLUME_RATIO = _num("PW_INTRADAY_CONFIRM_VOLUME_RATIO", 1.2)
# 计划结构合理性
PLAN_BAND_WIDTH_MIN = _num("PW_PLAN_BAND_WIDTH_MIN", 0.004)
PLAN_BAND_WIDTH_MAX = _num("PW_PLAN_BAND_WIDTH_MAX", 0.08)
PLAN_MID_PRICE_RATIO_MAX = _num("PW_PLAN_MID_PRICE_RATIO_MAX", 1.5)
# 入场区间宽度按 ATR 的比例给定（旧实现固定为现价 ±1%，与波动率无关：
# 振幅 2% 的蓝筹和振幅 9% 的小票拿到同样宽的区间，窄票容易被噪音扫掉、
# 宽票又常在区间外踏空）。半宽 = clamp(atr_pct * 该系数, min, max)。
PLAN_ATR_BAND_MULT = _num("PW_PLAN_ATR_BAND_MULT", 0.35)
PLAN_BAND_HALF_MIN = _num("PW_PLAN_BAND_HALF_MIN", 0.004)
PLAN_BAND_HALF_MAX = _num("PW_PLAN_BAND_HALF_MAX", 0.025)
# 止损：支撑与 ATR 取更靠外的一个，再用最小/最大幅度夹住
PLAN_ATR_STOP_MULT = _num("PW_PLAN_ATR_STOP_MULT", 1.2)
PLAN_STOP_MIN_PCT = _num("PW_PLAN_STOP_MIN_PCT", 0.02)
PLAN_STOP_MAX_PCT = _num("PW_PLAN_STOP_MAX_PCT", 0.08)
# 目标：阻力与 ATR 取更远的一个
PLAN_ATR_TARGET_MULT = _num("PW_PLAN_ATR_TARGET_MULT", 2.4)
# 目标涨幅上限：阻力位离得过远时（如 +20%），10 日 horizon 内基本不可能触及，
# 这种目标只会让 hit_target 恒为 0，失去诊断价值。
PLAN_TARGET_MAX_PCT = _num("PW_PLAN_TARGET_MAX_PCT", 0.15)
# 风险回报比门槛：低于该值的计划不允许拿满 quality（即进不了 active）。
# 改造前没有这项约束，实测出现过"目标 +0.5% / 止损 -6.4%"的计划拿到 100 分，
# 回测里目标极易触发、止损几乎打不到，胜率被系统性抬高。
# 门槛的落法是"收紧止损 / 直接降级"，**不是把目标抬到阻力之上**——后者只是把
# R:R 这个指标凑成 1.5，目标却变成公式产物而非市场结构（实测曾产出目标 +12%、
# 止损 -8%、R:R 恰好 1.50 的计划，三个数字全是常量边界）。
PLAN_MIN_RISK_REWARD = _num("PW_PLAN_MIN_RISK_REWARD", 1.5)
# active 定级：计划完整度下限 + 分数下限（市场池 / 关注池）
MIN_PLAN_QUALITY_FOR_ACTIVE = _num("PW_MIN_PLAN_QUALITY", 90.0)
ACTIVE_SCORE_MARKET_SCAN = _num("PW_ACTIVE_SCORE_MARKET_SCAN", 62.0)
ACTIVE_SCORE_WATCHLIST = _num("PW_ACTIVE_SCORE_WATCHLIST", 55.0)
# 行情过期：补位行情超过该分钟数视为不可用于开仓
STALE_QUOTE_MINUTES = _num("PW_STALE_QUOTE_MINUTES", 120.0)
# 日 K 并发拉取线程数
KLINE_FETCH_WORKERS = _int("PW_KLINE_FETCH_WORKERS", 6)

# ---------------------------------------------------------------- 策略层合成
# 可执行信号百分位映射区间。映射用中点口径 (idx+0.5)/n，两端都是开区间：
# 当日 n 只可执行票时最高分是 CEIL - span/(2n)，所以 100 分取不到，也不用取到
# （旧口径 idx/(n-1) 在 n=1 时除零，只有 1 只票就恒定 100，反而是虚高）。
ACTIONABLE_RANK_FLOOR = _num("PW_ACTIONABLE_RANK_FLOOR", 75.0)
ACTIONABLE_RANK_CEIL = _num("PW_ACTIONABLE_RANK_CEIL", 100.0)
# 分数语义与动作对齐的天花板
CAP_INACTIVE_SCORE = _num("PW_CAP_INACTIVE_SCORE", 69.0)
CAP_WATCH_UNHELD = _num("PW_CAP_WATCH_UNHELD", 65.0)
CAP_WATCH_HOLDING = _num("PW_CAP_WATCH_HOLDING", 78.0)
CAP_NO_ENTRY_PLAN = _num("PW_CAP_NO_ENTRY_PLAN", 66.0)

# 新闻催化
EVENT_SCORE_CAP = _num("PW_EVENT_SCORE_CAP", 6.0)
DIRECT_NEWS_COUNT_CAP = _num("PW_DIRECT_NEWS_COUNT_CAP", 2.0)
NEWS_CATALYST_CAP = _num("PW_NEWS_CATALYST_CAP", 4.5)
NEWS_IMPACT_SCALE = _num("PW_NEWS_IMPACT_SCALE", 0.8)
NEWS_IMPACT_HALF_LIFE_HOURS = _num("PW_NEWS_IMPACT_HALF_LIFE_HOURS", 24.0)
NEWS_BEARISH_WEIGHT = _num("PW_NEWS_BEARISH_WEIGHT", 1.5)
NEWS_NAME_ONLY_WEIGHT = _num("PW_NEWS_NAME_ONLY_WEIGHT", 0.5)

# 新闻门槛（软约束）：缺个股直接新闻不再硬降级，改为扣分 + 封顶到不可执行区间，
# 避免新闻服务覆盖不足时把整页机会清空；真正的下单口仍有 paper trading 硬门槛兜底。
NEWS_MISSING_PENALTY = _num("PW_NEWS_MISSING_PENALTY", 6.0)
NEWS_MISSING_CAP = _num("PW_NEWS_MISSING_CAP", 74.0)
MIN_EVENT_BIAS_FOR_SIGNAL = _num("PW_MIN_EVENT_BIAS_FOR_SIGNAL", 0.0)
# "缺直接新闻"的封顶只对"有新闻但都不直接"生效。若该股一条新闻都没有
# （news_count=0），那是数据覆盖缺失（unknown），不是"没有利好"。
# 混为一谈时实测后果：全市场 active 一律封顶 74 → 可执行数恒为 0，页面等于废掉。
# 设 PW_NEWS_MISSING_REQUIRES_DATA=0 可退回"没有直接新闻就封顶"的旧行为。
NEWS_MISSING_REQUIRES_DATA = _int("PW_NEWS_MISSING_REQUIRES_DATA", 1) != 0

# 组合约束
# 注：机会链路当前只扫 A 股（见 opportunity_markets.OPPORTUNITY_MARKETS），
# HK/US 两组值暂时不会命中，保留是为了将来放开港股/美股机会页时不用改代码。
MAX_UNHELD_ACTIVE_BY_MARKET = {
    "CN": _int("PW_MAX_UNHELD_CN", 30),
    "HK": _int("PW_MAX_UNHELD_HK", 20),
    "US": _int("PW_MAX_UNHELD_US", 20),
}
MAX_HIGH_RISK_RATIO_BY_MARKET = {
    "CN": _num("PW_MAX_HIGH_RISK_CN", 0.35),
    "HK": _num("PW_MAX_HIGH_RISK_HK", 0.32),
    "US": _num("PW_MAX_HIGH_RISK_US", 0.30),
}
MAX_SINGLE_STRATEGY_SHARE = _num("PW_MAX_SINGLE_STRATEGY_SHARE", 0.42)

# ---------------------------------------------------------------- 后验评估
# point-in-time 冻结上线日：该日之后（含）的快照必须带 frozen 才参与后验评估。
# 之前的行没有 frozen 属于历史数据，允许回退到当前值并标记 point_in_time=False。
POINT_IN_TIME_START = os.environ.get("PW_POINT_IN_TIME_START", "2026-11-01").strip()
# 允许冻结的 phase（盘中）；盘前与收盘后都不冻结
FREEZE_PHASES = frozenset({"trading", "break"})
# 有当日行情、需要盘中确认的 phase
INTRADAY_PHASES = frozenset({"trading", "break", "after_hours"})

# ---------------------------------------------------------------- 数据留存
# 策略侧四张表的保留天数（0 = 不清理）。daily 刷新 + 评估会稳定增长，
# 无清理时一年后策略表 ~50 万行、评估表 ~200 万行。
# 注意：strategy_outcomes 有 signal_run_id → strategy_signal_runs 的 ON DELETE CASCADE，
# 删信号会连带删评估，所以评估的实际保留期取两者较小值，这里设成一致避免误解。
RETENTION_CANDIDATE_DAYS = _int("PW_RETENTION_CANDIDATE_DAYS", 180)
RETENTION_SIGNAL_DAYS = _int("PW_RETENTION_SIGNAL_DAYS", 365)
RETENTION_OUTCOME_DAYS = _int("PW_RETENTION_OUTCOME_DAYS", 365)
RETENTION_MARKET_SCAN_DAYS = _int("PW_RETENTION_MARKET_SCAN_DAYS", 60)
# 因子快照与信号行一对一，不跟着信号一起删就会永久留孤儿行。保留天数与信号一致。
RETENTION_FACTOR_SNAPSHOT_DAYS = _int("PW_RETENTION_FACTOR_SNAPSHOT_DAYS", 365)
# 权重变更审计属于低频合规记录，单独给更长的保留期。
RETENTION_WEIGHT_HISTORY_DAYS = _int("PW_RETENTION_WEIGHT_HISTORY_DAYS", 730)
