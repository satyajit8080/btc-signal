"""Normalized market state handed to every engine.

Engines are pure functions of a MarketContext. Live mode builds it from provider fetches;
the backtester builds it from historical data cut at time t. Same engine code either way,
which is what makes the backtest meaningful.
"""
from __future__ import annotations

import bisect
from dataclasses import dataclass, field
from datetime import datetime, timezone

from app.normalize.metrics import freshness
from app.providers.catalog import source_for_provider


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


@dataclass(frozen=True)
class Datum:
    value: float
    provider: str
    ts: datetime
    meta: dict = field(default_factory=dict, compare=False, hash=False)


@dataclass(frozen=True)
class Candle:
    ts: datetime  # open time, UTC
    open: float
    high: float
    low: float
    close: float
    volume: float
    taker_buy_volume: float | None = None

    @property
    def close_ts(self) -> float:
        return self.ts.timestamp()


@dataclass
class Series:
    provider: str
    points: list[tuple[datetime, float]]  # ascending by time

    def __post_init__(self) -> None:
        self.points.sort(key=lambda p: p[0])

    def __len__(self) -> int:
        return len(self.points)

    @property
    def last_ts(self) -> datetime | None:
        return self.points[-1][0] if self.points else None

    def last(self) -> float | None:
        return self.points[-1][1] if self.points else None

    def at_or_before(self, ts: datetime) -> float | None:
        keys = [p[0] for p in self.points]
        i = bisect.bisect_right(keys, ts) - 1
        return self.points[i][1] if i >= 0 else None

    def pct_change(self, seconds: float) -> float | None:
        if len(self.points) < 2:
            return None
        end_ts, end_v = self.points[-1]
        start = self.at_or_before(datetime.fromtimestamp(end_ts.timestamp() - seconds, timezone.utc))
        if start is None or start == 0:
            return None
        return (end_v / start - 1.0) * 100.0

    def abs_change(self, seconds: float) -> float | None:
        if len(self.points) < 2:
            return None
        end_ts, end_v = self.points[-1]
        start = self.at_or_before(datetime.fromtimestamp(end_ts.timestamp() - seconds, timezone.utc))
        return None if start is None else end_v - start

    def mean_since(self, seconds: float) -> float | None:
        if not self.points:
            return None
        cutoff = self.points[-1][0].timestamp() - seconds
        vals = [v for t, v in self.points if t.timestamp() > cutoff]
        return sum(vals) / len(vals) if vals else None

    def cut(self, ts: datetime) -> "Series":
        return Series(self.provider, [p for p in self.points if p[0] <= ts])


@dataclass
class ProviderMeta:
    provider: str
    relevance: float
    reliability: float


@dataclass
class MarketContext:
    now: datetime
    scalars: dict[str, list[Datum]] = field(default_factory=dict)
    series: dict[str, dict[str, Series]] = field(default_factory=dict)
    candles: dict[str, list[Candle]] = field(default_factory=dict)
    candle_source: dict[str, str] = field(default_factory=dict)
    events: dict[str, list[dict]] = field(default_factory=dict)
    event_source: dict[str, list[str]] = field(default_factory=dict)
    reliability: dict[str, float] = field(default_factory=dict)  # dynamic, from provider health
    provider_status: dict[str, str] = field(default_factory=dict)
    fetched_at: dict[str, datetime] = field(default_factory=dict)  # when each provider's data was taken
    annotations: dict = field(default_factory=dict)  # cross-engine context (e.g. the regime)
    mode: str = "live"  # "live" | "backtest"

    # ---- builders ----
    def add_scalar(self, metric: str, datum: Datum) -> None:
        self.scalars.setdefault(metric, [])
        self.scalars[metric] = [d for d in self.scalars[metric] if d.provider != datum.provider]
        self.scalars[metric].append(datum)

    def add_series(self, metric: str, s: Series) -> None:
        if s.points:
            self.series.setdefault(metric, {})[s.provider] = s

    def set_candles(self, key: str, candles: list[Candle], provider: str) -> None:
        if candles:
            self.candles[key] = sorted(candles, key=lambda c: c.ts)
            self.candle_source[key] = provider

    def add_events(self, key: str, events: list[dict], provider: str) -> None:
        self.events.setdefault(key, []).extend(events)
        self.event_source.setdefault(key, [])
        if provider not in self.event_source[key]:
            self.event_source[key].append(provider)

    # ---- factors ----
    def age_s(self, ts: datetime) -> float:
        return max(0.0, (self.now - ts).total_seconds())

    def fresh(self, metric: str, ts: datetime | None) -> float:
        if ts is None:
            return 0.0
        return freshness(self.age_s(ts), metric)

    def relevance(self, provider: str) -> float:
        src = source_for_provider(provider)
        return src.relevance if src else 0.5

    def reliab(self, provider: str) -> float:
        base_id = provider.split(":", 1)[0]
        if base_id in self.reliability:
            return self.reliability[base_id]
        src = source_for_provider(provider)
        return src.base_reliability if src else 0.7

    # ---- accessors ----
    def get_scalars(self, metric: str, min_fresh: float = 0.0) -> list[Datum]:
        return [d for d in self.scalars.get(metric, []) if self.fresh(metric, d.ts) > min_fresh]

    def get_series(self, metric: str) -> dict[str, Series]:
        return {p: s for p, s in self.series.get(metric, {}).items() if self.fresh(metric, s.last_ts) > 0}

    def get_candles(self, key: str, metric: str | None = None) -> list[Candle]:
        cs = self.candles.get(key, [])
        if not cs:
            return []
        m = metric or {"spot_1h": "CANDLES_SPOT_1H", "spot_1d": "CANDLES_SPOT_1D", "spot_5m": "CANDLES_SPOT_5M",
                       "spot_15m": "CANDLES_SPOT_15M", "perp_1h": "CANDLES_PERP_1H",
                       "coinbase_1h": "CANDLES_COINBASE_1H"}.get(key, key)
        return cs if self.fresh(m, cs[-1].ts) > 0 else []

    def cut(self, ts: datetime) -> "MarketContext":
        """A view of this context as it was known at `ts` (no lookahead). Backtest helper."""
        ctx = MarketContext(now=ts, mode=self.mode, reliability=dict(self.reliability))
        for m, ds in self.scalars.items():
            for d in ds:
                if d.ts <= ts:
                    ctx.add_scalar(m, d)
        for m, by_p in self.series.items():
            for s in by_p.values():
                ctx.add_series(m, s.cut(ts))
        for k, cs in self.candles.items():
            # a candle is only known once it has closed
            span = {"1d": 86400, "5m": 300, "15m": 900}.get(k.rsplit("_", 1)[-1], 3600)
            kept = [c for c in cs if c.ts.timestamp() + span <= ts.timestamp()]
            ctx.set_candles(k, kept, self.candle_source.get(k, "?"))
        for k, evs in self.events.items():
            ctx.events[k] = [e for e in evs if e["ts"] <= ts]
            ctx.event_source[k] = list(self.event_source.get(k, []))
        return ctx
