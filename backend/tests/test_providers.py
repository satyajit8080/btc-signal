"""Adapter parsing against payloads shaped like each provider's documented/observed responses."""
import json
import time

import httpx
import pytest
import respx

from app.config import Settings
from app.core.context import MarketContext
from app.providers import exchanges, keyed, public_data
from app.providers.base import ProviderBlocked, ProviderError
from app.providers.catalog import ALL_SOURCES, REFERENCE_SOURCES, source_for_provider
from app.providers.liquidation_stream import LiquidationStream, parse_binance, parse_bybit
from app.providers.registry import ADAPTERS, build_providers
from conftest import NOW

T = int(NOW.timestamp() * 1000)
KL = [[T - 3600_000, "100", "110", "95", "105", "10", T - 1, "1000", 5, "6", "600", "0"]]


@pytest.fixture
async def client():
    async with httpx.AsyncClient() as c:
        yield c


def S(**kw):
    return Settings(database_url="sqlite://", **kw)


# ---------------- catalog ----------------
def test_catalog_has_exactly_40_reference_sources_and_relevance():
    assert len(REFERENCE_SOURCES) == 40
    by = {s.id: s for s in ALL_SOURCES}
    assert by["farside"].relevance == 1.0
    assert by["solscan"].relevance == 0.0 and by["rugcheck"].relevance == 0.0  # BTC weight 0
    assert by["tokensniffer"].relevance == 0.0
    assert by["whale_alert"].relevance > by["debank"].relevance
    for s in ALL_SOURCES:
        assert s.adapter is None or s.adapter in ADAPTERS or s.adapter in ("manual_etf", "liquidation_stream")
    assert source_for_provider("yahoo:^VIX").id == "yahoo"
    assert source_for_provider("unknown") is None


def test_registry_builds_all():
    ps = build_providers(S())
    assert {"binance_spot", "coinglass", "manual_etf", "liquidation_stream"} <= set(ps)
    assert not ps["coinglass"].is_configured() and ps["binance_spot"].is_configured()
    assert ps["coinglass"].__class__(S(coinglass_api_key="k")).is_configured()


# ---------------- exchanges ----------------
@respx.mock
async def test_binance_spot(client):
    respx.get(url__startswith="https://api.binance.com/api/v3/klines").mock(return_value=httpx.Response(200, json=KL))
    respx.get(url__startswith="https://api.binance.com/api/v3/ticker/price").mock(
        return_value=httpx.Response(200, json={"price": "105.5"}))
    r = await exchanges.BinanceSpot(S()).fetch(client, NOW)
    assert r.candles["spot_1h"][0].taker_buy_volume == 6.0
    assert r.scalars[0][0] == "BTC_PRICE_USDT" and r.scalars[0][1].value == 105.5


@respx.mock
async def test_binance_futures(client):
    base = "https://fapi.binance.com"
    respx.get(f"{base}/fapi/v1/premiumIndex").mock(return_value=httpx.Response(200, json={
        "lastFundingRate": "0.0001", "markPrice": "100", "time": T}))
    respx.get(f"{base}/futures/data/openInterestHist").mock(return_value=httpx.Response(200, json=[
        {"sumOpenInterest": "90000", "sumOpenInterestValue": "8e9", "timestamp": T - 3600_000},
        {"sumOpenInterest": "91000", "sumOpenInterestValue": "8.1e9", "timestamp": T}]))
    for p in ("globalLongShortAccountRatio", "topLongShortPositionRatio"):
        respx.get(f"{base}/futures/data/{p}").mock(return_value=httpx.Response(200, json=[{"longShortRatio": "1.5", "timestamp": T}]))
    respx.get(f"{base}/futures/data/takerlongshortRatio").mock(return_value=httpx.Response(200, json=[{"buySellRatio": "1.1", "timestamp": T}]))
    respx.get(f"{base}/fapi/v1/klines").mock(return_value=httpx.Response(200, json=KL))
    respx.get(f"{base}/fapi/v1/fundingRate").mock(return_value=httpx.Response(200, json=[{"fundingTime": T, "fundingRate": "0.0001"}]))
    r = await exchanges.BinanceFutures(S()).fetch(client, NOW)
    ctx = MarketContext(now=NOW)
    r.apply(ctx)
    assert ctx.scalars["BTC_FUNDING_RATE_8H"][0].value == 0.0001
    assert ctx.series["BTC_OPEN_INTEREST_BTC"]["binance_futures"].last() == 91000
    assert ctx.scalars["BTC_LONG_SHORT_ACCOUNT_RATIO"][0].value == 1.5
    assert ctx.fetched_at["binance_futures"] == NOW


