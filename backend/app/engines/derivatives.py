"""Engines: open interest (J), funding (K), liquidations (L), long/short ratio (M)."""
from __future__ import annotations

from datetime import timedelta
from statistics import median

from app.core.context import MarketContext
from app.engines.base import EngineResult, combine, make_signal, squash, unavailable
from app.engines.indicators import interp, pct
from app.indicators import technical as T
from app.indicators.core import percentile_rank
from app.indicators.result import IndicatorResult

# Funding (% per 8h) → score. Moderate funding reflects normal positioning in its direction;
# extremes shrink back toward zero (crowding is handled as TRADE risk, not as a reversal).
FUNDING_X = [-0.10, -0.05, -0.02, -0.005, 0.0, 0.01, 0.03, 0.05, 0.10]
FUNDING_Y = [-5, -10, -25, -15, 0, 10, 25, 10, 5]
LS_X = [0.4, 0.6, 0.75, 1.33, 2.0, 3.0]
LS_Y = [50, 30, 0, 0, -30, -50]  # only extremes carry meaning (squeeze risk)


def _price_change(ctx: MarketContext, hours: int) -> float | None:
    c = ctx.get_candles("spot_1h")
    return pct(c[-1].close, c[-1 - hours].close) if len(c) > hours else None


def classify_oi(oi_chg: float, price_chg: float) -> float:
    """Price↑OI↑ new positions (+), price↓OI↑ new shorts (−), price↑OI↓ short covering (weak +),
    price↓OI↓ long liquidation/deleveraging (weak −). Small price moves carry little meaning."""
    strength = 60.0 if oi_chg > 0 else 25.0
    direction = 1 if price_chg > 0 else -1 if price_chg < 0 else 0
    return direction * strength * squash(abs(oi_chg), 3.0) * min(1.0, abs(price_chg) / 1.0)


def _median_funding_pct(ctx: MarketContext) -> float | None:
    rates = [d.value * 100 for d in ctx.get_scalars("BTC_FUNDING_RATE_8H")]
    return median(rates) if rates else None


