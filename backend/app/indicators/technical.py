"""Technical indicators (spec §27 A–I, S–V). Each returns an IndicatorResult.

Interpretation is regime-aware where the spec requires it (RSI, Bollinger): the caller
passes the current regime label.
"""
from __future__ import annotations

from datetime import datetime, timedelta

from app.core.context import Candle
from app.indicators.core import (
    adx_series, atr_series, bandwidth_history, bollinger, ema_series, high_volume_nodes, macd_series,
    percentile_rank, pivots, previous_period_hl, resample, rsi_series, session_vwap, sma_series, vwap, volume_profile,
)
from app.indicators.result import IndicatorResult

TRENDING_BULL, TRENDING_BEAR, RANGE = "TRENDING_BULL", "TRENDING_BEAR", "RANGE"


def _sign(x: float) -> int:
    return 1 if x > 0 else -1 if x < 0 else 0


def _clamp(x: float, lo: float = -100, hi: float = 100) -> float:
    return max(lo, min(hi, x))


def _ts(c: list[Candle], tf_seconds: int) -> datetime:
    return c[-1].ts + timedelta(seconds=tf_seconds)


# ---------------- A. price trend: EMA 20/50/100/200 + ADX ----------------
def ema_trend(candles: list[Candle], tf: str, tf_s: int) -> IndicatorResult | None:
    closes = [c.close for c in candles]
    if len(closes) < 210:
        return None
    e20, e50, e100, e200 = (ema_series(closes, p) for p in (20, 50, 100, 200))
    p = closes[-1]
    a, b, c_, d = e20[-1], e50[-1], e100[-1], e200[-1]
    raw = {"price": p, "ema20": a, "ema50": b, "ema100": c_, "ema200": d}
    if p > a > b > d:
        base, label = 80.0, "strong bullish trend (price > EMA20 > EMA50 > EMA200)"
    elif p < a < b < d:
        base, label = -80.0, "strong bearish trend (price < EMA20 < EMA50 < EMA200)"
    else:
        base = 60 * (_sign(p - a) + _sign(a - b) + _sign(b - c_) + _sign(c_ - d)) / 4
        label = "mixed EMA stack"
    cross = 0
    for i in range(1, 4):  # crossover within the last 3 closed bars
        if e20[-i - 1] is not None and e50[-i - 1] is not None:
            if e20[-i - 1] <= e50[-i - 1] and e20[-i] > e50[-i]:
                cross = 1
            elif e20[-i - 1] >= e50[-i - 1] and e20[-i] < e50[-i]:
                cross = -1
    raw["ema20_50_cross"] = cross
    score = base + 20 * cross
    expl = f"{tf}: {label}"
    if cross:
        expl += f"; EMA20 crossed {'above' if cross > 0 else 'below'} EMA50 (momentum, not a trade by itself)"
    return IndicatorResult("ema_trend", raw, _clamp(score), 0.8, expl, _ts(candles, tf_s), tf)


def adx(candles: list[Candle], tf: str, tf_s: int) -> IndicatorResult | None:
    a, pdi, mdi = adx_series(candles, 14)
    if a[-1] is None:
        return None
    v, p, m = a[-1], pdi[-1], mdi[-1]
    meaningful = v > 25
    ranging = v < 20
    score = (_sign(p - m) * min(60.0, (v - 20) * 3)) if v > 20 else 0.0
    state = "trend meaningful" if meaningful else "mostly ranging" if ranging else "trend developing"
    return IndicatorResult("adx", {"adx": v, "plus_di": p, "minus_di": m}, score, 0.7,
                           f"{tf}: ADX {v:.0f} — {state}, {'+DI' if p > m else '−DI'} leads", _ts(candles, tf_s), tf,
                           {"adx_trending": meaningful, "adx_ranging": ranging})


def trend_bias(candles: list[Candle], tf: str, tf_s: int) -> IndicatorResult | None:
    """EMA stack scaled by ADX: a ranging market halves the trend reading."""
    e = ema_trend(candles, tf, tf_s)
    if e is None:
        return None
    a = adx(candles, tf, tf_s)
    factor = 1.0 if a is None else 0.5 if a.raw["adx"] < 20 else 0.75 if a.raw["adx"] <= 25 else 1.0
    raw = dict(e.raw) | ({"adx": a.raw["adx"]} if a else {})
    expl = e.explanation + (f"; ADX {a.raw['adx']:.0f}" if a else "")
    return IndicatorResult("trend_bias", raw, e.score * factor, 0.6 + 0.3 * factor, expl, e.ts, tf)


