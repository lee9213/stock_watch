"""表用途说明覆盖所有 ORM 表，并可通过迁移写入。"""

from sqlalchemy import create_engine, text


def test_table_comments_cover_all_mapped_tables():
    import src.modules.research.orchestration.models  # noqa: F401
    import src.platform.persistence.models  # noqa: F401
    from src.platform.persistence.database import Base
    from src.platform.persistence.table_comments import TABLE_COMMENTS

    mapped = set(Base.metadata.tables)
    commented = {name for name, _, _ in TABLE_COMMENTS}
    missing = mapped - commented
    assert not missing, f"未写说明的表: {sorted(missing)}"
    assert "schema_migrations" in commented


def test_m145_creates_comments_table_and_view(tmp_path):
    from src.platform.persistence.migrations import _m145_schema_table_comments
    from src.platform.persistence.table_comments import TABLE_COMMENTS

    engine = create_engine(f"sqlite:///{tmp_path / 'comments.db'}")
    with engine.begin() as conn:
        _m145_schema_table_comments(conn)
        tables = {
            row[0]
            for row in conn.execute(
                text("SELECT name FROM sqlite_master WHERE type='table'")
            )
        }
        views = {
            row[0]
            for row in conn.execute(
                text("SELECT name FROM sqlite_master WHERE type='view'")
            )
        }
        count = conn.execute(text("SELECT COUNT(*) FROM schema_table_comments")).scalar()
        sample = conn.execute(
            text("SELECT 说明 FROM \"表说明\" WHERE 表名='market_news'")
        ).scalar()
    engine.dispose()

    assert "schema_table_comments" in tables
    assert "表说明" in views
    assert count == len(TABLE_COMMENTS)
    assert sample and "事件驱动" in sample


def test_m146_drops_news_cache(tmp_path):
    from src.platform.persistence.migrations import _m146_drop_news_cache

    engine = create_engine(f"sqlite:///{tmp_path / 'news-cache.db'}")
    with engine.begin() as conn:
        conn.execute(
            text(
                """
CREATE TABLE news_cache (
    id INTEGER PRIMARY KEY,
    source TEXT,
    external_id TEXT,
    title TEXT,
    content TEXT,
    publish_time DATETIME,
    symbols JSON,
    importance INTEGER
)
"""
            )
        )
        conn.execute(
            text(
                """
CREATE TABLE schema_table_comments (
    table_name VARCHAR PRIMARY KEY,
    category VARCHAR,
    comment TEXT
)
"""
            )
        )
        conn.execute(
            text(
                "INSERT INTO schema_table_comments(table_name, category, comment) "
                "VALUES('news_cache', '事件驱动', '旧表')"
            )
        )
        _m146_drop_news_cache(conn)
        tables = {
            row[0]
            for row in conn.execute(
                text("SELECT name FROM sqlite_master WHERE type='table'")
            )
        }
        leftover = conn.execute(
            text("SELECT COUNT(*) FROM schema_table_comments WHERE table_name='news_cache'")
        ).scalar()
    engine.dispose()

    assert "news_cache" not in tables
    assert leftover == 0


def test_m147_adds_market_news_reason_columns(tmp_path):
    from src.platform.persistence.migrations import _m147_market_news_reasons

    engine = create_engine(f"sqlite:///{tmp_path / 'news-reasons.db'}")
    with engine.begin() as conn:
        conn.execute(
            text(
                """
CREATE TABLE market_news (
    id INTEGER PRIMARY KEY,
    digest TEXT,
    bullish_level INTEGER
)
"""
            )
        )
        _m147_market_news_reasons(conn)
        columns = {
            row[1]
            for row in conn.execute(text("PRAGMA table_info(market_news)"))
        }
        _m147_market_news_reasons(conn)
    engine.dispose()

    assert "bullish_level_reason" in columns
    assert "relation_reason" in columns


def test_m148_adds_retry_and_digest_hash(tmp_path):
    from src.platform.persistence.migrations import _m148_news_retry_and_digest_hash

    engine = create_engine(f"sqlite:///{tmp_path / 'news-retry.db'}")
    with engine.begin() as conn:
        conn.execute(text("CREATE TABLE market_news (id INTEGER PRIMARY KEY, digest TEXT)"))
        conn.execute(text("CREATE TABLE news_digest (id INTEGER PRIMARY KEY, title TEXT)"))
        _m148_news_retry_and_digest_hash(conn)
        news_cols = {row[1] for row in conn.execute(text("PRAGMA table_info(market_news)"))}
        digest_cols = {row[1] for row in conn.execute(text("PRAGMA table_info(news_digest)"))}
        _m148_news_retry_and_digest_hash(conn)
    engine.dispose()

    assert "analyze_fail_count" in news_cols
    assert "input_hash" in digest_cols


