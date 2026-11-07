"""Price/flow engines built on the indicator library (spec §27 A–I, S–V).

Directional: trend (MTF EMA+ADX), structure (MTF), momentum (RSI+MACD), volume, vwap,
spot_flow (Coinbase premium), cvd, volatility.
Context (non-directional): regime, levels (support/resistance), divergence.
"""
from __future__ import annotations

from datetime import timedelta
from statistics import median

from app.core.context import Candle, MarketContext
from app.engines.base import EngineResult, combine, make_signal, squash, unavailable, weighted_indicators
from app.indicators import technical as T
from app.indicators.core import adx_series, ema_series, pivots, resample

H4 = 4 * 3600
TF_SECONDS = {"5m": 300, "15m": 900, "1h": 3600, "4h": H4, "1D": 86400}
RANGE_FAMILY = {"RANGE", "ACCUMULATION", "DISTRIBUTION"}
PRICE_CANDLE_CAP = 0.8


def timeframes(ctx: MarketContext) -> dict[str, list[Candle]]:
    c1h = ctx.get_candles("spot_1h")
    return {"5m": ctx.get_candles("spot_5m"), "15m": ctx.get_candles("spot_15m"), "1h": c1h,
            "4h": resample(c1h, H4) if c1h else [], "1D": ctx.get_candles("spot_1d")}


def regime_label(ctx: MarketContext) -> str:
    r = ctx.annotations.get("regime", {}).get("primary", "")
    return "RANGE" if r in RANGE_FAMILY else r


def _single(engine: str, ctx: MarketContext, provider_key: str, metric: str, items: list[tuple], cap: float = PRICE_CANDLE_CAP) -> EngineResult | None:
    score, conf = weighted_indicators(items)
    inds = [i for i, _ in items if i is not None]
    if not inds:
        return None
    candles = ctx.candles.get(provider_key, [])
    ts = max(i.ts for i in inds) - timedelta(hours=1) if candles else None
    res = combine(engine, [make_signal(ctx, ctx.candle_source.get(provider_key, "binance_spot"), metric,
                                       candles[-1].ts if candles else ts, score, confidence=max(conf, 0.3))],
                  single_source_cap=cap)
    res.indicators = inds
    return res


