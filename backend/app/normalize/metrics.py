"""Canonical metric registry.

Providers report the same thing in different shapes (CoinGlass/Bybit/Binance OI, funding
per 1h vs 8h, ...). Adapters convert to these canonical names and units; everything
downstream speaks only this vocabulary.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class MetricSpec:
    name: str
    category: str
    unit: str
    expected_interval_s: int  # age at which freshness starts to decay
    max_age_s: int  # age at which freshness reaches 0 (data treated as stale)
    critical: bool = False
    tolerance: float = 0.0  # relative disagreement tolerated between providers
    track_history: bool = False  # persist scalar snapshots to build our own series


H = 3600
D = 86400

METRICS: dict[str, MetricSpec] = {m.name: m for m in (
    MetricSpec("BTC_PRICE_USD", "price", "USD", 120, 900, critical=True, tolerance=0.004),
    MetricSpec("BTC_PRICE_USDT", "price", "USDT", 120, 900, critical=True, tolerance=0.004),
    MetricSpec("USDT_USD", "price", "USD", 600, 3 * H, tolerance=0.003),
    MetricSpec("CANDLES_SPOT_1H", "candles", "OHLCV", 2 * H, 4 * H, critical=True),
    MetricSpec("CANDLES_SPOT_1D", "candles", "OHLCV", 2 * D, 3 * D),
    MetricSpec("CANDLES_SPOT_5M", "candles", "OHLCV", 600, 1800),
    MetricSpec("CANDLES_SPOT_15M", "candles", "OHLCV", 1800, 3600),
    MetricSpec("CANDLES_PERP_1H", "candles", "OHLCV", 2 * H, 4 * H),
    MetricSpec("CANDLES_COINBASE_1H", "candles", "OHLCV", 2 * H, 4 * H),
    MetricSpec("BTC_OPEN_INTEREST_BTC", "derivatives", "BTC", 2 * H, 6 * H, critical=True,
               tolerance=0.6, track_history=True),
    MetricSpec("BTC_OPEN_INTEREST_USD", "derivatives", "USD", 600, 2 * H),
    MetricSpec("BTC_OPEN_INTEREST_USD_HIST", "derivatives", "USD", 2 * H, 6 * H),
    MetricSpec("BTC_FUNDING_RATE_8H", "derivatives", "fraction/8h", 30 * 60, 9 * H, critical=True,
               tolerance=2.0),
    MetricSpec("BTC_FUNDING_RATE_HIST", "derivatives", "fraction/8h", 9 * H, 2 * D),
    MetricSpec("BTC_SPREAD_BPS", "liquidity", "bps", 300, 1800),
    MetricSpec("BTC_LONG_SHORT_ACCOUNT_RATIO", "derivatives", "ratio", 2 * H, 6 * H, tolerance=0.5),
    MetricSpec("BTC_TOP_TRADER_LS_RATIO", "derivatives", "ratio", 2 * H, 6 * H),
    MetricSpec("BTC_PERP_TAKER_BUY_SELL_RATIO", "derivatives", "ratio", 2 * H, 6 * H),
    MetricSpec("BTC_DVOL", "volatility", "vol pts", 2 * H, 6 * H),
    MetricSpec("BTC_FEE_FAST_SATVB", "network", "sat/vB", 600, 2 * H),
    MetricSpec("BTC_FEE_HOUR_SATVB", "network", "sat/vB", 600, 2 * H),
    MetricSpec("BTC_MEMPOOL_VSIZE", "network", "vB", 600, 2 * H),
    MetricSpec("BTC_MEMPOOL_COUNT", "network", "tx", 600, 2 * H),
    MetricSpec("BTC_BLOCK_HEIGHT", "network", "height", 1800, 3 * H),
    MetricSpec("BTC_HASHRATE", "network", "TH/s", 2 * D, 4 * D),
    MetricSpec("BTC_TX_COUNT", "onchain", "tx/day", 2 * D, 4 * D),
    MetricSpec("BTC_TX_VOLUME_USD", "onchain", "USD/day", 2 * D, 4 * D),
    MetricSpec("STABLECOIN_SUPPLY_USD", "liquidity", "USD", 2 * D, 4 * D),
    MetricSpec("STABLECOIN_ETH_SUPPLY_USD", "liquidity", "USD", H, 6 * H, track_history=True),
    MetricSpec("DEFI_TVL_USD", "liquidity", "USD", 2 * D, 4 * D),
    MetricSpec("MACRO_DXY", "macro", "index", 3 * D, 5 * D),
    MetricSpec("MACRO_US10Y", "macro", "pct", 3 * D, 5 * D),
    MetricSpec("MACRO_SPX", "macro", "index", 3 * D, 5 * D),
    MetricSpec("MACRO_VIX", "macro", "index", 3 * D, 5 * D),
    MetricSpec("FEAR_GREED", "sentiment", "0-100", 2 * D, 3 * D),
    MetricSpec("BTC_DOMINANCE", "sentiment", "pct", H, 6 * H),
    MetricSpec("CRYPTO_MCAP_CHANGE_24H", "sentiment", "pct", H, 6 * H),
    MetricSpec("WBTC_DEX_VOLUME_24H_USD", "liquidity", "USD", H, 6 * H, tolerance=0.5),
    MetricSpec("WBTC_DEX_LIQUIDITY_USD", "liquidity", "USD", H, 6 * H, tolerance=0.5),
    MetricSpec("ETF_NET_FLOW_USD", "etf", "USD/day", 2 * D, 4 * D),
    MetricSpec("EXCHANGE_NETFLOW_BTC", "flows", "BTC/day", 2 * D, 4 * D),
    MetricSpec("EXCHANGE_NETFLOW_BTC_1H", "flows", "BTC/h", 2 * H, 6 * H),
    MetricSpec("EXCHANGE_INFLOW_BTC", "flows", "BTC/day", 2 * D, 4 * D),
    MetricSpec("EXCHANGE_OUTFLOW_BTC", "flows", "BTC/day", 2 * D, 4 * D),
    MetricSpec("EXCHANGE_RESERVE_BTC", "onchain", "BTC", 2 * D, 4 * D),
    MetricSpec("BTC_MVRV", "onchain", "ratio", 2 * D, 4 * D),
    MetricSpec("BTC_SOPR", "onchain", "ratio", 2 * D, 4 * D),
    MetricSpec("BTC_ACTIVE_ADDRESSES", "onchain", "count/day", 2 * D, 4 * D),
    MetricSpec("BTC_LTH_SUPPLY", "onchain", "BTC", 2 * D, 4 * D),
    MetricSpec("BTC_DIFFICULTY", "network", "difficulty", 3 * D, 6 * D),
    MetricSpec("SMART_MONEY_NET_BTC", "positioning", "BTC", 3600, 3 * H, track_history=True),
    MetricSpec("SMART_MONEY_GROSS_BTC", "positioning", "BTC", 3600, 3 * H),
    MetricSpec("WHALE_TRANSFERS", "positioning", "events", 1800, 6 * H),
    MetricSpec("LIQUIDATIONS", "positioning", "events", 600, 2 * H),
    MetricSpec("LIQ_STREAM_UPTIME_S", "positioning", "s", 300, 900),
)}


def spec(name: str) -> MetricSpec:
    return METRICS.get(name) or MetricSpec(name, "other", "", 3600, 6 * 3600)


def freshness(age_s: float, metric: str) -> float:
    """1.0 while age ≤ expected interval, linear decay to 0 at max_age."""
    s = spec(metric)
    if age_s <= s.expected_interval_s:
        return 1.0
    if age_s >= s.max_age_s:
        return 0.0
    return 1.0 - (age_s - s.expected_interval_s) / (s.max_age_s - s.expected_interval_s)
