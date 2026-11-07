from datetime import timedelta

import pytest

from app.backtest.metrics import classification_stats
from app.core.context import Candle, Datum, Series
from app.engines import market
from app.indicators import core as C
from app.indicators import technical as T
from app.providers.keyed import classify_transfer
from app.providers.public_data import parse_coinmetrics
from conftest import NOW, H, build_ctx, make_candles


def flat(n, price=100.0, step=H, vol=10.0):
    return [Candle(NOW - step * (n - i), price, price * 1.001, price * 0.999, price, vol, vol / 2) for i in range(n)]


def ramp(n, start=100.0, pct=0.5, step=H, vol=10.0, taker=0.5):
    out, p = [], start
    for i in range(n):
        c = p * (1 + pct / 100)
        out.append(Candle(NOW - step * (n - i), p, max(p, c) * 1.001, min(p, c) * 0.999, c, vol, vol * taker))
        p = c
    return out


# ---------------- core math ----------------
def test_ema_sma_seeded_and_aligned():
    xs = [float(i) for i in range(1, 31)]
    e = C.ema_series(xs, 10)
    assert e[8] is None and e[9] == pytest.approx(5.5) and e[-1] > e[-2]
    s = C.sma_series(xs, 5)
    assert s[3] is None and s[4] == 3 and s[-1] == 28


def test_rsi_extremes_and_midpoint():
    up = [100 + i for i in range(40)]
    assert C.rsi_series(up)[-1] == 100.0
    down = [100 - i for i in range(40)]
    assert C.rsi_series(down)[-1] == pytest.approx(0.0)
    zig = [100 + (1 if i % 2 else -1) for i in range(60)]
    assert 40 < C.rsi_series(zig)[-1] < 60


def test_macd_cross_and_histogram():
    closes = [100.0] * 40 + [100 + i * 0.5 for i in range(30)]
    line, sig, hist = C.macd_series(closes)
    assert line[-1] > sig[-1] > 0 and hist[-1] > 0


def test_adx_trending_vs_ranging():
    a, p, m = C.adx_series(ramp(120, pct=0.8))
    assert a[-1] > 25 and p[-1] > m[-1]
    r = make_candles(200, H, NOW - H, 100, 0.0, 0.002)
    assert C.adx_series(r)[0][-1] < 30


def test_atr_bollinger_vwap_profile():
    cs = flat(60)
    assert C.atr_series(cs)[-1] == pytest.approx(0.2, rel=0.05)
    b = C.bollinger([c.close for c in cs])
    assert b["pct_b"] == 0.5 and b["bandwidth"] == 0
    assert C.vwap(cs) == pytest.approx(100.0)
    prof = C.volume_profile(ramp(50))
    assert len(prof) == 40 and C.high_volume_nodes(prof)


def test_previous_period_hl_and_resample():
    daily = [Candle(NOW.replace(hour=0) - timedelta(days=i), 1, 10 + i, 1 - i * 0.01, 5, 1) for i in range(15, 0, -1)]
    hl = C.previous_period_hl(daily, NOW)
    assert hl["prev_day_high"] == 11 and "prev_week_high" in hl
    r = C.resample(flat(8), 4 * 3600)
    assert all(x.volume >= 10 for x in r)


# ---------------- indicators ----------------
def test_ema_trend_strong_and_crossover():
    up = T.ema_trend(ramp(260, pct=0.3), "1h", 3600)
    assert up.score >= 80 and "strong bullish" in up.explanation and up.signal == "BULLISH"
    down = T.ema_trend(ramp(260, pct=-0.3), "1h", 3600)
    assert down.score <= -80
    assert T.ema_trend(ramp(50), "1h", 3600) is None


def test_trend_bias_halved_when_ranging():
    r = make_candles(300, H, NOW - H, 100, 0.0002, 0.001, seed=7)
    tb, et = T.trend_bias(r, "1h", 3600), T.ema_trend(r, "1h", 3600)
    a = T.adx(r, "1h", 3600)
    if a.raw["adx"] < 20:
        assert tb.score == pytest.approx(et.score * 0.5)


@pytest.mark.parametrize("regime,expected", [("TRENDING_BULL", 1), ("RANGE", -1), ("", 1)])
def test_rsi_overbought_depends_on_regime(regime, expected):
    r = T.rsi(ramp(60, pct=0.5), "1h", 3600, regime)
    assert r.flags["overbought"]
    assert (r.score > 0) == (expected > 0)