# ---------------- V. regime (context) ----------------
def regime(ctx: MarketContext, cfg: dict | None = None) -> EngineResult:
    cfg = cfg or {}
    tfs = timeframes(ctx)
    c1h, c4h, c1d = tfs["1h"], tfs["4h"], tfs["1D"]
    if len(c1h) < 210 or len(c4h) < 60:
        res = unavailable("regime", "insufficient history for regime", directional=False)
        ctx.annotations["regime"] = {"primary": "UNKNOWN", "tags": []}
        return res
    adx1 = adx_series(c1h)[0][-1] or 0.0
    adx4 = adx_series(c4h)[0][-1] or 0.0
    t4 = T.ema_trend(c4h, "4h", H4) if len(c4h) >= 210 else T.ema_trend(c1h, "1h", 3600)
    t1d = T.ema_trend(c1d, "1D", 86400) if len(c1d) >= 210 else None
    tdir = 1 if t4 and t4.score >= 40 else -1 if t4 and t4.score <= -40 else 0
    # recent liquidation cascade
    cut = ctx.now - timedelta(hours=1)
    liq_1h = sum(e["usd"] for e in ctx.events.get("liquidations", []) if e["ts"] >= cut and e.get("venue") != "coinglass_agg")
    cascade = liq_1h > cfg.get("liq_stream_cascade_1h_usd", 25e6)
    # accumulation / distribution evidence
    cvd3d = T.cvd_matrix(c1h, "1h", 3600, 72, "spot")
    flow_sign = 0
    nf = ctx.get_series("EXCHANGE_NETFLOW_BTC")
    if nf:
        s7 = sum(v for _, v in next(iter(nf.values())).points[-7:])
        flow_sign = -1 if s7 > 0 else 1  # outflows = accumulation
    acc_ev = (1 if cvd3d and cvd3d.raw["relative"] > 0.01 else -1 if cvd3d and cvd3d.raw["relative"] < -0.01 else 0) + flow_sign
    e20, e50 = ema_series([c.close for c in c4h], 20), ema_series([c.close for c in c4h], 50)
    recent_cross = any(e20[-i - 1] is not None and e50[-i - 1] is not None and
                       (e20[-i - 1] - e50[-i - 1]) * (e20[-i] - e50[-i]) < 0 for i in range(1, 4))
    htf_conflict = t1d is not None and t4 is not None and t1d.score * t4.score < 0 and abs(t1d.score) >= 40 and abs(t4.score) >= 40
    if cascade:
        primary = "LIQUIDATION_EVENT"
    elif max(adx1, adx4) > 25 and tdir > 0:
        primary = "TRENDING_BULL"
    elif max(adx1, adx4) > 25 and tdir < 0:
        primary = "TRENDING_BEAR"
    elif adx4 < 20:
        primary = "ACCUMULATION" if acc_ev >= 2 or (acc_ev >= 1 and not nf) else \
            "DISTRIBUTION" if acc_ev <= -2 or (acc_ev <= -1 and not nf) else "RANGE"
    else:
        primary = "TRANSITION"
    transition = primary == "TRANSITION" or recent_cross or htf_conflict
    atr = T.atr(c1h, "1h", 3600)
    tags = [primary]
    if atr and atr.raw["atr_pct_percentile"] > 0.8:
        tags.append("HIGH_VOLATILITY")
    elif atr and atr.raw["atr_pct_percentile"] < 0.2:
        tags.append("LOW_VOLATILITY")
    info = {"primary": primary, "tags": tags, "adx_1h": round(adx1, 2), "adx_4h": round(adx4, 2),
            "transition": transition, "recent_4h_ema_cross": recent_cross, "htf_conflict": htf_conflict,
            "liquidations_1h_usd": round(liq_1h)}
    ctx.annotations["regime"] = info
    res = EngineResult("regime", available=True, score=0.0, confidence=0.7, directional=False, confirmation="NONE")
    res.inputs, res.flags = info, {"regime": primary, "regime_transition": transition}
    reasons = [f"Regime {' / '.join(t.replace('_', ' ') for t in tags)} (ADX 1h {adx1:.0f}, 4h {adx4:.0f})"]
    if transition and primary != "TRANSITION":
        reasons.append("Regime in transition (" + ", ".join(
            x for x, b in (("fresh 4h EMA20/50 cross", recent_cross), ("1D vs 4h trend conflict", htf_conflict)) if b) + ")")
    res.reasons = reasons
    return res


# ---------------- A + T. trend (multi-timeframe EMA + ADX) ----------------
def trend(ctx: MarketContext, cfg: dict | None = None) -> EngineResult:
    tfs = timeframes(ctx)
    if len(tfs["1h"]) < 210:
        return unavailable("trend", "insufficient 1h candle history")
    biases = {tf: T.trend_bias(cs, tf, TF_SECONDS[tf]) for tf, cs in tfs.items() if len(cs) >= 210}
    adxs = [T.adx(cs, tf, TF_SECONDS[tf]) for tf, cs in tfs.items() if tf in ("1h", "4h", "1D") and len(cs) >= 40]

    def avg(keys):
        xs = [biases[k].score for k in keys if biases.get(k)]
        return sum(xs) / len(xs) if xs else None
    short, medium, htf = avg(["5m", "15m"]), avg(["1h", "4h"]), avg(["1D"])
    parts = [(0.45, htf), (0.40, medium), (0.15, short)]
    used = [(w, v) for w, v in parts if v is not None]
    final = sum(w * v for w, v in used) / sum(w for w, _ in used)
    reasons, note = [], None
    if htf is not None and short is not None and htf >= 40 and short < 0 and (medium or 0) >= 0:
        final, note = max(final, 0.0), "Higher timeframe bullish, short-term pullback"
    elif htf is not None and short is not None and htf <= -40 and short > 0 and (medium or 0) <= 0:
        final, note = min(final, 0.0), "Higher timeframe bearish, short-term bounce"
    sigs = [make_signal(ctx, ctx.candle_source["spot_1h"], "CANDLES_SPOT_1H", tfs["1h"][-1].ts, final)]
    cb = ctx.get_candles("coinbase_1h")
    if len(cb) >= 210:
        cbt = T.trend_bias(cb, "1h", 3600)
        if cbt:
            sigs.append(make_signal(ctx, "coinbase", "CANDLES_COINBASE_1H", cb[-1].ts, cbt.score, note="Coinbase 1h confirmation"))
    res = combine("trend", sigs, single_source_cap=PRICE_CANDLE_CAP)
    res.indicators = [b for b in biases.values() if b] + [a for a in adxs if a]
    bias = {"short_term": short, "medium_term": medium, "higher_timeframe": htf}
    lbl = lambda v: "n/a" if v is None else "bullish" if v >= 20 else "bearish" if v <= -20 else "neutral"  # noqa: E731
    reasons.append(f"Trend — HTF {lbl(htf)}, medium {lbl(medium)}, short-term {lbl(short)}")
    if note:
        reasons.append(note)
    d1 = biases.get("1D")
    if d1:
        reasons.append(d1.explanation)
    res.reasons = reasons
    res.inputs.update({k: None if v is None else round(v, 1) for k, v in bias.items()},
                      by_timeframe={k: round(v.score, 1) for k, v in biases.items() if v})
    res.flags["htf_bias"] = (htf if htf is not None else medium or 0.0) / 100
    res.flags["mtf_note"] = note
    return res


