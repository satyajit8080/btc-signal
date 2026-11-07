import pytest
from datetime import timedelta

from app.core.context import Datum
from app.engines.data_quality import assess
from app.normalize.consensus import consensus
from app.scoring.config import default_config, merge
from app.scoring.decision import classify, decide, run_engines, score_bucket
from conftest import NOW, build_ctx, relaxed_cfg


def test_bullish_market_trade_yes(bull_ctx):
    d = decide(bull_ctx, relaxed_cfg())
    assert d["direction"] == "BULLISH" and d["trade"] == "YES"
    assert d["score"] >= 30 and d["confidence"] >= 55
    assert d["entry"]["low"] < d["price"] and d["invalidation"] < d["entry"]["low"]
    assert d["why"] and all(w["supports"] for w in d["why"][:3])
    assert "not financial advice" in d["disclaimer"]


def test_bearish_market_trade_yes(bear_ctx):
    d = decide(bear_ctx, relaxed_cfg())
    assert d["direction"] == "BEARISH" and d["trade"] == "YES"
    assert d["invalidation"] > d["entry"]["high"]


def test_sideways_market_neutral_no_trade(flat_ctx):
    d = decide(flat_ctx, default_config())
    assert d["direction"] == "NEUTRAL" and d["trade"] == "NO"
    assert d["entry"] is None
    assert "Neutral" in d["summary"]


def test_bullish_but_crowded_longs_is_no_trade():
    d = decide(build_ctx(1, funding=0.0008, ls_ratio=2.6), relaxed_cfg())
    assert d["direction"] == "BULLISH" and d["trade"] == "NO"
    assert any("crowded" in b.lower() for b in d["trade_quality"]["blocking"])


def test_bearish_weak_confirmation_no_trade():
    ctx = build_ctx(-1, with_intel=False)
    cfg = merge(default_config(), {"trade": {"min_confidence": 90}})
    d = decide(ctx, cfg)
    assert d["trade"] == "NO"


def test_reward_risk_gate_blocks_close_targets(bull_ctx):
    d = decide(bull_ctx, default_config())
    assert d["direction"] == "BULLISH" and d["trade"] == "NO"
    assert d["levels"]["reward_risk"] < 1.5 and d["levels"]["target"] > d["entry"]["high"]
    assert any("Reward/risk" in b for b in d["trade_quality"]["blocking"])


def test_regime_transition_and_conflict_block_trade(bull_ctx):
    from app.scoring import decision as D
    engines = D.run_engines(bull_ctx, relaxed_cfg())
    engines["regime"].flags["regime_transition"] = True
    agg = D.aggregate(engines, relaxed_cfg())
    dq = D.assess(bull_ctx, engines)
    tq = D.trade_quality("BULLISH", 90, agg, dq, engines, {"invalidation_pct": 1.0, "reward_risk": 2.0}, relaxed_cfg(), bull_ctx)
    assert "Unclear transition regime" in tq["blocking"]
    agg.conflict = 0.5
    tq = D.trade_quality("BULLISH", 90, agg, dq, engines, {"invalidation_pct": 1.0, "reward_risk": 2.0}, relaxed_cfg(), bull_ctx)
    assert any("conflict" in b for b in tq["blocking"])


def test_poor_liquidity_blocks_trade(bull_ctx):
    bull_ctx.add_scalar("BTC_SPREAD_BPS", Datum(25.0, "coinbase", NOW))
    d = decide(bull_ctx, relaxed_cfg())
    assert d["trade"] == "NO" and any("Liquidity poor" in b for b in d["trade_quality"]["blocking"])
    assert d["trade_quality"]["liquidity"]["spread_bps"] == 25.0


def test_categories_prevent_double_counting(bull_ctx):
    d = decide(bull_ctx, default_config())
    assert set(d["categories"]) == {"TREND", "MOMENTUM", "FLOW", "DERIVATIVES", "POSITIONING", "ON-CHAIN", "MACRO", "VOLATILITY"}
    # adding a perfectly correlated duplicate of trend would not add a category: weights live at category level
    cfg = merge(default_config(), {"category_weights": {k: 0 for k in d["categories"]} | {"TREND": 1}})
    only_trend = decide(bull_ctx, cfg)
    assert abs(only_trend["raw_score"] - only_trend["categories"]["TREND"]["score"]) < 0.01
    total = sum(c["contribution"] for c in d["components"].values() if c.get("contribution"))
    assert abs(total - d["raw_score"]) < 1.0  # engine contributions add up to the category-weighted score


def test_every_engine_emits_indicator_records(bull_ctx):
    d = decide(bull_ctx, default_config())
    for name, c in d["components"].items():
        if c["available"] and c["directional"]:
            assert c["indicators"], name
            for i in c["indicators"]:
                assert {"raw", "score", "signal", "confidence", "explanation", "ts"} <= set(i)
                assert i["signal"] in ("BULLISH", "BEARISH", "NEUTRAL")


def test_divergence_reduces_confidence(bull_ctx):
    from app.scoring import decision as D
    engines = D.run_engines(bull_ctx, default_config())
    agg = D.aggregate(engines, default_config())
    dq = D.assess(bull_ctx, engines)
    engines["divergence"].flags["bearish_divergences"] = []
    base, _ = D.confidence(agg, dq, "BULLISH", None, engines)
    engines["divergence"].flags["bearish_divergences"] = ["1h RSI", "1h CVD"]
    lower, parts = D.confidence(agg, dq, "BULLISH", None, engines)
    assert lower == pytest.approx(max(0, base - 10)) and parts["opposing_divergences"] == ["1h RSI", "1h CVD"]


