"""Walk-forward historical replay through the exact live decision code.

No parameter is fitted inside the replay, so TRAIN / VALIDATION / TEST are chronological
evaluation windows: weights are only ever *recommended* from TRAIN, checked on VALIDATION,
and TEST stays untouched for an out-of-sample read.
"""
from __future__ import annotations

import asyncio
import math
from datetime import datetime, timedelta, timezone

from app.backtest.history import BACKTEST_ENGINES, fetch_history, step_context
from app.backtest.metrics import classification_stats, forward_outcome, summarize
from app.core.context import MarketContext
from app.engines.indicators import realized_vol
from app.learning.weights import recommend_weights
from app.scoring.config import ScoringConfig
from app.scoring.decision import decide

HORIZONS_H = (1, 4, 12, 24)
SPLITS = (("train", 0.6), ("validation", 0.2), ("test", 0.2))


def _regimes(full: MarketContext, t: datetime, closes_by_ts: dict, rv_cut: tuple[float, float]) -> list[str]:
    tags = []
    p_now = closes_by_ts.get(t)
    p_30 = closes_by_ts.get(t - timedelta(days=30))
    if p_now and p_30:
        r = (p_now / p_30 - 1) * 100
        tags.append("bull" if r > 10 else "bear" if r < -10 else "sideways")
    window = [closes_by_ts[t - timedelta(hours=h)] for h in range(24, -1, -1) if (t - timedelta(hours=h)) in closes_by_ts]
    rv = realized_vol(window)
    tags.append("high_vol" if rv >= rv_cut[1] else "low_vol" if rv <= rv_cut[0] else "mid_vol")
    return tags


