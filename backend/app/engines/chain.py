"""Engines: exchange netflow (O), on-chain (Q), Bitcoin network (R), stablecoin liquidity.

On-chain thresholds are regime-based: values are ranked against their own multi-year
history (percentiles) instead of fixed cut-offs.
"""
from __future__ import annotations

from app.core.context import MarketContext, Series
from app.engines.base import EngineResult, combine, make_signal, squash, unavailable
from app.indicators.core import percentile_rank, stdev
from app.indicators.result import IndicatorResult


def _avg_change(s: Series, short_days: int, long_days: int) -> float | None:
    a, b = s.mean_since(short_days * 86400), s.mean_since(long_days * 86400)
    if a is None or b is None or not b or len(s) < long_days * 0.6:
        return None
    return (a / b - 1) * 100


def exchange_flow(ctx: MarketContext, cfg: dict | None = None) -> EngineResult:
    """Netflow = inflow − outflow. Positive = BTC entering exchanges (bearish pressure)."""
    sigs, inds, reasons = [], [], []
    for prov, s in ctx.get_series("EXCHANGE_NETFLOW_BTC_1H").items():  # hourly (keyed providers)
        pts = [v for _, v in s.points]
        if len(pts) < 24:
            continue
        h1, h4, h24 = pts[-1], sum(pts[-4:]), sum(pts[-24:])
        sd = stdev(pts[-24 * 14:]) or 1.0
        raw = -100 * (0.2 * squash(h1 / sd, 2) + 0.3 * squash(h4 / (sd * 2), 2) + 0.5 * squash(h24 / (sd * 4.9), 2))
        sigs.append(make_signal(ctx, prov, "EXCHANGE_NETFLOW_BTC_1H", s.last_ts, raw, note=f"1h {h1:+,.0f} 4h {h4:+,.0f} 24h {h24:+,.0f} BTC"))
        inds.append(IndicatorResult("exchange_netflow", {"1h": h1, "4h": h4, "24h": h24}, raw, 0.7,
                                    f"{prov}: netflow 1h {h1:+,.0f}, 4h {h4:+,.0f}, 24h {h24:+,.0f} BTC", s.last_ts, "1h"))
        reasons.append(inds[-1].explanation)
    for prov, s in ctx.get_series("EXCHANGE_NETFLOW_BTC").items():  # daily
        pts = [v for _, v in s.points]
        if len(pts) < 14:
            continue
        d1, d7 = pts[-1], sum(pts[-7:])
        sd = stdev(pts[-90:]) or 1.0
        z1, z7 = d1 / sd, d7 / (sd * 7 ** 0.5)
        raw = -100 * (0.4 * squash(z1, 2) + 0.6 * squash(z7, 2))
        sigs.append(make_signal(ctx, prov, "EXCHANGE_NETFLOW_BTC", s.last_ts, raw,
                                note=f"netflow {d1:+,.0f} BTC last day (z {z1:+.1f}), 7d {d7:+,.0f} (z {z7:+.1f})"))
        txt = (f"{prov}: exchange netflow {d1:+,.0f} BTC last day, {d7:+,.0f} BTC 7d "
               f"({'outflow — coins leaving exchanges' if d7 < 0 else 'inflow — coins moving to exchanges'})")
        raw_d = {"netflow_1d": d1, "netflow_7d": d7, "z_1d": z1, "z_7d": z7}
        for m, k in (("EXCHANGE_INFLOW_BTC", "inflow_1d"), ("EXCHANGE_OUTFLOW_BTC", "outflow_1d")):
            ss = ctx.series.get(m, {}).get(prov)
            if ss:
                raw_d[k] = ss.last()
        inds.append(IndicatorResult("exchange_netflow", raw_d, raw, 0.65, txt, s.last_ts, "1D"))
        reasons.append(txt)
    if not sigs:
        return unavailable("exchange_flow", "no exchange-flow data (Coin Metrics, CryptoQuant, Glassnode or Dune)")
    res = combine("exchange_flow", sigs)
    res.indicators, res.reasons = inds, reasons
    return res


