"""日志降噪: 三方库过滤、access log 过滤、重复告警抑制。"""

import logging

from src.platform.observability.log_handler import (
    AccessLogFilter,
    NoiseFilter,
    RepeatFilter,
    is_noisy_logger,
)


def _record(name: str, level: int, msg: str = "hello", args=None) -> logging.LogRecord:
    return logging.LogRecord(
        name=name, level=level, pathname=__file__, lineno=1, msg=msg, args=args, exc_info=None
    )


def test_noisy_logger_matches_known_third_party():
    for name in (
        "httpx",
        "httpx._client",
        "httpcore.http11",
        "httpcore2.http11",  # 依赖里重复打包的同类库, 带版本后缀
        "httpx2",
        "urllib3.connectionpool",
        "apscheduler.scheduler",
        "apscheduler.executors.default",
        "openai._base_client",
        "sqlalchemy.engine",
    ):
        assert is_noisy_logger(name), name


def test_business_loggers_are_not_noisy():
    for name in (
        "src.modules.market.news_flow_scheduler",
        "marketdata.http",
        "uvicorn.access",  # 由 AccessLogFilter 单独处理, 不能整体丢弃
        "uvicorn.error",
    ):
        assert not is_noisy_logger(name), name


def test_noise_filter_drops_debug_but_keeps_warnings():
    f = NoiseFilter()
    assert not f.filter(_record("httpcore.http11", logging.DEBUG))
    assert not f.filter(_record("apscheduler.scheduler", logging.INFO))
    assert f.filter(_record("httpcore.http11", logging.WARNING))
    assert f.filter(_record("src.modules.market.news_flow_scheduler", logging.DEBUG))


def test_access_log_keeps_only_error_status():
    f = AccessLogFilter()
    ok = _record(
        "uvicorn.access",
        logging.INFO,
        '%s - "%s %s HTTP/%s" %d',
        ("127.0.0.1:1", "GET", "/api/health", "1.1", 200),
    )
    not_found = _record(
        "uvicorn.access",
        logging.INFO,
        '%s - "%s %s HTTP/%s" %d',
        ("127.0.0.1:1", "GET", "/api/nope", "1.1", 404),
    )
    assert not f.filter(ok)
    assert f.filter(not_found)
    # 非 access log 一律放行
    assert f.filter(_record("src.app", logging.INFO))


def test_repeat_filter_throttles_same_warning_only():
    f = RepeatFilter(window=60)
    first = _record("marketdata.http", logging.WARNING, "腾讯K线 获取失败 symbol=sh600000")
    second = _record("marketdata.http", logging.WARNING, "腾讯K线 获取失败 symbol=sh600000")
    other = _record("marketdata.http", logging.WARNING, "腾讯K线 获取失败 symbol=sz000001")
    error = _record("marketdata.http", logging.ERROR, "腾讯K线 获取失败 symbol=sh600000")

    assert f.filter(first)
    assert not f.filter(second)
    assert f.filter(other)  # 不同标的仍是独立信息
    assert f.filter(error)  # ERROR 不节流
    assert f._suppressed == 1


def test_repeat_filter_allows_again_after_window():
    f = RepeatFilter(window=0.05)
    rec = _record("marketdata.http", logging.WARNING, "same")
    assert f.filter(rec)
    assert not f.filter(rec)
    import time

    time.sleep(0.08)
    assert f.filter(rec)
