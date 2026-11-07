"""Persistence operations. Callers pass a Database; nothing here holds global state."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from sqlalchemy import delete, func, select

from app.core.context import Candle, MarketContext
from app.db.models import (
    BacktestRun, Candle1m, ConfigVersion, EtfFlow, LiquidationEvent, Observation, Signal, SignalOutcome,
    StateKV, SystemLog, WeightRecommendation,
)
from app.db.session import Database, as_utc
from app.normalize.metrics import METRICS
from app.scoring.config import ScoringConfig, default_config


class Repo:
    def __init__(self, db: Database):
        self.db = db
        self._last_obs: dict[tuple[str, str], datetime] = {}

    # ---------------- observations ----------------
    def save_observations(self, ctx: MarketContext) -> int:
        rows = []
        for metric, ds in ctx.scalars.items():
            for d in ds:
                key = (metric, d.provider)
                if self._last_obs.get(key) == d.ts:
                    continue  # cached provider result, already stored
                self._last_obs[key] = d.ts
                rows.append(Observation(ts=d.ts, fetched_at=ctx.now, metric=metric, provider=d.provider,
                                        value=d.value, meta=d.meta or None))
        if rows:
            with self.db.session() as s:
                s.add_all(rows)
        return len(rows)

    def load_history(self, metric: str, hours: int = 48) -> dict[str, list[tuple[datetime, float]]]:
        since = datetime.now(timezone.utc) - timedelta(hours=hours)
        with self.db.session() as s:
            res = s.execute(select(Observation.provider, Observation.ts, Observation.value)
                            .where(Observation.metric == metric, Observation.ts >= since)
                            .order_by(Observation.ts)).all()
        out: dict[str, list[tuple[datetime, float]]] = {}
        for p, ts, v in res:
            out.setdefault(p, []).append((as_utc(ts), v))
        return out

    def tracked_metrics(self) -> list[str]:
        return [m for m, sp in METRICS.items() if sp.track_history]

    def prune(self, keep_days: int = 30) -> None:
        cut = datetime.now(timezone.utc) - timedelta(days=keep_days)
        with self.db.session() as s:
            s.execute(delete(Observation).where(Observation.ts < cut))
            s.execute(delete(LiquidationEvent).where(LiquidationEvent.ts < cut))
            s.execute(delete(SystemLog).where(SystemLog.ts < cut))

    # ---------------- signals ----------------
    def save_signal(self, d: dict, config_version: int = 1) -> int:
        comps = {n: {k: c[k] for k in ("score", "confidence", "stance", "available", "weight", "contribution",
                                       "confirmation") if k in c} for n, c in d["components"].items()}
        sources = {n: {"signals": c["signals"], "inputs": c["inputs"]} for n, c in d["components"].items()}
        sig = Signal(ts=datetime.fromisoformat(d["ts"]), price=d["price"], direction=d["direction"], trade=d["trade"],
                     score=d["score"], confidence=d["confidence"], confidence_class=d["confidence_class"],
                     data_quality=d["data_quality"]["score"],
                     entry_low=(d["entry"] or {}).get("low"), entry_high=(d["entry"] or {}).get("high"),
                     invalidation=d["invalidation"], regime=d["regime"], mode=d.get("mode", "live"),
                     components=comps, sources=sources, payload=d, config_version=config_version)
        with self.db.session() as s:
            s.add(sig)
            s.flush()
            return sig.id

    def last_signal(self) -> Signal | None:
        with self.db.session() as s:
            return s.execute(select(Signal).order_by(Signal.ts.desc()).limit(1)).scalar_one_or_none()

    def signal_history(self, since: datetime, limit: int = 2000) -> list[dict]:
        with self.db.session() as s:
            sigs = s.execute(select(Signal).where(Signal.ts >= since).order_by(Signal.ts.desc()).limit(limit)).scalars().all()
            ids = [x.id for x in sigs]
            outs = s.execute(select(SignalOutcome).where(SignalOutcome.signal_id.in_(ids))).scalars().all() if ids else []
        by_sig: dict[int, dict] = {}
        for o in outs:
            by_sig.setdefault(o.signal_id, {})[o.horizon_min] = {
                "return_pct": round(o.return_pct, 3), "mfe_pct": round(o.mfe_pct, 3), "mae_pct": round(o.mae_pct, 3),
                "win": o.win, "hit_invalidation": o.hit_invalidation}
        return [{"id": x.id, "ts": as_utc(x.ts).isoformat(), "price": x.price, "direction": x.direction,
                 "trade": x.trade, "score": x.score, "confidence": x.confidence, "confidence_class": x.confidence_class,
                 "data_quality": x.data_quality, "entry_low": x.entry_low, "entry_high": x.entry_high,
                 "invalidation": x.invalidation, "regime": x.regime, "outcomes": by_sig.get(x.id, {})} for x in sigs]

    def signals_pending_outcome(self, horizon_min: int, now: datetime, max_age_days: int = 3) -> list[Signal]:
        due_before = now - timedelta(minutes=horizon_min)
        with self.db.session() as s:
            done = select(SignalOutcome.signal_id).where(SignalOutcome.horizon_min == horizon_min)
            return list(s.execute(select(Signal).where(
                Signal.ts <= due_before, Signal.ts >= now - timedelta(days=max_age_days),
                Signal.mode == "live", Signal.id.not_in(done))).scalars().all())

    def save_outcome(self, **kw) -> None:
        with self.db.session() as s:
            s.add(SignalOutcome(**kw))

    def outcome_rows(self, horizon_min: int = 240, mode: str = "live") -> list[tuple[Signal, SignalOutcome]]:
        with self.db.session() as s:
            return list(s.execute(select(Signal, SignalOutcome).join(SignalOutcome, SignalOutcome.signal_id == Signal.id)
                                  .where(SignalOutcome.horizon_min == horizon_min, Signal.mode == mode)).all())

    def performance(self, since: datetime | None = None) -> dict:
        with self.db.session() as s:
            q = select(Signal.direction, Signal.trade, SignalOutcome.horizon_min, SignalOutcome.return_pct,
                       SignalOutcome.mfe_pct, SignalOutcome.mae_pct, SignalOutcome.win) \
                .join(SignalOutcome, SignalOutcome.signal_id == Signal.id).where(Signal.mode == "live")
            if since:
                q = q.where(Signal.ts >= since)
            rows = s.execute(q).all()
        from app.backtest.metrics import summarize
        out: dict = {}
        for h in sorted({r[2] for r in rows}):
            hr = [r for r in rows if r[2] == h and r[0] != "NEUTRAL"]
            trades = [r for r in hr if r[1] == "YES"]
            out[str(h)] = {"directional": summarize([r[3] for r in hr], [r[4] for r in hr], [r[5] for r in hr]),
                           "trades": summarize([r[3] for r in trades], [r[4] for r in trades], [r[5] for r in trades])}
        return out

    def similar_lookup(self):
        """(direction, score bucket) → (samples, win rate) at the 4h horizon, from live outcomes."""
        from app.scoring.decision import score_bucket
        rows = self.outcome_rows(240)
        table: dict[tuple[str, str], list[bool]] = {}
        for sig, out in rows:
            if sig.direction == "NEUTRAL" or out.win is None:
                continue
            table.setdefault((sig.direction, score_bucket(sig.score)), []).append(bool(out.win))

        def lookup(direction: str, bucket: str) -> tuple[int, float]:
            xs = table.get((direction, bucket), [])
            return len(xs), (sum(xs) / len(xs) if xs else 0.5)
        return lookup

    # ---------------- candles ----------------
    def upsert_candles_1m(self, candles: list[Candle]) -> None:
        if not candles:
            return
        with self.db.session() as s:
            for c in candles:
                s.merge(Candle1m(ts=c.ts, open=c.open, high=c.high, low=c.low, close=c.close, volume=c.volume))

    def candles_1m(self, start: datetime, end: datetime) -> list[Candle1m]:
        with self.db.session() as s:
            rows = s.execute(select(Candle1m).where(Candle1m.ts >= start, Candle1m.ts < end).order_by(Candle1m.ts)).scalars().all()
        for r in rows:
            r.ts = as_utc(r.ts)
        return list(rows)

    # ---------------- liquidations / ETF ----------------
    def save_liquidation(self, ev: dict) -> None:
        with self.db.session() as s:
            s.add(LiquidationEvent(ts=ev["ts"], venue=ev["venue"], side=ev["side"], btc=ev.get("btc", 0.0),
                                   price=ev.get("price", 0.0), usd=ev["usd"]))

    def upsert_etf_flows(self, rows: list[dict], source: str) -> int:
        with self.db.session() as s:
            for r in rows:
                day = datetime.fromisoformat(str(r["day"])[:10]).replace(tzinfo=timezone.utc)
                s.merge(EtfFlow(day=day, net_flow_usd=float(r["net_flow_usd"]), source=source))
        return len(rows)

    def etf_series(self) -> list[tuple[datetime, float]]:
        with self.db.session() as s:
            rows = s.execute(select(EtfFlow).order_by(EtfFlow.day.desc()).limit(60)).scalars().all()
        return sorted((as_utc(r.day), r.net_flow_usd) for r in rows)

    # ---------------- config ----------------
    def active_config(self) -> tuple[int, ScoringConfig]:
        with self.db.session() as s:
            row = s.execute(select(ConfigVersion).where(ConfigVersion.active.is_(True))
                            .order_by(ConfigVersion.id.desc()).limit(1)).scalar_one_or_none()
            if row is None:
                cfg = default_config()
                row = ConfigVersion(config=cfg.model_dump(), note="defaults", active=True)
                s.add(row)
                s.flush()
            return row.id, ScoringConfig.model_validate(row.config)

    def save_config(self, cfg: ScoringConfig, note: str) -> int:
        with self.db.session() as s:
            for old in s.execute(select(ConfigVersion).where(ConfigVersion.active.is_(True))).scalars():
                old.active = False
            row = ConfigVersion(config=cfg.model_dump(), note=note, active=True)
            s.add(row)
            s.flush()
            return row.id

    def config_history(self, limit: int = 20) -> list[dict]:
        with self.db.session() as s:
            rows = s.execute(select(ConfigVersion).order_by(ConfigVersion.id.desc()).limit(limit)).scalars().all()
        return [{"id": r.id, "created_at": as_utc(r.created_at).isoformat(), "note": r.note, "active": r.active} for r in rows]

    # ---------------- recommendations / backtests ----------------
    def save_recommendation(self, source: str, active: dict, rec: dict, stats: dict) -> int:
        with self.db.session() as s:
            r = WeightRecommendation(source=source, active_weights=active, recommended_weights=rec, stats=stats)
            s.add(r)
            s.flush()
            return r.id

    def recommendations(self, limit: int = 10) -> list[dict]:
        with self.db.session() as s:
            rows = s.execute(select(WeightRecommendation).order_by(WeightRecommendation.id.desc()).limit(limit)).scalars().all()
        return [{"id": r.id, "created_at": as_utc(r.created_at).isoformat(), "source": r.source, "status": r.status,
                 "active_weights": r.active_weights, "recommended_weights": r.recommended_weights, "stats": r.stats}
                for r in rows]

    def decide_recommendation(self, rec_id: int, approve: bool) -> dict | None:
        with self.db.session() as s:
            r = s.get(WeightRecommendation, rec_id)
            if r is None or r.status != "pending":
                return None
            r.status = "approved" if approve else "rejected"
            r.decided_at = datetime.now(timezone.utc)
            return dict(r.recommended_weights)

    def create_backtest(self, params: dict) -> int:
        with self.db.session() as s:
            r = BacktestRun(params=params)
            s.add(r)
            s.flush()
            return r.id

    def finish_backtest(self, run_id: int, results: dict | None, error: str | None = None) -> None:
        with self.db.session() as s:
            r = s.get(BacktestRun, run_id)
            r.status = "error" if error else "done"
            r.results, r.error = results, error

    def backtests(self, limit: int = 10) -> list[dict]:
        with self.db.session() as s:
            rows = s.execute(select(BacktestRun).order_by(BacktestRun.id.desc()).limit(limit)).scalars().all()
        return [{"id": r.id, "created_at": as_utc(r.created_at).isoformat(), "status": r.status, "params": r.params,
                 "results": r.results, "error": r.error} for r in rows]

    # ---------------- kv / logs ----------------
    def kv_set(self, key: str, value: dict) -> None:
        with self.db.session() as s:
            s.merge(StateKV(key=key, value=value, updated_at=datetime.now(timezone.utc)))

    def kv_get(self, key: str) -> tuple[dict | None, datetime | None]:
        with self.db.session() as s:
            row = s.get(StateKV, key)
            return (row.value, as_utc(row.updated_at)) if row else (None, None)

    def log(self, level: str, source: str, message: str) -> None:
        with self.db.session() as s:
            s.add(SystemLog(level=level, source=source, message=message[:4000]))

    def logs(self, limit: int = 200, level: str | None = None) -> list[dict]:
        with self.db.session() as s:
            q = select(SystemLog).order_by(SystemLog.id.desc()).limit(limit)
            if level:
                q = q.where(SystemLog.level == level)
            rows = s.execute(q).scalars().all()
        return [{"ts": as_utc(r.ts).isoformat(), "level": r.level, "source": r.source, "message": r.message} for r in rows]

    def counts(self) -> dict:
        with self.db.session() as s:
            return {"signals": s.scalar(select(func.count(Signal.id))),
                    "outcomes": s.scalar(select(func.count(SignalOutcome.id))),
                    "observations": s.scalar(select(func.count(Observation.id)))}