# (metric, label, history-days needed, interpretation: +1 high percentile bullish, −1 high percentile bearish)
ONCHAIN = (
    ("BTC_MVRV", "MVRV", 365, -1),
    ("BTC_SOPR", "SOPR", 180, -1),
    ("BTC_ACTIVE_ADDRESSES", "Active addresses (7d avg)", 180, +1),
    ("BTC_LTH_SUPPLY", "Long-term holder supply (30d change)", 180, +1),
)


def onchain(ctx: MarketContext, cfg: dict | None = None) -> EngineResult:
    sigs, inds, reasons = [], [], []
    for metric, label, need, sign in ONCHAIN:
        for prov, s in ctx.get_series(metric).items():
            vals = [v for _, v in s.points]
            if len(vals) < need:
                continue
            if metric == "BTC_ACTIVE_ADDRESSES":
                cur = sum(vals[-7:]) / 7
                hist = [sum(vals[i - 7:i]) / 7 for i in range(7, len(vals))]
            elif metric == "BTC_LTH_SUPPLY":
                cur = vals[-1] / vals[-31] - 1 if len(vals) > 31 else 0.0
                hist = [vals[i] / vals[i - 30] - 1 for i in range(30, len(vals))]
            else:
                cur, hist = vals[-1], vals
            pr = percentile_rank(hist, cur)
            # extremes matter; the middle of the distribution is neutral
            raw = sign * 100 * squash((pr - 0.5) * 2, 0.8) * (0.5 if 0.25 < pr < 0.75 else 1.0)
            raw *= 0.6  # slow-moving valuation context: capped magnitude
            sigs.append(make_signal(ctx, prov, metric, s.last_ts, raw, note=f"{label} {cur:,.3f} ({pr:.0%} pctile)"))
            txt = f"{label} {cur:,.3f} — {pr:.0%} historical percentile"
            if metric == "BTC_MVRV":
                txt += " (overvalued zone)" if pr > 0.9 else " (undervalued zone)" if pr < 0.1 else ""
            inds.append(IndicatorResult(metric.lower(), {"value": cur, "percentile": pr, "history_days": len(hist)},
                                        raw, 0.5, txt, s.last_ts, "1D"))
            reasons.append(txt)
    for prov, s in ctx.get_series("EXCHANGE_RESERVE_BTC").items():
        c30 = s.pct_change(30 * 86400)
        if c30 is not None:
            raw = -100 * squash(c30, 2.0) * 0.6
            sigs.append(make_signal(ctx, prov, "EXCHANGE_RESERVE_BTC", s.last_ts, raw, note=f"reserves {c30:+.2f}% 30d"))
            txt = f"Exchange reserves {s.last():,.0f} BTC, {c30:+.2f}% over 30d"
            inds.append(IndicatorResult("exchange_reserves", {"reserve_btc": s.last(), "change_30d_pct": c30}, raw, 0.5, txt, s.last_ts, "1D"))
            reasons.append(txt)
    for metric, label, scale, amp in (("BTC_TX_COUNT", "Transaction count", 10, 30),
                                      ("BTC_TX_VOLUME_USD", "On-chain USD volume", 25, 40)):
        for prov, s in ctx.get_series(metric).items():
            chg = _avg_change(s, 7, 30)
            if chg is None:
                continue
            raw = amp * squash(chg, scale)
            sigs.append(make_signal(ctx, prov, metric, s.last_ts, raw, confidence=0.5, note=f"{label} 7d vs 30d {chg:+.1f}%"))
            inds.append(IndicatorResult(metric.lower(), {"change_7d_vs_30d_pct": chg}, raw, 0.4,
                                        f"{label} 7d avg {chg:+.1f}% vs 30d", s.last_ts, "1D"))
    if not sigs:
        return unavailable("onchain", "no on-chain series")
    res = combine("onchain", sigs)
    res.indicators, res.reasons = inds, reasons or [i.explanation for i in inds]
    return res


