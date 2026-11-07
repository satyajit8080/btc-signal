"""Engines → categories → final score → direction → confidence → trade quality → levels → explanation.

The quantitative pipeline decides. The explanation layer only describes what the numbers
already decided; it cannot change direction or trade status.
"""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from app.core.context import MarketContext
from app.engines import CATEGORIES, CONTEXT_ENGINES, DIRECTIONAL_ENGINES, ENGINE_CATEGORY, ENGINE_LABELS
from app.engines.base import EngineResult, clamp, unavailable
from app.engines.data_quality import DataQuality, assess
from app.engines.indicators import atr, pct
from app.indicators.result import IndicatorResult
from app.scoring.config import ScoringConfig

SimilarLookup = Callable[[str, str], tuple[int, float]]  # (direction, bucket) -> (n, win_rate)
VERSION = "2.0.0"
MIN_TARGET_STRENGTH = 3.0  # a reward target must be a significant level, not a single minor swing


def score_bucket(score: float) -> str:
    a = abs(score)
    return "weak" if a < 30 else "moderate" if a < 50 else "strong" if a < 70 else "extreme"


def _ensure_indicators(e: EngineResult, ctx: MarketContext) -> None:
    """Every engine exposes indicator-shaped output (raw, score, signal, confidence, explanation, ts)."""
    if e.indicators or not e.available:
        return
    for s in e.signals:
        e.indicators.append(IndicatorResult(f"{e.engine}:{s.provider}", {"note": s.note}, s.raw, s.confidence * s.freshness,
                                            s.note or (e.reasons[0] if e.reasons else e.engine), ctx.now))


def run_engines(ctx: MarketContext, cfg: ScoringConfig) -> dict[str, EngineResult]:
    params = cfg.engine_params.model_dump()
    out: dict[str, EngineResult] = {}
    for name, fn in {**CONTEXT_ENGINES, **DIRECTIONAL_ENGINES}.items():
        try:
            out[name] = fn(ctx, params)
        except Exception as e:  # one broken engine must not take the decision down
            out[name] = unavailable(name, f"engine error: {type(e).__name__}: {e}", directional=name not in CONTEXT_ENGINES)
        _ensure_indicators(out[name], ctx)
    return out


@dataclass
class Aggregate:
    score: float
    raw_score: float
    coverage: float
    agreement: float
    conflict: float
    avg_engine_conf: float
    contributions: dict[str, dict]
    categories: dict[str, dict]