# ---------------- B. RSI (regime-aware) ----------------
def rsi(candles: list[Candle], tf: str, tf_s: int, regime: str = "") -> IndicatorResult | None:
    r = rsi_series([c.close for c in candles], 14)
    if r[-1] is None:
        return None
    v = r[-1]
    flags = {"overbought": v > 70, "oversold": v < 30}
    if v > 70:
        if regime == TRENDING_BULL:
            score, txt = 40.0, "overbought, but a strong bull trend can stay overbought"
        elif regime == RANGE:
            score, txt = -40.0, "overbought inside a range — reversal warning"
        else:
            score, txt = 10.0, "overbought (momentum strong, stretched)"
    elif v < 30:
        if regime == TRENDING_BEAR:
            score, txt = -40.0, "oversold, but a strong bear trend can stay oversold"
        elif regime == RANGE:
            score, txt = 40.0, "oversold inside a range — bounce potential"
        else:
            score, txt = -10.0, "oversold (momentum weak, stretched)"
    elif v >= 50:
        score, txt = (v - 50) / 20 * 50, "bullish momentum zone (50–70)"
    else:
        score, txt = -(50 - v) / 20 * 50, "bearish momentum zone (30–50)"
    return IndicatorResult("rsi", {"rsi": v}, score, 0.6, f"{tf}: RSI {v:.0f} — {txt}", _ts(candles, tf_s), tf, flags)


# ---------------- C. MACD(12,26,9) ----------------
def macd(candles: list[Candle], tf: str, tf_s: int) -> IndicatorResult | None:
    line, sig, hist = macd_series([c.close for c in candles])
    if hist[-1] is None or hist[-2] is None or hist[-3] is None:
        return None
    l, s, h, h1, h2 = line[-1], sig[-1], hist[-1], hist[-2], hist[-3]
    rising, accel = h > h1, (h - h1) - (h1 - h2)
    cross = 1 if h1 <= 0 < h else -1 if h1 >= 0 > h else 0
    if l > s and rising:
        score, txt = 50.0, "MACD above signal, histogram rising"
    elif l < s and not rising:
        score, txt = -50.0, "MACD below signal, histogram falling"
    elif l > s:
        score, txt = 15.0, "MACD above signal but histogram fading"
    else:
        score, txt = -15.0, "MACD below signal but histogram recovering"
    score += 20 * cross + (10 if l > 0 else -10)
    if cross:
        txt += f"; {'bullish' if cross > 0 else 'bearish'} crossover"
    return IndicatorResult("macd", {"macd": l, "signal": s, "hist": h, "hist_accel": accel, "crossover": cross},
                           _clamp(score), 0.55, f"{tf}: {txt}", _ts(candles, tf_s), tf)


# ---------------- D. volume ratio matrix ----------------
def volume(candles: list[Candle], tf: str, tf_s: int) -> IndicatorResult | None:
    if len(candles) < 22:
        return None
    vols = [c.volume for c in candles[:-1]]
    sma = sma_series(vols, 20)[-1]
    cur = candles[-2]  # last closed bar (the final element may still be forming)
    ratio = cur.volume / sma if sma else 1.0
    chg = (cur.close / cur.open - 1) * 100
    up, hi = chg > 0, ratio > 1.2
    lo = ratio < 0.8
    if up and hi:
        score, txt = 25 + 35 * min(1.0, ratio - 1), "price up on rising volume — confirmed"
    elif not up and hi:
        score, txt = -25 - 35 * min(1.0, ratio - 1), "price down on rising volume — confirmed selling"
    elif up and lo:
        score, txt = 8.0, "price up on falling volume — weak rally"
    elif not up and lo:
        score, txt = -8.0, "price down on falling volume — weak selling"
    else:
        score, txt = 15.0 * _sign(chg), "volume near average"
    return IndicatorResult("volume", {"volume_ratio": ratio, "volume_sma20": sma, "bar_change_pct": chg}, score, 0.5,
                           f"{tf}: volume {ratio:.2f}× SMA20, bar {chg:+.2f}% — {txt}", cur.ts + timedelta(seconds=tf_s), tf,
                           {"weak_rally": up and lo, "weak_selling": (not up) and lo})


