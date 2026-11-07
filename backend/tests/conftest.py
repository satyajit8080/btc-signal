"""Deterministic synthetic market scenarios for engine/decision tests.

These are test fixtures only — the product never uses synthetic data.
"""
from __future__ import annotations

import random
from datetime import datetime, timedelta, timezone

import pytest

from app.core.context import Candle, Datum, MarketContext, Series

NOW = datetime(2026, 10, 6, 12, 0, tzinfo=timezone.utc)
H = timedelta(hours=1)
D = timedelta(days=1)


def make_candles(n: int, step: timedelta, end_open: datetime, start_price: float, drift: float, vol: float,
                 taker_bias: list[float] | float = 0.0, volume: float = 1000.0, volume_tail: tuple[int, float] = (0, 1.0),
                 seed: int = 1) -> list[Candle]:
    rnd = random.Random(seed)
    out, price = [], start_price
    for i in range(n):
        ts = end_open - step * (n - 1 - i)
        o = price
        c = o * (1 + drift + rnd.gauss(0, vol))
        hi = max(o, c) * (1 + abs(rnd.gauss(0, vol / 2)))
        lo = min(o, c) * (1 - abs(rnd.gauss(0, vol / 2)))
        v = volume * (volume_tail[1] if i >= n - volume_tail[0] else 1.0) * (1 + rnd.random() * 0.1)
        bias = taker_bias[i] if isinstance(taker_bias, list) else taker_bias
        out.append(Candle(ts, o, hi, lo, c, v, v * (0.5 + bias)))
        price = c
    return out


def series(provider: str, start_value: float, step_pct: float, n: int, step: timedelta, end: datetime) -> Series:
    pts, v = [], start_value
    for i in range(n):
        pts.append((end - step * (n - 1 - i), v))
        v *= 1 + step_pct / 100
    return Series(provider, pts)


