"""Source/engine performance learning → RECOMMENDED weights (never auto-applied).

For each engine: information coefficient (Pearson) between its score and the forward
return, on TRAIN. A change is only recommended when VALIDATION shows the same sign;
otherwise the weight is held. Negative-IC engines are reduced, never sign-flipped.
"""
from __future__ import annotations

from app.backtest.metrics import pearson

MIN_SAMPLES = 60
MAX_UP, MAX_DOWN = 1.75, 0.4


def _ic(rows: list[dict], engine: str, horizon: int) -> tuple[float, int]:
    xs, ys = [], []
    for r in rows:
        s = r["components"].get(engine)
        o = r["outcomes"].get(horizon)
        if s is None or o is None:
            continue
        xs.append(s)
        ys.append(o["raw"])
    return pearson(xs, ys), len(xs)


def recommend_weights(train: list[dict], validation: list[dict], active: dict[str, float], horizon: int = 4) -> dict:
    stats, rec = {}, {}
    for engine, w in active.items():
        ic_t, n_t = _ic(train, engine, horizon)
        ic_v, n_v = _ic(validation, engine, horizon)
        action = "hold"
        new_w = w
        if n_t >= MIN_SAMPLES and n_v >= MIN_SAMPLES // 3 and w > 0:
            consistent = (ic_t > 0) == (ic_v > 0) and abs(ic_t) > 0.02 and abs(ic_v) > 0.01
            if consistent and ic_t > 0:
                new_w = w * min(MAX_UP, 1 + 4 * ic_t)
                action = "increase"
            elif consistent and ic_t < 0:
                new_w = w * max(MAX_DOWN, 1 + 4 * ic_t)
                action = "decrease"
        elif n_t < MIN_SAMPLES:
            action = "insufficient_data"
        rec[engine] = round(new_w, 3)
        stats[engine] = {"ic_train": round(ic_t, 4), "n_train": n_t, "ic_validation": round(ic_v, 4),
                         "n_validation": n_v, "action": action}
    # keep the total weight budget unchanged so only relative influence moves
    total_a, total_r = sum(active.values()), sum(rec.values())
    if total_r > 0:
        rec = {k: round(v * total_a / total_r, 3) for k, v in rec.items()}
    return {"horizon_h": horizon, "recommended": rec, "active": dict(active), "stats": stats,
            "note": "Recommendation only. An admin must approve before it becomes the active weighting."}
