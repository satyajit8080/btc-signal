"""Free non-exchange adapters: Esplora (mempool.space), Blockchain.com, DeFiLlama,
alternative.me, CoinGecko, Yahoo macro, DexScreener, GeckoTerminal, Hyperliquid smart money."""
from __future__ import annotations

import asyncio
import json
import time
from datetime import datetime, timedelta, timezone

import httpx

from app.providers.base import (
    LiquidityProvider, MacroProvider, NetworkProvider, OnChainProvider, Provider, ProviderError,
    ProviderResult, get_json, post_json, sec,
)

WBTC = "0x2260fac5e5542a773aa44fbcfedf7c193bc2c599"


class Esplora(NetworkProvider):
    """mempool.space first, blockstream.info as an Esplora-compatible fallback."""
    id = "esplora"
    min_interval_s = 120

    async def fetch(self, client: httpx.AsyncClient, now: datetime) -> ProviderResult:
        errors = []
        for base in self.settings.esplora_list():
            try:
                fees, mem, height = await asyncio.gather(
                    get_json(client, f"{base}/fee-estimates", retries=0, timeout=6),
                    get_json(client, f"{base}/mempool", retries=0, timeout=6),
                    _get_text(client, f"{base}/blocks/tip/height"),
                )
            except (httpx.HTTPError, ProviderError) as e:
                errors.append(f"{base}: {type(e).__name__}")
                continue
            res = self.result(now)
            host = base.split("/")[2]
            res.scalar("BTC_FEE_FAST_SATVB", float(fees.get("1") or fees.get("2")), now, host=host)
            res.scalar("BTC_FEE_HOUR_SATVB", float(fees.get("6") or fees.get("5") or 1), now, host=host)
            res.scalar("BTC_MEMPOOL_VSIZE", float(mem["vsize"]), now, host=host)
            res.scalar("BTC_MEMPOOL_COUNT", float(mem["count"]), now, host=host)
            res.scalar("BTC_BLOCK_HEIGHT", float(height), now, host=host)
            res.notes.append(f"served by {host}")
            if errors:
                res.notes.append("fallback after: " + "; ".join(errors))
            return res
        raise ProviderError("all Esplora endpoints failed: " + "; ".join(errors))


async def _get_text(client: httpx.AsyncClient, url: str) -> str:
    r = await client.get(url, timeout=6)
    r.raise_for_status()
    return r.text.strip()


class BlockchainInfo(OnChainProvider):
    id = "blockchain_info"
    min_interval_s = 3600
    CHARTS = {"hash-rate": "BTC_HASHRATE", "n-transactions": "BTC_TX_COUNT",
              "estimated-transaction-volume-usd": "BTC_TX_VOLUME_USD", "difficulty": "BTC_DIFFICULTY"}

    async def fetch(self, client: httpx.AsyncClient, now: datetime) -> ProviderResult:
        res = self.result(now)
        names = list(self.CHARTS)
        payloads = await asyncio.gather(*[
            get_json(client, f"https://api.blockchain.info/charts/{n}",
                     params={"timespan": "90days", "format": "json", "sampled": "false"}) for n in names])
        for n, p in zip(names, payloads):
            res.add_series(self.CHARTS[n], [(sec(v["x"]), float(v["y"])) for v in p.get("values", [])])
        return res


class DefiLlama(LiquidityProvider):
    id = "defillama"
    min_interval_s = 3600

    async def fetch(self, client: httpx.AsyncClient, now: datetime) -> ProviderResult:
        res = self.result(now)
        stables, tvl = await asyncio.gather(
            get_json(client, "https://stablecoins.llama.fi/stablecoincharts/all"),
            get_json(client, "https://api.llama.fi/v2/historicalChainTvl"),
        )
        pts = [(sec(r["date"]), float(r["totalCirculatingUSD"]["peggedUSD"]))
               for r in stables[-180:] if r.get("totalCirculatingUSD", {}).get("peggedUSD")]
        res.add_series("STABLECOIN_SUPPLY_USD", pts, sub="stablecoins")
        res.add_series("DEFI_TVL_USD", [(sec(r["date"]), float(r["tvl"])) for r in tvl[-180:]], sub="tvl")
        return res


class AlternativeMe(Provider):
    id = "alternative_me"
    interfaces = ("sentiment",)
    min_interval_s = 3600

    async def fetch(self, client: httpx.AsyncClient, now: datetime) -> ProviderResult:
        res = self.result(now)
        data = await get_json(client, "https://api.alternative.me/fng/", params={"limit": 90})
        res.add_series("FEAR_GREED", [(sec(r["timestamp"]), float(r["value"])) for r in data["data"]])
        return res


