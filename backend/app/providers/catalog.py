"""Source catalog — the single source of truth for all 40 reference sources plus the
primary-data providers the engine actually reads.

Every entry records what the source is, whether it has an official API, how it can be
accessed, which BTC metrics it can legitimately contribute, and a default relevance.
`adapter` names the adapter id in `providers.registry`. Sources with no adapter are
catalogued honestly with the reason, and contribute nothing.

Access values:
  free        official public API, no key, verified reachable during build
  key         official API, needs a (possibly free) key — configurable via env
  paid        official API behind a paid plan — configurable via env if implemented
  no_api      no official public API; data only on the website / social posts
  blocked     public page exists but automated access is refused (e.g. Cloudflare)
"""
from __future__ import annotations

from dataclasses import dataclass, field

RELEVANCE = {
    "EXTREMELY HIGH": 1.0,
    "VERY HIGH": 0.9,
    "HIGH": 0.75,
    "MEDIUM/HIGH": 0.6,
    "MEDIUM": 0.45,
    "LOW/MEDIUM": 0.3,
    "LOW": 0.15,
    "VERY LOW": 0.05,
    "NONE": 0.0,
}


@dataclass(frozen=True)
class SourceEntry:
    id: str
    name: str
    url: str
    group: str
    btc_relevance: str
    access: str
    official_api: str
    tier: int
    number: int | None = None  # 1..40 for the reference universe, None for primary-data providers
    adapter: str | None = None
    metrics: tuple[str, ...] = ()
    engines: tuple[str, ...] = ()
    notes: str = ""
    base_reliability: float = 0.8
    directional: bool = True  # False → context only, never moves the BTC score

    @property
    def relevance(self) -> float:
        return RELEVANCE[self.btc_relevance] if self.directional else 0.0


def _s(**kw) -> SourceEntry:
    return SourceEntry(**kw)