def aggregate(engines: dict[str, EngineResult], cfg: ScoringConfig) -> Aggregate:
    cats: dict[str, dict] = {}
    contribs: dict[str, dict] = {}
    for cat, members in CATEGORIES.items():
        wsum = sum(cfg.weights.get(m, 0) for m in members)
        num = den = conf_acc = 0.0
        avail_w = 0.0
        for m in members:
            w = cfg.weights.get(m, 0)
            e = engines.get(m)
            contribs[m] = {"weight": w, "category": cat, "effective_weight": 0.0, "contribution": 0.0}
            if not e or not e.available or w <= 0:
                continue
            eff = w * (0.5 + 0.5 * e.confidence)
            num += eff * e.score
            den += eff
            conf_acc += w * e.confidence
            avail_w += w
            contribs[m]["effective_weight"] = round(eff, 4)
        cw = cfg.category_weights.get(cat, 0)
        if den == 0 or cw <= 0:
            cats[cat] = {"available": False, "score": 0.0, "confidence": 0.0, "coverage": 0.0, "weight": cw}
            continue
        cats[cat] = {"available": True, "score": num / den, "confidence": conf_acc / avail_w,
                     "coverage": avail_w / wsum if wsum else 0.0, "weight": cw, "_den": den}
    total_cw = sum(c["weight"] for c in cats.values() if c["weight"] > 0)
    num = den = 0.0
    for c in cats.values():
        if c["available"]:
            c["effective_weight"] = c["weight"] * (0.5 + 0.5 * c["confidence"])
            num += c["effective_weight"] * c["score"]
            den += c["effective_weight"]
    if den == 0:
        return Aggregate(0.0, 0.0, 0.0, 0.0, 0.0, 0.0, contribs, _clean(cats))
    raw = num / den
    coverage = sum(c["weight"] * c["coverage"] for c in cats.values() if c["available"]) / total_cw
    score = clamp(raw * coverage ** cfg.thresholds.coverage_exponent)
    sgn = 1 if raw > 0 else -1 if raw < 0 else 0
    agree = dirw = conflict = 0.0
    for cat, c in cats.items():
        if not c["available"]:
            continue
        c["contribution"] = round(c["effective_weight"] * c["score"] / den, 3)
        for m in CATEGORIES[cat]:  # engine share of its category's contribution
            e = engines.get(m)
            if e and e.available and contribs[m]["effective_weight"]:
                contribs[m]["contribution"] = round(c["contribution"] * contribs[m]["effective_weight"] * e.score
                                                    / (c["score"] * c["_den"]) if c["score"] else 0.0, 3)
        if abs(c["score"]) >= 10:  # agreement measured across categories, not correlated indicators
            dirw += c["effective_weight"]
            if sgn and (c["score"] > 0) == (sgn > 0):
                agree += c["effective_weight"]
            elif sgn and abs(c["score"]) >= 40:
                conflict += c["effective_weight"]
    confs = [c["confidence"] for c in cats.values() if c["available"]]
    return Aggregate(score=score, raw_score=raw, coverage=coverage, agreement=agree / dirw if dirw else 0.5,
                     conflict=conflict / den, avg_engine_conf=sum(confs) / len(confs), contributions=contribs,
                     categories=_clean(cats))


def _clean(cats: dict) -> dict:
    return {k: {kk: (round(vv, 3) if isinstance(vv, float) else vv) for kk, vv in v.items() if not kk.startswith("_")}
            for k, v in cats.items()}


def _opposing_divergences(engines: dict[str, EngineResult], direction: str) -> list[str]:
    d = engines.get("divergence")
    if not d or not d.available or direction == "NEUTRAL":
        return []
    return d.flags.get("bearish_divergences" if direction == "BULLISH" else "bullish_divergences", [])


def confidence(agg: Aggregate, dq: DataQuality, direction: str, similar: SimilarLookup | None,
               engines: dict[str, EngineResult] | None = None, penalty_per_div: float = 5.0) -> tuple[float, dict]:
    hist_n, hist_wr = (similar(direction, score_bucket(agg.score)) if similar and direction != "NEUTRAL" else (0, 0.5))
    hist = hist_wr if hist_n >= 30 else 0.5  # no claim from small samples
    parts = {
        "agreement": agg.agreement, "strength": min(1.0, abs(agg.score) / 60), "completeness": agg.coverage,
        "freshness": dq.score / 100, "cross_source": agg.avg_engine_conf, "history": hist,
    }
    w = {"agreement": 0.28, "strength": 0.18, "completeness": 0.18, "freshness": 0.12, "cross_source": 0.12, "history": 0.12}
    divs = _opposing_divergences(engines or {}, direction)
    div_pen = penalty_per_div * min(3, len(divs))
    value = 100 * sum(w[k] * parts[k] for k in w) - 25 * agg.conflict - div_pen
    parts.update(conflict_penalty=round(25 * agg.conflict, 2), divergence_penalty=div_pen, opposing_divergences=divs,
                 history_samples=hist_n)
    return max(0.0, min(100.0, value)), {k: round(v, 3) if isinstance(v, float) else v for k, v in parts.items()}


def classify(conf: float) -> str:
    return "HIGH" if conf >= 70 else "MEDIUM" if conf >= 45 else "LOW"


