import asyncio

import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.main import create_app
from app.scoring.decision import decide
from datetime import datetime, timezone

from conftest import build_ctx


def fresh_ctx(direction: int = 1):
    """Scenario stamped at the current hour so freshness/history windows behave like live data."""
    return build_ctx(direction, now=datetime.now(timezone.utc))

AUTH = {"Authorization": "Bearer test-admin"}


@pytest.fixture
def client():
    app = create_app(Settings(database_url="sqlite://", admin_token="test-admin", liquidation_stream=False,
                              redis_url=None), start_worker=False)
    with TestClient(app) as c:
        yield c


@pytest.fixture
def seeded(client):
    svc = client.app.state.svc
    d = decide(fresh_ctx(1), svc.config)
    d["signal_id"] = svc.repo.save_signal(d)
    svc.latest = d
    return client


def test_signal_warming_up(client):
    assert client.get("/api/v1/btc/signal").status_code == 503
    h = client.get("/api/v1/system/health").json()
    assert h["status"] == "degraded" and h["db"] is True


def test_public_endpoints(seeded):
    s = seeded.get("/api/v1/btc/signal").json()
    assert s["direction"] == "BULLISH" and s["trade"] in ("YES", "NO") and s["entry"]["low"] < s["entry"]["high"]
    assert "components" not in s and s["data_quality"]["label"] in ("HIGH", "MEDIUM", "LOW")
    assert seeded.get("/api/v1/btc/market").json()["funding"]["funding_pct_8h"]
    comps = seeded.get("/api/v1/btc/components").json()
    assert len(comps["components"]) == 24 and comps["trade_quality"]["gates"]
    src = seeded.get("/api/v1/btc/sources").json()
    assert src["summary"]["reference_sources"] == 40 and len(src["sources"]) > 40
    assert any(x["id"] == "solscan" and x["relevance_score"] == 0 for x in src["sources"])
    hist = seeded.get("/api/v1/btc/history?window=7D").json()
    assert hist["signals"][0]["direction"] == "BULLISH"
    assert seeded.get("/api/v1/btc/history?window=1y").status_code == 422
    perf = seeded.get("/api/v1/btc/performance").json()
    assert perf["backtest"] is None and "guarantee" in perf["note"]
    assert seeded.get("/api/v1/system/data-quality").json()["score"] > 60
    assert seeded.get("/api/v1/system/providers").json()["providers"]


def test_admin_auth(seeded):
    assert seeded.get("/api/v1/admin/config").status_code == 401
    assert seeded.get("/api/v1/admin/config", headers={"Authorization": "Bearer nope"}).status_code == 401
    assert seeded.get("/api/v1/admin/config", headers=AUTH).status_code == 200


def test_admin_without_token_is_local_only():
    app = create_app(Settings(database_url="sqlite://", admin_token=None, liquidation_stream=False), start_worker=False)
    with TestClient(app) as c:  # TestClient connects as a local client
        assert c.get("/api/v1/admin/config").status_code == 200
    with TestClient(app, client=("203.0.113.9", 5000)) as c:
        assert c.get("/api/v1/admin/config").status_code == 403


def test_admin_config_weights_and_providers(seeded):
    r = seeded.put("/api/v1/admin/config", headers=AUTH, json={"patch": {"weights": {"trend": 20}}, "note": "raise trend"})
    assert r.status_code == 200 and r.json()["config"]["weights"]["trend"] == 20
    assert seeded.put("/api/v1/admin/config", headers=AUTH, json={"patch": {"weights": {"x": 1}}}).status_code == 422
    assert seeded.post("/api/v1/admin/providers/bybit", headers=AUTH, json={"enabled": False}).json()["enabled"] is False
    assert seeded.app.state.svc.config.provider_enabled("bybit") is False
    assert seeded.post("/api/v1/admin/providers/nope", headers=AUTH, json={"enabled": False}).status_code == 404
    cfg = seeded.get("/api/v1/admin/config", headers=AUTH).json()
    assert cfg["version"] == 3 and len(cfg["history"]) == 3


