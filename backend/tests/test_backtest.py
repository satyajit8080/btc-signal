from datetime import datetime, timedelta, timezone

import pytest

from app.backtest.evaluator import evaluate_pending
from app.backtest.history import step_context
from app.backtest.metrics import forward_outcome, pearson, summarize
from app.backtest.runner import evaluate, run_replay
from app.core.context import Candle, Series
from app.db.repo import Repo
from app.db.session import Database
from app.learning.weights import recommend_weights
from app.scoring.config import default_config
from app.scoring.decision import decide
from conftest import NOW, build_ctx, relaxed_cfg


def test_forward_outcome_long_short_neutral():
    path = [(102, 99, 101), (105, 100, 104)]
    o = forward_outcome("BULLISH", 100, path)
    assert o.return_pct == pytest.approx(4) and o.mfe_pct == pytest.approx(5) and o.mae_pct == pytest.approx(-1) and o.win
    s = forward_outcome("BEARISH", 100, path)
    assert s.return_pct == pytest.approx(-4) and s.mfe_pct == pytest.approx(1) and s.mae_pct == pytest.approx(-5) and not s.win
    n = forward_outcome("NEUTRAL", 100, path)
    assert n.win is None and n.return_pct == pytest.approx(4)


def test_forward_outcome_stop_hit():
    o = forward_outcome("BULLISH", 100, [(101, 97, 98), (110, 98, 109)], invalidation=98)
    assert o.hit_invalidation and o.return_pct == pytest.approx(-2) and not o.win
    s = forward_outcome("BEARISH", 100, [(103, 99, 102)], invalidation=102.5)
    assert s.hit_invalidation and s.return_pct == pytest.approx(-2.5)


def test_summarize_and_drawdown():
    s = summarize([1.0, -2.0, 3.0, -1.0], [2, 1, 4, 0], [-1, -3, 0, -2])
    assert s["n"] == 4 and s["win_rate"] == 0.5 and s["max_drawdown_pct"] == -2.0
    assert s["expectancy_pct"] == pytest.approx(0.5 * 2 - 0.5 * 1.5)
    assert s["profit_factor"] == pytest.approx(4 / 3, abs=1e-3) and s["avg_mfe_pct"] == 1.75
    assert summarize([]) == {"n": 0}


def test_pearson():
    assert pearson([1, 2, 3], [2, 4, 6]) == pytest.approx(1)
    assert pearson([1, 2, 3], [3, 2, 1]) == pytest.approx(-1)
    assert pearson([1, 1, 1], [1, 2, 3]) == 0.0 and pearson([1], [1]) == 0.0


def _full_ctx(direction=1):
    full = build_ctx(direction, with_intel=False)
    full.mode = "backtest"
    full.add_series("BACKTEST_FUNDING", Series("binance_futures", [(NOW - timedelta(hours=8 * i), 0.0001) for i in range(60)]))
    return full


def test_step_context_no_lookahead_and_funding():
    full = _full_ctx()
    t = NOW - timedelta(days=5)
    ctx = step_context(full, t)
    assert ctx.candles["spot_1h"][-1].ts + timedelta(hours=1) <= t
    assert len(ctx.candles["spot_1h"]) <= 1000
    assert ctx.scalars["BTC_FUNDING_RATE_8H"][0].ts == t
    assert "BACKTEST_FUNDING" not in ctx.series


def test_replay_and_evaluate_splits():
    full = _full_ctx(1)
    cfg = default_config()
    res = run_replay(full, cfg, NOW - timedelta(days=12), NOW, step_hours=6)
    rows = res["rows"]
    assert len(rows) >= 30
    assert all(set(r["outcomes"]) <= {1, 4, 12, 24} for r in rows)
    assert all("whale" not in r["components"] for r in rows)  # excluded engines are not faked
    ev = evaluate(rows, cfg)
    assert set(ev["splits"]) == {"train", "validation", "test"}
    n = sum(ev["splits"][k]["steps"] for k in ev["splits"])
    assert n == len(rows)
    assert ev["splits"]["train"]["to"] < ev["splits"]["test"]["from"]  # chronological
    assert "bull" in ev["regimes"] or "sideways" in ev["regimes"]
    assert "caveat" in ev and "whale" in ev["engines_missing"]
    assert ev["recommendation"]["recommended"]


def _rows(engine_scores, rets):
    return [{"components": {"a": s, "b": -s, "c": 0.0 if i % 2 else 1.0}, "outcomes": {4: {"raw": r}}}
            for i, (s, r) in enumerate(zip(engine_scores, rets))]


