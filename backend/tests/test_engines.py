from datetime import timedelta

import pytest

from app.core.context import Datum, MarketContext, Series
from app.engines import chain, derivatives, flows, macro, market
from app.engines.base import SourceSignal, combine
from conftest import NOW, H, D, build_ctx, make_candles


# ---------------- base / combine ----------------
def test_combine_single_source_is_weak_and_capped():
    r = combine("x", [SourceSignal("a", 80, 1, 1, 1)], single_source_cap=0.4)
    assert r.available and r.confirmation == "WEAK" and r.confidence <= 0.4
    assert r.score == pytest.approx(80)


def test_combine_agreement_strong_vs_conflict():
    agree = combine("x", [SourceSignal(p, 60, 1, 1, 1) for p in "abc"])
    conflict = combine("x", [SourceSignal("a", 60, 1, 1, 1), SourceSignal("b", -60, 1, 1, 1), SourceSignal("c", 50, 1, 1, 1)])
    assert agree.confirmation == "STRONG"
    assert conflict.confidence < agree.confidence
    assert conflict.score < agree.score


def test_combine_weights_by_relevance_reliability_freshness():
    r = combine("x", [SourceSignal("good", 50, 1, 1, 1), SourceSignal("stale", -100, 1, 1, 0.0)])
    assert r.score == pytest.approx(50)  # zero-freshness source has zero contribution
    assert len(r.signals) == 1


def test_combine_no_data_unavailable():
    assert not combine("x", []).available


# ---------------- market ----------------
def test_trend_bull_and_bear(bull_ctx, bear_ctx):
    assert market.trend(bull_ctx).score > 50
    assert market.trend(bear_ctx).score < -50
    assert market.trend(bull_ctx).flags["htf_bias"] > 0


def test_trend_needs_history():
    ctx = MarketContext(now=NOW)
    ctx.set_candles("spot_1h", make_candles(50, H, NOW - H, 100, 0, 0.01), "binance_spot")
    assert not market.trend(ctx).available


def test_trend_stale_candles_unavailable(bull_ctx):
    bull_ctx.now = NOW + timedelta(hours=10)
    assert not market.trend(bull_ctx).available


def test_structure_multi_timeframe(bull_ctx, bear_ctx):
    b = market.structure(bull_ctx)
    assert b.score > 0 and {i.timeframe for i in b.indicators} >= {"1h", "4h"}
    assert market.structure(bear_ctx).score < 0


def test_levels_support_resistance(bull_ctx):
    market.regime(bull_ctx)
    lv = market.levels(bull_ctx)
    p = bull_ctx.candles["spot_1h"][-1].close
    assert not lv.directional and lv.levels["nearest_support"]["price"] < p < lv.levels["nearest_resistance"]["price"]
    srcs = {s for m in lv.levels["levels"] for s in m["sources"]}
    assert {"prev day high", "volume node"} & srcs


def test_spot_flow_premium(bull_ctx, bear_ctx):
    b = market.spot_flow(bull_ctx)
    assert b.score > 30 and b.inputs["coinbase_premium_bps"] > 10
    assert market.spot_flow(bear_ctx).score < -30


def test_cvd_positive_negative_and_divergence(bull_ctx, bear_ctx):
    assert market.cvd(bull_ctx).score > 50
    assert market.cvd(bear_ctx).score < -50
    # price up but spot sellers dominate → bearish divergence
    ctx = build_ctx(1)
    spot = make_candles(1000, H, NOW - H, 60000, 0.001, 0.002, taker_bias=[0.0] * 976 + [-0.06] * 24)
    ctx.set_candles("spot_1h", spot, "binance_spot")
    r = market.cvd(ctx)
    assert any("divergence" in x for x in r.reasons) and r.inputs["spot_cvd_24h"] < 0


def test_volatility_extreme_regime():
    ctx = build_ctx(1)
    ctx.add_series("BTC_DVOL", Series("deribit", [(NOW - H, 95.0)]))
    r = market.volatility(ctx, {"dvol_extreme": 85})
    assert r.flags["vol_regime"] == "EXTREME"


def test_volatility_dvol_spike_on_selloff_is_bearish():
    ctx = build_ctx(-1)
    ctx.add_series("BTC_DVOL", Series("deribit", [(NOW - 25 * H, 45.0), (NOW - H, 60.0)]))
    assert market.volatility(ctx).score < 0