def open_interest(ctx: MarketContext, cfg: dict | None = None) -> EngineResult:
    p4, p24 = _price_change(ctx, 4), _price_change(ctx, 24)
    if p24 is None:
        return unavailable("open_interest", "no price candles to pair with OI")
    sigs, chg24s, detail, inds = [], [], {}, []
    candidates = [(prov, s, False) for prov, s in ctx.get_series("BTC_OPEN_INTEREST_BTC").items()]
    candidates += [(prov, s, True) for prov, s in ctx.get_series("BTC_OPEN_INTEREST_USD_HIST").items()]
    for prov, s, usd in candidates:
        c4, c24 = s.pct_change(4 * 3600), s.pct_change(24 * 3600)
        if usd:  # strip the price effect out of USD-denominated OI
            c4 = ((1 + c4 / 100) / (1 + p4 / 100) - 1) * 100 if c4 is not None and p4 is not None else None
            c24 = ((1 + c24 / 100) / (1 + p24 / 100) - 1) * 100 if c24 is not None else None
        parts = []
        if c4 is not None and p4 is not None:
            parts.append((0.4, classify_oi(c4, p4)))
        if c24 is not None:
            parts.append((0.6, classify_oi(c24, p24)))
            chg24s.append(c24)
        if not parts:
            continue
        raw = sum(w * r for w, r in parts) / sum(w for w, _ in parts)
        metric = "BTC_OPEN_INTEREST_USD_HIST" if usd else "BTC_OPEN_INTEREST_BTC"
        sigs.append(make_signal(ctx, prov, metric, s.last_ts, raw, note=f"OI {c24 if c24 is not None else float('nan'):+.2f}% 24h"))
        detail[prov] = {"oi_chg_4h_pct": None if c4 is None else round(c4, 3), "oi_chg_24h_pct": None if c24 is None else round(c24, 3)}
        inds.append(IndicatorResult("oi_matrix", {"oi_chg_24h_pct": c24, "oi_chg_4h_pct": c4, "price_chg_24h_pct": p24},
                                    raw, 0.6, f"{prov}: OI {c24 if c24 is not None else 0:+.1f}% vs price {p24:+.1f}% (24h)",
                                    s.last_ts, "24h"))
    if not sigs:
        return unavailable("open_interest", "no OI history from any provider")
    m = median(chg24s) if chg24s else 0.0
    # Health check with funding and CVD (spec J): new longs are only "healthy" with spot buying and sane funding.
    f = _median_funding_pct(ctx)
    spot = ctx.get_candles("spot_1h")
    cvd = T.cvd_matrix(spot, "24h", 3600, 24, "spot") if len(spot) >= 168 else None
    cvd_rel = cvd.raw["relative"] if cvd else 0.0
    health = "n/a"
    if m > 0 and p24 > 0:
        healthy = cvd_rel > 0 and (f is None or f < 0.03)
        health = "healthy (spot-backed, funding sane)" if healthy else "leverage-driven (weak spot / hot funding)"
        if not healthy:
            for sg in sigs:
                sg.raw *= 0.5
    elif m > 0 and p24 < 0:
        health = "shorts adding" + (" with spot selling" if cvd_rel < 0 else " against spot buying (squeeze fuel)")
    res = combine("open_interest", sigs)
    res.indicators = inds
    res.inputs.update(providers=detail, price_chg_24h_pct=round(p24, 3), health=health)
    fast = (cfg or {}).get("oi_fast_pct", 8.0)
    res.flags["oi_expansion_fast"] = m > fast
    res.flags["oi_flush"] = m < -fast
    res.flags["oi_change_24h_pct"] = round(m, 3)
    kind = ("new positions entering" if p24 > 0 else "new shorts potentially entering") if m > 0 else \
        ("short covering / deleveraging" if p24 > 0 else "long liquidation / deleveraging")
    res.reasons = [f"OI {m:+.1f}% in 24h with price {p24:+.1f}% → {kind}", f"Move quality: {health}",
                   f"Confirmed by {len(sigs)} venue(s): {', '.join(detail)}"]
    if res.flags["oi_expansion_fast"]:
        res.reasons.append("OI expanding unusually fast")
    return res


def funding(ctx: MarketContext, cfg: dict | None = None) -> EngineResult:
    cfg = cfg or {}
    sigs, rates, inds = [], {}, []
    for d in ctx.get_scalars("BTC_FUNDING_RATE_8H"):
        f = d.value * 100
        rates[d.provider] = round(f, 5)
        sigs.append(make_signal(ctx, d.provider, "BTC_FUNDING_RATE_8H", d.ts, interp(f, FUNDING_X, FUNDING_Y),
                                note=f"funding {f:+.4f}%/8h"))
    if not sigs:
        return unavailable("funding", "no funding data")
    avg_f = median(rates.values())
    hist = []
    for s in ctx.get_series("BTC_FUNDING_RATE_HIST").values():
        hist = [v * 100 for _, v in s.points]
        break
    pr = percentile_rank(hist, avg_f) if len(hist) >= 20 else None
    trend = None
    if len(hist) >= 12:
        trend = sum(hist[-3:]) / 3 - sum(hist[-12:-3]) / 9
    res = combine("funding", sigs)
    crowded_pct = cfg.get("crowded_funding_pct", 0.03)
    extreme = cfg.get("extreme_funding_pct", 0.05)
    res.flags.update(
        crowded_longs=avg_f > crowded_pct or (pr is not None and pr > 0.95 and avg_f > 0.01),
        crowded_shorts=avg_f < -cfg.get("crowded_funding_pct_short", 0.02) or (pr is not None and pr < 0.05 and avg_f < 0),
        extreme_funding_long=avg_f > extreme, extreme_funding_short=avg_f < -extreme,
        funding_percentile=pr)
    state = "extreme positive (crowded longs, squeeze risk)" if res.flags["extreme_funding_long"] else \
        "elevated" if avg_f > crowded_pct else "moderately positive (normal long positioning)" if avg_f > 0.002 else \
        "flat" if avg_f >= -0.002 else "moderately negative (normal short positioning)" if avg_f > -extreme else \
        "extreme negative (crowded shorts, squeeze risk)"
    txt = f"Funding {state}: median {avg_f:+.4f}%/8h across {len(rates)} venue(s)"
    if pr is not None:
        txt += f", {pr:.0%} percentile of 30d"
    if trend is not None:
        txt += f", trend {'rising' if trend > 0 else 'falling'}"
    inds.append(IndicatorResult("funding", {"median_pct_8h": avg_f, "percentile_30d": pr, "trend": trend, "by_venue": rates},
                                res.score, res.confidence, txt, ctx.now, "8h", {"crowded": res.flags["crowded_longs"] or res.flags["crowded_shorts"]}))
    res.indicators = inds
    res.reasons = [txt]
    if res.flags["crowded_longs"] or res.flags["crowded_shorts"]:
        res.reasons.append("Extreme funding lowers trade quality; it does not reverse the direction")
    res.inputs.update(funding_pct_8h=rates, median_funding_pct=round(avg_f, 5), percentile_30d=pr)
    return res