def test_liquidation_cascade_blocks_trade(bull_ctx):
    bull_ctx.events["liquidations"].append({"ts": NOW - timedelta(minutes=10), "side": "short", "usd": 60e6, "venue": "bybit"})
    d = decide(bull_ctx, default_config())
    assert d["trade"] == "NO" and d["flags"]["liquidation_cascade"]


def test_extreme_volatility_blocks_trade(bull_ctx):
    from app.core.context import Series
    bull_ctx.add_series("BTC_DVOL", Series("deribit", [(NOW - timedelta(hours=1), 99.0)]))
    d = decide(bull_ctx, default_config())
    assert d["trade"] == "NO" and "Abnormally high volatility" in d["trade_quality"]["blocking"]


def test_stale_critical_data_means_no_trade(bull_ctx):
    bull_ctx.now = NOW + timedelta(hours=7)  # candles, OI and prices all stale
    d = decide(bull_ctx, default_config())
    assert d["trade"] == "NO"
    assert d["data_quality"]["critical_stale"]
    assert d["data_quality"]["score"] < 60


def test_missing_data_never_manufactured():
    ctx = build_ctx(1, with_intel=False)
    d = decide(ctx, default_config())
    for name in ("whale", "etf", "exchange_flow", "liquidation", "smart_money"):
        assert not d["components"][name]["available"]
        assert d["components"][name]["score"] == 0
    assert d["coverage"] < 0.85
    assert d["coverage"] < decide(build_ctx(1), default_config())["coverage"]


def test_low_data_quality_downgrades_direction(bull_ctx):
    cfg = merge(default_config(), {"thresholds": {"min_data_quality_for_direction": 101}})
    d = decide(bull_ctx, cfg)
    assert d["direction"] == "NEUTRAL" and "data quality" in d["direction_note"]


def test_contradictory_providers_lower_confidence():
    ctx = build_ctx(1)
    base = decide(ctx, default_config())["confidence"]
    ctx2 = build_ctx(1)
    ctx2.add_scalar("BTC_PRICE_USD", Datum(ctx2.candles["spot_1h"][-1].close * 1.05, "kraken", NOW))
    ctx2.add_scalar("BTC_PRICE_USD", Datum(ctx2.candles["spot_1h"][-1].close * 0.95, "coinbase", NOW))
    dq = assess(ctx2, run_engines(ctx2, default_config()))
    assert dq.categories["price"] < 1.0
    assert decide(ctx2, default_config())["confidence"] <= base


def test_confidence_uses_history_only_with_enough_samples(bull_ctx):
    good = decide(bull_ctx, default_config(), lambda d, b: (100, 0.9))["confidence_parts"]
    few = decide(bull_ctx, default_config(), lambda d, b: (5, 0.9))["confidence_parts"]
    assert good["history"] == 0.9 and few["history"] == 0.5


def test_weights_are_configurable(bull_ctx):
    only_trend = {k: 0 for k in default_config().weights} | {"trend": 1}
    cats = {k: 0 for k in default_config().category_weights} | {"TREND": 1}
    d = decide(bull_ctx, merge(default_config(), {"weights": only_trend, "category_weights": cats}))
    assert abs(d["raw_score"] - d["components"]["trend"]["score"]) < 1e-6


def test_engine_exception_isolated(bull_ctx, monkeypatch):
    from app import engines
    monkeypatch.setitem(engines.DIRECTIONAL_ENGINES, "macro", lambda ctx, cfg: 1 / 0)
    d = decide(bull_ctx, default_config())
    assert not d["components"]["macro"]["available"] and "engine error" in d["components"]["macro"]["reasons"][0]


def test_helpers():
    assert classify(75) == "HIGH" and classify(50) == "MEDIUM" and classify(10) == "LOW"
    assert score_bucket(10) == "weak" and score_bucket(-55) == "strong" and score_bucket(90) == "extreme"


def test_consensus_outlier_and_single_source():
    ctx = build_ctx(1)
    p = ctx.candles["spot_1h"][-1].close
    ctx.add_scalar("BTC_PRICE_USD", Datum(p * 1.2, "kraken", NOW))
    c = consensus(ctx, "BTC_PRICE_USD")
    assert "kraken" in c.outliers and abs(c.value / p - 1) < 0.01 and c.agreement < 1
    ctx.scalars["BTC_PRICE_USD"] = [Datum(p, "coinbase", NOW)]
    one = consensus(ctx, "BTC_PRICE_USD")
    assert one.agreement == 0.5 and one.n == 1
    assert consensus(ctx, "NOPE").value is None


def test_config_validation():
    import pytest
    with pytest.raises(Exception):
        merge(default_config(), {"weights": {"bogus": 1}})
    with pytest.raises(Exception):
        merge(default_config(), {"weights": {"trend": -1}})
    with pytest.raises(Exception):
        merge(default_config(), {"category_weights": {"NOPE": 1}})
    from app.scoring.config import ScoringConfig
    old = ScoringConfig.model_validate({"weights": {"trend": 5}})  # config saved by an older version
    assert old.weights["trend"] == 5 and "momentum" in old.weights and "TREND" in old.category_weights
