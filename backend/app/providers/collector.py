"""Runs enabled adapters concurrently, tracks their health, and assembles a MarketContext.

A provider that fails keeps its last good result, so its data ages and loses freshness
weight naturally — it is never re-stamped as new.
"""
from __future__ import annotations

import asyncio
import logging
import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime

import httpx

from app.core.context import MarketContext, Series, utcnow
from app.normalize.metrics import METRICS
from app.providers.base import Provider, ProviderBlocked, ProviderResult
from app.providers.catalog import source_for_provider

log = logging.getLogger("btcsignal.collector")


@dataclass
class ProviderHealth:
    id: str
    status: str = "pending"
    last_attempt: float = 0.0
    last_success: float | None = None
    last_error: str | None = None
    latency_ms: float | None = None
    success_count: int = 0
    error_count: int = 0
    consecutive_errors: int = 0
    recent: deque = field(default_factory=lambda: deque(maxlen=50))
    notes: list[str] = field(default_factory=list)

    def success_rate(self) -> float:
        return sum(self.recent) / len(self.recent) if self.recent else 1.0

    def reliability(self, base: float) -> float:
        r = base * (0.5 + 0.5 * self.success_rate())
        if self.consecutive_errors >= 3:
            r *= 0.5
        return round(r, 4)

    def as_dict(self) -> dict:
        return {"id": self.id, "status": self.status, "last_attempt": self.last_attempt or None,
                "last_success": self.last_success, "last_error": self.last_error,
                "latency_ms": self.latency_ms, "success_count": self.success_count,
                "error_count": self.error_count, "consecutive_errors": self.consecutive_errors,
                "success_rate": round(self.success_rate(), 3), "notes": self.notes[-5:]}


class Collector:
    def __init__(self, providers: dict[str, Provider], timeout: float = 12.0,
                 is_enabled: Callable[[str], bool] | None = None,
                 history_loader: Callable[[str], dict[str, list[tuple[datetime, float]]]] | None = None):
        self.providers = providers
        self.timeout = timeout
        self.is_enabled = is_enabled or (lambda _pid: True)
        self.history_loader = history_loader
        self.health: dict[str, ProviderHealth] = {pid: ProviderHealth(pid) for pid in providers}
        self.cache: dict[str, ProviderResult] = {}
        self._client: httpx.AsyncClient | None = None

    async def client(self) -> httpx.AsyncClient:
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(timeout=self.timeout, follow_redirects=True,
                                             headers={"User-Agent": "btc-signal/1.0"})
        return self._client

    async def close(self) -> None:
        if self._client:
            await self._client.aclose()

    async def _run_one(self, pid: str, p: Provider, now: datetime) -> None:
        h = self.health[pid]
        if not self.is_enabled(pid):
            h.status = "disabled"
            self.cache.pop(pid, None)
            return
        if not p.is_configured():
            h.status = "unconfigured"
            return
        interval = p.min_interval_s
        if h.consecutive_errors:
            interval = min(max(interval, 60) * 2 ** min(h.consecutive_errors - 1, 5), 1800)  # back off a failing source
        if time.time() - h.last_attempt < interval and (pid in self.cache or h.consecutive_errors):
            return  # not due; cached result keeps its original timestamps
        h.last_attempt = time.time()
        t0 = time.perf_counter()
        try:
            res = await asyncio.wait_for(p.fetch(await self.client(), now), timeout=self.timeout * 3)
            if res.empty:
                raise ValueError("provider returned no data")
            self.cache[pid] = res
            h.status, h.last_success, h.last_error = "ok", time.time(), None
            h.success_count += 1
            h.consecutive_errors = 0
            h.recent.append(True)
            h.notes = res.notes
        except ProviderBlocked as e:
            self._fail(h, "blocked", str(e))
        except Exception as e:  # any adapter failure is isolated to that adapter
            self._fail(h, "error", f"{type(e).__name__}: {e}"[:300])
        finally:
            h.latency_ms = round((time.perf_counter() - t0) * 1000, 1)

    @staticmethod
    def _fail(h: ProviderHealth, status: str, msg: str) -> None:
        h.status, h.last_error = status, msg
        h.error_count += 1
        h.consecutive_errors += 1
        h.recent.append(False)
        log.warning("provider %s %s: %s", h.id, status, msg)

    async def collect(self, now: datetime | None = None) -> MarketContext:
        now = now or utcnow()
        await asyncio.gather(*[self._run_one(pid, p, now) for pid, p in self.providers.items()])
        ctx = MarketContext(now=now)
        for pid, res in self.cache.items():
            if self.is_enabled(pid):
                res.apply(ctx)
        for pid, h in self.health.items():
            src = source_for_provider(pid)
            ctx.reliability[pid] = h.reliability(src.base_reliability if src else 0.7)
            ctx.provider_status[pid] = h.status
        self._attach_history(ctx)
        return ctx

    def _attach_history(self, ctx: MarketContext) -> None:
        """Snapshot-only metrics (e.g. Hyperliquid OI) get a series from our own store."""
        if not self.history_loader:
            return
        for name, m in METRICS.items():
            if not m.track_history:
                continue
            stored = self.history_loader(name)
            for provider, pts in stored.items():
                if provider in ctx.series.get(name, {}):
                    continue  # provider already supplies its own history
                live = [(d.ts, d.value) for d in ctx.scalars.get(name, []) if d.provider == provider]
                merged = {t: v for t, v in pts + live}
                ctx.add_series(name, Series(provider, sorted(merged.items())))