def long_short(ctx: MarketContext, cfg: dict | None = None) -> EngineResult:
    """Positioning, not direction: only crowded extremes matter (squeeze risk)."""
    ds = ctx.get_scalars("BTC_LONG_SHORT_ACCOUNT_RATIO")
    if not ds:
        return unavailable("long_short", "no long/short ratio data")
    sigs, inds, ratios = [], [], {}
    for d in ds:
        ratios[d.provider] = round(d.value, 3)
        sc = interp(d.value, LS_X, LS_Y)
        sigs.append(make_signal(ctx, d.provider, "BTC_LONG_SHORT_ACCOUNT_RATIO", d.ts, sc, note=f"L/S {d.value:.2f}"))
    res = combine("long_short", sigs)
    m = median(ratios.values())
    state = "extreme long positioning — downside squeeze risk" if m >= 2.0 else \
        "extreme short positioning — upside squeeze risk" if m <= 0.6 else "not extreme (not directional)"
    top = ctx.get_scalars("BTC_TOP_TRADER_LS_RATIO")
    txt = f"Long/short account ratio {m:.2f}: {state}"
    if top:
        txt += f"; top-trader position ratio {top[0].value:.2f}"
    res.indicators = [IndicatorResult("long_short_ratio", {"median": m, "by_venue": ratios}, res.score, res.confidence,
                                      txt, ctx.now, "1h", {"crowded_longs": m >= 2.0, "crowded_shorts": m <= 0.6})]
    res.flags.update(ls_crowded_longs=m >= cfg.get("crowded_ls", 2.2) if cfg else m >= 2.2, ls_crowded_shorts=m <= 0.6)
    res.reasons = [txt]
    return res


