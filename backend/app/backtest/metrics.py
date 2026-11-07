"""Outcome math shared by the live evaluator and the historical backtester."""
from __future__ import annotations

from dataclasses import dataclass


@dataclass
class Outcome:
    return_pct: float
    mfe_pct: float
    mae_pct: float
    hit_invalidation: bool
    win: bool | None
    exit_price: float


def forward_outcome(direction: str, entry: float, path: list[tuple[float, float, float]],
                    invalidation: float | None = None) -> Outcome:
    """path: chronological (high, low, close) bars after entry.
    Returns are measured in the signal's direction; NEUTRAL reports the raw move and no win/loss.
    If the invalidation level is touched, the trade is closed there (stop)."""
    sgn = -1 if direction == "BEARISH" else 1
    best = worst = 0.0
    exit_price = path[-1][2]
    hit = False
    for high, low, close in path:
        up, down = (high / entry - 1) * 100, (low / entry - 1) * 100
        fav, adv = (up, down) if sgn > 0 else (-down, -up)
        best, worst = max(best, fav), min(worst, adv)
        if invalidation and direction != "NEUTRAL":
            if (sgn > 0 and low <= invalidation) or (sgn < 0 and high >= invalidation):
                hit, exit_price = True, invalidation
                break
        exit_price = close
    ret = sgn * (exit_price / entry - 1) * 100
    return Outcome(return_pct=ret, mfe_pct=best, mae_pct=worst, hit_invalidation=hit,
                   win=None if direction == "NEUTRAL" else ret > 0, exit_price=exit_price)


def summarize(returns: list[float], mfes: list[float] | None = None, maes: list[float] | None = None) -> dict:
    n = len(returns)
    if n == 0:
        return {"n": 0}
    wins = [r for r in returns if r > 0]
    losses = [r for r in returns if r <= 0]
    wr = len(wins) / n
    avg_w = sum(wins) / len(wins) if wins else 0.0
    avg_l = -sum(losses) / len(losses) if losses else 0.0
    equity = peak = dd = 0.0
    for r in returns:
        equity += r
        peak = max(peak, equity)
        dd = min(dd, equity - peak)
    srt = sorted(returns)
    out = {
        "n": n, "win_rate": round(wr, 4), "avg_return_pct": round(sum(returns) / n, 4),
        "median_return_pct": round(srt[n // 2], 4), "expectancy_pct": round(wr * avg_w - (1 - wr) * avg_l, 4),
        "avg_win_pct": round(avg_w, 4), "avg_loss_pct": round(avg_l, 4),
        "profit_factor": round(sum(wins) / -sum(losses), 3) if losses and sum(losses) < 0 else None,
        "max_drawdown_pct": round(dd, 4), "total_return_pct": round(sum(returns), 4),
    }
    if mfes:
        out["avg_mfe_pct"] = round(sum(mfes) / len(mfes), 4)
    if maes:
        out["avg_mae_pct"] = round(sum(maes) / len(maes), 4)
    return out


def pearson(xs: list[float], ys: list[float]) -> float:
    n = len(xs)
    if n < 3:
        return 0.0
    mx, my = sum(xs) / n, sum(ys) / n
    sxy = sum((x - mx) * (y - my) for x, y in zip(xs, ys))
    sxx = sum((x - mx) ** 2 for x in xs)
    syy = sum((y - my) ** 2 for y in ys)
    return sxy / (sxx * syy) ** 0.5 if sxx > 0 and syy > 0 else 0.0


def classification_stats(rows: list[tuple[float, float, float, float]], horizon_h: int, threshold: float = 15.0) -> dict:
    """rows: (indicator score, raw forward return %, long MFE %, long MAE %).
    A score ≥ +threshold predicts up, ≤ −threshold predicts down; anything else is no call."""
    calls = [(1 if s > 0 else -1, r, up, dn) for s, r, up, dn in rows if abs(s) >= threshold]
    n_up = sum(1 for _, r, _, _ in rows if r > 0)
    n_dn = sum(1 for _, r, _, _ in rows if r < 0)
    if not calls:
        return {"n_obs": len(rows), "n_calls": 0}
    rets = [d * r for d, r, _, _ in calls]
    mfe = [up if d > 0 else -dn for d, _, up, dn in calls]
    mae = [dn if d > 0 else -up for d, _, up, dn in calls]
    bull = [(d, r) for d, r, _, _ in calls if d > 0]
    bear = [(d, r) for d, r, _, _ in calls if d < 0]
    tp_b = sum(1 for _, r in bull if r > 0)
    tp_s = sum(1 for _, r in bear if r < 0)
    mean = sum(rets) / len(rets)
    sd = (sum((x - mean) ** 2 for x in rets) / (len(rets) - 1)) ** 0.5 if len(rets) > 1 else 0.0
    base = summarize(rets, mfe, mae)
    return {
        "n_obs": len(rows), "n_calls": len(calls), "coverage": round(len(calls) / len(rows), 3),
        "accuracy": round(sum(1 for x in rets if x > 0) / len(rets), 4),
        "precision_bullish": round(tp_b / len(bull), 4) if bull else None,
        "recall_bullish": round(tp_b / n_up, 4) if n_up else None,
        "precision_bearish": round(tp_s / len(bear), 4) if bear else None,
        "recall_bearish": round(tp_s / n_dn, 4) if n_dn else None,
        "expectancy_pct": base["expectancy_pct"], "avg_return_pct": base["avg_return_pct"],
        "avg_mfe_pct": base.get("avg_mfe_pct"), "avg_mae_pct": base.get("avg_mae_pct"),
        "sharpe_annualized": round(mean / sd * (8760 / horizon_h) ** 0.5, 3) if sd else None,
        "max_drawdown_pct": base["max_drawdown_pct"],
    }
