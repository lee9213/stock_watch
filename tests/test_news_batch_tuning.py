"""攒批策略与提示词成本的回归测试。

背景（2026-11 实测）：
- 新闻到达约 5 条/小时、盘中每轮采集 0.25 条，"5 条一批"在供给上就凑不出来；
  同一轮采集是同步 put 一批 id，``get_nowait`` 一次就能取空——固定 1.5s 的攒批
  等待对真实链路毫无作用，只是给每条新闻多加 1.5 秒延迟。
- 因此攒批改为「按市场阶段取值」：盘中 0（时效优先），非交易时段才跨轮凑批。
- 固定指令占单次调用成本 82%，压缩输出格式与按需注入政策清单是主要杠杆。
"""

from __future__ import annotations

import asyncio
import time
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from src.modules.market import news_analysis
from src.modules.market.news_analysis import (
    BATCH_OUTPUT_SPEC,
    NewsSnapshot,
    build_analysis_prompt,
    build_batch_prompt,
)
from src.modules.market import news_flow_scheduler as nfs

SH = ZoneInfo("Asia/Shanghai")


def _at(day: int, hour: int, minute: int = 0) -> datetime:
    """2026-11-02 是周一，11-01 是周日（用于区分交易日与休市）。"""
    return datetime(2026, 11, day, hour, minute, tzinfo=SH)


@pytest.mark.parametrize(
    ("moment", "expected"),
    [
        (_at(2, 10), nfs.BATCH_LINGER_TRADING_SEC),
        (_at(2, 12), nfs.BATCH_LINGER_TRADING_SEC),
        (_at(2, 8, 30), 0.0),
        (_at(2, 16), nfs.BATCH_LINGER_AFTER_CLOSE_SEC),
        (_at(2, 23, 30), nfs.BATCH_LINGER_NIGHT_SEC),
        (_at(2, 3), nfs.BATCH_LINGER_NIGHT_SEC),
        (_at(1, 10), nfs.BATCH_LINGER_NON_TRADING_SEC),
    ],
)
def test_batch_linger_follows_market_phase(moment, expected):
    """盘中不等待；盘后 15 分钟；夜间/休市与采集间隔对齐。"""
    assert nfs.batch_linger_sec(now=moment) == expected


def test_trading_hours_never_wait(monkeypatch):
    """盘中必须立即分析：快讯的时效性比省一次调用重要。"""
    assert nfs.batch_linger_sec(_at(2, 10)) == 0.0
    assert nfs.batch_linger_sec(_at(2, 12)) == 0.0
    # 开盘前的 8 点为开盘做准备，同样不等待
    assert nfs.batch_linger_sec(_at(2, 8, 30)) == 0.0


def test_single_item_returns_immediately_when_linger_disabled(monkeypatch):
    """队列只剩一条、阶段不允许等待时，取批必须立刻返回。"""
    monkeypatch.setattr(nfs, "batch_linger_sec", lambda *a, **k: 0.0)

    async def _run():
        sched = nfs.NewsFlowScheduler()
        sched._analyze_queue.put_nowait(7)
        started = time.monotonic()
        batch = await sched._next_batch()
        return batch, time.monotonic() - started

    batch, elapsed = asyncio.run(_run())
    assert batch == [7]
    assert elapsed < 0.2, f"不允许攒批时不应等待，实际 {elapsed:.2f}s"


def test_wait_more_releases_early_when_market_opens(monkeypatch):
    """跨轮等待期间市场进入盘中，必须立刻放行，不能拖到 deadline。"""
    monkeypatch.setattr(nfs, "BATCH_PHASE_RECHECK_SEC", 0.02)
    calls = {"n": 0}

    def _linger(*_a, **_k):
        calls["n"] += 1
        # 第一次（取批时）允许等 5s，第二次（重查阶段）返回 0 表示已开盘
        return 5.0 if calls["n"] == 1 else 0.0

    monkeypatch.setattr(nfs, "batch_linger_sec", _linger)

    async def _run():
        sched = nfs.NewsFlowScheduler()
        sched._analyze_queue.put_nowait(3)
        started = time.monotonic()
        batch = await sched._next_batch()
        return batch, time.monotonic() - started

    batch, elapsed = asyncio.run(_run())
    assert batch == [3]
    assert elapsed < 1.0, f"阶段切换后应提前放行，实际 {elapsed:.2f}s"


def test_manual_ingest_bypasses_cross_round_linger(monkeypatch):
    """手动采集后的新闻必须立刻分析——否则周末要等 30 分钟才有结果。"""
    monkeypatch.setattr(nfs, "batch_linger_sec", lambda *a, **k: 1800.0)

    async def _run():
        sched = nfs.NewsFlowScheduler()
        assert sched._current_linger() == 1800.0

        async def _fake_ingest(**_kwargs):
            return {
                "ths": 0,
                "wallstreet": 0,
                "stock_star": 0,
                "created": 0,
                "enqueued": 0,
                "failed": False,
            }

        monkeypatch.setattr(sched, "_run_ingest", _fake_ingest)
        result = await sched.ingest_now()
        return result, sched._current_linger()

    result, linger = asyncio.run(_run())
    assert result["status"] == "ok"
    assert linger == 0.0, "手动采集窗口内不应再走跨轮攒批"


def test_policy_focus_injected_only_for_policy_news():
    """十五五清单只影响多标的排序偏好，不对每条新闻都注入。"""
    plain = build_analysis_prompt(
        news_time="",
        news_content="某城商行公告三季度净息差环比收窄5个基点",
        known_stocks=[],
    )
    policy = build_analysis_prompt(
        news_time="",
        news_content="工信部发布机器人产业三年行动方案，提出核心零部件国产化目标",
        known_stocks=[],
    )
    assert "十五五" not in plain
    assert "十五五" in policy
    assert len(policy) > len(plain)


def test_batch_prompt_keeps_rules_but_shorter_than_before():
    """压掉重复强调的输出格式说明，规则段必须完整保留。"""
    moments = [NewsSnapshot(id=i, digest=f"新闻{i}", raw_text=f"正文{i}") for i in range(1, 6)]
    prompt = build_batch_prompt(moments)

    # 规则段（字段定义）不能丢
    assert "市场关联度" in prompt and "bullish_level_reason" in prompt
    # 每条新闻只出现一次正文
    assert prompt.count("digest: 正文") == 5
    # 压缩后仍明确要求条数与 index 对齐（解析依赖这两点）
    assert "index 必须对应" in prompt
    assert len(BATCH_OUTPUT_SPEC) < 320, "输出格式说明应已压缩"
    assert len(prompt) < 1750, f"5 条批量 prompt 应短于 1750 字，实际 {len(prompt)}"


def test_analyze_worker_concurrency_is_two():
    """供给约 5 条/小时，4 个 worker 纯空转，还各持一份 60s 过期的客户端缓存。"""
    assert news_analysis.INGEST_ANALYZE_CONCURRENCY == 2
    assert nfs.ANALYZE_WORKER_CONCURRENCY == 2