def liquidation(ctx: MarketContext, cfg: dict | None = None) -> EngineResult:
    cfg = cfg or {}
    events = ctx.events.get("liquidations", [])
    sources = ctx.event_source.get("liquidations", [])
    if not sources:
        return unavailable("liquidation", "no liquidation feed connected")
    uptime = next((d.value for d in ctx.get_scalars("LIQ_STREAM_UPTIME_S")), None)
    spot = ctx.get_candles("spot_1h")
    cvd = T.cvd_matrix(spot, "4h", 3600, 4, "spot") if len(spot) >= 168 else None
    cvd_dir = 0 if not cvd else 1 if cvd.raw["relative"] > 0.01 else -1 if cvd.raw["relative"] < -0.01 else 0
    oi_chg = None
    for s in ctx.get_series("BTC_OPEN_INTEREST_BTC").values():
        oi_chg = s.pct_change(4 * 3600)
        break
    sigs, flags, detail, inds = [], {"liquidation_cascade": False, "deleveraging_reset": False}, {}, []
    for prov in sources:
        evs = [e for e in events if (e.get("venue") == "coinglass_agg") == (prov == "coinglass")]
        if prov == "liquidation_stream":
            if uptime is None or uptime < cfg.get("liq_min_uptime_s", 1800):
                continue
            thr4, cascade = cfg.get("liq_stream_4h_usd", 20e6), cfg.get("liq_stream_cascade_1h_usd", 25e6)
            ts = ctx.fetched_at.get(prov, ctx.now)
        else:
            thr4, cascade = cfg.get("liq_agg_4h_usd", 100e6), cfg.get("liq_agg_cascade_1h_usd", 150e6)
            ts = max((e["ts"] for e in evs), default=None)

        def tot(h: float, side: str) -> float:
            cut = ctx.now - timedelta(hours=h)
            return sum(e["usd"] for e in evs if e["ts"] >= cut and e["side"] == side)
        l1, s1, l4, s4 = tot(1, "long"), tot(1, "short"), tot(4, "long"), tot(4, "short")
        t1, t4 = l1 + s1, l4 + s4
        velocity = t1 / (t4 / 4) if t4 else 0.0  # >1: liquidations accelerating
        detail[prov] = {"long_1h": round(l1), "short_1h": round(s1), "long_4h": round(l4), "short_4h": round(s4),
                        "velocity": round(velocity, 2)}
        if t4 <= 0:
            sigs.append(make_signal(ctx, prov, "LIQUIDATIONS", ts, 0.0, note="no liquidations in 4h"))
            continue
        imb4 = (s4 - l4) / t4
        imb1 = (s1 - l1) / t1 if t1 else 0.0
        raw = 50 * (0.6 * imb4 + 0.4 * imb1) * min(1.0, (t4 / thr4) ** 0.5)
        # Spec L: liquidations confirm only together with spot flow.
        if imb4 > 0.3 and cvd_dir > 0:
            raw += 20
        elif imb4 < -0.3 and cvd_dir < 0:
            raw -= 20
        elif cvd_dir and (imb4 > 0) != (cvd_dir > 0):
            raw *= 0.5
        sigs.append(make_signal(ctx, prov, "LIQUIDATIONS", ts, raw, note=f"4h longs ${l4/1e6:.1f}M / shorts ${s4/1e6:.1f}M"))
        if t1 > cascade:
            flags["liquidation_cascade"] = True
            flags["cascade_side"] = "long" if l1 > s1 else "short"
        if t4 > thr4 and oi_chg is not None and oi_chg < -2:
            flags["deleveraging_reset"] = True
    if not sigs:
        return unavailable("liquidation", f"liquidation stream warming up ({(uptime or 0)/60:.0f} min observed)")
    res = combine("liquidation", sigs)
    res.flags.update(flags)
    res.inputs["by_source"] = detail
    agg = {k: sum(d[k] for d in detail.values()) for k in ("long_4h", "short_4h")}
    vel = max((d["velocity"] for d in detail.values()), default=0)
    dom = "short liquidations dominate" if agg["short_4h"] > agg["long_4h"] * 1.5 else \
        "long liquidations dominate" if agg["long_4h"] > agg["short_4h"] * 1.5 else "balanced"
    txt = f"4h liquidations: longs ${agg['long_4h']/1e6:.1f}M vs shorts ${agg['short_4h']/1e6:.1f}M — {dom}, velocity {vel:.1f}×"
    if cvd_dir:
        txt += f"; spot flow {'buying' if cvd_dir > 0 else 'selling'}"
    res.reasons = [txt]
    if flags["liquidation_cascade"]:
        res.reasons.append(f"Liquidation cascade in progress ({flags['cascade_side']}s)")
    if flags["deleveraging_reset"]:
        res.reasons.append("Large liquidations followed by falling OI — possible reset/deleveraging")
    if "liquidation_stream" in detail:
        res.reasons.append("Stream totals are a lower bound (Binance throttles to 1 event/s)")
    res.indicators = [IndicatorResult("liquidations", {**agg, "velocity": vel, "cvd_direction": cvd_dir}, res.score,
                                      res.confidence, txt, ctx.now, "4h", flags)]
    return res