# ---------------- E. ATR / volatility ----------------
def atr(candles: list[Candle], tf: str, tf_s: int, lookback: int = 720) -> IndicatorResult | None:
    a = atr_series(candles, 14)
    if a[-1] is None:
        return None
    price = candles[-1].close
    pct = a[-1] / price * 100
    hist = [x / c.close * 100 for x, c in zip(a[-lookback:], candles[-lookback:]) if x is not None]
    pr = percentile_rank(hist, pct)
    regime = "EXTREME" if pr > 0.97 else "HIGH" if pr > 0.8 else "LOW" if pr < 0.2 else "NORMAL"
    return IndicatorResult("atr", {"atr": a[-1], "atr_pct": pct, "atr_pct_percentile": pr}, 0.0, 0.8,
                           f"{tf}: ATR {a[-1]:,.0f} ({pct:.2f}% of price, {pr:.0%} percentile) — {regime.lower()} volatility",
                           _ts(candles, tf_s), tf, {"atr_regime": regime})


# ---------------- F. Bollinger ----------------
def bollinger_bands(candles: list[Candle], tf: str, tf_s: int, regime: str = "", volume_ratio: float = 1.0) -> IndicatorResult | None:
    closes = [c.close for c in candles]
    b = bollinger(closes)
    if not b:
        return None
    hist = bandwidth_history(closes[-500:], step=4)
    bw_pr = percentile_rank(hist, b["bandwidth"])
    prev = bollinger(closes[:-5]) if len(closes) > 25 else None
    expanding = prev is not None and b["bandwidth"] > prev["bandwidth"] * 1.1
    score, txt = 0.0, "inside the bands"
    if b["pct_b"] > 1 and expanding and volume_ratio > 1.2:
        score, txt = 40.0, "upside breakout with band expansion and volume"
    elif b["pct_b"] < 0 and expanding and volume_ratio > 1.2:
        score, txt = -40.0, "downside breakdown with band expansion and volume"
    elif regime == RANGE and b["pct_b"] > 1:
        score, txt = -20.0, "above upper band in a range — mean-reversion risk"
    elif regime == RANGE and b["pct_b"] < 0:
        score, txt = 20.0, "below lower band in a range — mean-reversion support"
    elif b["pct_b"] > 1 or b["pct_b"] < 0:
        txt = "riding the band (not a reversal signal by itself)"
    squeeze = bw_pr < 0.2
    if squeeze:
        txt += "; volatility squeeze"
    return IndicatorResult("bollinger", b | {"bandwidth_percentile": bw_pr}, score, 0.5,
                           f"{tf}: %B {b['pct_b']:.2f}, width {b['bandwidth']:.2%} — {txt}", _ts(candles, tf_s), tf,
                           {"squeeze": squeeze, "expansion": expanding})


# ---------------- H. VWAP ----------------
def vwap_bias(intraday: list[Candle], tf: str, tf_s: int, cvd_sign: int = 0, anchor_from: list[Candle] | None = None) -> IndicatorResult | None:
    v, n = session_vwap(intraday)
    if v is None or n < 3:
        return None
    p = intraday[-1].close
    dist = (p / v - 1) * 100
    score = 40 * _sign(dist) * min(1.0, abs(dist) / 0.5)
    confirmed = cvd_sign != 0 and cvd_sign == _sign(dist)
    score = score * (1.25 if confirmed else 0.6 if cvd_sign and cvd_sign != _sign(dist) else 1.0)
    raw = {"session_vwap": v, "distance_pct": dist}
    txt = f"price {dist:+.2f}% vs session VWAP — {'bullish' if dist > 0 else 'bearish'} intraday bias"
    if cvd_sign:
        txt += ", confirmed by CVD" if confirmed else ", CVD disagrees"
    if anchor_from:
        av = vwap(anchor_from)
        if av:
            raw["anchored_vwap"] = av
            above = p > av
            score += 15 if above else -15
            txt += f"; {'above' if above else 'below'} VWAP anchored at the last major swing"
    return IndicatorResult("vwap", raw, _clamp(score), 0.5, f"{tf}: {txt}", _ts(intraday, tf_s), tf)


