"""Free exchange adapters: Binance, Bybit, OKX, Hyperliquid, Coinbase, Kraken, Deribit."""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta

import httpx

from app.core.context import Candle
from app.providers.base import (
    DerivativesProvider, MarketDataProvider, ProviderError, ProviderResult, get_json, ms,
    parse_binance_klines, post_json, sec,
)


class BinanceSpot(MarketDataProvider):
    id = "binance_spot"
    min_interval_s = 60
    BASE = "https://api.binance.com/api/v3"

    async def fetch(self, client: httpx.AsyncClient, now: datetime) -> ProviderResult:
        res = self.result(now)
        def kl(iv: str, n: int):
            return get_json(client, f"{self.BASE}/klines", params={"symbol": "BTCUSDT", "interval": iv, "limit": n})
        h1, d1, m5, m15, px = await asyncio.gather(kl("1h", 1000), kl("1d", 400), kl("5m", 500), kl("15m", 500),
                                                   get_json(client, f"{self.BASE}/ticker/price", params={"symbol": "BTCUSDT"}))
        res.candles["spot_1h"] = parse_binance_klines(h1)
        res.candles["spot_1d"] = parse_binance_klines(d1)
        res.candles["spot_5m"] = parse_binance_klines(m5)
        res.candles["spot_15m"] = parse_binance_klines(m15)
        res.scalar("BTC_PRICE_USDT", float(px["price"]), now)
        return res


class BinanceFutures(DerivativesProvider):
    id = "binance_futures"
    min_interval_s = 60
    BASE = "https://fapi.binance.com"

    async def fetch(self, client: httpx.AsyncClient, now: datetime) -> ProviderResult:
        res = self.result(now)
        p = {"symbol": "BTCUSDT", "period": "1h"}
        prem, oi_hist, ls, top, taker, kl, fhist = await asyncio.gather(
            get_json(client, f"{self.BASE}/fapi/v1/premiumIndex", params={"symbol": "BTCUSDT"}),
            get_json(client, f"{self.BASE}/futures/data/openInterestHist", params={**p, "limit": 48}),
            get_json(client, f"{self.BASE}/futures/data/globalLongShortAccountRatio", params={**p, "limit": 2}),
            get_json(client, f"{self.BASE}/futures/data/topLongShortPositionRatio", params={**p, "limit": 2}),
            get_json(client, f"{self.BASE}/futures/data/takerlongshortRatio", params={**p, "limit": 48}),
            get_json(client, f"{self.BASE}/fapi/v1/klines", params={"symbol": "BTCUSDT", "interval": "1h", "limit": 200}),
            get_json(client, f"{self.BASE}/fapi/v1/fundingRate", params={"symbol": "BTCUSDT", "limit": 90}),
        )
        res.add_series("BTC_FUNDING_RATE_HIST", [(ms(r["fundingTime"]), float(r["fundingRate"])) for r in fhist])
        ts = ms(prem["time"]) if prem.get("time") else now
        # Binance BTCUSDT funds every 8h, so lastFundingRate is already an 8h rate.
        res.scalar("BTC_FUNDING_RATE_8H", float(prem["lastFundingRate"]), ts, mark=float(prem["markPrice"]))
        pts = [(ms(r["timestamp"]), float(r["sumOpenInterest"])) for r in oi_hist]
        res.add_series("BTC_OPEN_INTEREST_BTC", pts)
        if oi_hist:
            res.scalar("BTC_OPEN_INTEREST_USD", float(oi_hist[-1]["sumOpenInterestValue"]), ms(oi_hist[-1]["timestamp"]))
        if ls:
            res.scalar("BTC_LONG_SHORT_ACCOUNT_RATIO", float(ls[-1]["longShortRatio"]), ms(ls[-1]["timestamp"]))
        if top:
            res.scalar("BTC_TOP_TRADER_LS_RATIO", float(top[-1]["longShortRatio"]), ms(top[-1]["timestamp"]))
        res.add_series("BTC_PERP_TAKER_BUY_SELL_RATIO", [(ms(r["timestamp"]), float(r["buySellRatio"])) for r in taker])
        res.candles["perp_1h"] = parse_binance_klines(kl)
        return res