# ---------------- S. structure (multi-timeframe) ----------------
def structure(ctx: MarketContext, cfg: dict | None = None) -> EngineResult:
    tfs = timeframes(ctx)
    if len(tfs["1h"]) < 120:
        return unavailable("structure", "insufficient candle history")
    items = [(T.structure(tfs["1h"][-200:], "1h", 3600, 3), 0.25), (T.structure(tfs["4h"][-150:], "4h", H4), 0.4),
             (T.structure(tfs["1D"][-150:], "1D", 86400), 0.35)]
    res = _single("structure", ctx, "spot_1h", "CANDLES_SPOT_1H", items)
    if res is None:
        return unavailable("structure", "not enough swing points")
    res.reasons = [i.explanation for i in res.indicators]
    res.flags["choch"] = {i.timeframe: i.raw["choch"] for i in res.indicators if i.raw["choch"]}
    return res


# ---------------- B + C. momentum (RSI + MACD, regime-aware) ----------------
def momentum(ctx: MarketContext, cfg: dict | None = None) -> EngineResult:
    tfs = timeframes(ctx)
    rg = regime_label(ctx)
    items = []
    for tf, w in (("1h", 0.4), ("4h", 0.6)):
        cs = tfs[tf]
        if len(cs) >= 60:
            items += [(T.rsi(cs, tf, TF_SECONDS[tf], rg), w * 0.5), (T.macd(cs, tf, TF_SECONDS[tf]), w * 0.5)]
    res = _single("momentum", ctx, "spot_1h", "CANDLES_SPOT_1H", items)
    if res is None:
        return unavailable("momentum", "insufficient candles for RSI/MACD")
    res.reasons = [i.explanation for i in res.indicators]
    res.flags["overbought"] = any(i.flags.get("overbought") for i in res.indicators)
    res.flags["oversold"] = any(i.flags.get("oversold") for i in res.indicators)
    return res


# ---------------- D. volume ----------------
def volume(ctx: MarketContext, cfg: dict | None = None) -> EngineResult:
    tfs = timeframes(ctx)
    items = [(T.volume(tfs["1h"], "1h", 3600), 0.4), (T.volume(tfs["4h"], "4h", H4), 0.6)]
    res = _single("volume", ctx, "spot_1h", "CANDLES_SPOT_1H", items, cap=0.6)
    if res is None:
        return unavailable("volume", "insufficient candles")
    res.reasons = [i.explanation for i in res.indicators]
    return res


# ---------------- H. VWAP ----------------
def vwap(ctx: MarketContext, cfg: dict | None = None) -> EngineResult:
    tfs = timeframes(ctx)
    intraday = tfs["5m"] or tfs["1h"]
    tf = "5m" if tfs["5m"] else "1h"
    if not intraday:
        return unavailable("vwap", "no intraday candles")
    day = intraday[-1].ts.replace(hour=0, minute=0, second=0, microsecond=0)
    sess = [c for c in intraday if c.ts >= day]
    sgn = T.cvd_sign(sess, len(sess)) if sess else 0
    anchor = None
    c4 = tfs["4h"][-60:]
    if len(c4) > 20:
        ph, pl = pivots([c.high for c in c4], [c.low for c in c4], 3, 3)
        up = c4[-1].close > ema_series([c.close for c in c4], 20)[-1] if len(c4) >= 20 else True
        idx = (pl[-1] if pl else None) if up else (ph[-1] if ph else None)
        if idx is not None:
            anchor = [c for c in tfs["1h"] if c.ts >= c4[idx].ts]
    ind = T.vwap_bias(intraday, tf, TF_SECONDS[tf], sgn, anchor)
    res = _single("vwap", ctx, "spot_5m" if tf == "5m" else "spot_1h", "CANDLES_SPOT_5M" if tf == "5m" else "CANDLES_SPOT_1H",
                  [(ind, 1.0)], cap=0.6)
    if res is None:
        return unavailable("vwap", "session too short for VWAP")
    res.reasons = [ind.explanation]
    return res


