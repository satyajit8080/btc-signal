"""Historical data for the backtester — only sources with genuine free history.

Engines with no free history (whales, ETF, exchange netflow, liquidations, smart money,
network fee state) are simply absent in the replay; results are labelled as a
partial-engine backtest so nobody mistakes them for the full live engine.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

import httpx

from app.core.context import Datum, MarketContext, Series
from app.providers.base import get_json, ms, parse_binance_klines, post_json, sec  # noqa: F401
from app.providers.exchanges import parse_coinbase_candles
from app.providers.public_data import CoinMetrics, fetch_coinmetrics, parse_coinmetrics, parse_yahoo_chart

BACKTEST_ENGINES = ("trend", "structure", "momentum", "spot_flow", "cvd", "volume", "vwap", "open_interest", "funding",
                    "exchange_flow", "volatility", "stablecoin", "macro", "cross_market", "onchain", "network")


async def _binance_klines(client, url: str, interval_ms: int, start: datetime, end: datetime, interval: str):
    out, cur = [], int(start.timestamp() * 1000)
    stop = int(end.timestamp() * 1000)
    while cur < stop:
        rows = await get_json(client, url, params={"symbol": "BTCUSDT", "interval": interval, "startTime": cur,
                                                   "endTime": stop, "limit": 1000})
        if not rows:
            break
        out += rows
        cur = rows[-1][0] + interval_ms
    return parse_binance_klines(out)


async def _funding(client, start: datetime, end: datetime) -> list[tuple[datetime, float]]:
    out, cur = [], int(start.timestamp() * 1000)
    while cur < end.timestamp() * 1000:
        rows = await get_json(client, "https://fapi.binance.com/fapi/v1/fundingRate",
                              params={"symbol": "BTCUSDT", "startTime": cur, "limit": 1000})
        if not rows:
            break
        out += [(ms(r["fundingTime"]), float(r["fundingRate"])) for r in rows]
        cur = rows[-1]["fundingTime"] + 1
        if len(rows) < 1000:
            break
    return out


async def _bybit_oi(client, start: datetime, end: datetime) -> list[tuple[datetime, float]]:
    out, cur_end = [], int(end.timestamp() * 1000)
    s = int(start.timestamp() * 1000)
    while cur_end > s:
        d = await get_json(client, "https://api.bybit.com/v5/market/open-interest", params={
            "category": "linear", "symbol": "BTCUSDT", "intervalTime": "1h", "startTime": s, "endTime": cur_end, "limit": 200})
        rows = (d.get("result") or {}).get("list") or []
        if not rows:
            break
        out += [(ms(r["timestamp"]), float(r["openInterest"])) for r in rows]
        cur_end = min(int(r["timestamp"]) for r in rows) - 1
        if len(rows) < 200:
            break
    return sorted(set(out))


async def _coinbase(client, start: datetime, end: datetime):
    rows, cur = [], start
    while cur < end:
        nxt = min(cur + timedelta(hours=300), end)
        rows += await get_json(client, "https://api.exchange.coinbase.com/products/BTC-USD/candles",
                               params={"granularity": 3600, "start": cur.isoformat(), "end": nxt.isoformat()})
        cur = nxt
        await asyncio.sleep(0.15)  # public rate limit
    return parse_coinbase_candles(rows)


async def _dvol(client, start: datetime, end: datetime) -> list[tuple[datetime, float]]:
    out, cur_end = [], int(end.timestamp() * 1000)
    s = int(start.timestamp() * 1000)
    for _ in range(40):
        d = await get_json(client, "https://www.deribit.com/api/v2/public/get_volatility_index_data", params={
            "currency": "BTC", "resolution": 3600, "start_timestamp": s, "end_timestamp": cur_end})
        r = d["result"]
        out += [(ms(x[0]), float(x[4])) for x in r["data"]]
        if not r.get("continuation") or r["continuation"] <= s:
            break
        cur_end = r["continuation"]
    return sorted(set(out))


async def fetch_history(days: int, end: datetime | None = None, timeout: float = 30) -> MarketContext:
    end = end or datetime.now(timezone.utc).replace(minute=0, second=0, microsecond=0)
    start = end - timedelta(days=days)
    warm = start - timedelta(days=45)  # indicator warm-up (≥1000 1h bars)
    ctx = MarketContext(now=end, mode="backtest")
    async with httpx.AsyncClient(timeout=timeout, headers={"User-Agent": "btc-signal-backtest/1.0"}) as c:
        spot1h, spot1d, perp1h, funding, oi, cb, dvol, fng, stables, tvl, macro, chain, m5, m15, cm = await asyncio.gather(
            _binance_klines(c, "https://api.binance.com/api/v3/klines", 3_600_000, warm, end, "1h"),
            _binance_klines(c, "https://api.binance.com/api/v3/klines", 86_400_000, warm - timedelta(days=400), end, "1d"),
            _binance_klines(c, "https://fapi.binance.com/fapi/v1/klines", 3_600_000, warm, end, "1h"),
            _funding(c, warm, end), _bybit_oi(c, warm, end), _coinbase(c, warm, end), _dvol(c, warm, end),
            get_json(c, "https://api.alternative.me/fng/", params={"limit": 0}),
            get_json(c, "https://stablecoins.llama.fi/stablecoincharts/all"),
            get_json(c, "https://api.llama.fi/v2/historicalChainTvl"),
            asyncio.gather(*[get_json(c, f"https://query1.finance.yahoo.com/v8/finance/chart/{s}",
                                      params={"range": "2y", "interval": "1d"}) for s in ("DX-Y.NYB", "^TNX", "^GSPC", "^VIX")],
                           return_exceptions=True),
            asyncio.gather(*[get_json(c, f"https://api.blockchain.info/charts/{n}",
                                      params={"timespan": "2years", "format": "json", "sampled": "false"})
                             for n in ("hash-rate", "n-transactions", "estimated-transaction-volume-usd", "difficulty")],
                           return_exceptions=True),
            _binance_klines(c, "https://api.binance.com/api/v3/klines", 300_000, start - timedelta(days=2), end, "5m"),
            _binance_klines(c, "https://api.binance.com/api/v3/klines", 900_000, start - timedelta(days=6), end, "15m"),
            fetch_coinmetrics(c, CoinMetrics.URL, list(CoinMetrics.METRICS), end, start=start - timedelta(days=4 * 365)),
        )
    ctx.set_candles("spot_1h", spot1h, "binance_spot")
    ctx.set_candles("spot_1d", spot1d, "binance_spot")
    ctx.set_candles("perp_1h", perp1h, "binance_futures")
    ctx.set_candles("coinbase_1h", cb, "coinbase")
    ctx.set_candles("spot_5m", m5, "binance_spot")
    ctx.set_candles("spot_15m", m15, "binance_spot")
    for metric, pts in parse_coinmetrics(cm, CoinMetrics.METRICS).items():  # already shifted to day close
        ctx.add_series(metric, Series("coinmetrics", pts))
    ctx.add_series("BACKTEST_FUNDING", Series("binance_futures", funding))
    ctx.add_series("BTC_OPEN_INTEREST_BTC", Series("bybit", oi))
    ctx.add_series("BTC_DVOL", Series("deribit", dvol))
    # Daily series: a day's value is only known once that day has closed.
    day = timedelta(days=1)
    ctx.add_series("FEAR_GREED", Series("alternative_me", [(sec(r["timestamp"]) + day, float(r["value"])) for r in fng["data"]]))
    ctx.add_series("STABLECOIN_SUPPLY_USD", Series("defillama:stablecoins", [
        (sec(r["date"]) + day, float(r["totalCirculatingUSD"]["peggedUSD"])) for r in stables
        if r.get("totalCirculatingUSD", {}).get("peggedUSD")]))
    ctx.add_series("DEFI_TVL_USD", Series("defillama:tvl", [(sec(r["date"]) + day, float(r["tvl"])) for r in tvl]))
    for sym, metric, p in zip(("DX-Y.NYB", "^TNX", "^GSPC", "^VIX"), ("MACRO_DXY", "MACRO_US10Y", "MACRO_SPX", "MACRO_VIX"), macro):
        if not isinstance(p, Exception):
            ctx.add_series(metric, Series(f"yahoo:{sym}", [(t + day, v) for t, v in parse_yahoo_chart(p)]))
    for metric, p in zip(("BTC_HASHRATE", "BTC_TX_COUNT", "BTC_TX_VOLUME_USD", "BTC_DIFFICULTY"), chain):
        if not isinstance(p, Exception):
            ctx.add_series(metric, Series("blockchain_info", [(sec(v["x"]) + day, float(v["y"])) for v in p["values"]]))
    return ctx


LIVE_DEPTH = {"spot_1h": 1000, "spot_1d": 400, "perp_1h": 200, "coinbase_1h": 600, "spot_5m": 500, "spot_15m": 500}


def step_context(full: MarketContext, t: datetime) -> MarketContext:
    """The world as known at t, trimmed to the same depth the live adapters fetch."""
    ctx = full.cut(t)
    for k, n in LIVE_DEPTH.items():
        if k in ctx.candles:
            ctx.candles[k] = ctx.candles[k][-n:]
    for key in ("BTC_OPEN_INTEREST_BTC", "BTC_DVOL"):
        for p, s in list(ctx.series.get(key, {}).items()):
            s.points = s.points[-200:]
    f = ctx.series.pop("BACKTEST_FUNDING", {}).get("binance_futures")
    if f and f.points:
        # live reads the last settled rate stamped "now"; mirror that for parity
        ctx.add_scalar("BTC_FUNDING_RATE_8H", Datum(f.points[-1][1], "binance_futures", t))
        ctx.add_series("BTC_FUNDING_RATE_HIST", Series("binance_futures", f.points[-90:]))
    c = ctx.candles.get("spot_1h")
    if c:
        ctx.add_scalar("BTC_PRICE_USDT", Datum(c[-1].close, "binance_spot", c[-1].ts + timedelta(hours=1)))
    return ctx
