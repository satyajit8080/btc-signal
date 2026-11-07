"use client";

import { useEffect, useState } from "react";
import { HistoryRow, api, directionStyle, fmtPct, fmtUsd } from "@/lib/api";

const WINDOWS = ["24H", "7D", "30D", "90D"] as const;
const HORIZONS = [["60", "1h"], ["240", "4h"], ["1440", "24h"]] as const;

type Summary = { n: number; win_rate?: number; avg_return_pct?: number; expectancy_pct?: number; max_drawdown_pct?: number };
type Perf = {
  live: Record<string, { directional: Summary; trades: Summary }>;
  backtest: null | { id: number; created_at: string; params: { days: number }; caveat: string;
    splits: Record<string, { steps: number; directional_calls: number; trades: number; by_horizon: Record<string, { directional: Summary; trades: Summary }> }> };
  note: string;
};

function Stat({ s }: { s?: Summary }) {
  if (!s || !s.n) return <span className="text-zinc-600">no samples</span>;
  return (
    <span>
      n={s.n} · win {((s.win_rate ?? 0) * 100).toFixed(0)}% · exp {fmtPct(s.expectancy_pct, 3)}
    </span>
  );
}

export default function History() {
  const [win, setWin] = useState<(typeof WINDOWS)[number]>("24H");
  const [horizon, setHorizon] = useState("240");
  const [data, setData] = useState<{ win: string; rows: HistoryRow[] } | null>(null);
  const [perf, setPerf] = useState<Perf | null>(null);
  const [err, setErr] = useState<string | null>(null);

  useEffect(() => {
    api<{ signals: HistoryRow[] }>(`/api/v1/btc/history?window=${win.toLowerCase()}`)
      .then((d) => setData({ win, rows: d.signals })).catch((e) => setErr(e.message));
    api<Perf>(`/api/v1/btc/performance?window=${win.toLowerCase()}`).then(setPerf).catch(() => {});
  }, [win]);

  const rows = data?.win === win ? data.rows : null;

  return (
    <div className="space-y-5">
      <div className="flex items-center justify-between">
        <h1 className="text-xl font-bold">Signal history</h1>
        <div className="flex gap-1 rounded-lg bg-zinc-900 p-1 text-sm">
          {WINDOWS.map((w) => (
            <button key={w} onClick={() => setWin(w)} className={`rounded-md px-3 py-1 ${w === win ? "bg-zinc-700" : "text-zinc-400"}`}>{w}</button>
          ))}
        </div>
      </div>

      {perf && (
        <section className="space-y-2 rounded-2xl border border-zinc-800 bg-zinc-900/60 p-4 text-sm">
          <p className="font-semibold">Historical performance</p>
          <p className="text-xs text-zinc-500">Live signals (directional calls / TRADE YES only), measured in the signal&apos;s direction.</p>
          {HORIZONS.map(([k, label]) => (
            <div key={k} className="grid grid-cols-[3rem_1fr] gap-x-2 text-xs text-zinc-300">
              <span className="text-zinc-500">{label}</span>
              <span>calls: <Stat s={perf.live[k]?.directional} /><br />trades: <Stat s={perf.live[k]?.trades} /></span>
            </div>
          ))}
          {perf.backtest && (
            <div className="border-t border-zinc-800 pt-2 text-xs text-zinc-300">
              <p className="mb-1 text-zinc-400">Backtest #{perf.backtest.id} ({perf.backtest.params.days}d, 4h horizon)</p>
              {["train", "validation", "test"].map((s) => (
                <p key={s}>
                  <span className="inline-block w-20 text-zinc-500">{s === "test" ? "out-of-sample" : s}</span>
                  <Stat s={perf.backtest!.splits[s]?.by_horizon["4h"]?.trades} />
                </p>
              ))}
              <p className="mt-1 text-zinc-500">{perf.backtest.caveat}</p>
            </div>
          )}
          <p className="text-xs text-zinc-500">{perf.note}</p>
        </section>
      )}

      <div className="flex items-center gap-2 text-xs text-zinc-400">
        Result horizon:
        {HORIZONS.map(([k, label]) => (
          <button key={k} onClick={() => setHorizon(k)} className={`rounded px-2 py-0.5 ${k === horizon ? "bg-zinc-700 text-zinc-100" : ""}`}>{label}</button>
        ))}
      </div>

      {err && <p className="text-rose-400">{err}</p>}
      {!rows && !err && <p className="text-zinc-500">Loading…</p>}
      {rows && rows.length === 0 && <p className="text-zinc-500">No signals recorded in this window yet.</p>}
      {rows && rows.length > 0 && (
        <div className="overflow-x-auto rounded-2xl border border-zinc-800">
          <table className="w-full text-left text-sm">
            <thead className="bg-zinc-900 text-xs text-zinc-500">
              <tr>
                <th className="px-3 py-2">Time</th><th className="px-3 py-2">Price</th><th className="px-3 py-2">Direction</th>
                <th className="px-3 py-2">Trade</th><th className="px-3 py-2 text-right">Score</th>
                <th className="px-3 py-2 text-right">Conf.</th><th className="px-3 py-2 text-right">Result</th>
              </tr>
            </thead>
            <tbody>
              {rows.map((r) => {
                const o = r.outcomes[horizon];
                return (
                  <tr key={r.id} className="border-t border-zinc-800/70">
                    <td className="whitespace-nowrap px-3 py-2 text-zinc-400">{new Date(r.ts).toLocaleString([], { month: "short", day: "numeric", hour: "2-digit", minute: "2-digit" })}</td>
                    <td className="px-3 py-2 font-mono">{fmtUsd(r.price)}</td>
                    <td className={`px-3 py-2 ${directionStyle[r.direction].text}`}>{r.direction}</td>
                    <td className={`px-3 py-2 ${r.trade === "YES" ? "text-emerald-400" : "text-zinc-500"}`}>{r.trade}</td>
                    <td className="px-3 py-2 text-right font-mono">{r.score.toFixed(0)}</td>
                    <td className="px-3 py-2 text-right font-mono">{r.confidence.toFixed(0)}</td>
                    <td className="px-3 py-2 text-right font-mono">
                      {!o ? <span className="text-zinc-600">pending</span> : r.direction === "NEUTRAL"
                        ? <span className="text-zinc-500">{fmtPct(o.return_pct)} move</span>
                        : <span className={o.win ? "text-emerald-400" : "text-rose-400"}>{fmtPct(o.return_pct)}</span>}
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
      )}
    </div>
  );
}