def test_recommend_weights_rewards_predictive_and_punishes_noise():
    xs = [float(i % 10 - 5) for i in range(200)]
    rows = _rows(xs, [x * 0.1 for x in xs])
    rec = recommend_weights(rows[:120], rows[120:], {"a": 10, "b": 10, "c": 10})
    st = rec["stats"]
    assert st["a"]["action"] == "increase" and st["b"]["action"] == "decrease"
    assert rec["recommended"]["a"] > rec["recommended"]["b"]
    assert sum(rec["recommended"].values()) == pytest.approx(30, rel=1e-3)  # budget preserved
    few = recommend_weights(rows[:10], rows[10:20], {"a": 10})
    assert few["stats"]["a"]["action"] == "insufficient_data" and few["recommended"]["a"] == 10


def test_recommend_requires_validation_consistency():
    xs = [float(i % 10 - 5) for i in range(200)]
    train = _rows(xs[:120], [x * 0.1 for x in xs[:120]])
    val = _rows(xs[120:], [-x * 0.1 for x in xs[120:]])  # relationship flips out of sample
    rec = recommend_weights(train, val, {"a": 10, "b": 10})
    assert rec["stats"]["a"]["action"] == "hold"


# ---------------- live evaluator + repo ----------------
@pytest.fixture
def repo():
    db = Database("sqlite://")
    db.create_all()
    return Repo(db)


def test_evaluate_pending_live_signals(repo):
    now = datetime.now(timezone.utc).replace(second=0, microsecond=0)
    t0 = now - timedelta(days=2)
    d = decide(build_ctx(1, now=t0), relaxed_cfg())
    assert d["trade"] == "YES"
    sid = repo.save_signal(d)
    price = d["price"]
    candles = [Candle(t0 + timedelta(minutes=i), price * (1 + i * 1e-5), price * (1 + i * 1e-5 + 1e-4),
                      price * (1 + i * 1e-5 - 1e-4), price * (1 + (i + 1) * 1e-5), 1.0) for i in range(1500)]
    repo.upsert_candles_1m(candles)
    assert evaluate_pending(repo, now) == 6
    assert evaluate_pending(repo, now) == 0  # idempotent
    hist = repo.signal_history(now - timedelta(days=3))
    assert hist[0]["id"] == sid and set(hist[0]["outcomes"]) == {15, 30, 60, 240, 720, 1440}
    assert hist[0]["outcomes"][1440]["win"] is True
    perf = repo.performance()
    assert perf["240"]["trades"]["n"] == 1 and perf["240"]["trades"]["win_rate"] == 1.0
    n, wr = repo.similar_lookup()("BULLISH", "strong")
    assert n == 1 and wr == 1.0


def test_evaluator_waits_for_gappy_data(repo):
    now = datetime.now(timezone.utc)
    t0 = now - timedelta(hours=5)
    d = decide(build_ctx(1, now=t0), default_config())
    repo.save_signal(d)
    repo.upsert_candles_1m([Candle(t0 + timedelta(minutes=i), 1, 1, 1, 1, 1) for i in range(5)])
    assert evaluate_pending(repo, now) == 0


def test_repo_config_kv_etf_logs_recommendations(repo):
    vid, cfg = repo.active_config()
    assert vid == 1 and cfg.weights["trend"] == 12
    cfg.weights["trend"] = 20
    v2 = repo.save_config(cfg, "test")
    assert repo.active_config() == (v2, cfg) and len(repo.config_history()) == 2
    repo.kv_set("k", {"a": 1})
    assert repo.kv_get("k")[0] == {"a": 1} and repo.kv_get("none") == (None, None)
    assert repo.upsert_etf_flows([{"day": "2026-10-05", "net_flow_usd": 5e8}, {"day": "2026-10-05", "net_flow_usd": 6e8}], "farside (manual)") == 2
    assert repo.etf_series()[-1][1] == 6e8
    repo.log("ERROR", "t", "msg")
    assert repo.logs(level="ERROR")[0]["message"] == "msg"
    rid = repo.save_recommendation("backtest:1", {"trend": 12}, {"trend": 14}, {})
    assert repo.decide_recommendation(rid, True) == {"trend": 14}
    assert repo.decide_recommendation(rid, True) is None
    bid = repo.create_backtest({"days": 30})
    repo.finish_backtest(bid, {"ok": 1})
    assert repo.backtests()[0]["status"] == "done"
    repo.save_liquidation({"ts": NOW, "venue": "binance", "side": "long", "usd": 1e6})
    repo.prune(keep_days=0)


def test_repo_observations_and_history(repo):
    ctx = build_ctx(1, now=datetime.now(timezone.utc))
    from app.core.context import Datum
    ctx.add_scalar("SMART_MONEY_NET_BTC", Datum(10.0, "hyperliquid_smart_money", ctx.now))
    n = repo.save_observations(ctx)
    assert n > 0 and repo.save_observations(ctx) == 0  # unchanged cached datums not duplicated
    h = repo.load_history("SMART_MONEY_NET_BTC")
    assert h["hyperliquid_smart_money"][-1][1] == 10.0
    assert "SMART_MONEY_NET_BTC" in repo.tracked_metrics()
    assert repo.counts()["observations"] == n
