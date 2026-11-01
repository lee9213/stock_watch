"""候选列表的过滤下推：strategy 过滤必须在 SQL 里做，不能先 limit 再筛。

改造前 query 先取 limit*4 行，再在 Python 里按 strategy_tags 过滤，
选具体策略 + 高 min_score 时返回条数会远小于请求值（甚至为 0）。
"""

from __future__ import annotations

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import src.platform.persistence.models  # noqa: F401  注册所有 ORM 模型
import src.modules.strategy.entry_candidates as ec
from src.platform.persistence.database import Base
from src.platform.persistence.models import EntryCandidate


def _session_factory():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine)


def _seed(Session, *, tag: str, count: int, base_score: float, prefix: str) -> None:
    db = Session()
    try:
        for i in range(count):
            db.add(
                EntryCandidate(
                    stock_symbol=f"{prefix}{i:03d}",
                    stock_market="CN",
                    stock_name=f"票{prefix}{i}",
                    snapshot_date="2026-10-30",
                    status="active",
                    score=base_score - i * 0.01,
                    action="buy",
                    action_label="建仓",
                    candidate_source="market_scan",
                    strategy_tags=[tag],
                    plan_quality=100,
                )
            )
        db.commit()
    finally:
        db.close()


def test_strategy_filter_returns_full_page(monkeypatch):
    Session = _session_factory()
    # 高分都是 trend_follow，低分才是 momentum：先 limit 再过滤会一条都不剩
    _seed(Session, tag="trend_follow", count=30, base_score=99.0, prefix="6008")
    _seed(Session, tag="momentum", count=30, base_score=80.0, prefix="6009")
    monkeypatch.setattr(ec, "SessionLocal", Session)

    result = ec.list_entry_candidates(status="all", limit=10, strategy="momentum")
    assert result["count"] == 10
    assert all("momentum" in (item["strategy_tags"] or []) for item in result["items"])


def test_strategy_filter_matches_exact_tag_only(monkeypatch):
    Session = _session_factory()
    _seed(Session, tag="momentum_plus", count=5, base_score=99.0, prefix="6010")
    _seed(Session, tag="momentum", count=5, base_score=90.0, prefix="6011")
    monkeypatch.setattr(ec, "SessionLocal", Session)

    result = ec.list_entry_candidates(status="all", limit=20, strategy="momentum")
    # LIKE 粗筛可能带出 momentum_plus，Python 精确比对后必须只剩 momentum
    assert result["count"] == 5
    assert {tuple(item["strategy_tags"]) for item in result["items"]} == {("momentum",)}
