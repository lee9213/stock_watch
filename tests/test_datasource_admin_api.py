"""数据源管理端点新增能力:is_orphan 标记 + POST /reset-to-seed 温和对账。"""

from __future__ import annotations

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import src.modules.administration.api.datasources as ds
import src.platform.persistence.models as M  # noqa: F401  确保模型注册到 Base.metadata
from src.platform.persistence.database import Base, get_db
from src.platform.persistence.models import DataSource


def _client():
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    app = FastAPI()
    app.include_router(ds.router, prefix="/api/datasources")

    def _db():
        s = Session()
        try:
            yield s
        finally:
            s.close()

    app.dependency_overrides[get_db] = _db
    return TestClient(app), Session


def test_is_orphan_flags_orphan_and_normal_rows_correctly():
    """is_orphan: 孤儿 provider(news/cls)标 True,正常 seed provider(quote/tencent)标 False。"""
    from types import SimpleNamespace

    orphan_row = SimpleNamespace(
        id=1, name="财联社电报", type="news", provider="cls",
        config={}, enabled=True, priority=0, supports_batch=False, test_symbols=[],
    )
    normal_row = SimpleNamespace(
        id=2, name="腾讯行情", type="quote", provider="tencent",
        config={}, enabled=True, priority=0, supports_batch=True, test_symbols=[],
    )

    assert ds._to_response(orphan_row)["is_orphan"] is True
    assert ds._to_response(normal_row)["is_orphan"] is False


def test_reset_to_seed_endpoint_deletes_orphan_and_returns_summary():
    """POST /reset-to-seed: 冒烟 —— 删孤儿行、返回 summary(经中间件包裹前的原始 dict)。"""
    client, Session = _client()

    db = Session()
    db.add(
        DataSource(
            name="财联社电报", type="news", provider="cls", config={},
            enabled=True, priority=0, supports_batch=False, test_symbols=[],
        )
    )
    db.commit()
    db.close()

    resp = client.post("/api/datasources/reset-to-seed")
    assert resp.status_code == 200, resp.text
    body = resp.json()

    deleted_pairs = {(d["type"], d["provider"]) for d in body["deleted"]}
    assert ("news", "cls") in deleted_pairs
    assert "seeded_missing" in body

    db2 = Session()
    remaining = {(s.type, s.provider) for s in db2.query(DataSource).all()}
    assert ("news", "cls") not in remaining
    # 缺失的默认(如东财K线)应被补回
    assert ("kline", "eastmoney") in remaining
    db2.close()


# ---------------------------------------------------------------------------
# PUT /priority：页面「上移/下移」用的原子批量排序
#
# 前端曾经用 Promise.all 并发发两个 PUT 交换 priority：
#  - SQLite 只有一个写者，并发时后到的连接会以只读方式打开库，写操作直接抛
#    "attempt to write a readonly database"，一个请求 500、另一个已提交，
#    用户看到「提示失败、刷新却生效了」；
#  - 交换 priority 要求两条取值不同，同组出现相同值时交换是空操作，箭头点了没反应。
# ---------------------------------------------------------------------------


def _seed_three(Session) -> list[int]:
    db = Session()
    rows = [
        DataSource(name="甲", type="event_news", provider="alpha", config={},
                   enabled=True, priority=0, supports_batch=False, test_symbols=[]),
        DataSource(name="乙", type="event_news", provider="beta", config={},
                   enabled=True, priority=5, supports_batch=False, test_symbols=[]),
        DataSource(name="丙", type="event_news", provider="gamma", config={},
                   enabled=True, priority=5, supports_batch=False, test_symbols=[]),
    ]
    db.add_all(rows)
    db.commit()
    ids = [r.id for r in rows]
    db.close()
    return ids


def test_reorder_applies_whole_group_in_one_request():
    """一次提交整组顺序：重复 priority 也能被正确重排（旧实现交换 5 和 5 等于没动）。"""
    client, Session = _client()
    a, b, c = _seed_three(Session)

    resp = client.put(
        "/api/datasources/priority",
        json={"items": [
            {"id": c, "priority": 0},
            {"id": a, "priority": 10},
            {"id": b, "priority": 20},
        ]},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["updated"] == 3

    db = Session()
    ordered = [
        s.id for s in db.query(DataSource)
        .filter(DataSource.type == "event_news")
        .order_by(DataSource.priority, DataSource.id)
    ]
    assert ordered == [c, a, b], "整组按提交顺序生效，且这三次写入在同一事务里"
    db.close()


def test_reorder_skips_noop_updates():
    """已经是目标值时不写库，避免无意义的写锁。"""
    client, Session = _client()
    a, _b, _c = _seed_three(Session)

    resp = client.put(
        "/api/datasources/priority",
        json={"items": [{"id": a, "priority": 0}]},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["updated"] == 0


def test_reorder_accepts_empty_list():
    client, Session = _client()
    _seed_three(Session)

    resp = client.put("/api/datasources/priority", json={"items": []})
    assert resp.status_code == 200, resp.text
    assert resp.json()["updated"] == 0


def test_reorder_rejects_unknown_id_without_partial_write():
    """有一个 id 不存在时整批回滚，不能出现「改了一半」。"""
    client, Session = _client()
    a, _b, _c = _seed_three(Session)

    resp = client.put(
        "/api/datasources/priority",
        json={"items": [{"id": a, "priority": 99}, {"id": 987654, "priority": 1}]},
    )
    assert resp.status_code == 404, resp.text
    assert resp.json()["detail"]["code"] == "datasource_not_found"

    db = Session()
    assert db.get(DataSource, a).priority == 0, "校验失败时不能写入任何一条"
    db.close()


def test_reorder_rejects_duplicate_id():
    client, Session = _client()
    a, _b, _c = _seed_three(Session)

    resp = client.put(
        "/api/datasources/priority",
        json={"items": [{"id": a, "priority": 0}, {"id": a, "priority": 10}]},
    )
    assert resp.status_code == 400, resp.text
    assert resp.json()["detail"]["code"] == "datasource_duplicate_id"


def test_reorder_route_is_not_shadowed_by_path_param_route():
    """/priority 必须注册在 /{source_id} 之前，否则会被当成 id 解析成 422。"""
    client, Session = _client()
    a, _b, _c = _seed_three(Session)

    resp = client.put("/api/datasources/priority", json={"items": [{"id": a, "priority": 3}]})
    assert resp.status_code == 200, resp.text
    assert "detail" not in resp.json() or "path" not in str(resp.json().get("detail"))