# ---------------- spot flow: Coinbase premium ----------------
def spot_flow(ctx: MarketContext, cfg: dict | None = None) -> EngineResult:
    bn, cb = ctx.get_candles("spot_1h"), ctx.get_candles("coinbase_1h")
    if not (bn and cb):
        return unavailable("spot_flow", "need Coinbase and Binance spot candles")
    bmap = {c.ts: c.close for c in bn}
    prem = [(c.ts, (c.close / bmap[c.ts] - 1) * 1e4) for c in cb if c.ts in bmap]
    if len(prem) < 48:
        return unavailable("spot_flow", "not enough overlapping candles for the premium")
    recent = sum(p for _, p in prem[-4:]) / 4
    base = median([p for _, p in prem[-244:-4]])
    dev = recent - base
    sig = make_signal(ctx, "coinbase", "CANDLES_COINBASE_1H", prem[-1][0], 100 * squash(dev, 8),
                      note=f"premium {recent:+.1f}bps vs 10d base {base:+.1f}bps")
    res = combine("spot_flow", [sig], single_source_cap=0.6)
    from app.indicators.result import IndicatorResult
    res.indicators = [IndicatorResult("coinbase_premium", {"premium_bps": recent, "baseline_bps": base, "deviation_bps": dev},
                                      sig.raw, 0.6, f"Coinbase premium {dev:+.1f} bps vs 10-day norm",
                                      prem[-1][0] + timedelta(hours=1), "1h")]
    res.inputs.update(coinbase_premium_bps=round(recent, 2), premium_baseline_bps=round(base, 2))
    res.reasons = [f"Coinbase premium {'above' if dev > 0 else 'below'} its 10-day norm ({dev:+.1f} bps) — "
                   f"US spot {'demand' if dev > 0 else 'selling'}" if abs(dev) >= 3 else "Coinbase premium near its norm"]
    return res


# ---------------- I. CVD ----------------
def cvd(ctx: MarketContext, cfg: dict | None = None) -> EngineResult:
    spot, perp = ctx.get_candles("spot_1h"), ctx.get_candles("perp_1h")
    sigs, inds = [], []
    s24 = T.cvd_matrix(spot, "24h", 3600, 24, "spot") if len(spot) >= 168 else None
    s4 = T.cvd_matrix(spot, "4h", 3600, 4, "spot") if len(spot) >= 168 else None
    if s24 and s4:
        raw = 0.65 * s24.score + 0.35 * s4.score
        sigs.append(make_signal(ctx, ctx.candle_source["spot_1h"], "CANDLES_SPOT_1H", spot[-1].ts, raw, relevance=0.9,
                                note=s24.explanation))
        inds += [s24, s4]
    p24 = T.cvd_matrix(perp, "24h", 3600, 24, "futures", baseline_bars=200) if len(perp) >= 48 else None
    if p24:
        sigs.append(make_signal(ctx, ctx.candle_source["perp_1h"], "CANDLES_PERP_1H", perp[-1].ts, p24.score,
                                relevance=0.65, note=p24.explanation))
        inds.append(p24)
    if not sigs:
        return unavailable("cvd", "no taker-volume candles")
    res = combine("cvd", sigs, single_source_cap=0.6)
    res.indicators = inds
    res.reasons = [i.explanation for i in inds]
    if s24 and p24 and p24.raw["relative"] > 0.03 and s24.raw["relative"] < -0.01:
        res.reasons.append("Futures-led buying without spot support (fragile)")
    if s24:
        res.inputs.update(spot_cvd_24h=round(s24.raw["cvd_pct_of_volume"], 4), spot_cvd_relative=round(s24.raw["relative"], 4))
    res.flags["cvd_sign_24h"] = 1 if s24 and s24.raw["relative"] > 0.01 else -1 if s24 and s24.raw["relative"] < -0.01 else 0
    return res


