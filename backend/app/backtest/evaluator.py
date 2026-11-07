"""Scores stored live signals once each horizon has elapsed, from the local 1m candle store."""
from __future__ import annotations

import logging
from datetime import datetime, timedelta

from app.backtest.metrics import forward_outcome
from app.db.repo import Repo
from app.db.session import as_utc

log = logging.getLogger("btcsignal.evaluator")
HORIZONS_MIN = (15, 30, 60, 240, 720, 1440)


def evaluate_pending(repo: Repo, now: datetime, horizons: tuple[int, ...] = HORIZONS_MIN) -> int:
    done = 0
    for h in horizons:
        for sig in repo.signals_pending_outcome(h, now):
            start = as_utc(sig.ts)
            bars = repo.candles_1m(start, start + timedelta(minutes=h))
            if len(bars) < 0.9 * h:
                if now - start > timedelta(minutes=h + 180):
                    log.info("signal %s horizon %s: only %d/%d minute bars; skipped", sig.id, h, len(bars), h)
                continue  # wait for data; never evaluate on a gappy path
            entry = bars[0].open
            o = forward_outcome(sig.direction, entry, [(b.high, b.low, b.close) for b in bars],
                                sig.invalidation if sig.trade == "YES" else None)
            repo.save_outcome(signal_id=sig.id, horizon_min=h, exit_price=o.exit_price, return_pct=o.return_pct,
                              mfe_pct=o.mfe_pct, mae_pct=o.mae_pct, hit_invalidation=o.hit_invalidation, win=o.win)
            done += 1
    return done
