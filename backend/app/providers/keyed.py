"""Adapters that need a key or that scrape a public page.

These are written against each provider's published API documentation. None of them
had a key available during build, so they are unverified against live responses; the
admin panel shows them as 'unconfigured' until a key is set, and as 'error' (never as
connected) if the live schema differs.
"""
from __future__ import annotations

import asyncio
import re
import time
from collections.abc import Callable
from datetime import datetime, timedelta, timezone

import httpx

from app.providers.base import (
    DerivativesProvider, ETFProvider, LiquidityProvider, OnChainProvider, ProviderError,
    ProviderResult, WhaleProvider, get_json, ms, sec,
)


class CoinGlass(DerivativesProvider):
    id = "coinglass"
    interfaces = ("derivatives", "etf")
    key_setting = "coinglass_api_key"
    min_interval_s = 300
    BASE = "https://open-api-v4.coinglass.com/api"

    def _data(self, payload: dict) -> list:
        if str(payload.get("code")) != "0":
            raise ProviderError(f"coinglass code={payload.get('code')} {payload.get('msg')}")
        return payload.get("data") or []

    async def fetch(self, client: httpx.AsyncClient, now: datetime) -> ProviderResult:
        res = self.result(now)
        h = {"CG-API-KEY": self.api_key or "", "accept": "application/json"}
        oi, fr, liq, etf = await asyncio.gather(
            get_json(client, f"{self.BASE}/futures/open-interest/aggregated-history",
                     params={"symbol": "BTC", "interval": "1h", "limit": 48}, headers=h),
            get_json(client, f"{self.BASE}/futures/funding-rate/oi-weight-history",
                     params={"symbol": "BTC", "interval": "8h", "limit": 3}, headers=h),
            get_json(client, f"{self.BASE}/futures/liquidation/aggregated-history",
                     params={"symbol": "BTC", "interval": "1h", "limit": 24,
                             "exchange_list": "Binance,OKX,Bybit"}, headers=h),
            get_json(client, f"{self.BASE}/etf/bitcoin/flow-history", headers=h),
            return_exceptions=True,
        )
        if not isinstance(oi, Exception):
            res.add_series("BTC_OPEN_INTEREST_USD_HIST",
                           [(ms(r["time"]), float(r["close"])) for r in self._data(oi)])
        if not isinstance(fr, Exception):
            rows = self._data(fr)
            if rows:  # CoinGlass reports funding in percent
                res.scalar("BTC_FUNDING_RATE_8H", float(rows[-1]["close"]) / 100.0, ms(rows[-1]["time"]))
        if not isinstance(liq, Exception):
            evs = []
            for r in self._data(liq):
                t = ms(r["time"])
                evs.append({"ts": t, "side": "long", "usd": float(r.get("aggregated_long_liquidation_usd") or 0),
                            "venue": "coinglass_agg"})
                evs.append({"ts": t, "side": "short", "usd": float(r.get("aggregated_short_liquidation_usd") or 0),
                            "venue": "coinglass_agg"})
            res.events["liquidations"] = evs
        if not isinstance(etf, Exception):
            res.add_series("ETF_NET_FLOW_USD",
                           [(ms(r["timestamp"]), float(r["flow_usd"])) for r in self._data(etf)[-30:]])
        failures = [e for e in (oi, fr, liq, etf) if isinstance(e, Exception)]
        if len(failures) == 4:
            raise failures[0]
        for e in failures:
            res.notes.append(f"partial failure: {type(e).__name__}: {e}")
        return res


CUSTODIAN_HINTS = ("custody", "custodian", "etf", "prime", "institutional")


def classify_transfer(tx: dict) -> str:
    """Meaning is assigned only after the direction and the entities are known. Moves inside one
    entity, and custodian movements, are internal — never counted as whale selling."""
    fo, to = tx.get("from") or {}, tx.get("to") or {}
    f, t = fo.get("owner_type", "unknown"), to.get("owner_type", "unknown")
    fn, tn = (fo.get("owner") or "").lower(), (to.get("owner") or "").lower()
    if fn and fn == tn:
        return "internal"
    if any(h in fn for h in CUSTODIAN_HINTS) or any(h in tn for h in CUSTODIAN_HINTS):
        return "internal"
    if f == "exchange" and t == "exchange":
        return "exchange_to_exchange"
    if t == "exchange":
        return "to_exchange"
    if f == "exchange":
        return "from_exchange"
    return "wallet_to_wallet"