REFERENCE_SOURCES: tuple[SourceEntry, ...] = (
    # ---------------- Whale & wallet tracking ----------------
    _s(number=1, id="arkham", name="Arkham", url="https://arkm.com", group="whale",
       btc_relevance="VERY HIGH", access="key", tier=1,
       official_api="Arkham Intel API (api.arkm.com) — requires an approved API key",
       metrics=("ENTITY_TRANSFERS", "EXCHANGE_NETFLOW_BTC"), engines=("whale", "exchange_flow"),
       notes="Adapter not implemented: API access is application-gated and the response schema "
             "could not be verified without a key. Slot reserved in the whale confirmation set."),
    _s(number=2, id="nansen", name="Nansen", url="https://app.nansen.ai", group="whale",
       btc_relevance="HIGH", access="paid", tier=1,
       official_api="Nansen API — paid; smart-money datasets are EVM/Solana-centric",
       metrics=("SMART_MONEY_FLOWS",), engines=("smart_money",),
       notes="Not implemented: paid, and native BTC smart-money coverage is limited."),
    _s(number=3, id="debank", name="DeBank", url="https://debank.com", group="whale",
       btc_relevance="MEDIUM", access="paid", tier=2,
       official_api="DeBank Cloud OpenAPI — paid units; EVM chains only",
       metrics=(), engines=(), directional=False,
       notes="EVM-only. No native BTC. DeFi positioning is not forced into BTC direction."),
    _s(number=4, id="cielo", name="Cielo", url="https://app.cielo.finance", group="whale",
       btc_relevance="MEDIUM/HIGH", access="no_api", tier=2,
       official_api="No documented public API (app + Telegram alerts)",
       notes="No programmatic access found. Contributes nothing."),
    _s(number=5, id="whale_alert", name="Whale Alert", url="https://whale-alert.io", group="whale",
       btc_relevance="VERY HIGH", access="key", tier=1, adapter="whale_alert",
       official_api="Whale Alert REST API v1 — API key",
       metrics=("WHALE_TRANSFERS",), engines=("whale",),
       notes="Transfers classified wallet→exchange / exchange→wallet / wallet→wallet / "
             "exchange→exchange before any meaning is assigned."),
    _s(number=6, id="lookonchain", name="Lookonchain", url="https://lookonchain.com", group="whale",
       btc_relevance="HIGH", access="no_api", tier=1,
       official_api="No API — posts on X and the website feed",
       notes="Social posts are not quantitative truth. Not ingested; corroboration slot only."),
    _s(number=7, id="kolscan", name="Kolscan", url="https://kolscan.io", group="whale",
       btc_relevance="LOW", access="no_api", tier=3, directional=False,
       official_api="No public API (Solana KOL tracker)", notes="Memecoin KOL data; BTC weight 0."),
    _s(number=8, id="fomo", name="FOMO", url="https://fomo.family", group="whale",
       btc_relevance="LOW/MEDIUM", access="no_api", tier=3, directional=False,
       official_api="No public API", notes="No BTC-specific data available. Weight 0."),
    _s(number=9, id="gmgn", name="GMGN", url="https://gmgn.ai", group="whale",
       btc_relevance="LOW", access="no_api", tier=3, directional=False,
       official_api="No official public API (Solana memecoin terminal)", notes="Weight 0."),
    _s(number=10, id="solscan", name="Solscan", url="https://solscan.io", group="whale",
       btc_relevance="VERY LOW", access="key", tier=3, directional=False,
       official_api="Solscan Pro API — key", notes="Solana explorer. BTC directional weight 0."),
    _s(number=11, id="etherscan", name="Etherscan", url="https://etherscan.io", group="whale",
       btc_relevance="MEDIUM", access="key", tier=2, adapter="etherscan",
       official_api="Etherscan API — free key",
       metrics=("STABLECOIN_ETH_SUPPLY_USD",), engines=("stablecoin",),
       notes="Used for USDT/USDC supply on Ethereum as stablecoin-liquidity confirmation. "
             "ETH price is never used as a BTC signal."),
    # ---------------- Pre-trade / risk ----------------
    _s(number=12, id="rugcheck", name="RugCheck", url="https://rugcheck.xyz", group="risk",
       btc_relevance="NONE", access="free", tier=3, directional=False,
       official_api="Public API (Solana token risk)", notes="Token-risk tool. BTC weight 0."),
    _s(number=13, id="bubblemaps", name="Bubblemaps", url="https://v2.bubblemaps.io", group="risk",
       btc_relevance="LOW/MEDIUM", access="paid", tier=3, directional=False,
       official_api="Partner/paid API, token-holder clustering",
       notes="No BTC address clustering available. Weight 0."),
    _s(number=14, id="insightx", name="InsightX", url="https://insightx.network", group="risk",
       btc_relevance="MEDIUM", access="no_api", tier=3, directional=False,
       official_api="No public API found", notes="No BTC coverage found. Weight 0."),
    _s(number=15, id="solsniffer", name="SolSniffer", url="https://solsniffer.com", group="risk",
       btc_relevance="NONE", access="key", tier=3, directional=False,
       official_api="API key (Solana scam scoring)", notes="BTC weight 0."),
    _s(number=16, id="tokensniffer", name="TokenSniffer", url="https://tokensniffer.com", group="risk",
       btc_relevance="NONE", access="paid", tier=3, directional=False,
       official_api="Paid API (EVM token risk)", notes="BTC weight 0."),
    _s(number=17, id="dexscreener", name="DexScreener", url="https://dexscreener.com", group="risk",
       btc_relevance="LOW/MEDIUM", access="free", tier=3, adapter="dexscreener", directional=False,
       official_api="Free public API",
       metrics=("WBTC_DEX_VOLUME_24H_USD", "WBTC_DEX_LIQUIDITY_USD"), engines=("cross_market",),
       notes="WBTC DEX liquidity/volume, reported as context only — not a BTC direction input."),
    _s(number=18, id="birdeye", name="Birdeye", url="https://birdeye.so", group="risk",
       btc_relevance="LOW", access="key", tier=3, directional=False,
       official_api="Birdeye API — key (Solana-centric)", notes="Not implemented; Solana data."),
    _s(number=19, id="geckoterminal", name="GeckoTerminal", url="https://geckoterminal.com", group="risk",
       btc_relevance="LOW/MEDIUM", access="free", tier=3, adapter="geckoterminal", directional=False,
       official_api="Free public API v2",
       metrics=("WBTC_DEX_VOLUME_24H_USD", "WBTC_DEX_LIQUIDITY_USD"), engines=("cross_market",),
       notes="Second WBTC DEX source; cross-checked against DexScreener. Context only."),
    # ---------------- Unlocks / token events ----------------
    _s(number=20, id="tokenomist", name="Tokenomist", url="https://tokenomist.ai", group="unlocks",
       btc_relevance="LOW", access="paid", tier=3, directional=False,
       official_api="Paid API", notes="BTC has no unlocks. No BTC unlock signal is invented."),
    _s(number=21, id="defillama_unlocks", name="DeFiLlama Unlocks", url="https://defillama.com/unlocks",
       group="unlocks", btc_relevance="LOW", access="paid", tier=3, directional=False,
       official_api="Emissions endpoints are on the DeFiLlama Pro API", notes="Weight 0."),
    _s(number=22, id="cryptorank", name="CryptoRank", url="https://cryptorank.io", group="unlocks",
       btc_relevance="MEDIUM", access="key", tier=3,
       official_api="CryptoRank API — key",
       notes="Not implemented: its BTC market data duplicates primary exchange feeds."),
    _s(number=23, id="dropstab", name="DropsTab", url="https://dropstab.com", group="unlocks",
       btc_relevance="LOW", access="paid", tier=3, directional=False,
       official_api="Paid API", notes="Vesting data. Weight 0."),
    # ---------------- Leverage & liquidations ----------------
    _s(number=24, id="coinglass_hyperliquid", name="CoinGlass Hyperliquid",
       url="https://coinglass.com/hyperliquid", group="derivatives", btc_relevance="VERY HIGH",
       access="key", tier=1, adapter="coinglass",
       official_api="CoinGlass API v4 — key",
       metrics=("BTC_OPEN_INTEREST_BTC", "BTC_FUNDING_RATE_8H", "LIQUIDATIONS"),
       engines=("open_interest", "funding", "liquidation"),
       notes="Served by the shared CoinGlass adapter. Hyperliquid positioning is also read "
             "directly from Hyperliquid's free API."),
    _s(number=25, id="hyperliquid_leaderboard", name="Hyperliquid Leaderboard",
       url="https://app.hyperliquid.xyz/leaderboard", group="derivatives", btc_relevance="HIGH",
       access="free", tier=2, adapter="hyperliquid_smart_money",
       official_api="Hyperliquid info API + public leaderboard JSON",
       metrics=("SMART_MONEY_NET_BTC", "SMART_MONEY_GROSS_BTC"), engines=("smart_money",),
       notes="Net BTC position of the top-N monthly-PnL accounts. Profitability is not assumed "
             "to predict direction: engine confidence is capped.", base_reliability=0.75),
    _s(number=26, id="coinglass", name="CoinGlass", url="https://coinglass.com", group="derivatives",
       btc_relevance="VERY HIGH", access="key", tier=1, adapter="coinglass",
       official_api="CoinGlass API v4 — key (heatmap on higher plans)",
       metrics=("BTC_OPEN_INTEREST_BTC", "BTC_FUNDING_RATE_8H", "LIQUIDATIONS", "ETF_NET_FLOW_USD"),
       engines=("open_interest", "funding", "liquidation", "etf"),
       notes="Adapter written against CoinGlass v4 docs; unverified live until a key is supplied."),
    _s(number=27, id="velo", name="Velo", url="https://velo.xyz", group="derivatives",
       btc_relevance="VERY HIGH", access="key", tier=1,
       official_api="Velo API — key",
       notes="Not implemented: needs a key to verify the schema. Velo aggregates the same exchange "
             "APIs (Binance/Bybit/OKX/Deribit) this engine reads directly."),
    _s(number=28, id="coinank", name="CoinAnk", url="https://coinank.com", group="derivatives",
       btc_relevance="VERY HIGH", access="key", tier=1,
       official_api="CoinAnk OpenAPI — key",
       notes="Not implemented: needs a key to verify the schema. Same primary exchange data is "
             "read directly."),
    # ---------------- Where the money goes ----------------
    _s(number=29, id="defillama", name="DeFiLlama", url="https://defillama.com", group="liquidity",
       btc_relevance="MEDIUM", access="free", tier=2, adapter="defillama",
       official_api="Free public API (stablecoins.llama.fi, api.llama.fi)",
       metrics=("STABLECOIN_SUPPLY_USD", "DEFI_TVL_USD"), engines=("stablecoin", "cross_market"),
       notes="Stablecoin supply = liquidity regime. TVL alone is not treated as BTC bullish.",
       base_reliability=0.9),
    _s(number=30, id="token_terminal", name="Token Terminal", url="https://tokenterminal.com",
       group="liquidity", btc_relevance="MEDIUM", access="paid", tier=3,
       official_api="Paid API", notes="Not implemented: no statistically validated BTC use yet."),
    _s(number=31, id="cryptofees", name="CryptoFees", url="https://cryptofees.info", group="liquidity",
       btc_relevance="LOW/MEDIUM", access="no_api", tier=3, directional=False,
       official_api="No stable documented API",
       notes="BTC fee data comes from mempool.space instead. Not a trade trigger."),
    _s(number=32, id="artemis", name="Artemis", url="https://app.artemis.xyz", group="liquidity",
       btc_relevance="MEDIUM", access="key", tier=2,
       official_api="Artemis API — key", notes="Not implemented; DeFiLlama covers stablecoin supply."),
    _s(number=33, id="farside", name="Farside", url="https://farside.co.uk", group="etf",
       btc_relevance="EXTREMELY HIGH", access="blocked", tier=1, adapter="farside",
       official_api="No API. Public HTML table behind a Cloudflare challenge",
       metrics=("ETF_NET_FLOW_USD",), engines=("etf",),
       notes="Adapter attempts the public table and reports 'blocked' honestly when challenged. "
             "ETF flows also come from CoinGlass (key) or operator import via the admin API.",
       base_reliability=0.6),
    _s(number=34, id="dune", name="Dune", url="https://dune.com", group="onchain",
       btc_relevance="HIGH", access="key", tier=2, adapter="dune",
       official_api="Dune API — key; executes saved queries",
       metrics=("CONFIGURABLE",), engines=("onchain", "exchange_flow"),
       notes="Queries are configured (BTCS_DUNE_QUERIES), never assumed correct: each mapped "
             "metric must be validated before its weight is raised."),
    _s(number=35, id="messari", name="Messari", url="https://messari.io", group="research",
       btc_relevance="MEDIUM/HIGH", access="key", tier=2,
       official_api="Messari API — key", notes="Not implemented; market data duplicates primary feeds."),
    # ---------------- L2 / ETH / BTC network ----------------
    _s(number=36, id="l2beat", name="L2BEAT", url="https://l2beat.com", group="network",
       btc_relevance="LOW", access="free", tier=3, directional=False,
       official_api="Public JSON endpoints", notes="ETH L2 data. BTC directional weight 0."),
    _s(number=37, id="ultrasound", name="ultrasound.money", url="https://ultrasound.money",
       group="network", btc_relevance="LOW", access="no_api", tier=3, directional=False,
       official_api="No official API", notes="ETH supply is not a BTC signal. Weight 0."),
    _s(number=38, id="mempool_space", name="mempool.space", url="https://mempool.space", group="network",
       btc_relevance="EXTREMELY HIGH", access="free", tier=1, adapter="esplora",
       official_api="Free Esplora-compatible REST API (falls back to blockstream.info)",
       metrics=("BTC_FEE_FAST_SATVB", "BTC_MEMPOOL_VSIZE", "BTC_MEMPOOL_COUNT", "BTC_BLOCK_HEIGHT"),
       engines=("network",), base_reliability=0.9,
       notes="mempool.space timed out from the build machine's network; the same Esplora API on "
             "blockstream.info served the data. The host actually used is recorded on every datum."),
    # ---------------- Portfolio / smart money ----------------
    _s(number=39, id="step_finance", name="Step Finance", url="https://app.step.finance",
       group="portfolio", btc_relevance="LOW", access="no_api", tier=3, directional=False,
       official_api="No public API (Solana portfolio)", notes="Weight 0."),
    _s(number=40, id="debank_stream", name="DeBank Stream", url="https://debank.com/stream",
       group="portfolio", btc_relevance="MEDIUM", access="paid", tier=2, directional=False,
       official_api="DeBank Cloud OpenAPI — paid; EVM only", notes="No native BTC data. Weight 0."),
)

