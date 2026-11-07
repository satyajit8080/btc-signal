"""Public liquidation websockets (Binance forceOrder, Bybit allLiquidation).

Side convention after parsing: side="long" means a LONG position was liquidated
(a forced sell), side="short" means a short was liquidated (a forced buy).
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from collections import deque
from datetime import datetime, timezone

log = logging.getLogger("btcsignal.liq")

BINANCE_WS = "wss://fstream.binance.com/ws/btcusdt@forceOrder"
BYBIT_WS = "wss://stream.bybit.com/v5/public/linear"


def parse_binance(msg: dict) -> dict | None:
    o = msg.get("o") or {}
    if o.get("s") != "BTCUSDT":
        return None
    qty = float(o.get("z") or o.get("q") or 0)
    px = float(o.get("ap") or o.get("p") or 0)
    # SELL order = the exchange closing a long.
    side = "long" if o.get("S") == "SELL" else "short"
    return {"ts": datetime.fromtimestamp(o["T"] / 1000, timezone.utc), "venue": "binance",
            "side": side, "btc": qty, "price": px, "usd": qty * px}


def parse_bybit(msg: dict) -> list[dict]:
    if not str(msg.get("topic", "")).startswith("allLiquidation"):
        return []
    out = []
    for d in msg.get("data") or []:
        qty, px = float(d.get("v") or 0), float(d.get("p") or 0)
        # Bybit docs: "Buy" means a long position has been liquidated.
        side = "long" if d.get("S") == "Buy" else "short"
        out.append({"ts": datetime.fromtimestamp(int(d["T"]) / 1000, timezone.utc), "venue": "bybit",
                    "side": side, "btc": qty, "price": px, "usd": qty * px})
    return out


class LiquidationStream:
    def __init__(self, maxlen: int = 50_000, on_event=None):
        self.events: deque[dict] = deque(maxlen=maxlen)
        self.connected_at: dict[str, float] = {}
        self.on_event = on_event
        self._tasks: list[asyncio.Task] = []

    def add(self, ev: dict) -> None:
        self.events.append(ev)
        if self.on_event:
            try:
                self.on_event(ev)
            except Exception:  # persistence must never kill the stream
                log.exception("liquidation persist failed")

    def connected_venues(self) -> list[str]:
        return sorted(self.connected_at)

    def connected_any(self) -> bool:
        return bool(self.connected_at)

    def uptime_s(self, now: float) -> float:
        return max((now - t for t in self.connected_at.values()), default=0.0)

    def events_since(self, ts: datetime) -> list[dict]:
        return [e for e in self.events if e["ts"] >= ts]

    def start(self) -> None:
        self._tasks = [asyncio.create_task(self._run("binance", BINANCE_WS, None)),
                       asyncio.create_task(self._run("bybit", BYBIT_WS,
                                                     {"op": "subscribe", "args": ["allLiquidation.BTCUSDT"]}))]

    async def stop(self) -> None:
        for t in self._tasks:
            t.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)

    async def _run(self, venue: str, url: str, sub: dict | None) -> None:
        import websockets
        backoff = 1.0
        while True:
            try:
                async with websockets.connect(url, ping_interval=20, open_timeout=15) as ws:
                    if sub:
                        await ws.send(json.dumps(sub))
                    self.connected_at[venue] = time.time()
                    backoff = 1.0
                    async for raw in ws:
                        msg = json.loads(raw)
                        if venue == "binance":
                            ev = parse_binance(msg)
                            if ev:
                                self.add(ev)
                        else:
                            for ev in parse_bybit(msg):
                                self.add(ev)
            except asyncio.CancelledError:
                raise
            except Exception as e:
                log.warning("liquidation stream %s dropped: %s", venue, e)
            self.connected_at.pop(venue, None)
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 60)