def test_m149_adds_raw_text_and_backfills_unanalyzed(tmp_path):
    from src.platform.persistence.migrations import _m149_market_news_raw_text

    engine = create_engine(f"sqlite:///{tmp_path / 'news-raw.db'}")
    with engine.begin() as conn:
        conn.execute(text("CREATE TABLE market_news (id INTEGER PRIMARY KEY, digest TEXT, analyzed INTEGER)"))
        conn.execute(text("INSERT INTO market_news(id, digest, analyzed) VALUES (1, '未分析原文', 0), (2, '模型摘要', 1)"))
        _m149_market_news_raw_text(conn)
        _m149_market_news_raw_text(conn)
        rows = dict(conn.execute(text("SELECT id, raw_text FROM market_news ORDER BY id")).fetchall())
    engine.dispose()

    assert rows[1] == "未分析原文"
    assert rows[2] == ""


def test_m150_relabels_only_non_research_ths_reports(tmp_path):
    from src.platform.persistence.migrations import _m150_relabel_ths_reports

    engine = create_engine(f"sqlite:///{tmp_path / 'news-relabel.db'}")
    with engine.begin() as conn:
        conn.execute(text(
            "CREATE TABLE market_news (id INTEGER PRIMARY KEY, digest TEXT, raw_text TEXT, sources TEXT, news_type TEXT)"
        ))
        conn.execute(text(
            "INSERT INTO market_news(id, digest, raw_text, sources, news_type) VALUES "
            "(1, '南方泵业公告出售资产', '', 'ths', 'report'), "
            "(2, '券商研报上调评级', '', 'ths', 'report'), "
            "(3, '模型摘要', '原文：某券商研报', 'ths', 'report'), "
            "(4, '证券之星行业周报', '', 'stock_star', 'report')"
        ))
        _m150_relabel_ths_reports(conn)
        rows = dict(conn.execute(text("SELECT id, news_type FROM market_news ORDER BY id")).fetchall())
    engine.dispose()

    assert rows == {1: "news", 2: "report", 3: "report", 4: "report"}


def test_m151_deletes_neutral_news_and_records_discards(tmp_path):
    from src.platform.persistence.migrations import _m151_discard_neutral_news

    engine = create_engine(f"sqlite:///{tmp_path / 'news-discard.db'}")
    with engine.begin() as conn:
        conn.execute(text(
            "CREATE TABLE market_news (id INTEGER PRIMARY KEY, digest TEXT, news_md5 TEXT, url TEXT, "
            "analyzed INTEGER, bullish_level INTEGER)"
        ))
        conn.execute(text(
            "INSERT INTO market_news(id, digest, news_md5, url, analyzed, bullish_level) VALUES "
            "(1, '中性已分析', 'm1', 'https://a', 1, 0), "
            "(2, '利多已分析', 'm2', 'https://b', 1, 5), "
            "(3, '未分析', 'm3', '', 0, 0)"
        ))
        _m151_discard_neutral_news(conn)
        _m151_discard_neutral_news(conn)
        remaining = [r[0] for r in conn.execute(text("SELECT id FROM market_news ORDER BY id"))]
        discards = conn.execute(text("SELECT news_md5, url FROM market_news_discards")).fetchall()
    engine.dispose()

    assert remaining == [2, 3]
    assert [tuple(r) for r in discards] == [("m1", "https://a")]


