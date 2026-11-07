"""FastAPI application."""
from __future__ import annotations

import asyncio
import hmac
import json
import logging
from contextlib import asynccontextmanager
from datetime import datetime, timezone

from fastapi import Depends, FastAPI, Header, HTTPException, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from app.backtest.runner import run_backtest
from app.config import Settings, get_settings
from app.db.session import Database
from app.scoring.config import ScoringConfig, merge
from app.service import SignalService, history_window

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logging.getLogger("httpx").setLevel(logging.WARNING)
log = logging.getLogger("btcsignal.api")


class DBLogHandler(logging.Handler):
    """Mirror warnings/errors into system_logs for the admin panel."""

    def __init__(self, svc: SignalService):
        super().__init__(level=logging.WARNING)
        self.svc = svc

    def emit(self, record: logging.LogRecord) -> None:
        try:
            self.svc.repo.log(record.levelname, record.name, record.getMessage())
        except Exception:  # pragma: no cover - logging must never raise
            pass


# ---------------- request bodies ----------------
class ConfigPatch(BaseModel):
    patch: dict
    note: str = Field("admin edit", max_length=500)


class ProviderToggle(BaseModel):
    enabled: bool


class BacktestRequest(BaseModel):
    days: int = Field(90, ge=14, le=365)
    step_hours: int = Field(4, ge=1, le=24)


class EtfRow(BaseModel):
    day: str = Field(pattern=r"^\d{4}-\d{2}-\d{2}$")
    net_flow_usd: float


class EtfImport(BaseModel):
    source: str = Field(min_length=3, max_length=64, description="Where the figures were published")
    rows: list[EtfRow] = Field(min_length=1, max_length=400)


