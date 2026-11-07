"""Indicator math. Pure functions over candle lists / float series; no I/O.

Conventions: series functions return a list aligned to the input (leading values are
None until the indicator has enough history). Wilder smoothing is used for RSI/ATR/ADX.
"""
from __future__ import annotations

import math
from datetime import datetime, timedelta, timezone

from app.core.context import Candle

Num = float | None


# ---------------- moving averages ----------------
def ema_series(values: list[float], period: int) -> list[Num]:
    out: list[Num] = [None] * len(values)
    if len(values) < period:
        return out
    k = 2.0 / (period + 1)
    prev = sum(values[:period]) / period  # SMA seed
    out[period - 1] = prev
    for i in range(period, len(values)):
        prev = values[i] * k + prev * (1 - k)
        out[i] = prev
    return out


def sma_series(values: list[float], period: int) -> list[Num]:
    out: list[Num] = [None] * len(values)
    acc = 0.0
    for i, v in enumerate(values):
        acc += v
        if i >= period:
            acc -= values[i - period]
        if i >= period - 1:
            out[i] = acc / period
    return out


def stdev(values: list[float]) -> float:
    if len(values) < 2:
        return 0.0
    m = sum(values) / len(values)
    return math.sqrt(sum((v - m) ** 2 for v in values) / len(values))


# ---------------- oscillators ----------------
def rsi_series(closes: list[float], period: int = 14) -> list[Num]:
    out: list[Num] = [None] * len(closes)
    if len(closes) <= period:
        return out
    gains = [max(0.0, b - a) for a, b in zip(closes[:-1], closes[1:])]
    losses = [max(0.0, a - b) for a, b in zip(closes[:-1], closes[1:])]
    ag, al = sum(gains[:period]) / period, sum(losses[:period]) / period

    def val(g: float, l: float) -> float:
        return 100.0 if l == 0 else 100 - 100 / (1 + g / l)
    out[period] = val(ag, al)
    for i in range(period, len(gains)):
        ag = (ag * (period - 1) + gains[i]) / period
        al = (al * (period - 1) + losses[i]) / period
        out[i + 1] = val(ag, al)
    return out


def macd_series(closes: list[float], fast: int = 12, slow: int = 26, signal: int = 9) -> tuple[list[Num], list[Num], list[Num]]:
    ef, es = ema_series(closes, fast), ema_series(closes, slow)
    line: list[Num] = [None if a is None or b is None else a - b for a, b in zip(ef, es)]
    start = next((i for i, v in enumerate(line) if v is not None), len(line))
    sig_part = ema_series([v for v in line[start:] if v is not None], signal)
    sig: list[Num] = [None] * start + sig_part
    hist: list[Num] = [None if a is None or b is None else a - b for a, b in zip(line, sig)]
    return line, sig, hist


def true_ranges(candles: list[Candle]) -> list[float]:
    trs = [candles[0].high - candles[0].low] if candles else []
    for prev, c in zip(candles[:-1], candles[1:]):
        trs.append(max(c.high - c.low, abs(c.high - prev.close), abs(c.low - prev.close)))
    return trs


def atr_series(candles: list[Candle], period: int = 14) -> list[Num]:
    trs = true_ranges(candles)
    out: list[Num] = [None] * len(trs)
    if len(trs) < period:
        return out
    a = sum(trs[:period]) / period
    out[period - 1] = a
    for i in range(period, len(trs)):
        a = (a * (period - 1) + trs[i]) / period
        out[i] = a
    return out


def adx_series(candles: list[Candle], period: int = 14) -> tuple[list[Num], list[Num], list[Num]]:
    """Returns (ADX, +DI, −DI) with Wilder smoothing."""
    n = len(candles)
    adx: list[Num] = [None] * n
    pdi_out: list[Num] = [None] * n
    mdi_out: list[Num] = [None] * n
    if n < 2 * period + 1:
        return adx, pdi_out, mdi_out
    trs = true_ranges(candles)
    pdm, mdm = [0.0], [0.0]
    for prev, c in zip(candles[:-1], candles[1:]):
        up, down = c.high - prev.high, prev.low - c.low
        pdm.append(up if up > down and up > 0 else 0.0)
        mdm.append(down if down > up and down > 0 else 0.0)
    tr_s, p_s, m_s = sum(trs[1:period + 1]), sum(pdm[1:period + 1]), sum(mdm[1:period + 1])
    dxs: list[float] = []
    for i in range(period + 1, n):
        tr_s = tr_s - tr_s / period + trs[i]
        p_s = p_s - p_s / period + pdm[i]
        m_s = m_s - m_s / period + mdm[i]
        pdi = 100 * p_s / tr_s if tr_s else 0.0
        mdi = 100 * m_s / tr_s if tr_s else 0.0
        pdi_out[i], mdi_out[i] = pdi, mdi
        dxs.append(100 * abs(pdi - mdi) / (pdi + mdi) if pdi + mdi else 0.0)
        if len(dxs) == period:
            adx[i] = sum(dxs) / period
        elif len(dxs) > period:
            adx[i] = (adx[i - 1] * (period - 1) + dxs[-1]) / period  # type: ignore[operator]
    return adx, pdi_out, mdi_out