def test_rsi_oversold_in_range_is_bounce():
    r = T.rsi(ramp(60, pct=-0.5), "1h", 3600, "RANGE")
    assert r.flags["oversold"] and r.score > 0


def test_macd_indicator():
    m = T.macd([*flat(40), *ramp(30, pct=0.4)], "1h", 3600)
    assert m.score > 0 and "above signal" in m.explanation


@pytest.mark.parametrize("pct,vol_mult,sign,flag", [(1, 3.0, 1, None), (-1, 3.0, -1, None), (1, 0.3, 1, "weak_rally"),
                                                    (-1, 0.3, -1, "weak_selling")])
def test_volume_matrix(pct, vol_mult, sign, flag):
    cs = flat(30)
    last = cs[-2]
    cs[-2] = Candle(last.ts, 100, 102, 98, 100 * (1 + pct / 100), 10 * vol_mult, 5)
    v = T.volume(cs, "1h", 3600)
    assert (v.score > 0) == (sign > 0)
    if flag:
        assert v.flags[flag] and abs(v.score) < 15
    else:
        assert abs(v.score) > 40


def test_atr_regime_and_bollinger_breakout():
    cs = flat(300)
    cs += ramp(10, start=100, pct=1.5, vol=40)
    a = T.atr(cs, "1h", 3600)
    assert a.flags["atr_regime"] in ("HIGH", "EXTREME") and a.score == 0
    b = T.bollinger_bands(cs, "1h", 3600, "", volume_ratio=2.0)
    assert b.raw["pct_b"] > 1 and b.score == 40
    rng = T.bollinger_bands(cs, "1h", 3600, "RANGE", volume_ratio=0.5)
    assert rng.score == -20  # mean-reversion context only in a range


def test_vwap_bias_with_cvd_confirmation():
    cs = ramp(30, pct=0.1, step=timedelta(minutes=5))
    a = T.vwap_bias(cs, "5m", 300, cvd_sign=1)
    b = T.vwap_bias(cs, "5m", 300, cvd_sign=-1)
    assert a.raw["distance_pct"] > 0 and a.score > b.score > 0


@pytest.mark.parametrize("pct,taker,expect", [(0.2, 0.62, "confirmed buying"), (0.2, 0.38, "bearish divergence"),
                                              (-0.2, 0.38, "confirmed selling"), (-0.2, 0.62, "absorption")])
def test_cvd_matrix(pct, taker, expect):
    cs = ramp(200, pct=0.0) + ramp(24, start=100, pct=pct, taker=taker)
    r = T.cvd_matrix(cs, "24h", 3600, 24, "spot")
    assert expect in r.explanation


def test_structure_choch_and_breakout():
    up = ramp(80, pct=0.3)
    zig = [Candle(c.ts, c.open, c.high * (1.01 if i % 6 == 3 else 1), c.low * (0.99 if i % 6 == 0 else 1), c.close, 1)
           for i, c in enumerate(up)]
    s = T.structure(zig, "1h", 3600)
    assert s.score > 0 and "HH + HL" in s.explanation
    crash = zig + [Candle(zig[-1].ts + H, zig[-1].close, zig[-1].close, zig[-1].close * 0.85, zig[-1].close * 0.86, 1)]
    c = T.structure(crash, "1h", 3600)
    assert c.raw["choch"] == -1 and c.score < 0