class Bybit(DerivativesProvider):
    id = "bybit"
    min_interval_s = 60
    BASE = "https://api.bybit.com/v5/market"

    @staticmethod
    def _ok(payload: dict) -> dict:
        if payload.get("retCode") != 0:
            raise ProviderError(f"bybit retCode={payload.get('retCode')} {payload.get('retMsg')}")
        return payload["result"]

    async def fetch(self, client: httpx.AsyncClient, now: datetime) -> ProviderResult:
        res = self.result(now)
        q = {"category": "linear", "symbol": "BTCUSDT"}
        tick, oi, ratio = await asyncio.gather(
            get_json(client, f"{self.BASE}/tickers", params=q),
            get_json(client, f"{self.BASE}/open-interest", params={**q, "intervalTime": "1h", "limit": 48}),
            get_json(client, f"{self.BASE}/account-ratio", params={**q, "period": "1h", "limit": 1}),
        )
        t = self._ok(tick)["list"][0]
        ts = ms(tick.get("time", now.timestamp() * 1000))
        res.scalar("BTC_PRICE_USDT", float(t["lastPrice"]), ts)
        res.scalar("BTC_FUNDING_RATE_8H", float(t["fundingRate"]), ts)  # BTCUSDT funds every 8h
        res.scalar("BTC_OPEN_INTEREST_USD", float(t["openInterestValue"]), ts)
        pts = [(ms(r["timestamp"]), float(r["openInterest"])) for r in self._ok(oi)["list"]]
        res.add_series("BTC_OPEN_INTEREST_BTC", pts)  # linear OI is quoted in BTC
        rl = self._ok(ratio)["list"]
        if rl and float(rl[0]["sellRatio"]) > 0:
            res.scalar("BTC_LONG_SHORT_ACCOUNT_RATIO", float(rl[0]["buyRatio"]) / float(rl[0]["sellRatio"]),
                       ms(rl[0]["timestamp"]))
        return res


class OKX(DerivativesProvider):
    id = "okx"
    min_interval_s = 60
    BASE = "https://www.okx.com/api/v5"

    @staticmethod
    def _data(payload: dict) -> list:
        if str(payload.get("code")) != "0":
            raise ProviderError(f"okx code={payload.get('code')} {payload.get('msg')}")
        return payload["data"]

    async def fetch(self, client: httpx.AsyncClient, now: datetime) -> ProviderResult:
        res = self.result(now)
        inst = "BTC-USDT-SWAP"
        fr, oi, tk = await asyncio.gather(
            get_json(client, f"{self.BASE}/public/funding-rate", params={"instId": inst}),
            get_json(client, f"{self.BASE}/public/open-interest", params={"instType": "SWAP", "instId": inst}),
            get_json(client, f"{self.BASE}/market/ticker", params={"instId": "BTC-USDT"}),
        )
        f = self._data(fr)[0]
        res.scalar("BTC_FUNDING_RATE_8H", float(f["fundingRate"]), ms(f.get("ts") or now.timestamp() * 1000))
        o = self._data(oi)[0]
        # OKX is a single snapshot; the store turns snapshots into a series over time.
        res.scalar("BTC_OPEN_INTEREST_BTC", float(o["oiCcy"]), ms(o["ts"]))
        if o.get("oiUsd"):
            res.scalar("BTC_OPEN_INTEREST_USD", float(o["oiUsd"]), ms(o["ts"]))
        t = self._data(tk)[0]
        res.scalar("BTC_PRICE_USDT", float(t["last"]), ms(t["ts"]))
        return res