def run_replay(full: MarketContext, cfg: ScoringConfig, start: datetime, end: datetime, step_hours: int = 4) -> dict:
    cfg = cfg.model_copy(deep=True)
    # Engines without free history are dropped from the replay rather than faked.
    cfg.weights = {k: (w if k in BACKTEST_ENGINES else 0.0) for k, w in cfg.weights.items()}
    spot = full.candles["spot_1h"]
    by_ts = {c.ts: c for c in spot}
    closes_by_ts = {c.ts + timedelta(hours=1): c.close for c in spot}
    rvs = sorted(realized_vol([c.close for c in spot[i - 24:i + 1]]) for i in range(24, len(spot), 24))
    rv_cut = (rvs[len(rvs) // 3], rvs[2 * len(rvs) // 3]) if rvs else (0, math.inf)
    rows = []
    t = start
    while t <= end - timedelta(hours=max(HORIZONS_H)):
        ctx = step_context(full, t)
        d = decide(ctx, cfg)
        entry_bar = by_ts.get(t)
        if entry_bar is None:
            t += timedelta(hours=step_hours)
            continue
        row = {"ts": t.isoformat(), "price": entry_bar.open, "direction": d["direction"], "trade": d["trade"],
               "score": d["score"], "confidence": d["confidence"], "data_quality": d["data_quality"]["score"],
               "components": {n: c["score"] for n, c in d["components"].items() if c["available"] and c["directional"]},
               "categories": {k: v["score"] for k, v in d["categories"].items() if v["available"]},
               "indicators": {f"{n}.{i['name']}{'.' + i['timeframe'] if i['timeframe'] else ''}": i["score"]
                              for n, c in d["components"].items() if c["available"] and c["directional"]
                              for i in c["indicators"]},
               "regimes": _regimes(full, t, closes_by_ts, rv_cut), "outcomes": {}}
        for h in HORIZONS_H:
            path = [by_ts.get(t + timedelta(hours=i)) for i in range(h)]
            if any(p is None for p in path):
                continue
            bars = [(p.high, p.low, p.close) for p in path]
            o = forward_outcome(d["direction"], entry_bar.open, bars, d["invalidation"] if d["trade"] == "YES" else None)
            raw = forward_outcome("BULLISH", entry_bar.open, bars)  # undirected move, for IC
            row["outcomes"][h] = {"ret": o.return_pct, "mfe": o.mfe_pct, "mae": o.mae_pct, "raw": raw.return_pct,
                                  "raw_mfe": raw.mfe_pct, "raw_mae": raw.mae_pct}
        big = row["outcomes"].get(24, {}).get("raw")
        if big is not None and abs(big) >= 5:
            row["regimes"].append("major_rally" if big > 0 else "major_crash")
        rows.append(row)
        t += timedelta(hours=step_hours)
    return {"rows": rows}


def _section(rows: list[dict]) -> dict:
    out = {"steps": len(rows), "directional_calls": sum(r["direction"] != "NEUTRAL" for r in rows),
           "trades": sum(r["trade"] == "YES" for r in rows), "by_horizon": {}}
    for h in HORIZONS_H:
        d = [r for r in rows if r["direction"] != "NEUTRAL" and h in r["outcomes"]]
        tr = [r for r in d if r["trade"] == "YES"]
        out["by_horizon"][f"{h}h"] = {
            "directional": summarize([r["outcomes"][h]["ret"] for r in d], [r["outcomes"][h]["mfe"] for r in d],
                                     [r["outcomes"][h]["mae"] for r in d]),
            "trades": summarize([r["outcomes"][h]["ret"] for r in tr], [r["outcomes"][h]["mfe"] for r in tr],
                                [r["outcomes"][h]["mae"] for r in tr]),
        }
    return out


def evaluate(rows: list[dict], cfg: ScoringConfig) -> dict:
    n = len(rows)
    splits, i = {}, 0
    for name, frac in SPLITS:
        j = n if name == "test" else i + int(n * frac)
        splits[name] = rows[i:j]
        i = j
    results = {"splits": {k: _section(v) | {"from": v[0]["ts"] if v else None, "to": v[-1]["ts"] if v else None}
                          for k, v in splits.items()},
               "regimes": {}}
    tags = sorted({t for r in rows for t in r["regimes"]})
    for tag in tags:
        results["regimes"][tag] = _section([r for r in rows if tag in r["regimes"]])
    results["recommendation"] = recommend_weights(splits["train"], splits["validation"], cfg.weights, horizon=4)
    results["indicator_stats"] = indicator_stats(rows, splits)
    results["engines_replayed"] = list(BACKTEST_ENGINES)
    results["engines_missing"] = [k for k in cfg.weights if k not in BACKTEST_ENGINES]
    results["caveat"] = ("Partial-engine replay: whale, ETF, exchange-flow, liquidation and smart-money engines have no "
                         "free history and are excluded. Live performance is tracked separately from stored signals.")
    return results


async def run_backtest(cfg: ScoringConfig, days: int = 90, step_hours: int = 4) -> dict:
    end = datetime.now(timezone.utc).replace(minute=0, second=0, microsecond=0)
    full = await fetch_history(days, end)
    start = end - timedelta(days=days)
    # CPU-bound replay runs off the event loop so the live signal cycle keeps ticking
    replay = await asyncio.to_thread(run_replay, full, cfg, start, end, step_hours)
    res = evaluate(replay["rows"], cfg)
    res["params"] = {"days": days, "step_hours": step_hours, "start": start.isoformat(), "end": end.isoformat()}
    res["sample"] = replay["rows"][-50:]
    return res


def indicator_stats(rows: list[dict], splits: dict[str, list[dict]], horizon: int = 4) -> dict:
    """Backtest every indicator, every category and the final score on its own (spec §27 Z).
    Reported on the full sample and on the out-of-sample TEST split."""
    def stats_for(rs: list[dict], getter) -> dict:
        data = []
        for r in rs:
            o = r["outcomes"].get(horizon)
            v = getter(r)
            if o is None or v is None:
                continue
            data.append((v, o["raw"], o.get("raw_mfe", 0.0), o.get("raw_mae", 0.0)))
        return classification_stats(data, horizon)

    keys = sorted({k for r in rows for k in r.get("indicators", {})})
    cats = sorted({k for r in rows for k in r.get("categories", {})})
    out: dict = {"horizon_h": horizon, "indicators": {}, "categories": {}, "final": {}}
    for label, rs in (("all", rows), ("test", splits.get("test", []))):
        for k in keys:
            out["indicators"].setdefault(k, {})[label] = stats_for(rs, lambda r, k=k: r.get("indicators", {}).get(k))
        for k in cats:
            out["categories"].setdefault(k, {})[label] = stats_for(rs, lambda r, k=k: r.get("categories", {}).get(k))
        out["final"][label] = stats_for(rs, lambda r: r["score"])
    return out