class WhaleAlert(WhaleProvider):
    id = "whale_alert"
    key_setting = "whale_alert_api_key"
    min_interval_s = 300
    WINDOW_S = 24 * 3600

    def __init__(self, settings):
        super().__init__(settings)
        self._buffer: dict[str, dict] = {}
        self._last_ts = 0

    async def fetch(self, client: httpx.AsyncClient, now: datetime) -> ProviderResult:
        start = max(int(now.timestamp()) - 3600, self._last_ts)
        data = await get_json(client, "https://api.whale-alert.io/v1/transactions", params={
            "api_key": self.api_key, "min_value": 1_000_000, "start": start, "currency": "btc", "limit": 100})
        if data.get("result") != "success":
            raise ProviderError(f"whale alert: {data.get('message')}")
        for tx in data.get("transactions") or []:
            if tx.get("blockchain") != "bitcoin":
                continue
            key = f"{tx.get('hash')}:{tx.get('id')}"
            self._buffer[key] = {"ts": sec(tx["timestamp"]), "usd": float(tx.get("amount_usd") or 0),
                                 "btc": float(tx.get("amount") or 0), "kind": classify_transfer(tx),
                                 "from": (tx.get("from") or {}).get("owner", ""),
                                 "to": (tx.get("to") or {}).get("owner", "")}
            self._last_ts = max(self._last_ts, int(tx["timestamp"]))
        cutoff = now - timedelta(seconds=self.WINDOW_S)
        self._buffer = {k: v for k, v in self._buffer.items() if v["ts"] >= cutoff}
        res = self.result(now)
        res.events["whale_transfers"] = list(self._buffer.values())
        return res


class CryptoQuant(OnChainProvider):
    id = "cryptoquant"
    key_setting = "cryptoquant_api_key"
    min_interval_s = 3600

    async def fetch(self, client: httpx.AsyncClient, now: datetime) -> ProviderResult:
        h = {"Authorization": f"Bearer {self.api_key}"}
        url = "https://api.cryptoquant.com/v1/btc/exchange-flows/netflow"
        day, hour = await asyncio.gather(
            get_json(client, url, params={"exchange": "all_exchange", "window": "day", "limit": 90}, headers=h),
            get_json(client, url, params={"exchange": "all_exchange", "window": "hour", "limit": 24 * 14}, headers=h))
        res = self.result(now)
        for payload, metric in ((day, "EXCHANGE_NETFLOW_BTC"), (hour, "EXCHANGE_NETFLOW_BTC_1H")):
            rows = (payload.get("result") or {}).get("data") or []
            res.add_series(metric, [(datetime.fromisoformat(str(r.get("datetime") or r["date"])).replace(tzinfo=timezone.utc),
                                     float(r["netflow_total"])) for r in rows])
        return res


class Glassnode(OnChainProvider):
    id = "glassnode"
    key_setting = "glassnode_api_key"
    min_interval_s = 3600

    async def fetch(self, client: httpx.AsyncClient, now: datetime) -> ProviderResult:
        h = {"X-Api-Key": self.api_key or ""}
        base = "https://api.glassnode.com/v1/metrics"
        days = {"EXCHANGE_NETFLOW_BTC": 120, "EXCHANGE_NETFLOW_BTC_1H": 14, "BTC_SOPR": 1460, "BTC_LTH_SUPPLY": 1460}
        paths = (("transactions/transfers_volume_exchanges_net", "EXCHANGE_NETFLOW_BTC", "24h"),
                 ("transactions/transfers_volume_exchanges_net", "EXCHANGE_NETFLOW_BTC_1H", "1h"),
                 ("indicators/sopr", "BTC_SOPR", "24h"),
                 ("supply/lth_sum", "BTC_LTH_SUPPLY", "24h"))
        results = await asyncio.gather(*[
            get_json(client, f"{base}/{p}", headers=h,
                     params={"a": "BTC", "i": i, "s": int((now - timedelta(days=days[m])).timestamp())})
            for p, m, i in paths], return_exceptions=True)
        res = self.result(now)
        for (p, metric, _), rows in zip(paths, results):
            if isinstance(rows, Exception):
                res.notes.append(f"{metric}: {type(rows).__name__}")  # e.g. metric outside the plan's tier
                continue
            res.add_series(metric, [(sec(r["t"]), float(r["v"])) for r in rows])
        if not res.series:
            raise ProviderError("glassnode returned no metrics: " + "; ".join(res.notes))
        return res


class Etherscan(LiquidityProvider):
    id = "etherscan"
    key_setting = "etherscan_api_key"
    min_interval_s = 3600
    TOKENS = {"USDT": "0xdac17f958d2ee523a2206206994597c13d831ec7",
              "USDC": "0xa0b86991c6218b36c1d19d4a2e9eb0ce3606eb48"}

    async def fetch(self, client: httpx.AsyncClient, now: datetime) -> ProviderResult:
        total = 0.0
        for addr in self.TOKENS.values():
            d = await get_json(client, "https://api.etherscan.io/v2/api", params={
                "chainid": 1, "module": "stats", "action": "tokensupply",
                "contractaddress": addr, "apikey": self.api_key})
            if str(d.get("status")) != "1":
                raise ProviderError(f"etherscan: {d.get('message')} {d.get('result')}")
            total += int(d["result"]) / 1e6  # both tokens use 6 decimals
            await asyncio.sleep(0.25)  # free tier: 5 req/s
        res = self.result(now)
        res.scalar("STABLECOIN_ETH_SUPPLY_USD", total, now)
        return res


