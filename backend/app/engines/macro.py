"""Engines 15 and 17: macro, cross-market crypto regime."""
from __future__ import annotations

from app.core.context import MarketContext
from app.engines.base import EngineResult, combine, make_signal, squash, unavailable
from app.engines.indicators import interp
from app.normalize.consensus import consensus

WEEK = 7 * 86400


def macro(ctx: MarketContext, cfg: dict | None = None) -> EngineResult:
    sigs, reasons, inputs = [], [], {}

    def each(metric: str):
        return ctx.get_series(metric).items()

    for prov, s in each("MACRO_DXY"):
        c = s.pct_change(WEEK)
        if c is not None:
            sigs.append(make_signal(ctx, prov, "MACRO_DXY", s.last_ts, -100 * squash(c, 1.0), note=f"DXY {c:+.2f}% 1w"))
            reasons.append(f"Dollar index {c:+.2f}% this week ({'headwind' if c > 0 else 'tailwind'})")
            inputs["dxy_1w_pct"] = round(c, 3)
    for prov, s in each("MACRO_US10Y"):
        c = s.abs_change(WEEK)
        if c is not None:
            bp = c * 100
            sigs.append(make_signal(ctx, prov, "MACRO_US10Y", s.last_ts, -100 * squash(bp, 15), note=f"10Y {bp:+.0f}bp 1w"))
            reasons.append(f"US 10Y yield {bp:+.0f} bp this week")
            inputs["us10y_1w_bp"] = round(bp, 1)
    for prov, s in each("MACRO_SPX"):
        c = s.pct_change(WEEK)
        if c is not None:
            sigs.append(make_signal(ctx, prov, "MACRO_SPX", s.last_ts, 100 * squash(c, 2.0), note=f"S&P {c:+.2f}% 1w"))
            reasons.append(f"S&P 500 {c:+.2f}% this week")
            inputs["spx_1w_pct"] = round(c, 3)
    for prov, s in each("MACRO_VIX"):
        v = s.last()
        sigs.append(make_signal(ctx, prov, "MACRO_VIX", s.last_ts, interp(v, [12, 15, 20, 25, 35], [30, 15, 0, -30, -70]),
                                note=f"VIX {v:.1f}"))
        reasons.append(f"VIX {v:.1f} ({'stress' if v > 25 else 'calm' if v < 16 else 'normal'})")
        inputs["vix"] = round(v, 2)
    if not sigs:
        return unavailable("macro", "no macro data")
    res = combine("macro", sigs)
    res.reasons, res.inputs = reasons, {**res.inputs, **inputs}
    return res


def cross_market(ctx: MarketContext, cfg: dict | None = None) -> EngineResult:
    sigs, reasons, inputs = [], [], {}
    for prov, s in ctx.get_series("FEAR_GREED").items():
        v = s.last()
        raw = interp(v, [0, 10, 25, 45, 55, 75, 90, 100], [50, 50, 25, 0, 0, -25, -50, -50])
        sigs.append(make_signal(ctx, prov, "FEAR_GREED", s.last_ts, raw, note=f"Fear & Greed {v:.0f}"))
        reasons.append(f"Fear & Greed {v:.0f}" + (" — extreme greed (contrarian caution)" if v >= 75 else
                                                  " — extreme fear (contrarian support)" if v <= 25 else ""))
        inputs["fear_greed"] = v
    for d in ctx.get_scalars("CRYPTO_MCAP_CHANGE_24H"):
        sigs.append(make_signal(ctx, d.provider, "CRYPTO_MCAP_CHANGE_24H", d.ts, 60 * squash(d.value, 3.0),
                                note=f"total crypto mcap {d.value:+.2f}% 24h"))
        inputs["crypto_mcap_24h_pct"] = round(d.value, 3)
    for prov, s in ctx.get_series("DEFI_TVL_USD").items():
        c = s.pct_change(WEEK)
        if c is not None:
            sigs.append(make_signal(ctx, prov, "DEFI_TVL_USD", s.last_ts, 40 * squash(c, 5.0), confidence=0.5,
                                    note=f"DeFi TVL {c:+.2f}% 1w"))
            inputs["defi_tvl_1w_pct"] = round(c, 3)
    dom = ctx.get_scalars("BTC_DOMINANCE")
    if dom:
        inputs["btc_dominance_pct"] = round(dom[0].value, 2)
        reasons.append(f"BTC dominance {dom[0].value:.1f}% (context only)")
    wv = consensus(ctx, "WBTC_DEX_VOLUME_24H_USD")
    if wv.value is not None:
        inputs["wbtc_dex_volume"] = wv.as_dict()  # context only: never moves the score
    if not sigs:
        return unavailable("cross_market", "no cross-market data")
    res = combine("cross_market", sigs)
    res.reasons, res.inputs = reasons, {**res.inputs, **inputs}
    return res