def build_ctx(direction: int = 1, *, now: datetime = NOW, funding: float | None = None, ls_ratio: float | None = None,
              with_intel: bool = True) -> MarketContext:
    """direction: +1 bullish world, -1 bearish world, 0 sideways."""
    d = direction
    ctx = MarketContext(now=now)
    last_open = now - H
    tail = [0.0] * 976 + [0.07 * d] * 24
    spot = make_candles(1000, H, last_open, 60000, 0.0006 * d, 0.003, taker_bias=tail, volume_tail=(24, 1.6 if d else 1.0))
    ctx.set_candles("spot_1h", spot, "binance_spot")
    daily = make_candles(400, D, now.replace(hour=0) - D, 40000, 0.004 * d, 0.01, seed=2)
    scale = spot[-1].close / daily[-1].close
    ctx.set_candles("spot_1d", [Candle(c.ts, c.open * scale, c.high * scale, c.low * scale, c.close * scale, c.volume,
                                       c.taker_buy_volume) for c in daily], "binance_spot")
    ctx.set_candles("perp_1h", make_candles(200, H, last_open, spot[-200].open, 0.0006 * d, 0.003,
                                            taker_bias=[0.0] * 176 + [0.05 * d] * 24, seed=3), "binance_futures")
    prem = [0.0] * 596 + [0.0025 * d] * 4
    ctx.set_candles("coinbase_1h", [Candle(c.ts, c.open, c.high, c.low, c.close * (1 + p), c.volume)
                                    for c, p in zip(spot[-600:], prem)], "coinbase")
    price = spot[-1].close
    for p, off in (("coinbase", 1.0002), ("kraken", 0.9999), ("deribit", 1.0)):
        ctx.add_scalar("BTC_PRICE_USD", Datum(price * off, p, now))
    ctx.add_series("BTC_OPEN_INTEREST_BTC", series("binance_futures", 90000, 0.3 if d else 0.0, 48, H, now - H))
    ctx.add_series("BTC_OPEN_INTEREST_BTC", series("bybit", 55000, 0.28 if d else 0.0, 48, H, now - H))
    f = funding if funding is not None else (0.00008 if d > 0 else -0.00005 if d < 0 else 0.0001)
    for p in ("binance_futures", "bybit", "hyperliquid"):
        ctx.add_scalar("BTC_FUNDING_RATE_8H", Datum(f, p, now))
    lsr = ls_ratio if ls_ratio is not None else (1.2 if d >= 0 else 0.9)
    ctx.add_scalar("BTC_LONG_SHORT_ACCOUNT_RATIO", Datum(lsr, "binance_futures", now - H))
    ctx.add_series("BTC_DVOL", series("deribit", 50, -0.1 * d, 200, H, now - H))
    ctx.add_series("STABLECOIN_SUPPLY_USD", series("defillama:stablecoins", 300e9, 0.2 * d, 120, D, now - 12 * H))
    ctx.add_series("DEFI_TVL_USD", series("defillama:tvl", 100e9, 0.3 * d, 120, D, now - 12 * H))
    ctx.add_series("MACRO_DXY", series("yahoo:DX-Y.NYB", 100, -0.15 * d, 60, D, now - 20 * H))
    ctx.add_series("MACRO_US10Y", series("yahoo:^TNX", 4.2, -0.3 * d, 60, D, now - 20 * H))
    ctx.add_series("MACRO_SPX", series("yahoo:^GSPC", 6000, 0.3 * d, 60, D, now - 20 * H))
    ctx.add_series("MACRO_VIX", Series("yahoo:^VIX", [(now - 20 * H, 14.0 if d > 0 else 27.0 if d < 0 else 19.0)]))
    ctx.add_series("FEAR_GREED", Series("alternative_me", [(now - 10 * H, 55.0)]))
    ctx.add_scalar("CRYPTO_MCAP_CHANGE_24H", Datum(2.0 * d, "coingecko", now))
    ctx.add_series("BTC_TX_COUNT", series("blockchain_info", 500000, 0.3 * d, 60, D, now - 20 * H))
    ctx.add_series("BTC_TX_VOLUME_USD", series("blockchain_info", 8e9, 0.5 * d, 60, D, now - 20 * H))
    ctx.add_series("BTC_HASHRATE", series("blockchain_info", 9e8, 0.2 * d, 60, D, now - 20 * H))
    ctx.add_scalar("BTC_FEE_FAST_SATVB", Datum(4.0, "esplora", now, {"host": "mempool.space"}))
    ctx.add_scalar("BTC_MEMPOOL_VSIZE", Datum(30e6, "esplora", now))
    if with_intel:
        wd, dep = (600e6, 100e6) if d > 0 else (100e6, 600e6) if d < 0 else (200e6, 200e6)
        ctx.add_events("whale_transfers", [
            {"ts": now - 2 * H, "usd": wd, "btc": wd / price, "kind": "from_exchange", "from": "binance", "to": ""},
            {"ts": now - 3 * H, "usd": dep, "btc": dep / price, "kind": "to_exchange", "from": "", "to": "coinbase"},
            {"ts": now - 4 * H, "usd": 300e6, "btc": 300e6 / price, "kind": "wallet_to_wallet", "from": "", "to": ""},
        ], "whale_alert")
        ctx.fetched_at["whale_alert"] = now
        ctx.add_series("ETF_NET_FLOW_USD", Series("coinglass", [(now.replace(hour=0) - D * i, 300e6 * d) for i in range(6, 0, -1)]))
        ctx.add_series("EXCHANGE_NETFLOW_BTC", Series("cryptoquant", [(now.replace(hour=0) - D * i, -3000.0 * d + 800.0 * (-1) ** i) for i in range(30, 0, -1)]))
        ls, ss = (5e6, 30e6) if d > 0 else (30e6, 5e6) if d < 0 else (10e6, 10e6)
        ctx.add_events("liquidations", [{"ts": now - 2 * H, "side": "long", "usd": ls, "venue": "binance"},
                                        {"ts": now - 2 * H, "side": "short", "usd": ss, "venue": "bybit"}], "liquidation_stream")
        ctx.add_scalar("LIQ_STREAM_UPTIME_S", Datum(7200, "liquidation_stream", now))
        ctx.fetched_at["liquidation_stream"] = now
        ctx.add_scalar("SMART_MONEY_NET_BTC", Datum(500.0 * d, "hyperliquid_smart_money", now, {"longs": 10, "shorts": 3, "traders": 50}))
        ctx.add_scalar("SMART_MONEY_GROSS_BTC", Datum(800.0, "hyperliquid_smart_money", now))
    return ctx


def relaxed_cfg():
    """Default config with the reward/risk gate relaxed: the synthetic worlds place the previous week's
    high/low close to price, so that gate is tested separately."""
    from app.scoring.config import default_config, merge
    return merge(default_config(), {"trade": {"min_reward_risk": 0.5}})


@pytest.fixture
def bull_ctx() -> MarketContext:
    return build_ctx(1)


@pytest.fixture
def bear_ctx() -> MarketContext:
    return build_ctx(-1)


@pytest.fixture
def flat_ctx() -> MarketContext:
    return build_ctx(0)