@respx.mock
async def test_bybit_and_error(client):
    base = "https://api.bybit.com/v5/market"
    respx.get(f"{base}/tickers").mock(return_value=httpx.Response(200, json={"retCode": 0, "time": T, "result": {"list": [
        {"lastPrice": "100", "fundingRate": "0.0002", "openInterestValue": "5e9"}]}}))
    respx.get(f"{base}/open-interest").mock(return_value=httpx.Response(200, json={"retCode": 0, "result": {"list": [
        {"openInterest": "55000", "timestamp": str(T)}]}}))
    respx.get(f"{base}/account-ratio").mock(return_value=httpx.Response(200, json={"retCode": 0, "result": {"list": [
        {"buyRatio": "0.6", "sellRatio": "0.4", "timestamp": str(T)}]}}))
    r = await exchanges.Bybit(S()).fetch(client, NOW)
    vals = {m: d.value for m, d in r.scalars}
    assert vals["BTC_LONG_SHORT_ACCOUNT_RATIO"] == pytest.approx(1.5)
    respx.get(f"{base}/tickers").mock(return_value=httpx.Response(200, json={"retCode": 10001, "retMsg": "bad"}))
    with pytest.raises(ProviderError):
        await exchanges.Bybit(S()).fetch(client, NOW)


@respx.mock
async def test_okx(client):
    base = "https://www.okx.com/api/v5"
    respx.get(f"{base}/public/funding-rate").mock(return_value=httpx.Response(200, json={"code": "0", "data": [{"fundingRate": "0.0001", "ts": str(T)}]}))
    respx.get(f"{base}/public/open-interest").mock(return_value=httpx.Response(200, json={"code": "0", "data": [{"oiCcy": "30000", "oiUsd": "3e9", "ts": str(T)}]}))
    respx.get(f"{base}/market/ticker").mock(return_value=httpx.Response(200, json={"code": "0", "data": [{"last": "100", "ts": str(T)}]}))
    r = await exchanges.OKX(S()).fetch(client, NOW)
    assert {m for m, _ in r.scalars} >= {"BTC_FUNDING_RATE_8H", "BTC_OPEN_INTEREST_BTC", "BTC_PRICE_USDT"}
    respx.get(f"{base}/public/funding-rate").mock(return_value=httpx.Response(200, json={"code": "51001", "msg": "x"}))
    with pytest.raises(ProviderError):
        await exchanges.OKX(S()).fetch(client, NOW)


@respx.mock
async def test_hyperliquid_hourly_funding_normalized(client):
    respx.post("https://api.hyperliquid.xyz/info").mock(return_value=httpx.Response(200, json=[
        {"universe": [{"name": "ETH"}, {"name": "BTC"}]},
        [{}, {"funding": "0.0000125", "openInterest": "30000", "markPx": "100"}]]))
    r = await exchanges.Hyperliquid(S()).fetch(client, NOW)
    f = next(d for m, d in r.scalars if m == "BTC_FUNDING_RATE_8H")
    assert f.value == pytest.approx(0.0001)  # 1h × 8


@respx.mock
async def test_hyperliquid_missing_btc(client):
    respx.post("https://api.hyperliquid.xyz/info").mock(return_value=httpx.Response(200, json=[{"universe": [{"name": "ETH"}]}, [{}]]))
    with pytest.raises(ProviderError):
        await exchanges.Hyperliquid(S()).fetch(client, NOW)