def test_admin_recommendation_flow(seeded):
    svc = seeded.app.state.svc
    rid = svc.repo.save_recommendation("backtest:1", svc.config.weights, {**svc.config.weights, "cvd": 15}, {})
    assert seeded.get("/api/v1/admin/recommendations", headers=AUTH).json()["recommendations"][0]["status"] == "pending"
    assert seeded.post(f"/api/v1/admin/recommendations/{rid}/maybe", headers=AUTH).status_code == 400
    assert seeded.post(f"/api/v1/admin/recommendations/{rid}/approve", headers=AUTH).json()["status"] == "approved"
    assert svc.config.weights["cvd"] == 15  # applied only after approval
    assert seeded.post(f"/api/v1/admin/recommendations/{rid}/approve", headers=AUTH).status_code == 409
    rid2 = svc.repo.save_recommendation("live", svc.config.weights, {**svc.config.weights, "cvd": 1}, {})
    assert seeded.post(f"/api/v1/admin/recommendations/{rid2}/reject", headers=AUTH).json()["status"] == "rejected"
    assert svc.config.weights["cvd"] == 15


def test_admin_etf_import_logs_signal_detail(seeded):
    body = {"source": "farside.co.uk (transcribed)", "rows": [{"day": "2026-10-05", "net_flow_usd": 4.2e8}]}
    assert seeded.post("/api/v1/admin/etf-flows", headers=AUTH, json=body).json()["imported"] == 1
    assert seeded.post("/api/v1/admin/etf-flows", headers=AUTH, json={"source": "x", "rows": []}).status_code == 422
    assert seeded.app.state.svc.providers["manual_etf"].is_configured()
    assert "logs" in seeded.get("/api/v1/admin/logs", headers=AUTH).json()
    sid = seeded.app.state.svc.latest["signal_id"]
    assert seeded.get(f"/api/v1/admin/signals/{sid}", headers=AUTH).json()["direction"] == "BULLISH"
    assert seeded.get("/api/v1/admin/signals/99999", headers=AUTH).status_code == 404
    assert seeded.get("/api/v1/admin/backtests", headers=AUTH).json()["runs"] == []


def test_admin_backtest_job_records_failure(seeded, monkeypatch):
    import app.main as main

    async def boom(cfg, days, step):
        raise RuntimeError("no network in tests")
    monkeypatch.setattr(main, "run_backtest", boom)
    r = seeded.post("/api/v1/admin/backtest", headers=AUTH, json={"days": 30, "step_hours": 4})
    assert r.json()["status"] == "running"
    runs = seeded.get("/api/v1/admin/backtests", headers=AUTH).json()["runs"]
    assert runs[0]["status"] in ("running", "error")
    assert seeded.post("/api/v1/admin/backtest", headers=AUTH, json={"days": 5}).status_code == 422


def test_admin_backtest_job_success(seeded, monkeypatch):
    import app.main as main

    async def fake(cfg, days, step):
        return {"recommendation": {"active": {"trend": 12}, "recommended": {"trend": 13}, "stats": {}},
                "splits": {}, "regimes": {}, "caveat": "c", "params": {}, "sample": []}
    monkeypatch.setattr(main, "run_backtest", fake)
    seeded.post("/api/v1/admin/backtest", headers=AUTH, json={"days": 30})
    svc = seeded.app.state.svc
    for _ in range(50):
        if svc.repo.backtests()[0]["status"] == "done":
            break
        asyncio.run(asyncio.sleep(0.02))
    assert svc.repo.backtests()[0]["status"] == "done"
    assert svc.repo.recommendations()[0]["source"].startswith("backtest:")
    assert seeded.get("/api/v1/btc/performance").json()["backtest"]["caveat"] == "c"


def test_service_cycle_persists_and_publishes(client, monkeypatch):
    svc = client.app.state.svc

    async def fake_collect(now=None):
        return fresh_ctx(1)
    monkeypatch.setattr(svc.collector, "collect", fake_collect)
    q = svc.subscribe()
    d = asyncio.run(svc.run_cycle())
    assert d["signal_id"] and q.get_nowait()["ts"] == d["ts"]
    d2 = asyncio.run(svc.run_cycle())
    assert "signal_id" not in d2  # same state within the record interval → not re-stored
    assert svc.cache.get("btc:signal:latest")["ts"] == d2["ts"]
    svc.unsubscribe(q)
    assert client.get("/api/v1/btc/signal").json()["direction"] == "BULLISH"
    assert client.get("/api/v1/system/health").json()["status"] == "ok"