# Primary-data providers. The derivatives dashboards above (CoinGlass, Velo, CoinAnk)
# are aggregations of these exchange APIs; reading them directly is free and auditable.
PRIMARY_SOURCES: tuple[SourceEntry, ...] = (
    _s(id="binance_spot", name="Binance Spot", url="https://api.binance.com", group="market",
       btc_relevance="VERY HIGH", access="free", tier=1, adapter="binance_spot",
       official_api="Binance public REST", base_reliability=0.95,
       metrics=("CANDLES_SPOT_1H", "CANDLES_SPOT_1D", "BTC_PRICE_USDT"),
       engines=("trend", "structure", "spot_flow", "cvd", "volatility")),
    _s(id="binance_futures", name="Binance USDⓈ-M Futures", url="https://fapi.binance.com",
       group="derivatives", btc_relevance="VERY HIGH", access="free", tier=1, adapter="binance_futures",
       official_api="Binance public REST", base_reliability=0.95,
       metrics=("BTC_OPEN_INTEREST_BTC", "BTC_FUNDING_RATE_8H", "BTC_LONG_SHORT_ACCOUNT_RATIO",
                "CANDLES_PERP_1H"), engines=("open_interest", "funding", "cvd")),
    _s(id="bybit", name="Bybit", url="https://api.bybit.com", group="derivatives",
       btc_relevance="VERY HIGH", access="free", tier=1, adapter="bybit",
       official_api="Bybit v5 public REST", base_reliability=0.9,
       metrics=("BTC_OPEN_INTEREST_BTC", "BTC_FUNDING_RATE_8H", "BTC_LONG_SHORT_ACCOUNT_RATIO",
                "BTC_PRICE_USDT"), engines=("open_interest", "funding")),
    _s(id="okx", name="OKX", url="https://www.okx.com", group="derivatives",
       btc_relevance="VERY HIGH", access="free", tier=1, adapter="okx",
       official_api="OKX v5 public REST", base_reliability=0.85,
       metrics=("BTC_OPEN_INTEREST_BTC", "BTC_FUNDING_RATE_8H", "BTC_PRICE_USDT"),
       engines=("open_interest", "funding"),
       notes="Timed out from the build machine's network; works where OKX is reachable."),
    _s(id="hyperliquid", name="Hyperliquid", url="https://api.hyperliquid.xyz", group="derivatives",
       btc_relevance="HIGH", access="free", tier=2, adapter="hyperliquid",
       official_api="Hyperliquid info API", base_reliability=0.9,
       metrics=("BTC_OPEN_INTEREST_BTC", "BTC_FUNDING_RATE_8H"), engines=("open_interest", "funding")),
    _s(id="coinbase", name="Coinbase Exchange", url="https://api.exchange.coinbase.com", group="market",
       btc_relevance="VERY HIGH", access="free", tier=1, adapter="coinbase",
       official_api="Coinbase Exchange public REST", base_reliability=0.95,
       metrics=("BTC_PRICE_USD", "CANDLES_COINBASE_1H"), engines=("spot_flow",),
       notes="US spot venue; Coinbase premium vs Binance is the spot-demand proxy."),
    _s(id="kraken", name="Kraken", url="https://api.kraken.com", group="market",
       btc_relevance="HIGH", access="free", tier=2, adapter="kraken",
       official_api="Kraken public REST", base_reliability=0.9,
       metrics=("BTC_PRICE_USD", "USDT_USD"), engines=()),
    _s(id="deribit", name="Deribit", url="https://www.deribit.com", group="volatility",
       btc_relevance="VERY HIGH", access="free", tier=1, adapter="deribit",
       official_api="Deribit public JSON-RPC", base_reliability=0.95,
       metrics=("BTC_DVOL", "BTC_PRICE_USD"), engines=("volatility",)),
    _s(id="blockchain_info", name="Blockchain.com charts", url="https://api.blockchain.info",
       group="onchain", btc_relevance="HIGH", access="free", tier=2, adapter="blockchain_info",
       official_api="Blockchain.com charts API", base_reliability=0.8,
       metrics=("BTC_HASHRATE", "BTC_TX_COUNT", "BTC_TX_VOLUME_USD"), engines=("network", "onchain")),
    _s(id="coinmetrics", name="Coin Metrics Community", url="https://community-api.coinmetrics.io", group="onchain",
       btc_relevance="HIGH", access="free", tier=1, adapter="coinmetrics",
       official_api="Coin Metrics community API v4 (free, no key)", base_reliability=0.9,
       metrics=("BTC_MVRV", "BTC_ACTIVE_ADDRESSES", "EXCHANGE_INFLOW_BTC", "EXCHANGE_OUTFLOW_BTC",
                "EXCHANGE_NETFLOW_BTC", "EXCHANGE_RESERVE_BTC"), engines=("onchain", "exchange_flow"),
       notes="Free MVRV, active addresses and exchange flows with multi-year history (percentiles). Recent days "
             "may carry 'flash' (preliminary) status. SOPR / LTH supply need Glassnode."),
    _s(id="alternative_me", name="alternative.me Fear & Greed", url="https://api.alternative.me",
       group="sentiment", btc_relevance="MEDIUM", access="free", tier=2, adapter="alternative_me",
       official_api="Public API", base_reliability=0.85, metrics=("FEAR_GREED",),
       engines=("cross_market",)),
    _s(id="coingecko", name="CoinGecko", url="https://api.coingecko.com", group="sentiment",
       btc_relevance="MEDIUM", access="free", tier=2, adapter="coingecko",
       official_api="Public API (rate limited)", base_reliability=0.8,
       metrics=("BTC_DOMINANCE", "CRYPTO_MCAP_CHANGE_24H"), engines=("cross_market",)),
    _s(id="yahoo", name="Yahoo Finance (macro)", url="https://query1.finance.yahoo.com", group="macro",
       btc_relevance="MEDIUM", access="free", tier=2, adapter="yahoo",
       official_api="Unofficial public chart endpoint — reliability discounted", base_reliability=0.65,
       metrics=("MACRO_DXY", "MACRO_US10Y", "MACRO_SPX", "MACRO_VIX"), engines=("macro",)),
    _s(id="liquidation_stream", name="Exchange liquidation streams",
       url="wss://fstream.binance.com + wss://stream.bybit.com", group="derivatives",
       btc_relevance="VERY HIGH", access="free", tier=1, adapter="liquidation_stream",
       official_api="Binance forceOrder + Bybit allLiquidation public websockets", base_reliability=0.8,
       metrics=("LIQUIDATIONS",), engines=("liquidation",),
       notes="Binance pushes at most one liquidation per second per symbol, so totals are a "
             "lower bound. Imbalance (long vs short) is what the engine uses."),
    _s(id="cryptoquant", name="CryptoQuant", url="https://cryptoquant.com", group="onchain",
       btc_relevance="VERY HIGH", access="paid", tier=1, adapter="cryptoquant",
       official_api="CryptoQuant API — paid key", metrics=("EXCHANGE_NETFLOW_BTC",),
       engines=("exchange_flow",), notes="Tier-1 if connected. Unverified live until a key is set."),
    _s(id="glassnode", name="Glassnode", url="https://glassnode.com", group="onchain",
       btc_relevance="VERY HIGH", access="paid", tier=1, adapter="glassnode",
       official_api="Glassnode API — paid key", metrics=("EXCHANGE_NETFLOW_BTC",),
       engines=("exchange_flow",), notes="Tier-1 if connected. Unverified live until a key is set."),
    _s(id="manual_etf", name="Operator ETF import", url="admin API", group="etf",
       btc_relevance="EXTREMELY HIGH", access="free", tier=1, adapter="manual_etf",
       official_api="POST /api/v1/admin/etf-flows (operator-entered published figures)",
       metrics=("ETF_NET_FLOW_USD",), engines=("etf",), base_reliability=0.85),
)

ALL_SOURCES: tuple[SourceEntry, ...] = REFERENCE_SOURCES + PRIMARY_SOURCES
BY_ID: dict[str, SourceEntry] = {s.id: s for s in ALL_SOURCES}
BY_ADAPTER: dict[str, list[SourceEntry]] = {}
for _src in ALL_SOURCES:
    if _src.adapter:
        BY_ADAPTER.setdefault(_src.adapter, []).append(_src)


def source_for_provider(provider_id: str) -> SourceEntry | None:
    """Map a provider id (adapter id, possibly 'adapter:sub') to its catalog entry."""
    base = provider_id.split(":", 1)[0]
    if base in BY_ID:
        return BY_ID[base]
    entries = BY_ADAPTER.get(base)
    if not entries:
        return None
    # Prefer the most relevant entry served by that adapter.
    return max(entries, key=lambda e: e.relevance)


assert len(REFERENCE_SOURCES) == 40, "the reference universe must list exactly 40 sources"
assert sorted(s.number for s in REFERENCE_SOURCES) == list(range(1, 41))
