"""Provider adapter interfaces.

Each adapter converts one external API into canonical metrics (see normalize.metrics).
Adapters are replaceable: engines never import an adapter, they read the MarketContext.
"""
from __future__ import annotations

import asyncio
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

import httpx

from app.config import Settings
from app.core.context import Candle, Datum, MarketContext, Series


class ProviderError(Exception):
    """Raised when a provider is reachable but returns unusable data."""


class ProviderBlocked(ProviderError):
    """Raised when automated access is refused (Cloudflare, 403, geo-block)."""


@dataclass
class ProviderResult:
    provider: str
    fetched_at: datetime
    scalars: list[tuple[str, Datum]] = field(default_factory=list)
    series: list[tuple[str, Series]] = field(default_factory=list)
    candles: dict[str, list[Candle]] = field(default_factory=dict)
    events: dict[str, list[dict]] = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)

    def scalar(self, metric: str, value: float, ts: datetime, sub: str | None = None, **meta: Any) -> None:
        pid = f"{self.provider}:{sub}" if sub else self.provider
        self.scalars.append((metric, Datum(float(value), pid, ts, meta)))

    def add_series(self, metric: str, points: list[tuple[datetime, float]], sub: str | None = None) -> None:
        pid = f"{self.provider}:{sub}" if sub else self.provider
        if points:
            self.series.append((metric, Series(pid, list(points))))

    def apply(self, ctx: MarketContext) -> None:
        ctx.fetched_at[self.provider] = self.fetched_at
        for m, d in self.scalars:
            ctx.add_scalar(m, d)
        for m, s in self.series:
            ctx.add_series(m, s)
        for k, cs in self.candles.items():
            ctx.set_candles(k, cs, self.provider)
        for k, evs in self.events.items():
            ctx.add_events(k, evs, self.provider)

    @property
    def empty(self) -> bool:
        return not (self.scalars or self.series or self.candles or any(self.events.values()))


class Provider(ABC):
    id: str = ""
    interfaces: tuple[str, ...] = ()
    key_setting: str | None = None  # Settings attribute holding the API key
    min_interval_s: int = 60

    def __init__(self, settings: Settings):
        self.settings = settings

    def is_configured(self) -> bool:
        return self.key_setting is None or bool(getattr(self.settings, self.key_setting, None))

    @property
    def api_key(self) -> str | None:
        return getattr(self.settings, self.key_setting, None) if self.key_setting else None

    @abstractmethod
    async def fetch(self, client: httpx.AsyncClient, now: datetime) -> ProviderResult: ...

    def result(self, now: datetime) -> ProviderResult:
        return ProviderResult(self.id, now)


# Interface markers named in the spec. An adapter may implement several.
class MarketDataProvider(Provider, ABC):
    interfaces = ("market",)


class DerivativesProvider(Provider, ABC):
    interfaces = ("derivatives",)


class WhaleProvider(Provider, ABC):
    interfaces = ("whale",)


class OnChainProvider(Provider, ABC):
    interfaces = ("onchain",)


class ETFProvider(Provider, ABC):
    interfaces = ("etf",)


class MacroProvider(Provider, ABC):
    interfaces = ("macro",)


class NetworkProvider(Provider, ABC):
    interfaces = ("network",)


class LiquidityProvider(Provider, ABC):
    interfaces = ("liquidity",)


# ---------------- helpers ----------------
def ms(ts_ms: float | int | str) -> datetime:
    return datetime.fromtimestamp(float(ts_ms) / 1000.0, timezone.utc)


def sec(ts_s: float | int | str) -> datetime:
    return datetime.fromtimestamp(float(ts_s), timezone.utc)


async def get_json(client: httpx.AsyncClient, url: str, *, params: dict | None = None,
                   headers: dict | None = None, retries: int = 1, timeout: float | None = None) -> Any:
    last: Exception | None = None
    for attempt in range(retries + 1):
        try:
            kw = {"timeout": timeout} if timeout else {}
            r = await client.get(url, params=params, headers=headers, **kw)
            if r.status_code in (403, 451) or "Just a moment" in r.text[:400]:
                raise ProviderBlocked(f"{url} refused automated access (HTTP {r.status_code})")
            if r.status_code == 429 or r.status_code >= 500:
                raise httpx.HTTPStatusError(f"HTTP {r.status_code}", request=r.request, response=r)
            r.raise_for_status()
            return r.json()
        except ProviderBlocked:
            raise
        except (httpx.TransportError, httpx.HTTPStatusError) as e:
            last = e
            if attempt < retries:
                await asyncio.sleep(0.5 * (attempt + 1))
    assert last is not None
    raise last


async def post_json(client: httpx.AsyncClient, url: str, body: dict, headers: dict | None = None) -> Any:
    r = await client.post(url, json=body, headers=headers)
    r.raise_for_status()
    return r.json()


def parse_binance_klines(rows: list) -> list[Candle]:
    return [Candle(ms(r[0]), float(r[1]), float(r[2]), float(r[3]), float(r[4]), float(r[5]),
                   float(r[9])) for r in rows]