# ---------------- derivatives ----------------
@pytest.mark.parametrize("oi,price,sign", [(5, 2, 1), (5, -2, -1), (-5, 2, 1), (-5, -2, -1), (5, 0, 0)])
def test_classify_oi_quadrants(oi, price, sign):
    v = derivatives.classify_oi(oi, price)
    assert (v > 0) - (v < 0) == sign


def test_oi_expansion_vs_contraction_strength():
    assert derivatives.classify_oi(5, 2) > derivatives.classify_oi(-5, 2) > 0  # new longs > short covering


def test_open_interest_engine(bull_ctx, bear_ctx):
    b = derivatives.open_interest(bull_ctx)
    assert b.score > 0 and b.inputs["providers"]
    assert derivatives.open_interest(bear_ctx).score < 0  # OI up, price down = new shorts


def test_oi_fast_expansion_flag():
    ctx = build_ctx(1)
    from conftest import series
    ctx.add_series("BTC_OPEN_INTEREST_BTC", series("binance_futures", 90000, 0.6, 48, H, NOW - H))
    ctx.add_series("BTC_OPEN_INTEREST_BTC", series("bybit", 55000, 0.6, 48, H, NOW - H))
    assert derivatives.open_interest(ctx, {"oi_fast_pct": 8}).flags["oi_expansion_fast"]


def test_oi_usd_series_price_adjusted():
    ctx = build_ctx(1)
    ctx.series.pop("BTC_OPEN_INTEREST_BTC")
    c = ctx.candles["spot_1h"]
    p24 = c[-1].close / c[-25].close
    # USD OI rising exactly with price = no change in coin terms
    pts = [(NOW - H * (48 - i), 8e9 * (p24 ** (i / 24))) for i in range(48)]
    ctx.add_series("BTC_OPEN_INTEREST_USD_HIST", Series("coinglass", pts))
    r = derivatives.open_interest(ctx)
    assert abs(r.inputs["providers"]["coinglass"]["oi_chg_24h_pct"]) < 1.5


def test_extreme_funding_flags_crowding_without_reversing():
    r = derivatives.funding(build_ctx(1, funding=0.0008))
    assert r.flags["crowded_longs"] and r.flags["extreme_funding_long"]
    assert -15 < r.score < 20  # extreme funding shrinks toward neutral, it does not flip to bearish
    moderate = derivatives.funding(build_ctx(1, funding=0.0003))
    assert moderate.score > r.score and moderate.score > 15  # normal bullish positioning


def test_funding_moderately_negative_is_bearish_positioning():
    r = derivatives.funding(build_ctx(-1, funding=-0.0002))
    assert r.score < -15 and not r.flags["extreme_funding_short"]
    x = derivatives.funding(build_ctx(-1, funding=-0.0008))
    assert x.flags["crowded_shorts"] and x.score > r.score


def test_funding_percentile_and_trend():
    ctx = build_ctx(1)
    ctx.add_series("BTC_FUNDING_RATE_HIST", Series("binance_futures", [(NOW - H * 8 * i, 0.00001 * (30 - i)) for i in range(30, 0, -1)]))
    r = derivatives.funding(ctx)
    assert r.flags["funding_percentile"] is not None and r.indicators[0].raw["trend"] is not None


@pytest.mark.parametrize("ratio,sign", [(2.6, -1), (0.5, 1), (1.2, 0)])
def test_long_short_only_extremes_matter(ratio, sign):
    r = derivatives.long_short(build_ctx(0, ls_ratio=ratio))
    s = (r.score > 10) - (r.score < -10)
    assert s == sign


def test_funding_healthy_small_positive(bull_ctx):
    r = derivatives.funding(bull_ctx)
    assert 0 <= r.score < 20 and not r.flags["crowded_longs"]
    assert r.confirmation in ("STRONG", "MODERATE")


def test_liquidation_short_squeeze_and_cascade():
    ctx = build_ctx(1)
    r = derivatives.liquidation(ctx)
    assert r.score > 0 and not r.flags["liquidation_cascade"]
    ctx.events["liquidations"].append({"ts": NOW - timedelta(minutes=20), "side": "long", "usd": 40e6, "venue": "binance"})
    r2 = derivatives.liquidation(ctx)
    assert r2.flags["liquidation_cascade"] and r2.flags["cascade_side"] == "long"


def test_liquidation_stream_warming_up():
    ctx = build_ctx(1)
    ctx.scalars["LIQ_STREAM_UPTIME_S"] = [Datum(60, "liquidation_stream", NOW)]
    r = derivatives.liquidation(ctx)
    assert not r.available and "warming" in r.reasons[0]


