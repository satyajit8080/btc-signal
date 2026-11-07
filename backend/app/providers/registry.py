"""Builds adapter instances. Swapping a provider = changing this mapping."""
from __future__ import annotations

from app.config import Settings
from app.providers import exchanges, keyed, public_data
from app.providers.base import Provider

ADAPTERS: dict[str, type[Provider]] = {
    "binance_spot": exchanges.BinanceSpot,
    "binance_futures": exchanges.BinanceFutures,
    "bybit": exchanges.Bybit,
    "okx": exchanges.OKX,
    "hyperliquid": exchanges.Hyperliquid,
    "coinbase": exchanges.Coinbase,
    "kraken": exchanges.Kraken,
    "deribit": exchanges.Deribit,
    "esplora": public_data.Esplora,
    "blockchain_info": public_data.BlockchainInfo,
    "defillama": public_data.DefiLlama,
    "alternative_me": public_data.AlternativeMe,
    "coingecko": public_data.CoinGecko,
    "yahoo": public_data.YahooMacro,
    "dexscreener": public_data.DexScreener,
    "coinmetrics": public_data.CoinMetrics,
    "geckoterminal": public_data.GeckoTerminal,
    "hyperliquid_smart_money": public_data.HyperliquidSmartMoney,
    "coinglass": keyed.CoinGlass,
    "whale_alert": keyed.WhaleAlert,
    "cryptoquant": keyed.CryptoQuant,
    "glassnode": keyed.Glassnode,
    "etherscan": keyed.Etherscan,
    "dune": keyed.Dune,
    "farside": keyed.Farside,
}


def build_providers(settings: Settings, *, etf_loader=None, liq_stream=None) -> dict[str, Provider]:
    out: dict[str, Provider] = {aid: cls(settings) for aid, cls in ADAPTERS.items()}
    out["manual_etf"] = keyed.ManualETF(settings, etf_loader)
    out["liquidation_stream"] = keyed.LiquidationStreamProvider(settings, liq_stream)
    return out
