"""Signal service: one cycle = collect → normalize → engines → decision → persist → publish.

Runs inside the API process (dev) or as a dedicated worker process (`python -m app.worker`).
Cross-process readers get the latest state from Redis when configured, else from the DB.
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from datetime import datetime, timedelta, timezone

from app.backtest.evaluator import evaluate_pending
from app.config import Settings
from app.core.context import utcnow
from app.db.repo import Repo
from app.db.session import Database
from app.providers.base import get_json, parse_binance_klines
from app.providers.catalog import ALL_SOURCES, source_for_provider
from app.providers.collector import Collector
from app.providers.liquidation_stream import LiquidationStream
from app.providers.registry import build_providers
from app.scoring.decision import decide

log = logging.getLogger("btcsignal.service")
LATEST_KEY = "btc:signal:latest"
HEALTH_KEY = "btc:providers:health"


class Cache:
    """Redis when available; otherwise the state_kv table. Never required for correctness."""

    def __init__(self, repo: Repo, redis_url: str | None):
        self.repo = repo
        self.redis = None
        if redis_url:
            try:
                import redis
                self.redis = redis.Redis.from_url(redis_url, socket_timeout=2)
                self.redis.ping()
            except Exception as e:
                log.warning("redis unavailable (%s); falling back to database state", e)
                self.redis = None

    def set(self, key: str, value: dict) -> None:
        if self.redis:
            try:
                self.redis.set(key, json.dumps(value, default=str))
            except Exception as e:
                log.warning("redis set failed: %s", e)
        self.repo.kv_set(key, value)

    def get(self, key: str) -> dict | None:
        if self.redis:
            try:
                raw = self.redis.get(key)
                if raw:
                    return json.loads(raw)
            except Exception as e:
                log.warning("redis get failed: %s", e)
        value, _ = self.repo.kv_get(key)
        return value


class SignalService:
    def __init__(self, settings: Settings, db: Database):
        self.settings = settings
        self.db = db
        self.repo = Repo(db)
        self.cache = Cache(self.repo, settings.redis_url)
        self.config_version, self.config = self.repo.active_config()
        self.liq_stream = LiquidationStream(on_event=self._persist_liq) if settings.liquidation_stream else None
        self.providers = build_providers(settings, etf_loader=self.repo.etf_series, liq_stream=self.liq_stream)
        self.collector = Collector(self.providers, timeout=settings.http_timeout,
                                   is_enabled=lambda pid: self.config.provider_enabled(pid),
                                   history_loader=lambda m: self.repo.load_history(m, 48))
        self.latest: dict | None = None
        self._last_record = 0.0
        self._last_direction_trade: tuple[str, str] | None = None
        self._subscribers: set[asyncio.Queue] = set()
        self._task: asyncio.Task | None = None
        self._cycles = 0

    def _persist_liq(self, ev: dict) -> None:
        if ev["usd"] >= 10_000:  # keep the table small; the in-memory buffer has everything
            self.repo.save_liquidation(ev)

    def reload_config(self) -> None:
        self.config_version, self.config = self.repo.active_config()

    # ---------------- cycle ----------------
    async def run_cycle(self) -> dict:
        t0 = time.perf_counter()
        ctx = await self.collector.collect()
        self.repo.save_observations(ctx)
        decision = decide(ctx, self.config, self.repo.similar_lookup())
        decision["cycle_ms"] = round((time.perf_counter() - t0) * 1000)
        decision["config_version"] = self.config_version
        now = time.time()
        state = (decision["direction"], decision["trade"])
        if now - self._last_record >= self.settings.signal_record_seconds or state != self._last_direction_trade:
            decision["signal_id"] = self.repo.save_signal(decision, self.config_version)
            self._last_record = now
            self._last_direction_trade = state
        self.latest = decision
        self.cache.set(LATEST_KEY, decision)
        self.cache.set(HEALTH_KEY, self.provider_health())
        for q in list(self._subscribers):
            if q.full():
                q.get_nowait()
            q.put_nowait(decision)
        self._cycles += 1
        return decision

    async def housekeeping(self) -> None:
        try:
            client = await self.collector.client()
            rows = await get_json(client, "https://api.binance.com/api/v3/klines",
                                  params={"symbol": "BTCUSDT", "interval": "1m", "limit": 180})
            self.repo.upsert_candles_1m(parse_binance_klines(rows)[:-1])  # only closed minutes
        except Exception as e:
            log.warning("1m candle refresh failed: %s", e)
        try:
            n = evaluate_pending(self.repo, utcnow())
            if n:
                log.info("evaluated %d signal outcomes", n)
        except Exception as e:
            log.exception("outcome evaluation failed: %s", e)
        if self._cycles % 1440 == 0:
            self.repo.prune()

    async def loop(self) -> None:
        if self.liq_stream:
            self.liq_stream.start()
        last_house = 0.0
        while True:
            started = time.time()
            try:
                await self.run_cycle()
            except Exception as e:
                log.exception("cycle failed")
                self.repo.log("ERROR", "cycle", f"{type(e).__name__}: {e}")
            if time.time() - last_house > 120:
                await self.housekeeping()
                last_house = time.time()
            await asyncio.sleep(max(1.0, self.settings.cycle_seconds - (time.time() - started)))

    def start(self) -> None:
        self._task = asyncio.create_task(self.loop())

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)
        if self.liq_stream:
            await self.liq_stream.stop()
        await self.collector.close()

    # ---------------- readers ----------------
    def get_latest(self) -> dict | None:
        return self.latest or self.cache.get(LATEST_KEY)

    def subscribe(self) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue(maxsize=5)
        self._subscribers.add(q)
        return q

    def unsubscribe(self, q: asyncio.Queue) -> None:
        self._subscribers.discard(q)

    def provider_health(self) -> list[dict]:
        out = []
        for pid, h in self.collector.health.items():
            src = source_for_provider(pid)
            d = h.as_dict()
            d.update(enabled=self.config.provider_enabled(pid), configured=self.providers[pid].is_configured(),
                     interfaces=list(self.providers[pid].interfaces), min_interval_s=self.providers[pid].min_interval_s,
                     reliability=h.reliability(src.base_reliability if src else 0.7),
                     relevance=src.relevance if src else None)
            out.append(d)
        return out

    def sources_report(self) -> list[dict]:
        health = {h["id"]: h for h in (self.provider_health() if self.collector else [])}
        if not any(h["status"] != "pending" for h in health.values()):
            health = {h["id"]: h for h in (self.cache.get(HEALTH_KEY) or [])} or health
        out = []
        for s in ALL_SOURCES:
            h = health.get(s.adapter) if s.adapter else None
            out.append({"number": s.number, "id": s.id, "name": s.name, "url": s.url, "group": s.group,
                        "tier": s.tier, "btc_relevance": s.btc_relevance, "relevance_score": s.relevance,
                        "access": s.access, "official_api": s.official_api, "adapter": s.adapter,
                        "metrics": list(s.metrics), "engines": list(s.engines), "notes": s.notes,
                        "directional": s.directional, "base_reliability": s.base_reliability,
                        "status": h["status"] if h else ("no_adapter" if not s.adapter else "pending"),
                        "live": bool(h and h["status"] == "ok"), "last_success": h["last_success"] if h else None,
                        "last_error": h["last_error"] if h else None,
                        "reliability": h["reliability"] if h else None})
        return out


def history_window(window: str) -> datetime:
    days = {"24h": 1, "7d": 7, "30d": 30, "90d": 90}.get(window.lower(), 1)
    return datetime.now(timezone.utc) - timedelta(days=days)