# ---------------- I. CVD matrix ----------------
def cvd_matrix(candles: list[Candle], tf: str, tf_s: int, bars: int, label: str, baseline_bars: int = 720) -> IndicatorResult | None:
    w = candles[-bars:]
    base_w = [c for c in candles[-baseline_bars:] if c.taker_buy_volume is not None]
    if len(w) < bars or any(c.taker_buy_volume is None for c in w) or not base_w:
        return None
    vol = sum(c.volume for c in w)
    norm = sum(2 * c.taker_buy_volume - c.volume for c in w) / vol if vol else 0.0
    bvol = sum(c.volume for c in base_w)
    base = sum(2 * c.taker_buy_volume - c.volume for c in base_w) / bvol if bvol else 0.0
    d = norm - base  # relative to the venue's own structural skew
    pchg = (w[-1].close / w[0].open - 1) * 100
    pu, cu = pchg > 0.3, d > 0.01
    pd_, cd = pchg < -0.3, d < -0.01
    strength = min(1.0, abs(d) / 0.04)
    if pu and cu:
        score, txt = 60 * strength + 10, "price up + CVD up → confirmed buying"
    elif pu and cd:
        score, txt = -25 * strength, "price up + CVD down → bearish divergence / weak rally"
    elif pd_ and cd:
        score, txt = -60 * strength - 10, "price down + CVD down → confirmed selling"
    elif pd_ and cu:
        score, txt = 25 * strength, "price down + CVD up → possible absorption"
    else:
        score, txt = 40 * _sign(d) * strength, "flat price; CVD " + ("rising" if d > 0 else "falling")
    return IndicatorResult(f"{label}_cvd", {"cvd_pct_of_volume": norm, "baseline": base, "relative": d, "price_change_pct": pchg},
                           _clamp(score), 0.65, f"{label} {tf}: {txt} ({d:+.1%} vs norm)", _ts(candles, tf_s), tf,
                           {"cvd_divergence": (pu and cd) or (pd_ and cu)})


def cvd_sign(candles: list[Candle], bars: int) -> int:
    w = candles[-bars:]
    if not w or any(c.taker_buy_volume is None for c in w):
        return 0
    v = sum(c.volume for c in w)
    n = sum(2 * c.taker_buy_volume - c.volume for c in w) / v if v else 0
    return 1 if n > 0.02 else -1 if n < -0.02 else 0


# ---------------- S. market structure ----------------
def structure(candles: list[Candle], tf: str, tf_s: int, left: int = 2) -> IndicatorResult | None:
    if len(candles) < 30:
        return None
    hi, lo = [c.high for c in candles], [c.low for c in candles]
    ph, pl = pivots(hi, lo, left, left)
    if len(ph) < 2 or len(pl) < 2:
        return None
    h1, h2, l1, l2 = hi[ph[-2]], hi[ph[-1]], lo[pl[-2]], lo[pl[-1]]
    close = candles[-1].close
    hh, hl, lh, ll = h2 > h1, l2 > l1, h2 < h1, l2 < l1
    bull, bear = hh and hl, lh and ll
    bos = 1 if close > h2 else -1 if close < l2 else 0
    choch = -1 if bull and close < l2 else 1 if bear and close > h2 else 0
    rng = candles[-21:-1]
    r_hi, r_lo = max(c.high for c in rng), min(c.low for c in rng)
    breakout = 1 if close > r_hi else -1 if close < r_lo else 0
    if choch:
        score = 50.0 * choch
        txt = f"change of character {'bullish' if choch > 0 else 'bearish'}"
    elif bull:
        score, txt = 50 + 25 * max(bos, 0), "HH + HL (bullish structure)"
    elif bear:
        score, txt = -50 + 25 * min(bos, 0), "LH + LL (bearish structure)"
    else:
        score, txt = 20.0 * bos, "mixed swings"
    if breakout:
        score += 15 * breakout
        txt += f"; range {'breakout' if breakout > 0 else 'breakdown'}"
    raw = {"last_high": h2, "prev_high": h1, "last_low": l2, "prev_low": l1, "bos": bos, "choch": choch,
           "range_breakout": breakout, "range_high": r_hi, "range_low": r_lo}
    return IndicatorResult("structure", raw, _clamp(score), 0.65, f"{tf}: {txt}", _ts(candles, tf_s), tf,
                           {"choch": choch, "bos": bos})