# ---------------- E + F. volatility ----------------
def volatility(ctx: MarketContext, cfg: dict | None = None) -> EngineResult:
    cfg = cfg or {}
    c1h = ctx.get_candles("spot_1h")
    if len(c1h) < 200:
        return unavailable("volatility", "insufficient candle history")
    a = T.atr(c1h, "1h", 3600)
    vr = T.volume(c1h, "1h", 3600)
    bb = T.bollinger_bands(c1h, "1h", 3600, regime_label(ctx), vr.raw["volume_ratio"] if vr else 1.0)
    inds = [x for x in (a, bb) if x]
    p24 = (c1h[-1].close / c1h[-25].close - 1) * 100
    sigs = [make_signal(ctx, ctx.candle_source["spot_1h"], "CANDLES_SPOT_1H", c1h[-1].ts, bb.score if bb else 0.0)]
    dvol_last = None
    for prov, s in ctx.get_series("BTC_DVOL").items():
        dvol_last = s.last()
        chg = s.abs_change(86400) or 0.0
        raw = -min(50.0, 10 * chg) if chg > 3 and p24 < 0 else min(30.0, 8 * -chg) if chg < -2 and p24 > 0 else 0.0
        sigs.append(make_signal(ctx, prov, "BTC_DVOL", s.last_ts, raw, note=f"DVOL {dvol_last:.1f} ({chg:+.1f} 24h)"))
    regime_v = a.flags["atr_regime"] if a else "NORMAL"
    if (dvol_last or 0) > cfg.get("dvol_extreme", 85):
        regime_v = "EXTREME"
    elif (dvol_last or 0) > 65 and regime_v in ("NORMAL", "LOW"):
        regime_v = "HIGH"
    res = combine("volatility", sigs, single_source_cap=PRICE_CANDLE_CAP)
    res.indicators = inds
    res.flags.update(vol_regime=regime_v, atr_pct=a.raw["atr_pct"] if a else None,
                     bb_squeeze=bool(bb and bb.flags["squeeze"]))
    res.reasons = [f"Volatility regime {regime_v}"] + [i.explanation for i in inds]
    if dvol_last is not None:
        res.reasons.append(f"Implied vol (DVOL) {dvol_last:.0f}")
    res.inputs.update(atr_pct=a.raw["atr_pct"] if a else None, dvol=dvol_last)
    return res


# ---------------- G. support / resistance (context) ----------------
def levels(ctx: MarketContext, cfg: dict | None = None) -> EngineResult:
    c1h = ctx.get_candles("spot_1h")
    if len(c1h) < 100:
        return unavailable("levels", "insufficient history for levels", directional=False)
    price = c1h[-1].close
    sr = T.support_resistance(price, ctx.now, c1h, ctx.get_candles("spot_1d"), ctx.get_candles("spot_5m"),
                              [e for e in ctx.events.get("liquidations", []) if e.get("price")])
    res = EngineResult("levels", available=True, score=0.0, confidence=0.7, directional=False)
    res.levels = sr
    fmt = lambda m: f"{m['price']:,.0f} ({', '.join(sorted(set(m['sources'])))})" if m else "none found"  # noqa: E731
    res.reasons = [f"Nearest support {fmt(sr['nearest_support'])}", f"Nearest resistance {fmt(sr['nearest_resistance'])}",
                   f"Major support {fmt(sr['major_support'])}; major resistance {fmt(sr['major_resistance'])}"]
    return res


# ---------------- U. divergence (context: adjusts confidence) ----------------
def divergence(ctx: MarketContext, cfg: dict | None = None) -> EngineResult:
    c1h = ctx.get_candles("spot_1h")
    if len(c1h) < 150:
        return unavailable("divergence", "insufficient history", directional=False)
    oi = []
    for s in ctx.get_series("BTC_OPEN_INTEREST_BTC").values():
        oi = s.points
        break
    d1 = T.divergences(c1h, "1h", 3600, oi)
    d4 = T.divergences(resample(c1h, H4), "4h", H4)
    inds = [x for x in (d1, d4) if x]
    res = EngineResult("divergence", available=True, score=0.0, confidence=0.6, directional=False)
    res.indicators = inds
    bear = sorted({f"{i.timeframe} {n}" for i in inds for n in i.raw["bearish"]})
    bull = sorted({f"{i.timeframe} {n}" for i in inds for n in i.raw["bullish"]})
    res.flags.update(bearish_divergences=bear, bullish_divergences=bull)
    res.reasons = [i.explanation for i in inds]
    return res