def network(ctx: MarketContext, cfg: dict | None = None) -> EngineResult:
    """Low weight by design: hash rate / difficulty trend only; fees, mempool, blocks are context."""
    sigs, inds, reasons, inputs = [], [], [], {}
    for metric, label in (("BTC_HASHRATE", "Hash rate"), ("BTC_DIFFICULTY", "Difficulty")):
        for prov, s in ctx.get_series(metric).items():
            chg = _avg_change(s, 7, 30) if metric == "BTC_HASHRATE" else s.pct_change(30 * 86400)
            if chg is None:
                continue
            raw = 30 * squash(chg, 5)
            sigs.append(make_signal(ctx, prov, metric, s.last_ts, raw, note=f"{label} {chg:+.1f}%"))
            txt = f"{label} {chg:+.1f}% ({'7d vs 30d' if metric == 'BTC_HASHRATE' else '30d'}) — " \
                  f"{'miners expanding' if chg > 0 else 'miner stress'}"
            inds.append(IndicatorResult(metric.lower(), {"change_pct": chg, "value": s.last()}, raw, 0.4, txt, s.last_ts, "1D"))
            reasons.append(txt)
            break
    for metric, key in (("BTC_FEE_FAST_SATVB", "fee_fast_satvb"), ("BTC_FEE_HOUR_SATVB", "fee_hour_satvb"),
                        ("BTC_MEMPOOL_VSIZE", "mempool_vsize"), ("BTC_BLOCK_HEIGHT", "block_height"),
                        ("BTC_MEMPOOL_COUNT", "mempool_tx")):
        ds = ctx.get_scalars(metric)
        if ds:
            inputs[key] = ds[0].value
            inputs[f"{key}_source"] = ds[0].meta.get("host", ds[0].provider)
    if "fee_fast_satvb" in inputs:
        vmb = inputs.get("mempool_vsize", 0) / 1e6
        txt = (f"Fee pressure {inputs['fee_fast_satvb']:.1f} sat/vB next block, mempool {vmb:.0f} MvB "
               f"({'congested' if vmb > 100 else 'clear'}) — context only")
        inds.append(IndicatorResult("mempool", {"fee_fast_satvb": inputs["fee_fast_satvb"], "mempool_mvb": vmb}, 0.0, 0.5,
                                    txt, ctx.now, "live"))
        reasons.append(txt)
    if not sigs:
        if inputs:
            res = EngineResult("network", available=True, score=0.0, confidence=0.3, reasons=reasons, confirmation="WEAK")
            res.inputs, res.indicators = inputs, inds
            return res
        return unavailable("network", "no Bitcoin network data")
    res = combine("network", sigs)
    res.reasons, res.inputs, res.indicators = reasons, {**res.inputs, **inputs}, inds
    return res


def stablecoin(ctx: MarketContext, cfg: dict | None = None) -> EngineResult:
    sigs, reasons, inds = [], [], []
    for prov, s in ctx.get_series("STABLECOIN_SUPPLY_USD").items():
        c7, c30 = s.pct_change(7 * 86400), s.pct_change(30 * 86400)
        if c7 is None:
            continue
        raw = 100 * (0.5 * squash(c7, 1.0) + 0.5 * squash(c30 or 0.0, 3.0))
        sigs.append(make_signal(ctx, prov, "STABLECOIN_SUPPLY_USD", s.last_ts, raw, note=f"supply 7d {c7:+.2f}%, 30d {c30 or 0:+.2f}%"))
        txt = f"Stablecoin supply ${s.last()/1e9:,.1f}B: {c7:+.2f}% 7d, {c30 or 0:+.2f}% 30d"
        reasons.append(txt)
        inds.append(IndicatorResult("stablecoin_supply", {"supply_usd": s.last(), "change_7d_pct": c7, "change_30d_pct": c30},
                                    raw, 0.6, txt, s.last_ts, "1D"))
    for prov, s in ctx.get_series("STABLECOIN_ETH_SUPPLY_USD").items():
        span = (s.points[-1][0] - s.points[0][0]).total_seconds() if len(s) > 1 else 0
        if span < 20 * 3600:
            continue
        c1 = s.pct_change(24 * 3600)
        if c1 is not None:
            sigs.append(make_signal(ctx, prov, "STABLECOIN_ETH_SUPPLY_USD", s.last_ts, 100 * squash(c1, 0.3),
                                    note=f"USDT+USDC on Ethereum {c1:+.2f}% 24h"))
    if not sigs:
        return unavailable("stablecoin", "no stablecoin supply data")
    res = combine("stablecoin", sigs)
    res.reasons, res.indicators = reasons or ["Stablecoin supply change measured on Ethereum only"], inds
    return res
