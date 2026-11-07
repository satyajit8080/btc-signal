"""Small, dependency-free indicator helpers."""
from __future__ import annotations

import math
from datetime import datetime, timezone

from app.core.context import Candle


def ema(values: list[float], period: int) -> list[float]:
    if not values:
        return []
    k = 2.0 / (period + 1)
    out = [values[0]]
    for v in values[1:]:
        out.append(v * k + out[-1] * (1 - k))
    return out


def sign(x: float) -> int:
    return 1 if x > 0 else -1 if x < 0 else 0


def pct(a: float, b: float) -> float:
    return (a / b - 1.0) * 100.0 if b else 0.0


def atr(candles: list[Candle], period: int = 14) -> float:
    if len(candles) < 2:
        return 0.0
    trs = []
    for prev, c in zip(candles[:-1], candles[1:]):
        trs.append(max(c.high - c.low, abs(c.high - prev.close), abs(c.low - prev.close)))
    window = trs[-period:]
    return sum(window) / len(window)


def resample(candles: list[Candle], hours: int) -> list[Candle]:
    """Aggregate 1h candles into N-hour candles aligned to UTC multiples of N."""
    buckets: dict[int, list[Candle]] = {}
    span = hours * 3600
    for c in candles:
        buckets.setdefault(int(c.ts.timestamp()) // span, []).append(c)
    out = []
    for key in sorted(buckets):
        cs = buckets[key]
        tb = [c.taker_buy_volume for c in cs]
        out.append(Candle(datetime.fromtimestamp(key * span, timezone.utc),
                          cs[0].open, max(c.high for c in cs), min(c.low for c in cs), cs[-1].close,
                          sum(c.volume for c in cs), None if any(t is None for t in tb) else sum(tb)))
    return out


def swings(candles: list[Candle], left: int = 2, right: int = 2) -> tuple[list[tuple[int, float]], list[tuple[int, float]]]:
    """Fractal swing highs/lows: a bar higher/lower than `left`/`right` neighbours."""
    highs, lows = [], []
    for i in range(left, len(candles) - right):
        c = candles[i]
        if all(c.high > candles[j].high for j in range(i - left, i + right + 1) if j != i):
            highs.append((i, c.high))
        if all(c.low < candles[j].low for j in range(i - left, i + right + 1) if j != i):
            lows.append((i, c.low))
    return highs, lows


def realized_vol(closes: list[float], periods_per_year: float = 24 * 365) -> float:
    if len(closes) < 3:
        return 0.0
    rets = [math.log(b / a) for a, b in zip(closes[:-1], closes[1:]) if a > 0 and b > 0]
    if len(rets) < 2:
        return 0.0
    mu = sum(rets) / len(rets)
    var = sum((r - mu) ** 2 for r in rets) / (len(rets) - 1)
    return math.sqrt(var * periods_per_year) * 100.0


def percentile_rank(values: list[float], x: float) -> float:
    if not values:
        return 0.5
    return sum(1 for v in values if v <= x) / len(values)


def window_sum(candles: list[Candle], hours: int, attr: str) -> float:
    return sum(getattr(c, attr) or 0.0 for c in candles[-hours:])


def interp(x: float, xs: list[float], ys: list[float]) -> float:
    """Piecewise-linear interpolation, clamped at both ends."""
    if x <= xs[0]:
        return ys[0]
    for (x0, y0), (x1, y1) in zip(zip(xs, ys), zip(xs[1:], ys[1:])):
        if x <= x1:
            return y0 + (y1 - y0) * (x - x0) / (x1 - x0)
    return ys[-1]