def test_divergence_detection():
    cs = []
    p = 100.0
    for i in range(140):  # two rallies; the second makes a higher high on weaker buying
        p *= 1.004 if (i // 20) % 2 == 0 else 0.997
        taker = 0.8 if i < 70 else 0.3
        cs.append(Candle(NOW - H * (140 - i), p, p * 1.001, p * 0.999, p, 10, 10 * taker))
    d = T.divergences(cs, "1h", 3600)
    assert d.score == 0 and "CVD" in d.raw["bearish"] and not d.raw["bullish"]  # evidence, not a trade


def test_support_resistance_merges_sources():
    ctx = build_ctx(1)
    p = ctx.candles["spot_1h"][-1].close
    liq = [{"price": p * 0.99, "usd": 5e6}]
    sr = T.support_resistance(p, NOW, ctx.candles["spot_1h"], ctx.candles["spot_1d"], [], liq)
    assert sr["nearest_support"]["price"] < p < sr["nearest_resistance"]["price"]
    assert sr["major_support"]["strength"] >= sr["nearest_support"]["strength"]
    assert any("liquidation cluster" in m["sources"] for m in sr["levels"])


# ---------------- regime & multi-timeframe ----------------
def test_regime_trending_bull_and_annotation(bull_ctx):
    r = market.regime(bull_ctx)
    assert r.inputs["primary"] in ("TRENDING_BULL", "TRANSITION") and bull_ctx.annotations["regime"]["primary"] == r.inputs["primary"]
    assert not r.directional


def test_regime_liquidation_event(bull_ctx):
    bull_ctx.events["liquidations"].append({"ts": NOW - timedelta(minutes=5), "side": "long", "usd": 80e6, "venue": "binance"})
    assert market.regime(bull_ctx).inputs["primary"] == "LIQUIDATION_EVENT"


def test_regime_unknown_without_history():
    from app.core.context import MarketContext
    ctx = MarketContext(now=NOW)
    assert not market.regime(ctx).available and ctx.annotations["regime"]["primary"] == "UNKNOWN"


def test_mtf_short_term_pullback_does_not_flip_bearish(bull_ctx):
    # a bearish 5m/15m tape inside a bullish higher-timeframe trend
    bull_ctx.set_candles("spot_5m", ramp(300, start=bull_ctx.candles["spot_1h"][-1].close * 1.03, pct=-0.01,
                                         step=timedelta(minutes=5)), "binance_spot")
    bull_ctx.set_candles("spot_15m", ramp(300, start=bull_ctx.candles["spot_1h"][-1].close * 1.05, pct=-0.015,
                                          step=timedelta(minutes=15)), "binance_spot")
    t = market.trend(bull_ctx)
    assert t.inputs["short_term"] < 0 and t.inputs["higher_timeframe"] > 40
    assert t.score >= 0 and t.flags["mtf_note"] == "Higher timeframe bullish, short-term pullback"


def test_momentum_and_volume_engines(bull_ctx, bear_ctx):
    market.regime(bull_ctx)
    market.regime(bear_ctx)
    assert market.momentum(bull_ctx).score > market.momentum(bear_ctx).score
    assert market.volume(bull_ctx).available


# ---------------- providers / stats ----------------
@pytest.mark.parametrize("tx,kind", [
    ({"from": {"owner_type": "exchange", "owner": "binance"}, "to": {"owner_type": "exchange", "owner": "binance"}}, "internal"),
    ({"from": {"owner_type": "unknown", "owner": "Coinbase Custody"}, "to": {"owner_type": "exchange", "owner": "coinbase"}}, "internal"),
    ({"from": {"owner_type": "exchange", "owner": "kraken"}, "to": {"owner_type": "unknown"}}, "from_exchange"),
])
def test_whale_entity_classification(tx, kind):
    assert classify_transfer(tx) == kind


def test_parse_coinmetrics_netflow_and_day_close():
    rows = [{"time": "2026-10-03T00:00:00.000000000Z", "CapMVRVCur": "1.5", "FlowInExNtv": "100", "FlowOutExNtv": "160",
             "SplyExNtv": "2600000", "AdrActCnt": "600000"}]
    out = parse_coinmetrics(rows, {"CapMVRVCur": "BTC_MVRV", "FlowInExNtv": "EXCHANGE_INFLOW_BTC",
                                   "FlowOutExNtv": "EXCHANGE_OUTFLOW_BTC", "SplyExNtv": "EXCHANGE_RESERVE_BTC"})
    assert out["EXCHANGE_NETFLOW_BTC"][0][1] == -60  # inflow − outflow
    assert out["BTC_MVRV"][0][0].day == 4  # known only after the day closes


def test_classification_stats():
    rows = [(50, 1.0, 1.5, -0.2), (60, -0.5, 0.2, -0.8), (-40, -1.0, 0.1, -1.2), (5, 2.0, 2.0, 0.0)]
    s = classification_stats(rows, 4)
    assert s["n_calls"] == 3 and s["accuracy"] == pytest.approx(2 / 3, abs=1e-3)
    assert s["precision_bullish"] == 0.5 and s["precision_bearish"] == 1.0
    assert s["recall_bullish"] == 0.5 and s["sharpe_annualized"] is not None
    assert classification_stats([(1, 1, 1, 1)], 4)["n_calls"] == 0


def test_series_noop():
    assert Series("x", []).last() is None and Datum(1, "p", NOW).value == 1