class Hyperliquid(DerivativesProvider):
    id = "hyperliquid"
    min_interval_s = 60
    URL = "https://api.hyperliquid.xyz/info"

    async def fetch(self, client: httpx.AsyncClient, now: datetime) -> ProviderResult:
        res = self.result(now)
        meta, ctxs = await post_json(client, self.URL, {"type": "metaAndAssetCtxs"})
        idx = next((i for i, a in enumerate(meta["universe"]) if a["name"] == "BTC"), None)
        if idx is None:
            raise ProviderError("BTC not in Hyperliquid universe")
        c = ctxs[idx]
        # Hyperliquid funding is hourly; ×8 normalizes to the canonical 8h rate.
        res.scalar("BTC_FUNDING_RATE_8H", float(c["funding"]) * 8, now, raw_hourly=float(c["funding"]))
        res.scalar("BTC_OPEN_INTEREST_BTC", float(c["openInterest"]), now)
        res.scalar("BTC_OPEN_INTEREST_USD", float(c["openInterest"]) * float(c["markPx"]), now)
        return res


class Coinbase(MarketDataProvider):
    id = "coinbase"
    min_interval_s = 60
    BASE = "https://api.exchange.coinbase.com/products/BTC-USD"

    async def fetch(self, client: httpx.AsyncClient, now: datetime) -> ProviderResult:
        res = self.result(now)
        tick, c1, c2 = await asyncio.gather(
            get_json(client, f"{self.BASE}/ticker"),
            get_json(client, f"{self.BASE}/candles", params={"granularity": 3600}),
            get_json(client, f"{self.BASE}/candles", params={
                "granularity": 3600,
                "start": (now - timedelta(hours=600)).isoformat(),
                "end": (now - timedelta(hours=300)).isoformat()}),
        )
        res.scalar("BTC_PRICE_USD", float(tick["price"]), now)
        bid, ask = float(tick.get("bid") or 0), float(tick.get("ask") or 0)
        if bid > 0 and ask >= bid:
            res.scalar("BTC_SPREAD_BPS", (ask - bid) / ((ask + bid) / 2) * 1e4, now)
        res.candles["coinbase_1h"] = parse_coinbase_candles(list(c1) + list(c2))
        return res


def parse_coinbase_candles(rows: list) -> list[Candle]:
    seen: dict[float, Candle] = {}
    for r in rows:  # [time, low, high, open, close, volume]
        seen[float(r[0])] = Candle(sec(r[0]), float(r[3]), float(r[2]), float(r[1]), float(r[4]), float(r[5]))
    return [seen[k] for k in sorted(seen)]


class Kraken(MarketDataProvider):
    id = "kraken"
    min_interval_s = 60

    async def fetch(self, client: httpx.AsyncClient, now: datetime) -> ProviderResult:
        res = self.result(now)
        data = await get_json(client, "https://api.kraken.com/0/public/Ticker", params={"pair": "XBTUSD,USDTZUSD"})
        if data.get("error"):
            raise ProviderError(f"kraken: {data['error']}")
        r = data["result"]
        if "XXBTZUSD" in r:
            res.scalar("BTC_PRICE_USD", float(r["XXBTZUSD"]["c"][0]), now)
        if "USDTZUSD" in r:
            res.scalar("USDT_USD", float(r["USDTZUSD"]["c"][0]), now)
        return res


class Deribit(MarketDataProvider):
    id = "deribit"
    min_interval_s = 120
    BASE = "https://www.deribit.com/api/v2/public"

    async def fetch(self, client: httpx.AsyncClient, now: datetime) -> ProviderResult:
        res = self.result(now)
        end = int(now.timestamp() * 1000)
        start = end - 8 * 86400 * 1000
        dvol, idx = await asyncio.gather(
            get_json(client, f"{self.BASE}/get_volatility_index_data", params={
                "currency": "BTC", "resolution": 3600, "start_timestamp": start, "end_timestamp": end}),
            get_json(client, f"{self.BASE}/get_index_price", params={"index_name": "btc_usd"}),
        )
        res.add_series("BTC_DVOL", [(ms(r[0]), float(r[4])) for r in dvol["result"]["data"]])
        res.scalar("BTC_PRICE_USD", float(idx["result"]["index_price"]), now)
        return res
