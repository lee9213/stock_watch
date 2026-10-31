from src.modules.strategy.opportunity_markets import (
    OPPORTUNITY_MARKETS,
    resolve_opportunity_market_query,
    resolve_opportunity_markets,
)


def test_opportunity_markets_are_a_share_only():
    assert OPPORTUNITY_MARKETS == ("CN",)


def test_resolve_opportunity_markets_defaults_and_drops_offshore():
    assert resolve_opportunity_markets(None) == ["CN"]
    assert resolve_opportunity_markets(["CN", "HK", "US"]) == ["CN"]
    assert resolve_opportunity_markets(["US", "HK"]) == []
    assert resolve_opportunity_markets(["cn"]) == ["CN"]


def test_resolve_opportunity_market_query():
    assert resolve_opportunity_market_query("") == ["CN"]
    assert resolve_opportunity_market_query("ALL") == ["CN"]
    assert resolve_opportunity_market_query("CN") == ["CN"]
    assert resolve_opportunity_market_query("HK") is None
    assert resolve_opportunity_market_query("US") is None