class Dune(OnChainProvider):
    id = "dune"
    key_setting = "dune_api_key"
    min_interval_s = 3600

    def is_configured(self) -> bool:
        return super().is_configured() and bool(self.settings.dune_query_list())

    async def fetch(self, client: httpx.AsyncClient, now: datetime) -> ProviderResult:
        res = self.result(now)
        for q in self.settings.dune_query_list():
            data = await get_json(client, f"https://api.dune.com/api/v1/query/{int(q['query_id'])}/results",
                                  params={"limit": 1000}, headers={"X-Dune-API-Key": self.api_key or ""})
            rows = (data.get("result") or {}).get("rows") or []
            pts = []
            for r in rows:
                t, v = r.get(q.get("date_col", "day")), r.get(q.get("value_col", "value"))
                if t is None or v is None:
                    continue
                ts = datetime.fromisoformat(str(t).replace(" UTC", "").replace("Z", "")[:19]).replace(tzinfo=timezone.utc)
                pts.append((ts, float(v)))
            res.add_series(q["metric"], pts, sub=f"q{q['query_id']}")
        return res


_FARSIDE_DATE = re.compile(r"^\s*(\d{1,2} [A-Z][a-z]{2} \d{4})\s*$")


def parse_farside_html(html: str) -> list[tuple[datetime, float]]:
    """Rows look like: <td>11 Jan 2024</td> ... <td>655.3</td> where the last cell is the
    total in US$m and negatives are written as (123.4)."""
    out = []
    for row in re.findall(r"<tr[^>]*>(.*?)</tr>", html, flags=re.S | re.I):
        cells = [re.sub(r"<[^>]+>", "", c).strip() for c in re.findall(r"<t[dh][^>]*>(.*?)</t[dh]>", row, flags=re.S | re.I)]
        if len(cells) < 3 or not _FARSIDE_DATE.match(cells[0]):
            continue
        total = cells[-1].replace(",", "")
        if total in ("", "-"):
            continue
        neg = total.startswith("(") and total.endswith(")")
        try:
            v = float(total.strip("()"))
        except ValueError:
            continue
        d = datetime.strptime(cells[0].strip(), "%d %b %Y").replace(tzinfo=timezone.utc)
        out.append((d, (-v if neg else v) * 1e6))
    return out


class Farside(ETFProvider):
    id = "farside"
    min_interval_s = 3600

    async def fetch(self, client: httpx.AsyncClient, now: datetime) -> ProviderResult:
        r = await client.get("https://farside.co.uk/bitcoin-etf-flow-all-data/",
                             headers={"User-Agent": "Mozilla/5.0 (BTC-Signal research)"})
        if r.status_code == 403 or "Just a moment" in r.text[:500]:
            from app.providers.base import ProviderBlocked
            raise ProviderBlocked("farside.co.uk served a Cloudflare challenge; no data taken")
        r.raise_for_status()
        pts = parse_farside_html(r.text)
        if not pts:
            raise ProviderError("farside table not found / layout changed")
        res = self.result(now)
        res.add_series("ETF_NET_FLOW_USD", pts[-30:])
        return res


class ManualETF(ETFProvider):
    """Operator-entered published ETF flow figures (POST /api/v1/admin/etf-flows)."""
    id = "manual_etf"
    min_interval_s = 60

    def __init__(self, settings, loader: Callable[[], list[tuple[datetime, float]]] | None = None):
        super().__init__(settings)
        self.loader = loader

    def is_configured(self) -> bool:
        return self.loader is not None and bool(self.loader())

    async def fetch(self, client: httpx.AsyncClient, now: datetime) -> ProviderResult:
        res = self.result(now)
        res.add_series("ETF_NET_FLOW_USD", self.loader() if self.loader else [])
        return res


class LiquidationStreamProvider(DerivativesProvider):
    """Exposes the websocket collector's rolling buffer as a provider."""
    id = "liquidation_stream"
    min_interval_s = 1

    def __init__(self, settings, stream=None):
        super().__init__(settings)
        self.stream = stream

    def is_configured(self) -> bool:
        return self.stream is not None and self.settings.liquidation_stream

    async def fetch(self, client: httpx.AsyncClient, now: datetime) -> ProviderResult:
        res = self.result(now)
        if not self.stream or not self.stream.connected_any():
            raise ProviderError("liquidation websockets not connected")
        res.events["liquidations"] = self.stream.events_since(now - timedelta(hours=24))
        res.scalar("LIQ_STREAM_UPTIME_S", self.stream.uptime_s(time.time()), now,
                   venues=",".join(self.stream.connected_venues()))
        return res