def direction_of(agg: Aggregate, conf: float, dq: DataQuality, cfg: ScoringConfig) -> tuple[str, str | None]:
    t = cfg.thresholds
    raw_dir = "BULLISH" if agg.score >= t.bullish else "BEARISH" if agg.score <= t.bearish else "NEUTRAL"
    if raw_dir == "NEUTRAL":
        return raw_dir, None
    if dq.score < t.min_data_quality_for_direction:
        return "NEUTRAL", f"score {agg.score:+.0f} but data quality {dq.score:.0f} too low to call direction"
    if conf < t.min_confidence_for_direction:
        return "NEUTRAL", f"score {agg.score:+.0f} but confidence {conf:.0f} too low to call direction"
    return raw_dir, None


def compute_levels(ctx: MarketContext, engines: dict[str, EngineResult], direction: str, price: float) -> dict | None:
    c1h = ctx.get_candles("spot_1h")
    if direction == "NEUTRAL" or len(c1h) < 30 or not price:
        return None
    a = atr(c1h, 14)  # ATR-based: no fixed % stops
    sr = engines["levels"].levels if engines.get("levels") and engines["levels"].available else {}
    pool = sr.get("levels", [])
    if direction == "BULLISH":
        lo, hi = price - 0.5 * a, price + 0.1 * a
        sup = [m for m in pool if m["price"] < lo and pct(m["price"], price) > -5]
        anchor = max(sup, key=lambda m: m["price"]) if sup else None
        inv = (anchor["price"] if anchor else price - 2 * a) - 0.25 * a
        tgt = [m for m in pool if m["price"] > hi * 1.002 and pct(m["price"], price) < 10 and m["strength"] >= MIN_TARGET_STRENGTH]
        target = min(tgt, key=lambda m: m["price"]) if tgt else None
    else:
        lo, hi = price - 0.1 * a, price + 0.5 * a
        res_ = [m for m in pool if m["price"] > hi and pct(m["price"], price) < 5]
        anchor = min(res_, key=lambda m: m["price"]) if res_ else None
        inv = (anchor["price"] if anchor else price + 2 * a) + 0.25 * a
        tgt = [m for m in pool if m["price"] < lo * 0.998 and pct(m["price"], price) > -10 and m["strength"] >= MIN_TARGET_STRENGTH]
        target = max(tgt, key=lambda m: m["price"]) if tgt else None
    mid = (lo + hi) / 2
    risk = abs(mid - inv)
    rr = abs(target["price"] - mid) / risk if target and risk else None
    return {"entry_low": round(lo, 1), "entry_high": round(hi, 1), "invalidation": round(inv, 1), "atr_1h": round(a, 1),
            "atr_pct": round(a / price * 100, 3), "invalidation_pct": round(abs(pct(inv, price)), 3),
            "invalidation_atr": round(risk / a, 2) if a else None,
            "basis": ", ".join(sorted(set(anchor["sources"]))) if anchor else "2×ATR",
            "target": round(target["price"], 1) if target else None,
            "target_basis": ", ".join(sorted(set(target["sources"]))) if target else None,
            "reward_risk": round(rr, 2) if rr is not None else None}


def _liquidity(ctx: MarketContext) -> tuple[float | None, float | None]:
    spreads = [d.value for d in ctx.get_scalars("BTC_SPREAD_BPS")]
    c = ctx.get_candles("spot_1h")
    vr = None
    if len(c) >= 168:
        v24 = sum(x.volume for x in c[-24:])
        v7 = sum(x.volume for x in c[-168:]) / 7
        vr = v24 / v7 if v7 else None
    return (min(spreads) if spreads else None), vr