def test_liquidation_no_feed():
    assert not derivatives.liquidation(build_ctx(1, with_intel=False)).available


# ---------------- flows ----------------
def test_whale_accumulation_and_selling(bull_ctx, bear_ctx):
    acc = flows.whale(bull_ctx)
    assert acc.score > 50 and acc.confirmation == "WEAK" and acc.confidence <= 0.4
    assert flows.whale(bear_ctx).score < -50


def test_whale_wallet_to_wallet_is_neutral():
    ctx = build_ctx(0)
    ctx.events["whale_transfers"] = [{"ts": NOW - H, "usd": 5e9, "kind": "wallet_to_wallet"},
                                     {"ts": NOW - H, "usd": 5e9, "kind": "exchange_to_exchange"}]
    assert flows.whale(ctx).score == pytest.approx(0)


def test_whale_unavailable_without_feed():
    assert not flows.whale(build_ctx(1, with_intel=False)).available


def test_etf_inflow_outflow(bull_ctx, bear_ctx):
    assert flows.etf(bull_ctx).score > 50
    assert flows.etf(bear_ctx).score < -50
    assert not flows.etf(build_ctx(1, with_intel=False)).available


def test_exchange_flow_outflow_bullish(bull_ctx, bear_ctx):
    assert chain.exchange_flow(bull_ctx).score > 0
    assert chain.exchange_flow(bear_ctx).score < 0


def test_exchange_netflow_hourly_windows():
    ctx = build_ctx(0, with_intel=False)
    ctx.add_series("EXCHANGE_NETFLOW_BTC_1H", Series("cryptoquant", [(NOW - H * i, 200.0 if i < 4 else 10.0 * (-1) ** i)
                                                                     for i in range(48, 0, -1)]))
    r = chain.exchange_flow(ctx)
    assert r.score < -30 and set(r.indicators[0].raw) == {"1h", "4h", "24h"}


def test_onchain_percentiles():
    ctx = build_ctx(0, with_intel=False)
    ctx.add_series("BTC_MVRV", Series("coinmetrics", [(NOW - D * i, 1.0 + i / 400) for i in range(800, 0, -1)]))
    r = chain.onchain(ctx)
    mvrv = next(i for i in r.indicators if i.name == "btc_mvrv")
    assert mvrv.raw["percentile"] < 0.05 and mvrv.score > 0  # lowest MVRV in history → undervalued


def test_smart_money(bull_ctx):
    r = flows.smart_money(bull_ctx)
    assert r.score > 0 and r.confidence <= 0.5


def test_smart_money_thin_cohort_low_weight():
    ctx = build_ctx(1)
    ctx.scalars["SMART_MONEY_NET_BTC"] = [Datum(500, "hyperliquid_smart_money", NOW, {"longs": 1, "shorts": 0})]
    assert flows.smart_money(ctx).signals[0].confidence == pytest.approx(1 / 8)


# ---------------- chain / macro ----------------
def test_stablecoin_growth_and_contraction(bull_ctx, bear_ctx):
    assert chain.stablecoin(bull_ctx).score > 30
    assert chain.stablecoin(bear_ctx).score < -30


def test_network_and_onchain_low_magnitude(bull_ctx):
    n, o = chain.network(bull_ctx), chain.onchain(bull_ctx)
    assert 0 < n.score <= 30 and abs(o.score) <= 40
    assert n.inputs["fee_fast_satvb"] == 4.0


def test_network_context_only_without_hashrate():
    ctx = MarketContext(now=NOW)
    ctx.add_scalar("BTC_FEE_FAST_SATVB", Datum(5, "esplora", NOW))
    r = chain.network(ctx)
    assert r.available and r.score == 0


def test_macro(bull_ctx, bear_ctx):
    assert macro.macro(bull_ctx).score > 20
    assert macro.macro(bear_ctx).score < -20


def test_cross_market_fear_greed_extremes():
    ctx = build_ctx(0)
    ctx.series["FEAR_GREED"] = {"alternative_me": Series("alternative_me", [(NOW - H, 92.0)])}
    assert macro.cross_market(ctx).score < 0
    ctx.series["FEAR_GREED"] = {"alternative_me": Series("alternative_me", [(NOW - H, 8.0)])}
    assert macro.cross_market(ctx).score > 0


def test_daily_series_stale_after_max_age(bull_ctx):
    bull_ctx.now = NOW + 6 * D
    assert not chain.stablecoin(bull_ctx).available