class CoinGecko(Provider):
    id = "coingecko"
    interfaces = ("market",)
    min_interval_s = 600

    async def fetch(self, client: httpx.AsyncClient, now: datetime) -> ProviderResult:
        res = self.result(now)
        g = (await get_json(client, "https://api.coingecko.com/api/v3/global"))["data"]
        ts = sec(g.get("updated_at", now.timestamp()))
        res.scalar("BTC_DOMINANCE", float(g["market_cap_percentage"]["btc"]), ts)
        res.scalar("CRYPTO_MCAP_CHANGE_24H", float(g["market_cap_change_percentage_24h_usd"]), ts)
        return res


class YahooMacro(MacroProvider):
    """Unofficial endpoint — the catalog discounts its reliability accordingly."""
    id = "yahoo"
    min_interval_s = 1800
    SYMBOLS = {"DX-Y.NYB": "MACRO_DXY", "^TNX": "MACRO_US10Y", "^GSPC": "MACRO_SPX", "^VIX": "MACRO_VIX"}

    async def fetch(self, client: httpx.AsyncClient, now: datetime) -> ProviderResult:
        res = self.result(now)
        syms = list(self.SYMBOLS)
        payloads = await asyncio.gather(*[
            get_json(client, f"https://query1.finance.yahoo.com/v8/finance/chart/{s}",
                     params={"range": "3mo", "interval": "1d"}) for s in syms], return_exceptions=True)
        for s, p in zip(syms, payloads):
            if isinstance(p, Exception):
                res.notes.append(f"{s}: {type(p).__name__}")
                continue
            res.add_series(self.SYMBOLS[s], parse_yahoo_chart(p), sub=s)
        if not res.series:
            raise ProviderError("no macro series returned")
        return res


def parse_yahoo_chart(payload: dict) -> list[tuple[datetime, float]]:
    r = payload["chart"]["result"][0]
    closes = r["indicators"]["quote"][0]["close"]
    return [(sec(t), float(c)) for t, c in zip(r["timestamp"], closes) if c is not None]


class DexScreener(LiquidityProvider):
    id = "dexscreener"
    min_interval_s = 900

    async def fetch(self, client: httpx.AsyncClient, now: datetime) -> ProviderResult:
        res = self.result(now)
        data = await get_json(client, f"https://api.dexscreener.com/latest/dex/tokens/{WBTC}")
        pairs = [p for p in (data.get("pairs") or []) if p.get("chainId") == "ethereum"]
        if not pairs:
            raise ProviderError("no WBTC pairs returned")
        res.scalar("WBTC_DEX_VOLUME_24H_USD", sum(float((p.get("volume") or {}).get("h24") or 0) for p in pairs), now)
        res.scalar("WBTC_DEX_LIQUIDITY_USD", sum(float((p.get("liquidity") or {}).get("usd") or 0) for p in pairs), now)
        return res


class GeckoTerminal(LiquidityProvider):
    id = "geckoterminal"
    min_interval_s = 900

    async def fetch(self, client: httpx.AsyncClient, now: datetime) -> ProviderResult:
        res = self.result(now)
        data = await get_json(client, f"https://api.geckoterminal.com/api/v2/networks/eth/tokens/{WBTC}")
        a = data["data"]["attributes"]
        res.scalar("WBTC_DEX_VOLUME_24H_USD", float(a["volume_usd"]["h24"]), now)
        res.scalar("WBTC_DEX_LIQUIDITY_USD", float(a["total_reserve_in_usd"]), now)
        return res