def create_app(settings: Settings | None = None, start_worker: bool | None = None) -> FastAPI:
    settings = settings or get_settings()
    run_worker = settings.run_worker_in_api if start_worker is None else start_worker

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        db = Database(settings.database_url)
        db.create_all()
        svc = SignalService(settings, db)
        handler = DBLogHandler(svc)
        logging.getLogger("btcsignal").addHandler(handler)
        app.state.svc = svc
        app.state.backtests = set()
        if run_worker:
            svc.start()
        yield
        logging.getLogger("btcsignal").removeHandler(handler)
        await svc.stop()

    app = FastAPI(title="BTC Signal", version="1.0.0", lifespan=lifespan,
                  description="Bitcoin market-condition intelligence. Not financial advice.")
    app.add_middleware(CORSMiddleware, allow_origins=[o.strip() for o in settings.cors_origins.split(",")],
                       allow_methods=["*"], allow_headers=["*"])

    def svc(request: Request) -> SignalService:
        return request.app.state.svc

    def admin(request: Request, authorization: str | None = Header(default=None)) -> None:
        if not settings.admin_token:
            # No token configured: admin works without login, but only from this machine.
            host = request.client.host if request.client else ""
            if host in ("127.0.0.1", "::1", "localhost", "testclient"):
                return
            raise HTTPException(403, "Admin is local-only. Set BTCS_ADMIN_TOKEN to use it remotely.")
        token = (authorization or "").removeprefix("Bearer ").strip()
        if not hmac.compare_digest(token, settings.admin_token):
            raise HTTPException(401, "invalid admin token")

    def latest_or_503(s: SignalService) -> dict:
        d = s.get_latest()
        if not d:
            raise HTTPException(503, "No signal computed yet — the engine is warming up")
        return d

    # ---------------- public ----------------
    @app.get("/api/v1/btc/signal")
    def signal(s: SignalService = Depends(svc)):
        d = latest_or_503(s)
        return {k: d[k] for k in ("asset", "ts", "price", "change_24h_pct", "direction", "direction_note", "score",
                                  "confidence", "confidence_class", "trade", "entry", "invalidation",
                                  "levels_reference_only", "regime", "summary", "why", "disclaimer", "version")} | {
            "levels": d.get("levels"), "support_resistance": d.get("support_resistance"),
            "regime_detail": d.get("regime_detail"),
            "data_quality": {k: d["data_quality"][k] for k in ("score", "label", "critical_stale", "reasons")},
            "trade_blocking": d["trade_quality"]["blocking"],
            "age_s": round((datetime.now(timezone.utc) - datetime.fromisoformat(d["ts"])).total_seconds(), 1)}

    @app.get("/api/v1/btc/market")
    def market(s: SignalService = Depends(svc)):
        d = latest_or_503(s)
        c = d["components"]
        return {"ts": d["ts"], "price": d["price"], "change_24h_pct": d["change_24h_pct"],
                "price_consensus": d["data_quality"]["price_consensus"], "regime": d["regime"],
                "funding": c["funding"]["inputs"], "open_interest": c["open_interest"]["inputs"],
                "volatility": c["volatility"]["inputs"], "flags": d["flags"]}

    @app.get("/api/v1/btc/components")
    def components(s: SignalService = Depends(svc)):
        d = latest_or_503(s)
        return {"ts": d["ts"], "score": d["score"], "raw_score": d["raw_score"], "coverage": d["coverage"],
                "categories": d.get("categories", {}),
                "agreement": d["agreement"], "confidence_parts": d["confidence_parts"],
                "trade_quality": d["trade_quality"], "components": d["components"]}

    @app.get("/api/v1/btc/sources")
    def sources(s: SignalService = Depends(svc)):
        rows = s.sources_report()
        ref = [r for r in rows if r["number"]]
        return {"summary": {"reference_sources": len(ref), "with_adapter": sum(1 for r in ref if r["adapter"]),
                            "live_now": sum(1 for r in ref if r["live"]),
                            "primary_providers_live": sum(1 for r in rows if not r["number"] and r["live"])},
                "sources": rows}

    @app.get("/api/v1/btc/history")
    def history(window: str = Query("24h", pattern="(?i)^(24h|7d|30d|90d)$"), limit: int = Query(500, le=5000),
                s: SignalService = Depends(svc)):
        return {"window": window, "signals": s.repo.signal_history(history_window(window), limit)}

    @app.get("/api/v1/btc/performance")
    def performance(window: str = Query("30d", pattern="(?i)^(24h|7d|30d|90d)$"), s: SignalService = Depends(svc)):
        bts = [b for b in s.repo.backtests(5) if b["status"] == "done"]
        latest_bt = bts[0] if bts else None
        return {"live": s.repo.performance(history_window(window)),
                "backtest": {k: latest_bt[k] for k in ("id", "created_at", "params")} | {
                    "splits": latest_bt["results"]["splits"], "regimes": latest_bt["results"]["regimes"],
                    "caveat": latest_bt["results"]["caveat"]} if latest_bt else None,
                "note": "Historical performance does not guarantee future results."}

    @app.get("/api/v1/btc/stream")
    async def stream(request: Request, s: SignalService = Depends(svc)):
        async def gen():
            q = s.subscribe()
            last_ts = None
            try:
                d = s.get_latest()
                if d:
                    last_ts = d["ts"]
                    yield f"event: signal\ndata: {json.dumps(d, default=str)}\n\n"
                while not await request.is_disconnected():
                    try:
                        d = await asyncio.wait_for(q.get(), timeout=5)
                    except asyncio.TimeoutError:
                        d = s.get_latest()  # covers a separate worker process
                        if not d or d["ts"] == last_ts:
                            yield ": keepalive\n\n"
                            continue
                    last_ts = d["ts"]
                    yield f"event: signal\ndata: {json.dumps(d, default=str)}\n\n"
            finally:
                s.unsubscribe(q)
        return StreamingResponse(gen(), media_type="text/event-stream",
                                 headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})

    @app.get("/api/v1/system/data-quality")
    def data_quality(s: SignalService = Depends(svc)):
        d = latest_or_503(s)
        return d["data_quality"] | {"ts": d["ts"]}

    @app.get("/api/v1/system/providers")
    def providers(s: SignalService = Depends(svc)):
        live = s.provider_health()
        if all(h["status"] == "pending" for h in live):
            live = s.cache.get("btc:providers:health") or live
        return {"providers": live}

    @app.get("/api/v1/system/health")
    def health(s: SignalService = Depends(svc)):
        d = s.get_latest()
        age = (datetime.now(timezone.utc) - datetime.fromisoformat(d["ts"])).total_seconds() if d else None
        try:
            counts = s.repo.counts()
            db_ok = True
        except Exception:
            counts, db_ok = {}, False
        stale = age is None or age > 5 * settings.cycle_seconds
        return {"status": "ok" if db_ok and not stale else "degraded", "db": db_ok,
                "redis": s.cache.redis is not None, "last_signal_age_s": age, "worker_in_process": run_worker,
                "liquidation_stream": s.liq_stream.connected_venues() if s.liq_stream else [], "counts": counts}

    # ---------------- admin ----------------
    @app.get("/api/v1/admin/config", dependencies=[Depends(admin)])
    def get_config(s: SignalService = Depends(svc)):
        s.reload_config()
        return {"version": s.config_version, "config": s.config.model_dump(), "history": s.repo.config_history()}


    @app.put("/api/v1/admin/config", dependencies=[Depends(admin)])
    def put_config(body: ConfigPatch, s: SignalService = Depends(svc)):
        try:
            new = merge(s.config, body.patch)
        except Exception as e:
            raise HTTPException(422, f"invalid config: {e}")
        vid = s.repo.save_config(new, body.note)
        s.reload_config()
        log.warning("config updated to version %s: %s", vid, body.note)
        return {"version": vid, "config": new.model_dump()}


    @app.post("/api/v1/admin/providers/{provider_id}", dependencies=[Depends(admin)])
    def toggle_provider(provider_id: str, body: ProviderToggle, s: SignalService = Depends(svc)):
        if provider_id not in s.providers:
            raise HTTPException(404, "unknown provider")
        cfg = ScoringConfig.model_validate(s.config.model_dump())
        cfg.providers_enabled[provider_id] = body.enabled
        vid = s.repo.save_config(cfg, f"{'enable' if body.enabled else 'disable'} provider {provider_id}")
        s.reload_config()
        return {"version": vid, "provider": provider_id, "enabled": body.enabled}

    @app.get("/api/v1/admin/recommendations", dependencies=[Depends(admin)])
    def recommendations(s: SignalService = Depends(svc)):
        return {"active_weights": s.config.weights, "recommendations": s.repo.recommendations()}

    @app.post("/api/v1/admin/recommendations/{rec_id}/{action}", dependencies=[Depends(admin)])
    def decide_recommendation(rec_id: int, action: str, s: SignalService = Depends(svc)):
        if action not in ("approve", "reject"):
            raise HTTPException(400, "action must be approve or reject")
        weights = s.repo.decide_recommendation(rec_id, action == "approve")
        if weights is None:
            raise HTTPException(409, "recommendation not found or already decided")
        if action == "approve":
            vid = s.repo.save_config(merge(s.config, {"weights": weights}), f"approved weight recommendation #{rec_id}")
            s.reload_config()
            return {"status": "approved", "version": vid}
        return {"status": "rejected"}


    @app.post("/api/v1/admin/backtest", dependencies=[Depends(admin)])
    async def start_backtest(body: BacktestRequest, request: Request, s: SignalService = Depends(svc)):
        run_id = s.repo.create_backtest(body.model_dump())

        async def job():
            try:
                res = await run_backtest(s.config, body.days, body.step_hours)
                rec = res["recommendation"]
                res["recommendation_id"] = s.repo.save_recommendation(
                    f"backtest:{run_id}", rec["active"], rec["recommended"], rec["stats"])
                s.repo.finish_backtest(run_id, res)
            except Exception as e:
                log.exception("backtest %s failed", run_id)
                s.repo.finish_backtest(run_id, None, f"{type(e).__name__}: {e}")
        task = asyncio.create_task(job())
        request.app.state.backtests.add(task)
        task.add_done_callback(request.app.state.backtests.discard)
        return {"run_id": run_id, "status": "running"}

    @app.get("/api/v1/admin/backtests", dependencies=[Depends(admin)])
    def backtests(s: SignalService = Depends(svc)):
        runs = s.repo.backtests()
        for r in runs:
            if r["results"]:
                r["results"] = {k: v for k, v in r["results"].items() if k != "sample"}
        return {"runs": runs}



    @app.post("/api/v1/admin/etf-flows", dependencies=[Depends(admin)])
    def import_etf(body: EtfImport, s: SignalService = Depends(svc)):
        n = s.repo.upsert_etf_flows([r.model_dump() for r in body.rows], body.source)
        return {"imported": n}

    @app.get("/api/v1/admin/logs", dependencies=[Depends(admin)])
    def logs(level: str | None = None, limit: int = Query(200, le=1000), s: SignalService = Depends(svc)):
        return {"logs": s.repo.logs(limit, level)}

    @app.get("/api/v1/admin/signals/{signal_id}", dependencies=[Depends(admin)])
    def signal_detail(signal_id: int, s: SignalService = Depends(svc)):
        from app.db.models import Signal
        with s.db.session() as sess:
            row = sess.get(Signal, signal_id)
            if not row:
                raise HTTPException(404, "not found")
            return row.payload

    return app


app = create_app()