# ---------------- U. divergence ----------------
def divergences(candles: list[Candle], tf: str, tf_s: int, oi_points: list[tuple[datetime, float]] | None = None,
                window: int = 120) -> IndicatorResult | None:
    c = candles[-window:]
    if len(c) < 40:
        return None
    closes = [x.close for x in candles]
    off = len(candles) - len(c)
    rsi_v = rsi_series(closes)[off:]
    hist = macd_series(closes)[2][off:]
    cvd_cum, acc = [], 0.0
    for x in c:
        acc += (2 * x.taker_buy_volume - x.volume) if x.taker_buy_volume is not None else 0.0
        cvd_cum.append(acc)
    vols = [x.volume for x in c]
    oi_map = {t: v for t, v in (oi_points or [])}
    oi_at = [oi_map.get(x.ts + timedelta(seconds=tf_s)) or oi_map.get(x.ts) for x in c]
    ph, pl = pivots([x.high for x in c], [x.low for x in c], 3, 3)
    bear, bull = [], []
    series = {"RSI": rsi_v, "MACD": hist, "CVD": cvd_cum, "volume": vols, "OI": oi_at}
    if len(ph) >= 2:
        i, j = ph[-2], ph[-1]
        if c[j].high > c[i].high:
            for name, s in series.items():
                if s[i] is not None and s[j] is not None and s[j] < s[i]:
                    bear.append(name)
    if len(pl) >= 2:
        i, j = pl[-2], pl[-1]
        if c[j].low < c[i].low:
            for name, s in series.items():
                if s[i] is not None and s[j] is not None and s[j] > s[i]:
                    bull.append(name)
    txt = []
    if bear:
        txt.append("bearish divergence vs " + ", ".join(bear) + " (higher price high, weaker indicator)")
    if bull:
        txt.append("bullish divergence vs " + ", ".join(bull) + " (lower price low, stronger indicator)")
    return IndicatorResult("divergence", {"bearish": bear, "bullish": bull}, 0.0, 0.6,
                           f"{tf}: " + ("; ".join(txt) if txt else "no divergence at the last two swings"),
                           _ts(candles, tf_s), tf, {"bearish_divergences": bear, "bullish_divergences": bull})


# ---------------- G. support / resistance ----------------
def support_resistance(price: float, now: datetime, c1h: list[Candle], c1d: list[Candle], c5m: list[Candle],
                       liq_events: list[dict] | None = None) -> dict:
    cands: list[tuple[float, float, str]] = []  # (price, strength, source)
    for cs, tf, s, left in ((c1h[-200:], "1h swing", 1.0, 3), (resample(c1h[-720:], 4 * 3600), "4h swing", 2.0, 2),
                            (c1d[-120:], "1D swing", 3.0, 2)):
        if len(cs) > 10:
            ph, pl = pivots([c.high for c in cs], [c.low for c in cs], left, left)
            cands += [(cs[i].high, s, tf) for i in ph[-6:]] + [(cs[i].low, s, tf) for i in pl[-6:]]
    for k, v in previous_period_hl(c1d, now).items():
        cands.append((v, 3.0 if "week" in k else 2.0, k.replace("_", " ")))
    sv, _ = session_vwap(c5m or c1h)
    if sv:
        cands.append((sv, 1.0, "session VWAP"))
    for p in high_volume_nodes(volume_profile(c1h[-720:])):
        cands.append((p, 2.0, "volume node"))
    if liq_events:
        bins: dict[int, float] = {}
        for e in liq_events:
            if e.get("price"):
                bins[int(e["price"] // (price * 0.002))] = bins.get(int(e["price"] // (price * 0.002)), 0) + e["usd"]
        for b, usd in sorted(bins.items(), key=lambda x: x[1], reverse=True)[:3]:
            if usd > 1e6:
                cands.append(((b + 0.5) * price * 0.002, 2.0, "liquidation cluster"))
    # merge levels within 0.3 %
    merged: list[dict] = []
    for p, s, src in sorted(cands):
        if merged and abs(p / merged[-1]["price"] - 1) < 0.003:
            m = merged[-1]
            m["price"] = (m["price"] * m["strength"] + p * s) / (m["strength"] + s)
            m["strength"] += s
            m["sources"].append(src)
        else:
            merged.append({"price": p, "strength": s, "sources": [src]})
    below = [m for m in merged if m["price"] < price * 0.999]
    above = [m for m in merged if m["price"] > price * 1.001]
    near = lambda xs, f: f(xs, key=lambda m: abs(m["price"] - price)) if xs else None  # noqa: E731
    major = lambda xs: max([m for m in xs if abs(m["price"] / price - 1) < 0.1] or [None], key=lambda m: m["strength"] if m else 0)  # noqa: E731
    return {"nearest_support": near(below, min), "nearest_resistance": near(above, min),
            "major_support": major(below), "major_resistance": major(above), "levels": merged}