@respx.mock
async def test_coinbase_kraken_deribit(client):
    respx.get("https://api.exchange.coinbase.com/products/BTC-USD/ticker").mock(return_value=httpx.Response(200, json={"price": "100"}))
    respx.get(url__startswith="https://api.exchange.coinbase.com/products/BTC-USD/candles").mock(
        return_value=httpx.Response(200, json=[[T // 1000, 95, 110, 100, 105, 3], [T // 1000 - 3600, 90, 101, 99, 100, 2]]))
    r = await exchanges.Coinbase(S()).fetch(client, NOW)
    cs = r.candles["coinbase_1h"]
    assert len(cs) == 2 and cs[0].ts < cs[1].ts and cs[1].open == 100 and cs[1].low == 95
    respx.get(url__startswith="https://api.kraken.com").mock(return_value=httpx.Response(200, json={"error": [], "result": {
        "XXBTZUSD": {"c": ["100.5", "1"]}, "USDTZUSD": {"c": ["0.9998", "1"]}}}))
    r = await exchanges.Kraken(S()).fetch(client, NOW)
    assert {m for m, _ in r.scalars} == {"BTC_PRICE_USD", "USDT_USD"}
    respx.get(url__startswith="https://www.deribit.com/api/v2/public/get_volatility_index_data").mock(
        return_value=httpx.Response(200, json={"result": {"data": [[T, 50, 51, 49, 50.5]]}}))
    respx.get(url__startswith="https://www.deribit.com/api/v2/public/get_index_price").mock(
        return_value=httpx.Response(200, json={"result": {"index_price": 100.1}}))
    r = await exchanges.Deribit(S()).fetch(client, NOW)
    assert r.series[0][1].last() == 50.5


@respx.mock
async def test_kraken_error(client):
    respx.get(url__startswith="https://api.kraken.com").mock(return_value=httpx.Response(200, json={"error": ["EGeneral"]}))
    with pytest.raises(ProviderError):
        await exchanges.Kraken(S()).fetch(client, NOW)


# ---------------- public data ----------------
@respx.mock
async def test_esplora_falls_back_to_second_host(client):
    respx.get(url__startswith="https://mempool.space").mock(side_effect=httpx.ConnectTimeout("t"))
    respx.get("https://blockstream.info/api/fee-estimates").mock(return_value=httpx.Response(200, json={"1": 5.0, "6": 2.0}))
    respx.get("https://blockstream.info/api/mempool").mock(return_value=httpx.Response(200, json={"count": 10, "vsize": 1000}))
    respx.get("https://blockstream.info/api/blocks/tip/height").mock(return_value=httpx.Response(200, text="900000"))
    r = await public_data.Esplora(S()).fetch(client, NOW)
    fee = next(d for m, d in r.scalars if m == "BTC_FEE_FAST_SATVB")
    assert fee.value == 5.0 and fee.meta["host"] == "blockstream.info"
    assert any("fallback" in n for n in r.notes)


@respx.mock
async def test_esplora_all_fail(client):
    respx.get(url__regex=r".*").mock(side_effect=httpx.ConnectTimeout("t"))
    with pytest.raises(ProviderError):
        await public_data.Esplora(S()).fetch(client, NOW)


@respx.mock
async def test_blockchain_defillama_fng_coingecko(client):
    respx.get(url__startswith="https://api.blockchain.info/charts/").mock(
        return_value=httpx.Response(200, json={"values": [{"x": 1, "y": 2.0}]}))
    r = await public_data.BlockchainInfo(S()).fetch(client, NOW)
    assert len(r.series) == 4
    respx.get("https://stablecoins.llama.fi/stablecoincharts/all").mock(return_value=httpx.Response(200, json=[
        {"date": "1700000000", "totalCirculatingUSD": {"peggedUSD": 1e11}}, {"date": "1700086400", "totalCirculatingUSD": {}}]))
    respx.get("https://api.llama.fi/v2/historicalChainTvl").mock(return_value=httpx.Response(200, json=[{"date": 1700000000, "tvl": 5e10}]))
    r = await public_data.DefiLlama(S()).fetch(client, NOW)
    ctx = MarketContext(now=NOW)
    r.apply(ctx)
    assert "defillama:stablecoins" in ctx.series["STABLECOIN_SUPPLY_USD"]
    assert len(ctx.series["STABLECOIN_SUPPLY_USD"]["defillama:stablecoins"]) == 1  # empty peg skipped
    respx.get(url__startswith="https://api.alternative.me/fng/").mock(
        return_value=httpx.Response(200, json={"data": [{"timestamp": "1700000000", "value": "40"}]}))
    assert (await public_data.AlternativeMe(S()).fetch(client, NOW)).series[0][1].last() == 40
    respx.get("https://api.coingecko.com/api/v3/global").mock(return_value=httpx.Response(200, json={"data": {
        "market_cap_percentage": {"btc": 58.1}, "market_cap_change_percentage_24h_usd": -1.2, "updated_at": 1700000000}}))
    r = await public_data.CoinGecko(S()).fetch(client, NOW)
    assert {m: d.value for m, d in r.scalars} == {"BTC_DOMINANCE": 58.1, "CRYPTO_MCAP_CHANGE_24H": -1.2}


def _yahoo(vals):
    return {"chart": {"result": [{"timestamp": [1700000000 + i * 86400 for i in range(len(vals))],
                                  "indicators": {"quote": [{"close": vals}]}}]}}


@respx.mock
async def test_yahoo_partial_and_total_failure(client):
    respx.get(url__regex=r".*DX-Y.*").mock(return_value=httpx.Response(200, json=_yahoo([100, None, 101])))
    respx.get(url__regex=r".*(%5E|\^).*").mock(return_value=httpx.Response(500))
    r = await public_data.YahooMacro(S()).fetch(client, NOW)
    assert len(r.series) == 1 and len(r.series[0][1]) == 2  # None closes dropped
    assert any("^VIX" in n for n in r.notes)


@respx.mock
async def test_yahoo_total_failure(client):
    respx.get(url__regex=r".*").mock(return_value=httpx.Response(500))
    with pytest.raises(ProviderError):
        await public_data.YahooMacro(S()).fetch(client, NOW)


@respx.mock
async def test_dex_sources(client):
    respx.get(url__startswith="https://api.dexscreener.com").mock(return_value=httpx.Response(200, json={"pairs": [
        {"chainId": "ethereum", "volume": {"h24": 1e6}, "liquidity": {"usd": 5e6}},
        {"chainId": "base", "volume": {"h24": 9e9}, "liquidity": {"usd": 9e9}}]}))
    r = await public_data.DexScreener(S()).fetch(client, NOW)
    assert dict((m, d.value) for m, d in r.scalars)["WBTC_DEX_VOLUME_24H_USD"] == 1e6
    respx.get(url__startswith="https://api.geckoterminal.com").mock(return_value=httpx.Response(200, json={"data": {"attributes": {
        "volume_usd": {"h24": "2e6"}, "total_reserve_in_usd": "6e6"}}}))
    r = await public_data.GeckoTerminal(S()).fetch(client, NOW)
    assert len(r.scalars) == 2
    respx.get(url__startswith="https://api.dexscreener.com").mock(return_value=httpx.Response(200, json={"pairs": None}))
    with pytest.raises(ProviderError):
        await public_data.DexScreener(S()).fetch(client, NOW)


def test_smart_money_cohort_and_positions():
    rows = [{"ethAddress": f"0x{i}", "accountValue": str(2e6 if i % 2 else 1e5),
             "windowPerformances": [["month", {"pnl": str(i * 1000)}]]} for i in range(10)]
    cohort = public_data.select_cohort(rows, 3, 1e6)
    assert cohort == ["0x9", "0x7", "0x5"]
    states = [{"assetPositions": [{"position": {"coin": "BTC", "szi": "2"}}, {"position": {"coin": "ETH", "szi": "50"}}]},
              {"assetPositions": [{"position": {"coin": "BTC", "szi": "-0.5"}}]}, {"assetPositions": []}]
    assert public_data.summarize_positions(states) == (1.5, 2.5, 1, 1)


@respx.mock
async def test_smart_money_fetch(client):
    lb = {"leaderboardRows": [{"ethAddress": f"0x{i}", "accountValue": "5e6",
                               "windowPerformances": [["month", {"pnl": "100"}]]} for i in range(4)]}
    respx.get("https://stats-data.hyperliquid.xyz/Mainnet/leaderboard").mock(return_value=httpx.Response(200, content=json.dumps(lb)))
    respx.post("https://api.hyperliquid.xyz/info").mock(return_value=httpx.Response(200, json={
        "assetPositions": [{"position": {"coin": "BTC", "szi": "1"}}]}))
    p = public_data.HyperliquidSmartMoney(S())
    r = await p.fetch(client, NOW)
    net = next(d for m, d in r.scalars if m == "SMART_MONEY_NET_BTC")
    assert net.value == 4 and net.meta["longs"] == 4
    respx.post("https://api.hyperliquid.xyz/info").mock(return_value=httpx.Response(500))
    with pytest.raises(ProviderError):
        await p.fetch(client, NOW)


# ---------------- keyed ----------------
@respx.mock
async def test_coinglass_parsing_and_partial_failure(client):
    base = "https://open-api-v4.coinglass.com/api"
    respx.get(f"{base}/futures/open-interest/aggregated-history").mock(return_value=httpx.Response(200, json={"code": "0", "data": [{"time": T, "close": "8e10"}]}))
    respx.get(f"{base}/futures/funding-rate/oi-weight-history").mock(return_value=httpx.Response(200, json={"code": "0", "data": [{"time": T, "close": "0.01"}]}))
    respx.get(f"{base}/futures/liquidation/aggregated-history").mock(return_value=httpx.Response(200, json={"code": "0", "data": [
        {"time": T, "aggregated_long_liquidation_usd": 1e6, "aggregated_short_liquidation_usd": 2e6}]}))
    respx.get(f"{base}/etf/bitcoin/flow-history").mock(return_value=httpx.Response(500))
    r = await keyed.CoinGlass(S(coinglass_api_key="k")).fetch(client, NOW)
    f = next(d for m, d in r.scalars if m == "BTC_FUNDING_RATE_8H")
    assert f.value == pytest.approx(0.0001)  # percent → fraction
    assert len(r.events["liquidations"]) == 2 and r.notes


@respx.mock
async def test_coinglass_all_fail(client):
    respx.get(url__startswith="https://open-api-v4.coinglass.com").mock(return_value=httpx.Response(200, json={"code": "401", "msg": "no"}))
    with pytest.raises(ProviderError):
        await keyed.CoinGlass(S(coinglass_api_key="k")).fetch(client, NOW)


@pytest.mark.parametrize("f,t,kind", [("exchange", "unknown", "from_exchange"), ("unknown", "exchange", "to_exchange"),
                                      ("exchange", "exchange", "exchange_to_exchange"), ("unknown", "unknown", "wallet_to_wallet")])
def test_whale_classification(f, t, kind):
    assert keyed.classify_transfer({"from": {"owner_type": f}, "to": {"owner_type": t}}) == kind


@respx.mock
async def test_whale_alert_buffer_dedupe(client):
    tx = {"blockchain": "bitcoin", "hash": "h", "id": "1", "timestamp": int(NOW.timestamp()) - 60, "amount": 100,
          "amount_usd": 1e7, "from": {"owner_type": "exchange", "owner": "binance"}, "to": {"owner_type": "unknown"}}
    eth = dict(tx, blockchain="ethereum", hash="e")
    respx.get(url__startswith="https://api.whale-alert.io").mock(return_value=httpx.Response(200, json={"result": "success", "transactions": [tx, tx, eth]}))
    p = keyed.WhaleAlert(S(whale_alert_api_key="k"))
    r = await p.fetch(client, NOW)
    r2 = await p.fetch(client, NOW)
    assert len(r.events["whale_transfers"]) == 1 == len(r2.events["whale_transfers"])
    assert r.events["whale_transfers"][0]["kind"] == "from_exchange"
    respx.get(url__startswith="https://api.whale-alert.io").mock(return_value=httpx.Response(200, json={"result": "error", "message": "bad key"}))
    with pytest.raises(ProviderError):
        await p.fetch(client, NOW)


@respx.mock
async def test_cryptoquant_glassnode_etherscan_dune(client):
    respx.get(url__startswith="https://api.cryptoquant.com").mock(return_value=httpx.Response(200, json={"result": {"data": [
        {"date": "2026-10-05", "netflow_total": -1200.5}]}}))
    r = await keyed.CryptoQuant(S(cryptoquant_api_key="k")).fetch(client, NOW)
    assert r.series[0][1].last() == -1200.5
    respx.get(url__startswith="https://api.glassnode.com").mock(return_value=httpx.Response(200, json=[{"t": 1700000000, "v": 50.0}]))
    assert (await keyed.Glassnode(S(glassnode_api_key="k")).fetch(client, NOW)).series[0][1].last() == 50.0
    respx.get(url__startswith="https://api.etherscan.io").mock(return_value=httpx.Response(200, json={"status": "1", "result": "1000000000000"}))
    r = await keyed.Etherscan(S(etherscan_api_key="k")).fetch(client, NOW)
    assert r.scalars[0][1].value == pytest.approx(2e6)
    q = json.dumps([{"query_id": 7, "metric": "EXCHANGE_NETFLOW_BTC", "date_col": "day", "value_col": "v"}])
    respx.get(url__startswith="https://api.dune.com/api/v1/query/7/results").mock(return_value=httpx.Response(200, json={"result": {"rows": [
        {"day": "2026-10-05 00:00:00.000 UTC", "v": 12}, {"day": None, "v": 1}]}}))
    d = keyed.Dune(S(dune_api_key="k", dune_queries=q))
    assert d.is_configured()
    r = await d.fetch(client, NOW)
    assert r.series[0][1].provider == "dune:q7" and len(r.series[0][1]) == 1
    assert not keyed.Dune(S(dune_api_key="k")).is_configured()
    assert S(dune_queries="not json").dune_query_list() == []


@respx.mock
async def test_etherscan_error(client):
    respx.get(url__startswith="https://api.etherscan.io").mock(return_value=httpx.Response(200, json={"status": "0", "message": "NOTOK"}))
    with pytest.raises(ProviderError):
        await keyed.Etherscan(S(etherscan_api_key="k")).fetch(client, NOW)


FARSIDE = """<table><tr><th>Date</th><th>IBIT</th><th>Total</th></tr>
<tr><td>03 Oct 2026</td><td>100.0</td><td>250.5</td></tr>
<tr><td>06 Oct 2026</td><td>(50.0)</td><td>(1,120.0)</td></tr>
<tr><td>Total</td><td>1</td><td>2</td></tr><tr><td>07 Oct 2026</td><td>-</td><td>-</td></tr></table>"""


def test_farside_parser():
    pts = keyed.parse_farside_html(FARSIDE)
    assert [v for _, v in pts] == [250.5e6, -1120.0e6]


@respx.mock
async def test_farside_blocked_and_ok(client):
    respx.get(url__startswith="https://farside.co.uk").mock(return_value=httpx.Response(403, text="<title>Just a moment...</title>"))
    with pytest.raises(ProviderBlocked):
        await keyed.Farside(S()).fetch(client, NOW)
    respx.get(url__startswith="https://farside.co.uk").mock(return_value=httpx.Response(200, text=FARSIDE))
    r = await keyed.Farside(S()).fetch(client, NOW)
    assert len(r.series[0][1]) == 2
    respx.get(url__startswith="https://farside.co.uk").mock(return_value=httpx.Response(200, text="<html>changed</html>"))
    with pytest.raises(ProviderError):
        await keyed.Farside(S()).fetch(client, NOW)


async def test_manual_etf_and_liquidation_provider(client):
    m = keyed.ManualETF(S(), loader=lambda: [(NOW, 1e8)])
    assert m.is_configured() and (await m.fetch(client, NOW)).series[0][1].last() == 1e8
    assert not keyed.ManualETF(S(), loader=lambda: []).is_configured()
    stream = LiquidationStream()
    lp = keyed.LiquidationStreamProvider(S(), stream)
    with pytest.raises(ProviderError):
        await lp.fetch(client, NOW)
    stream.connected_at["binance"] = time.time() - 100
    stream.add({"ts": NOW, "venue": "binance", "side": "long", "usd": 1e6})
    r = await lp.fetch(client, NOW)
    assert len(r.events["liquidations"]) == 1 and r.scalars[0][1].value >= 100


def test_liquidation_message_parsing():
    b = parse_binance({"o": {"s": "BTCUSDT", "S": "SELL", "z": "2", "ap": "100", "T": T}})
    assert b["side"] == "long" and b["usd"] == 200
    assert parse_binance({"o": {"s": "ETHUSDT"}}) is None
    by = parse_bybit({"topic": "allLiquidation.BTCUSDT", "data": [{"T": T, "S": "Sell", "v": "1", "p": "100"},
                                                                  {"T": T, "S": "Buy", "v": "1", "p": "100"}]})
    assert [e["side"] for e in by] == ["short", "long"]
    assert parse_bybit({"op": "subscribe"}) == []
    s = LiquidationStream(maxlen=2, on_event=lambda e: 1 / 0)  # persistence failure must not raise
    for _ in range(3):
        s.add({"ts": NOW, "usd": 1})
    assert len(s.events) == 2 and s.uptime_s(time.time()) == 0 and not s.connected_any()