class HyperliquidSmartMoney(Provider):
    """Net BTC position of the top-N accounts by 30-day PnL on Hyperliquid.

    The leaderboard JSON is ~40 MB, so the cohort is refreshed every 12h and only the
    cohort's positions are polled each interval.
    """
    id = "hyperliquid_smart_money"
    interfaces = ("whale", "derivatives")
    min_interval_s = 900
    COHORT_TTL_S = 12 * 3600
    MIN_ACCOUNT_USD = 1_000_000

    def __init__(self, settings):
        super().__init__(settings)
        self._cohort: list[str] = []
        self._cohort_at = 0.0

    async def _refresh_cohort(self, client: httpx.AsyncClient) -> None:
        r = await client.get("https://stats-data.hyperliquid.xyz/Mainnet/leaderboard", timeout=60)
        r.raise_for_status()
        rows = json.loads(r.content)["leaderboardRows"]
        self._cohort = select_cohort(rows, self.settings.hyperliquid_top_n, self.MIN_ACCOUNT_USD)
        self._cohort_at = time.time()

    async def fetch(self, client: httpx.AsyncClient, now: datetime) -> ProviderResult:
        if not self._cohort or time.time() - self._cohort_at > self.COHORT_TTL_S:
            await self._refresh_cohort(client)
        sem = asyncio.Semaphore(5)

        async def one(addr: str):
            async with sem:
                return await post_json(client, "https://api.hyperliquid.xyz/info",
                                       {"type": "clearinghouseState", "user": addr})

        states = await asyncio.gather(*[one(a) for a in self._cohort], return_exceptions=True)
        ok = [s for s in states if not isinstance(s, Exception)]
        if len(ok) < max(3, len(self._cohort) // 2):
            raise ProviderError(f"only {len(ok)}/{len(self._cohort)} cohort states returned")
        net, gross, longs, shorts = summarize_positions(ok)
        res = self.result(now)
        res.scalar("SMART_MONEY_NET_BTC", net, now, traders=len(ok), longs=longs, shorts=shorts)
        res.scalar("SMART_MONEY_GROSS_BTC", gross, now)
        return res


def select_cohort(rows: list[dict], top_n: int, min_account_usd: float) -> list[str]:
    def month_pnl(row: dict) -> float:
        for window, perf in row.get("windowPerformances", []):
            if window == "month":
                return float(perf.get("pnl") or 0)
        return 0.0
    eligible = [r for r in rows if float(r.get("accountValue") or 0) >= min_account_usd]
    eligible.sort(key=month_pnl, reverse=True)
    return [r["ethAddress"] for r in eligible[:top_n] if month_pnl(r) > 0]


def summarize_positions(states: list[dict]) -> tuple[float, float, int, int]:
    net = gross = 0.0
    longs = shorts = 0
    for st in states:
        for ap in st.get("assetPositions", []):
            pos = ap.get("position", {})
            if pos.get("coin") != "BTC":
                continue
            szi = float(pos.get("szi") or 0)
            net += szi
            gross += abs(szi)
            longs += szi > 0
            shorts += szi < 0
    return net, gross, longs, shorts


class CoinMetrics(OnChainProvider):
    """Coin Metrics community API (free, no key): MVRV, active addresses, exchange in/outflows
    and reserves, with multi-year daily history for percentile ranking."""
    id = "coinmetrics"
    min_interval_s = 3600
    URL = "https://community-api.coinmetrics.io/v4/timeseries/asset-metrics"
    METRICS = {"CapMVRVCur": "BTC_MVRV", "AdrActCnt": "BTC_ACTIVE_ADDRESSES", "FlowInExNtv": "EXCHANGE_INFLOW_BTC",
               "FlowOutExNtv": "EXCHANGE_OUTFLOW_BTC", "SplyExNtv": "EXCHANGE_RESERVE_BTC"}

    async def fetch(self, client: httpx.AsyncClient, now: datetime) -> ProviderResult:
        rows = await fetch_coinmetrics(client, self.URL, list(self.METRICS), now)
        res = self.result(now)
        for metric, pts in parse_coinmetrics(rows, self.METRICS).items():
            res.add_series(metric, pts)
        if not res.series:
            raise ProviderError("coinmetrics returned no rows")
        return res


def parse_coinmetrics(rows: list[dict], metrics: dict[str, str]) -> dict[str, list]:
    out: dict[str, list] = {m: [] for m in metrics.values()}
    out["EXCHANGE_NETFLOW_BTC"] = []
    for r in rows:
        # a day's value is complete only once that day has closed
        ts = datetime.fromisoformat(r["time"][:19]).replace(tzinfo=timezone.utc) + timedelta(days=1)
        for k, metric in metrics.items():
            if r.get(k) not in (None, ""):
                out[metric].append((ts, float(r[k])))
        if r.get("FlowInExNtv") and r.get("FlowOutExNtv"):
            out["EXCHANGE_NETFLOW_BTC"].append((ts, float(r["FlowInExNtv"]) - float(r["FlowOutExNtv"])))  # in − out
    return out


async def fetch_coinmetrics(client: httpx.AsyncClient, url: str, metrics: list[str], now: datetime,
                            start: datetime | None = None) -> list[dict]:
    rows, token = [], None
    params = {"assets": "btc", "metrics": ",".join(metrics), "frequency": "1d", "page_size": 10000,
              "start_time": (start or now - timedelta(days=4 * 365)).strftime("%Y-%m-%d")}
    for _ in range(10):
        p = dict(params, **({"next_page_token": token} if token else {}))
        d = await get_json(client, url, params=p)
        rows += d.get("data", [])
        token = d.get("next_page_token")
        if not token:
            break
    return rows