def test_m152_deletes_weak_signal_news(tmp_path):
    from src.platform.persistence.migrations import (
        _m151_discard_neutral_news,
        _m152_discard_weak_signal_news,
    )

    engine = create_engine(f"sqlite:///{tmp_path / 'news-weak.db'}")
    with engine.begin() as conn:
        conn.execute(text(
            "CREATE TABLE market_news (id INTEGER PRIMARY KEY, digest TEXT, news_md5 TEXT, url TEXT, "
            "analyzed INTEGER, bullish_level INTEGER)"
        ))
        conn.execute(text(
            "INSERT INTO market_news(id, digest, news_md5, url, analyzed, bullish_level) VALUES "
            "(1, '弱利多', 'w1', '', 1, 1), "
            "(2, '弱利空', 'w2', '', 1, -1), "
            "(3, '明确利多', 'w3', '', 1, 2), "
            "(4, '明确利空', 'w4', '', 1, -3), "
            "(5, '未分析', 'w5', '', 0, 1)"
        ))
        _m151_discard_neutral_news(conn)
        _m152_discard_weak_signal_news(conn)
        _m152_discard_weak_signal_news(conn)
        remaining = [r[0] for r in conn.execute(text("SELECT id FROM market_news ORDER BY id"))]
        discarded = sorted(r[0] for r in conn.execute(text("SELECT news_md5 FROM market_news_discards")))
    engine.dispose()

    assert remaining == [3, 4, 5]
    assert discarded == ["w1", "w2"]


def test_m153_backfills_title_key_and_drops_cross_source_copies(tmp_path):
    from src.platform.persistence.migrations import (
        _m151_discard_neutral_news,
        _m153_news_title_dedupe,
    )

    engine = create_engine(f"sqlite:///{tmp_path / 'news-title.db'}")
    with engine.begin() as conn:
        conn.execute(text(
            "CREATE TABLE market_news (id INTEGER PRIMARY KEY, digest TEXT, raw_text TEXT, news_md5 TEXT, "
            "url TEXT, news_time DATETIME, analyzed INTEGER, bullish_level INTEGER)"
        ))
        conn.execute(text(
            "INSERT INTO market_news(id, digest, raw_text, news_md5, url, news_time, analyzed, bullish_level) VALUES "
            "(1, '摘要1', '佑驾创新与腾讯云达成战略合作。正文', 'a', '', '2026-10-30 02:00:00', 1, 6), "
            "(2, '摘要2', '佑驾创新与腾讯云达成战略合作。（原标题：佑驾创新与腾讯云达成战略合作）', 'b', '', '2026-10-30 02:40:00', 1, 4), "
            "(3, '摘要3', 'A股开盘：三大指数集体低开。昨天', 'c', '', '2026-10-29 01:30:00', 1, -5), "
            "(4, '摘要4', 'A股开盘：三大指数集体低开。今天', 'd', '', '2026-10-30 01:30:00', 1, -3), "
            "(5, '摘要5', '暴雷！短标题', 'e', '', '2026-10-30 02:00:00', 1, -4)"
        ))
        _m151_discard_neutral_news(conn)
        _m153_news_title_dedupe(conn)
        _m153_news_title_dedupe(conn)
        rows = conn.execute(text("SELECT id, title_key FROM market_news ORDER BY id")).fetchall()
        discard_cols = {r[1] for r in conn.execute(text("PRAGMA table_info(market_news_discards)"))}
    engine.dispose()

    assert [r[0] for r in rows] == [1, 3, 4, 5]
    assert dict(rows)[1] == "佑驾创新与腾讯云达成战略合作"
    assert dict(rows)[5] == ""
    assert {"title_key", "news_time"} <= discard_cols


def test_m154_drops_similar_title_copies(tmp_path):
    from src.platform.persistence.migrations import _m154_news_similar_title_dedupe

    engine = create_engine(f"sqlite:///{tmp_path / 'news-similar.db'}")
    with engine.begin() as conn:
        conn.execute(text(
            "CREATE TABLE market_news (id INTEGER PRIMARY KEY, title_key TEXT, news_time DATETIME)"
        ))
        conn.execute(text(
            "INSERT INTO market_news(id, title_key, news_time) VALUES "
            "(1, '日本央行将政策利率维持在1.25%不变符合市场预期', '2026-10-30 02:00:00'), "
            "(2, '日本央行将政策利率维持在1.25%不变', '2026-10-30 02:20:00'), "
            "(3, '广期所多晶硅主力合约大跌5%报34460元/吨', '2026-10-30 03:00:00'), "
            "(4, '上期所沪锡主力合约大跌5%报363170元/吨', '2026-10-30 03:05:00'), "
            "(5, '日本央行将政策利率维持在1.25%不变', '2026-10-31 03:00:00'), "
            "(6, '', '2026-10-30 02:10:00')"
        ))
        _m154_news_similar_title_dedupe(conn)
        _m154_news_similar_title_dedupe(conn)
        remaining = [r[0] for r in conn.execute(text("SELECT id FROM market_news ORDER BY id"))]
    engine.dispose()

    assert remaining == [1, 3, 4, 5, 6]
