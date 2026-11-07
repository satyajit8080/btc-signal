"""Engine registry and category grouping.

Correlated indicators (EMA, MACD, RSI...) are first combined inside their engine, engines
inside their CATEGORY, and only category scores are weighted into the final score. This
stops three views of the same price move counting as three confirmations.
"""
from __future__ import annotations

from app.engines import chain, derivatives, flows, macro, market

# Context engines run first; directional engines may read ctx.annotations they produce.
CONTEXT_ENGINES = {
    "regime": market.regime,
    "levels": market.levels,
    "divergence": market.divergence,
}

DIRECTIONAL_ENGINES = {
    "trend": market.trend,
    "structure": market.structure,
    "momentum": market.momentum,
    "spot_flow": market.spot_flow,
    "cvd": market.cvd,
    "volume": market.volume,
    "vwap": market.vwap,
    "etf": flows.etf,
    "open_interest": derivatives.open_interest,
    "funding": derivatives.funding,
    "liquidation": derivatives.liquidation,
    "long_short": derivatives.long_short,
    "whale": flows.whale,
    "smart_money": flows.smart_money,
    "exchange_flow": chain.exchange_flow,
    "onchain": chain.onchain,
    "network": chain.network,
    "stablecoin": chain.stablecoin,
    "macro": macro.macro,
    "cross_market": macro.cross_market,
    "volatility": market.volatility,
}

CATEGORIES: dict[str, tuple[str, ...]] = {
    "TREND": ("trend", "structure"),
    "MOMENTUM": ("momentum",),
    "FLOW": ("spot_flow", "cvd", "volume", "vwap", "etf"),
    "DERIVATIVES": ("open_interest", "funding", "liquidation"),
    "POSITIONING": ("long_short", "whale", "smart_money"),
    "ON-CHAIN": ("exchange_flow", "onchain", "network", "stablecoin"),
    "MACRO": ("macro", "cross_market"),
    "VOLATILITY": ("volatility",),
}
ENGINE_CATEGORY = {e: c for c, es in CATEGORIES.items() for e in es}

ENGINE_LABELS = {
    "trend": "Trend (EMA/ADX, multi-timeframe)", "structure": "Market structure", "momentum": "Momentum (RSI/MACD)",
    "spot_flow": "Spot flow (Coinbase premium)", "cvd": "CVD / order flow", "volume": "Volume",
    "vwap": "VWAP", "etf": "ETF flows", "open_interest": "Open interest", "funding": "Funding",
    "liquidation": "Liquidations", "long_short": "Long/short ratio", "whale": "Whales", "smart_money": "Smart money",
    "exchange_flow": "Exchange netflow", "onchain": "On-chain", "network": "Bitcoin network",
    "stablecoin": "Stablecoin liquidity", "macro": "Macro", "cross_market": "Crypto regime", "volatility": "Volatility",
    "regime": "Market regime", "levels": "Support / resistance", "divergence": "Divergences",
}

assert set(ENGINE_CATEGORY) == set(DIRECTIONAL_ENGINES), "every directional engine needs exactly one category"