def trade_quality(direction: str, conf: float, agg: Aggregate, dq: DataQuality, engines: dict[str, EngineResult],
                  levels: dict | None, cfg: ScoringConfig, ctx: MarketContext | None = None) -> dict:
    g = cfg.trade
    flags = {k: v for e in engines.values() for k, v in e.flags.items()}
    bull, bear = direction == "BULLISH", direction == "BEARISH"
    htf = flags.get("htf_bias", 0.0) or 0.0
    spread, vol_ratio = _liquidity(ctx) if ctx else (None, None)
    crowd_long = flags.get("crowded_longs") or flags.get("extreme_funding_long") or flags.get("ls_crowded_longs")
    crowd_short = flags.get("crowded_shorts") or flags.get("extreme_funding_short") or flags.get("ls_crowded_shorts")
    rr = levels.get("reward_risk") if levels else None
    gates = [
        ("direction", direction != "NEUTRAL", "No directional edge (NEUTRAL)"),
        ("data_quality", dq.score >= g.min_data_quality and not dq.critical_stale,
         f"Data quality {dq.score:.0f} (min {g.min_data_quality:.0f}){'; critical data missing or stale' if dq.critical_stale else ''}"),
        ("confidence", conf >= g.min_confidence, f"Confidence {conf:.0f} below minimum {g.min_confidence:.0f}"),
        ("agreement", agg.agreement >= g.min_agreement, f"Category agreement {agg.agreement:.0%} (min {g.min_agreement:.0%})"),
        ("conflict", agg.conflict <= g.max_conflict, f"Signals strongly conflict ({agg.conflict:.0%} of weight opposes)"),
        ("crowding", not ((bull and crowd_long) or (bear and crowd_short)), "Funding/positioning dangerously crowded in the signal direction"),
        ("volatility", not (g.block_on_extreme_volatility and flags.get("vol_regime") == "EXTREME"), "Abnormally high volatility"),
        ("liquidation_cascade", not (g.block_on_liquidation_cascade and flags.get("liquidation_cascade")), "Liquidation cascade in progress"),
        ("leverage", not (g.block_on_fast_oi_expansion and flags.get("oi_expansion_fast") and (crowd_long if bull else crowd_short if bear else False)),
         "Leverage-driven move (fast OI build with crowded funding)"),
        ("higher_timeframe", not ((bull and htf <= -g.htf_opposition_limit) or (bear and htf >= g.htf_opposition_limit)),
         "Signal fights the higher-timeframe trend"),
        ("regime", not (g.block_on_regime_transition and flags.get("regime_transition")), "Unclear transition regime"),
        ("liquidity", (spread is None or spread <= g.max_spread_bps) and (vol_ratio is None or vol_ratio >= g.min_volume_ratio),
         f"Liquidity poor (spread {spread if spread is not None else float('nan'):.1f} bps, volume {vol_ratio or 0:.2f}× normal)"),
        ("risk", direction == "NEUTRAL" or bool(levels) and g.min_invalidation_pct <= levels["invalidation_pct"] <= g.max_invalidation_pct,
         f"Entry too far from invalidation ({levels['invalidation_pct']:.2f}%, allowed {g.min_invalidation_pct}–{g.max_invalidation_pct}%)"
         if levels else "No valid invalidation level"),
        ("reward_risk", rr is None or rr >= g.min_reward_risk,
         f"Reward/risk {rr if rr is not None else 0:.2f} to the next level is below {g.min_reward_risk}"),
    ]
    out = [{"gate": n, "passed": bool(ok), "reason": None if ok else why} for n, ok, why in gates]
    passed = all(x["passed"] for x in out)
    return {"trade": "YES" if passed else "NO", "score": round(100 * sum(x["passed"] for x in out) / len(out), 1),
            "gates": out, "blocking": [x["reason"] for x in out if not x["passed"]],
            "liquidity": {"spread_bps": spread, "volume_ratio_24h": None if vol_ratio is None else round(vol_ratio, 3)}}


def regime_of(engines: dict[str, EngineResult]) -> str:
    r = engines.get("regime")
    if r and r.available:
        return " / ".join(t.replace("_", " ") for t in r.inputs["tags"])
    return "UNKNOWN"