def bollinger(closes: list[float], period: int = 20, k: float = 2.0) -> dict | None:
    if len(closes) < period:
        return None
    w = closes[-period:]
    mid = sum(w) / period
    sd = stdev(w)
    upper, lower = mid + k * sd, mid - k * sd
    width = (upper - lower) / mid if mid else 0.0
    pct_b = (closes[-1] - lower) / (upper - lower) if upper > lower else 0.5
    return {"mid": mid, "upper": upper, "lower": lower, "pct_b": pct_b, "bandwidth": width}


def bandwidth_history(closes: list[float], period: int = 20, k: float = 2.0, step: int = 1) -> list[float]:
    out = []
    for i in range(period, len(closes) + 1, step):
        b = bollinger(closes[:i][-period:], period, k)
        if b:
            out.append(b["bandwidth"])
    return out


# ---------------- volume / VWAP ----------------
def vwap(candles: list[Candle]) -> float | None:
    pv = sum((c.high + c.low + c.close) / 3 * c.volume for c in candles)
    v = sum(c.volume for c in candles)
    return pv / v if v else None


def session_vwap(candles: list[Candle]) -> tuple[float | None, int]:
    """VWAP since 00:00 UTC of the last candle's day (BTC trades 24/7; UTC day = session)."""
    if not candles:
        return None, 0
    day = candles[-1].ts.replace(hour=0, minute=0, second=0, microsecond=0)
    sess = [c for c in candles if c.ts >= day]
    return vwap(sess), len(sess)


def volume_profile(candles: list[Candle], bins: int = 40) -> list[tuple[float, float]]:
    """(price_level, volume) per bin, volume attributed to each candle's typical price."""
    if not candles:
        return []
    lo, hi = min(c.low for c in candles), max(c.high for c in candles)
    if hi <= lo:
        return []
    width = (hi - lo) / bins
    vols = [0.0] * bins
    for c in candles:
        tp = (c.high + c.low + c.close) / 3
        vols[min(bins - 1, int((tp - lo) / width))] += c.volume
    return [(lo + (i + 0.5) * width, v) for i, v in enumerate(vols)]


def high_volume_nodes(profile: list[tuple[float, float]], top: int = 3) -> list[float]:
    """Local maxima of the profile, largest first."""
    peaks = [profile[i] for i in range(1, len(profile) - 1)
             if profile[i][1] >= profile[i - 1][1] and profile[i][1] >= profile[i + 1][1]]
    return [p for p, _ in sorted(peaks, key=lambda x: x[1], reverse=True)[:top]]


# ---------------- structure ----------------
def pivots(values_hi: list[float], values_lo: list[float], left: int = 3, right: int = 3) -> tuple[list[int], list[int]]:
    highs, lows = [], []
    for i in range(left, len(values_hi) - right):
        win = range(i - left, i + right + 1)
        if all(values_hi[i] >= values_hi[j] for j in win) and any(values_hi[i] > values_hi[j] for j in win if j != i):
            highs.append(i)
        if all(values_lo[i] <= values_lo[j] for j in win) and any(values_lo[i] < values_lo[j] for j in win if j != i):
            lows.append(i)
    return highs, lows


def resample(candles: list[Candle], seconds: int) -> list[Candle]:
    buckets: dict[int, list[Candle]] = {}
    for c in candles:
        buckets.setdefault(int(c.ts.timestamp()) // seconds, []).append(c)
    out = []
    for key in sorted(buckets):
        cs = buckets[key]
        tb = [c.taker_buy_volume for c in cs]
        out.append(Candle(datetime.fromtimestamp(key * seconds, timezone.utc), cs[0].open, max(c.high for c in cs),
                          min(c.low for c in cs), cs[-1].close, sum(c.volume for c in cs),
                          None if any(t is None for t in tb) else sum(tb)))
    return out


def previous_period_hl(candles_1d: list[Candle], now: datetime) -> dict:
    """Previous UTC day and previous ISO week high/low from daily candles."""
    today = now.replace(hour=0, minute=0, second=0, microsecond=0)
    prev_day = [c for c in candles_1d if c.ts == today - timedelta(days=1)]
    week_start = today - timedelta(days=today.weekday())
    prev_week = [c for c in candles_1d if week_start - timedelta(days=7) <= c.ts < week_start]
    out = {}
    if prev_day:
        out.update(prev_day_high=prev_day[0].high, prev_day_low=prev_day[0].low)
    if prev_week:
        out.update(prev_week_high=max(c.high for c in prev_week), prev_week_low=min(c.low for c in prev_week))
    return out


def percentile_rank(values: list[float], x: float) -> float:
    if not values:
        return 0.5
    return sum(1 for v in values if v <= x) / len(values)


def last(xs: list[Num], back: int = 0) -> Num:
    """Value `back` steps before the end, skipping nothing (None if not yet defined)."""
    return xs[-1 - back] if len(xs) > back else None
