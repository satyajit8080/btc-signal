from datetime import timedelta

import httpx
import pytest

from app.config import Settings
from app.core.context import Datum, Series
from app.providers.base import Provider, ProviderBlocked, ProviderResult, get_json
from app.providers.collector import Collector, ProviderHealth
from conftest import NOW

import respx


class Fake(Provider):
    def __init__(self, pid, behaviour, key_setting=None, interval=0):
        super().__init__(Settings(database_url="sqlite://"))
        self.id, self.behaviour, self.key_setting, self.min_interval_s = pid, behaviour, key_setting, interval
        self.calls = 0

    async def fetch(self, client, now):
        self.calls += 1
        if self.behaviour == "ok":
            r = ProviderResult(self.id, now)
            r.scalar("BTC_PRICE_USD", 100.0, now)
            return r
        if self.behaviour == "empty":
            return ProviderResult(self.id, now)
        if self.behaviour == "blocked":
            raise ProviderBlocked("cloudflare")
        raise RuntimeError("boom")


async def test_failures_are_isolated_and_reported():
    ps = {"good": Fake("good", "ok"), "bad": Fake("bad", "error"), "cf": Fake("cf", "blocked"),
          "empty": Fake("empty", "empty"), "keyed": Fake("keyed", "ok", key_setting="coinglass_api_key")}
    c = Collector(ps)
    ctx = await c.collect(NOW)
    st = {k: h.status for k, h in c.health.items()}
    assert st == {"good": "ok", "bad": "error", "cf": "blocked", "empty": "error", "keyed": "unconfigured"}
    assert [d.provider for d in ctx.scalars["BTC_PRICE_USD"]] == ["good"]
    assert ctx.reliability["bad"] < ctx.reliability["good"]
    await c.close()


async def test_disabled_provider_is_skipped():
    p = Fake("good", "ok")
    c = Collector({"good": p}, is_enabled=lambda pid: False)
    ctx = await c.collect(NOW)
    assert p.calls == 0 and c.health["good"].status == "disabled" and not ctx.scalars


async def test_cached_result_not_restamped_and_backoff():
    p = Fake("good", "ok", interval=3600)
    c = Collector({"good": p})
    await c.collect(NOW)
    ctx = await c.collect(NOW + timedelta(minutes=30))
    assert p.calls == 1
    assert ctx.scalars["BTC_PRICE_USD"][0].ts == NOW  # ages naturally
    assert ctx.fresh("BTC_PRICE_USD", NOW) < 1.0
    bad = Fake("bad", "error")
    c2 = Collector({"bad": bad})
    await c2.collect(NOW)
    await c2.collect(NOW)
    assert bad.calls == 1  # backing off after a failure


def test_health_reliability():
    h = ProviderHealth("x")
    assert h.reliability(0.9) == 0.9
    for _ in range(4):
        h.recent.append(False)
    h.consecutive_errors = 4
    assert h.reliability(0.9) < 0.3
    assert h.as_dict()["success_rate"] == 0.0


async def test_history_attach_for_snapshot_metrics():
    class OI(Fake):
        async def fetch(self, client, now):
            r = ProviderResult(self.id, now)
            r.scalar("BTC_OPEN_INTEREST_BTC", 110.0, now)
            return r
    stored = {"hl": [(NOW - timedelta(hours=24), 100.0)]}
    c = Collector({"hl": OI("hl", "ok")}, history_loader=lambda m: stored if m == "BTC_OPEN_INTEREST_BTC" else {})
    ctx = await c.collect(NOW)
    s = ctx.series["BTC_OPEN_INTEREST_BTC"]["hl"]
    assert s.pct_change(24 * 3600) == pytest.approx(10.0)


@respx.mock
async def test_get_json_retry_and_block():
    calls = {"n": 0}

    def flaky(request):
        calls["n"] += 1
        return httpx.Response(502) if calls["n"] == 1 else httpx.Response(200, json={"ok": 1})
    respx.get("https://x.test/a").mock(side_effect=flaky)
    respx.get("https://x.test/b").mock(return_value=httpx.Response(403, text="Just a moment"))
    respx.get("https://x.test/c").mock(return_value=httpx.Response(503))
    async with httpx.AsyncClient() as cl:
        assert await get_json(cl, "https://x.test/a") == {"ok": 1}
        with pytest.raises(ProviderBlocked):
            await get_json(cl, "https://x.test/b")
        with pytest.raises(httpx.HTTPStatusError):
            await get_json(cl, "https://x.test/c", retries=0)


def test_series_helpers():
    s = Series("p", [(NOW, 2.0), (NOW - timedelta(hours=2), 1.0)])
    assert s.points[0][1] == 1.0 and s.last() == 2.0
    assert s.pct_change(7200) == pytest.approx(100.0) and s.abs_change(7200) == 1.0
    assert s.at_or_before(NOW - timedelta(hours=5)) is None
    assert s.mean_since(10_000) == 1.5 and len(s.cut(NOW - timedelta(hours=1))) == 1
    assert Series("p", []).pct_change(10) is None and Series("p", []).mean_since(1) is None


def test_context_cut_has_no_lookahead(bull_ctx):
    t = NOW - timedelta(hours=10)
    c = bull_ctx.cut(t)
    assert all(x.ts + timedelta(hours=1) <= t for x in c.candles["spot_1h"])
    assert all(e["ts"] <= t for e in c.events["whale_transfers"])
    assert all(d.ts <= t for ds in c.scalars.values() for d in ds)
    bull_ctx.add_scalar("BTC_PRICE_USD", Datum(1.0, "kraken", NOW))
    assert len([d for d in bull_ctx.scalars["BTC_PRICE_USD"] if d.provider == "kraken"]) == 1  # replaced, not duplicated