def explain(direction: str, trade: dict, engines: dict[str, EngineResult], agg: Aggregate) -> tuple[str, list[dict]]:
    ranked = sorted(((n, agg.contributions.get(n, {}).get("contribution", 0.0)) for n, e in engines.items()
                     if e.available and n in DIRECTIONAL_ENGINES), key=lambda x: abs(x[1]), reverse=True)
    sgn = 1 if direction == "BULLISH" else -1 if direction == "BEARISH" else 0
    why = []
    for n, c in ranked:
        e = engines[n]
        if abs(e.score) < 15 or not e.reasons:
            continue
        supports = (e.score > 0) == (sgn > 0) if sgn else None
        why.append({"engine": n, "label": ENGINE_LABELS[n], "category": ENGINE_CATEGORY[n], "stance": e.stance,
                    "supports": supports, "text": e.reasons[0], "contribution": c})
        if len(why) >= 7:
            break
    tr = engines.get("trend")
    note = tr.flags.get("mtf_note") if tr and tr.available else None
    lean = "upside" if agg.score > 0 else "downside" if agg.score < 0 else "neither side"
    if direction == "NEUTRAL":
        summary = f"Market condition: Neutral. Evidence is mixed (score {agg.score:+.0f}); no directional edge."
    else:
        summary = f"Market condition: {direction.title()}. Current evidence favors {lean} (score {agg.score:+.0f})."
    if note:
        summary += f" {note}."
    if trade["trade"] == "YES":
        summary += " Trade setup detected."
    elif direction != "NEUTRAL":
        summary += " No trade: " + "; ".join(trade["blocking"][:2]) + "."
    return summary, why


def decide(ctx: MarketContext, cfg: ScoringConfig, similar: SimilarLookup | None = None) -> dict:
    engines = run_engines(ctx, cfg)
    dq = assess(ctx, engines)
    agg = aggregate(engines, cfg)
    pre_dir = "BULLISH" if agg.score > 0 else "BEARISH" if agg.score < 0 else "NEUTRAL"
    conf, conf_parts = confidence(agg, dq, pre_dir, similar, engines, cfg.trade.divergence_confidence_penalty)
    direction, downgrade = direction_of(agg, conf, dq, cfg)
    price = dq.price
    levels = compute_levels(ctx, engines, direction, price) if price else None
    tq = trade_quality(direction, conf, agg, dq, engines, levels, cfg, ctx)
    summary, why = explain(direction, tq, engines, agg)
    c1h = ctx.get_candles("spot_1h")
    chg24 = pct(c1h[-1].close, c1h[-25].close) if len(c1h) >= 25 else None
    components = {}
    for n, e in engines.items():
        d = e.as_dict()
        d.update({"weight": 0, "effective_weight": 0, "contribution": 0, **agg.contributions.get(n, {}),
                  "label": ENGINE_LABELS.get(n, n), "category": ENGINE_CATEGORY.get(n, "CONTEXT")})
        components[n] = d
    sr = engines["levels"].levels if engines.get("levels") and engines["levels"].available else {}
    return {
        "version": VERSION, "asset": "BTC", "ts": ctx.now.isoformat(), "mode": ctx.mode,
        "price": round(price, 2) if price else None, "change_24h_pct": round(chg24, 3) if chg24 is not None else None,
        "direction": direction, "direction_note": downgrade,
        "score": round(agg.score, 2), "raw_score": round(agg.raw_score, 2), "coverage": round(agg.coverage, 3),
        "agreement": round(agg.agreement, 3), "categories": agg.categories,
        "confidence": round(conf, 1), "confidence_class": classify(conf), "confidence_parts": conf_parts,
        "trade": tq["trade"], "trade_quality": tq,
        "entry": {"low": levels["entry_low"], "high": levels["entry_high"]} if levels else None,
        "invalidation": levels["invalidation"] if levels else None,
        "levels": levels, "levels_reference_only": tq["trade"] != "YES",
        "support_resistance": {k: sr.get(k) for k in ("nearest_support", "nearest_resistance", "major_support", "major_resistance")},
        "data_quality": dq.as_dict(), "regime": regime_of(engines),
        "regime_detail": engines["regime"].inputs if engines.get("regime") and engines["regime"].available else None,
        "summary": summary, "why": why, "components": components,
        "flags": {k: v for e in engines.values() for k, v in e.flags.items()},
        "disclaimer": "Market intelligence, not financial advice. Historical performance does not guarantee future results.",
    }
