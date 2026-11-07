"use client";

import { useCallback, useEffect, useState } from "react";
import { ComponentsResponse, api } from "@/lib/api";

type Source = {
  number: number | null; id: string; name: string; url: string; group: string; tier: number; btc_relevance: string;
  relevance_score: number; access: string; official_api: string; adapter: string | null; status: string; live: boolean;
  last_error: string | null; reliability: number | null; notes: string; directional: boolean;
};
type Health = {
  id: string; status: string; enabled: boolean; configured: boolean; last_success: number | null; last_error: string | null;
  latency_ms: number | null; success_rate: number; reliability: number; error_count: number; notes: string[];
};
type Rec = { id: number; created_at: string; source: string; status: string; recommended_weights: Record<string, number>;
  stats: Record<string, { ic_train: number; ic_validation: number; n_train: number; action: string }> };
type Run = { id: number; created_at: string; status: string; params: { days: number; step_hours: number }; error: string | null;
  results: null | { splits: Record<string, { steps: number; directional_calls: number; trades: number;
    by_horizon: Record<string, { directional: { n: number; win_rate?: number; expectancy_pct?: number }; trades: { n: number; win_rate?: number; expectancy_pct?: number; max_drawdown_pct?: number } }> }>;
    regimes: Record<string, { steps: number; by_horizon: Record<string, { directional: { n: number; win_rate?: number; expectancy_pct?: number } }> }>;
    caveat: string; indicator_stats?: IndicatorStats } };
type IStat = { n_calls: number; coverage?: number; accuracy?: number; precision_bullish?: number | null; precision_bearish?: number | null;
  recall_bullish?: number | null; recall_bearish?: number | null; expectancy_pct?: number; avg_mfe_pct?: number; avg_mae_pct?: number;
  sharpe_annualized?: number | null; max_drawdown_pct?: number };
type IndicatorStats = { horizon_h: number; indicators: Record<string, Record<string, IStat>>; categories: Record<string, Record<string, IStat>>;
  final: Record<string, IStat> };

const TABS = ["Providers", "Sources (40)", "Components", "Weights", "Backtest", "ETF import", "Logs"] as const;

function pct(v?: number) { return v == null ? "—" : `${(v * 100).toFixed(0)}%`; }
function fmtTs(s: number | string | null) {
  if (s == null) return "—";
  const d = typeof s === "number" ? new Date(s * 1000) : new Date(s);
  return d.toLocaleString([], { month: "short", day: "numeric", hour: "2-digit", minute: "2-digit", second: "2-digit" });
}
const statusColor = (s: string) =>
  s === "ok" ? "text-emerald-400" : s === "error" || s === "blocked" ? "text-rose-400" : s === "disabled" ? "text-zinc-500" : "text-amber-300";

export default function Admin() {
  const [token, setToken] = useState(() => {
    try {
      return typeof window === "undefined" ? "" : sessionStorage.getItem("btcs_admin") ?? "";
    } catch {
      return "";
    }
  });
  const [tab, setTab] = useState<(typeof TABS)[number]>("Providers");
  const [msg, setMsg] = useState<string | null>(null);
  const [cfg, setCfg] = useState<{ version: number; config: { weights: Record<string, number> } } | null>(null);

  const call = useCallback(<T,>(path: string, init?: RequestInit) => api<T>(path, { ...init, token }), [token]);

  const loadCfg = useCallback(() => {
    call<typeof cfg>("/api/v1/admin/config").then((c) => { setCfg(c); setMsg(null); }).catch((e) => setMsg(e.message));
  }, [call]);
  useEffect(loadCfg, [loadCfg]);

  return (
    <div className="space-y-4">
      <div className="flex flex-wrap items-center gap-2">
        <h1 className="mr-auto text-xl font-bold">Admin</h1>
        <input type="password" placeholder="Token (not needed on this PC)" value={token} suppressHydrationWarning
          onChange={(e) => { setToken(e.target.value); try { sessionStorage.setItem("btcs_admin", e.target.value); } catch {} }}
          className="w-48 rounded-md border border-zinc-700 bg-zinc-900 px-2 py-1 text-sm" />
        {cfg && <span className="text-xs text-zinc-500">config v{cfg.version}</span>}
      </div>
      {msg && <p className="text-sm text-rose-400">{msg}</p>}
      <div className="flex flex-wrap gap-1 text-sm">
        {TABS.map((t) => (
          <button key={t} onClick={() => setTab(t)} className={`rounded-md px-3 py-1 ${t === tab ? "bg-zinc-700" : "bg-zinc-900 text-zinc-400"}`}>{t}</button>
        ))}
      </div>
      {tab === "Providers" && <Providers call={call} onChange={loadCfg} />}
      {tab === "Sources (40)" && <Sources />}
      {tab === "Components" && <Components />}
      {tab === "Weights" && (cfg ? <Weights key={cfg.version} call={call} cfg={cfg} reload={loadCfg} /> : <p className="text-zinc-500">Loading config… (a token is only needed when the server is remote)</p>)}
      {tab === "Backtest" && <Backtest call={call} />}
      {tab === "ETF import" && <EtfImport call={call} />}
      {tab === "Logs" && <Logs call={call} />}
    </div>
  );
}

type Call = <T>(path: string, init?: RequestInit) => Promise<T>;

function Providers({ call, onChange }: { call: Call; onChange: () => void }) {
  const [rows, setRows] = useState<Health[]>([]);
  const load = useCallback(() => { api<{ providers: Health[] }>("/api/v1/system/providers").then((d) => setRows(d.providers)).catch(() => {}); }, []);
  useEffect(() => { load(); const t = setInterval(load, 15000); return () => clearInterval(t); }, [load]);
  const toggle = async (id: string, enabled: boolean) => {
    await call(`/api/v1/admin/providers/${id}`, { method: "POST", body: JSON.stringify({ enabled }) });
    onChange(); load();
  };
  return (
    <div className="overflow-x-auto rounded-xl border border-zinc-800">
      <table className="w-full text-left text-xs">
        <thead className="bg-zinc-900 text-zinc-500"><tr>
          <th className="p-2">Adapter</th><th className="p-2">Status</th><th className="p-2">Last success</th>
          <th className="p-2 text-right">Latency</th><th className="p-2 text-right">Success</th><th className="p-2 text-right">Reliab.</th><th className="p-2">Enabled</th>
        </tr></thead>
        <tbody>{rows.map((h) => (
          <tr key={h.id} className="border-t border-zinc-800/70 align-top">
            <td className="p-2 font-mono">{h.id}</td>
            <td className={`p-2 ${statusColor(h.status)}`}>{h.status}{h.last_error && <div className="max-w-xs truncate text-zinc-500" title={h.last_error}>{h.last_error}</div>}</td>
            <td className="p-2 text-zinc-400">{fmtTs(h.last_success)}</td>
            <td className="p-2 text-right">{h.latency_ms ? `${h.latency_ms.toFixed(0)}ms` : "—"}</td>
            <td className="p-2 text-right">{pct(h.success_rate)}</td>
            <td className="p-2 text-right">{h.reliability?.toFixed(2)}</td>
            <td className="p-2"><input type="checkbox" checked={h.enabled} onChange={(e) => toggle(h.id, e.target.checked)} /></td>
          </tr>))}
        </tbody>
      </table>
    </div>
  );
}

function Sources() {
  const [rows, setRows] = useState<Source[]>([]);
  const [summary, setSummary] = useState<Record<string, number>>({});
  useEffect(() => { api<{ sources: Source[]; summary: Record<string, number> }>("/api/v1/btc/sources").then((d) => { setRows(d.sources); setSummary(d.summary); }); }, []);
  return (
    <div className="space-y-2">
      <p className="text-xs text-zinc-400">
        {summary.reference_sources} reference sources · {summary.with_adapter} with an adapter · {summary.live_now} live now ·
        {" "}{summary.primary_providers_live} primary providers live. Relevance 0 = never moves the BTC score.
      </p>
      <div className="overflow-x-auto rounded-xl border border-zinc-800">
        <table className="w-full text-left text-xs">
          <thead className="bg-zinc-900 text-zinc-500"><tr>
            <th className="p-2">#</th><th className="p-2">Source</th><th className="p-2">Tier</th><th className="p-2">BTC relevance</th>
            <th className="p-2">Access</th><th className="p-2">Status</th><th className="p-2">Notes</th>
          </tr></thead>
          <tbody>{rows.map((s) => (
            <tr key={s.id} className="border-t border-zinc-800/70 align-top">
              <td className="p-2 text-zinc-500">{s.number ?? "·"}</td>
              <td className="p-2"><a href={s.url.startsWith("http") ? s.url : undefined} className="hover:underline" target="_blank" rel="noreferrer">{s.name}</a></td>
              <td className="p-2">{s.tier}</td>
              <td className="p-2">{s.btc_relevance} <span className="text-zinc-500">({s.relevance_score})</span></td>
              <td className="p-2">{s.access}</td>
              <td className={`p-2 ${statusColor(s.status)}`}>{s.status}</td>
              <td className="max-w-sm p-2 text-zinc-400">{s.official_api}{s.notes ? ` — ${s.notes}` : ""}</td>
            </tr>))}
          </tbody>
        </table>
      </div>
    </div>
  );
}

function Components() {
  const [d, setD] = useState<ComponentsResponse | null>(null);
  useEffect(() => { api<ComponentsResponse>("/api/v1/btc/components").then(setD).catch(() => {}); }, []);
  if (!d) return <p className="text-zinc-500">Loading…</p>;
  return (
    <div className="space-y-3 text-xs">
      <p className="text-zinc-400">Score {d.score.toFixed(1)} · coverage {pct(d.coverage)} · agreement {pct(d.agreement)} ·
        confidence parts {Object.entries(d.confidence_parts).map(([k, v]) => `${k}=${typeof v === "number" ? v.toFixed(2) : v}`).join(", ")}</p>
      {Object.values(d.components).map((c) => (
        <div key={c.engine} className="rounded-lg border border-zinc-800 p-3">
          <p className="font-semibold">{c.label} <span className="font-normal text-zinc-500">w={c.weight} · score {c.score.toFixed(1)} · conf {pct(c.confidence)} · {c.confirmation} · contrib {c.contribution?.toFixed(2)}</span></p>
          {!c.available && <p className="text-amber-300">{c.reasons[0]}</p>}
          {c.signals.map((s) => (
            <p key={s.provider} className="font-mono text-zinc-400">{s.provider}: raw {s.raw.toFixed(1)} · weight {s.weight.toFixed(3)} · fresh {s.freshness.toFixed(2)} {s.note && `· ${s.note}`}</p>
          ))}
        </div>
      ))}
    </div>
  );
}

function Weights({ call, cfg, reload }: { call: Call; cfg: { config: { weights: Record<string, number> } }; reload: () => void }) {
  const [draft, setDraft] = useState<Record<string, number>>(cfg.config.weights);
  const [recs, setRecs] = useState<Rec[]>([]);
  const [msg, setMsg] = useState("");
  const loadRecs = useCallback(() => { call<{ recommendations: Rec[] }>("/api/v1/admin/recommendations").then((d) => setRecs(d.recommendations)).catch(() => {}); }, [call]);
  useEffect(loadRecs, [loadRecs]);
  const save = async () => {
    try { await call("/api/v1/admin/config", { method: "PUT", body: JSON.stringify({ patch: { weights: draft }, note: "manual weight edit" }) }); setMsg("Saved."); reload(); }
    catch (e) { setMsg((e as Error).message); }
  };
  const decide = async (id: number, action: "approve" | "reject") => { await call(`/api/v1/admin/recommendations/${id}/${action}`, { method: "POST" }); loadRecs(); reload(); };
  const pending = recs.find((r) => r.status === "pending");
  return (
    <div className="space-y-4 text-sm">
      <div className="overflow-x-auto rounded-xl border border-zinc-800">
        <table className="w-full text-left text-xs">
          <thead className="bg-zinc-900 text-zinc-500"><tr><th className="p-2">Engine</th><th className="p-2">Active</th><th className="p-2">Recommended</th><th className="p-2">IC train / val</th><th className="p-2">Action</th></tr></thead>
          <tbody>{Object.keys(draft).map((k) => {
            const st = pending?.stats[k];
            return (
              <tr key={k} className="border-t border-zinc-800/70">
                <td className="p-2">{k}</td>
                <td className="p-2"><input type="number" min={0} step={0.5} value={draft[k]} className="w-20 rounded bg-zinc-900 px-1"
                  onChange={(e) => setDraft({ ...draft, [k]: Number(e.target.value) })} /></td>
                <td className="p-2">{pending ? pending.recommended_weights[k] : "—"}</td>
                <td className="p-2 font-mono">{st ? `${st.ic_train.toFixed(3)} / ${st.ic_validation.toFixed(3)}` : "—"}</td>
                <td className="p-2 text-zinc-400">{st?.action ?? ""}</td>
              </tr>);
          })}</tbody>
        </table>
      </div>
      <div className="flex flex-wrap gap-2">
        <button onClick={save} className="rounded-md bg-zinc-700 px-3 py-1">Save active weights</button>
        {pending && <>
          <button onClick={() => decide(pending.id, "approve")} className="rounded-md bg-emerald-700 px-3 py-1">Approve recommendation #{pending.id}</button>
          <button onClick={() => decide(pending.id, "reject")} className="rounded-md bg-zinc-800 px-3 py-1">Reject</button>
        </>}
        {msg && <span className="text-zinc-400">{msg}</span>}
      </div>
      <p className="text-xs text-zinc-500">Recommendations come from backtests (train IC, confirmed on validation). They never change production weights until approved here.</p>
    </div>
  );
}

function Backtest({ call }: { call: Call }) {
  const [runs, setRuns] = useState<Run[]>([]);
  const [days, setDays] = useState(90);
  const load = useCallback(() => { call<{ runs: Run[] }>("/api/v1/admin/backtests").then((d) => setRuns(d.runs)).catch(() => {}); }, [call]);
  useEffect(() => { load(); const t = setInterval(load, 10000); return () => clearInterval(t); }, [load]);
  const start = async () => { await call("/api/v1/admin/backtest", { method: "POST", body: JSON.stringify({ days, step_hours: 4 }) }); load(); };
  return (
    <div className="space-y-3 text-xs">
      <div className="flex items-center gap-2 text-sm">
        <input type="number" min={14} max={365} value={days} onChange={(e) => setDays(Number(e.target.value))} className="w-20 rounded bg-zinc-900 px-2 py-1" /> days
        <button onClick={start} className="rounded-md bg-zinc-700 px-3 py-1">Run backtest</button>
      </div>
      {runs.map((r) => (
        <div key={r.id} className="rounded-lg border border-zinc-800 p-3">
          <p className="font-semibold">#{r.id} · {fmtTs(r.created_at)} · {r.params.days}d · <span className={statusColor(r.status === "done" ? "ok" : r.status)}>{r.status}</span></p>
          {r.error && <p className="text-rose-400">{r.error}</p>}
          {r.results && (
            <>
              <table className="mt-2 w-full text-left">
                <thead className="text-zinc-500"><tr><th>Split</th><th>Steps</th><th>Calls</th><th>Trades</th><th>4h trade win</th><th>4h trade exp.</th><th>4h call win</th></tr></thead>
                <tbody>{Object.entries(r.results.splits).map(([k, s]) => (
                  <tr key={k}><td>{k === "test" ? "test (OOS)" : k}</td><td>{s.steps}</td><td>{s.directional_calls}</td><td>{s.trades}</td>
                    <td>{pct(s.by_horizon["4h"]?.trades.win_rate)}</td><td>{s.by_horizon["4h"]?.trades.expectancy_pct?.toFixed(3) ?? "—"}%</td>
                    <td>{pct(s.by_horizon["4h"]?.directional.win_rate)}</td></tr>))}
                </tbody>
              </table>
              <p className="mt-2 text-zinc-400">Regimes: {Object.entries(r.results.regimes).map(([k, v]) =>
                `${k} (${v.by_horizon["4h"]?.directional.n ?? 0} calls, win ${pct(v.by_horizon["4h"]?.directional.win_rate)})`).join(" · ")}</p>
              <p className="mt-1 text-zinc-500">{r.results.caveat}</p>
              {r.results.indicator_stats && <IndicatorTable stats={r.results.indicator_stats} />}
            </>
          )}
        </div>
      ))}
    </div>
  );
}

function EtfImport({ call }: { call: Call }) {
  const [text, setText] = useState("");
  const [source, setSource] = useState("farside.co.uk (transcribed)");
  const [msg, setMsg] = useState("");
  const submit = async () => {
    const rows = text.trim().split(/\n+/).map((l) => l.split(/[,\s]+/)).filter((p) => p.length >= 2)
      .map(([day, v]) => ({ day, net_flow_usd: Number(v) * 1e6 }));
    try { const r = await call<{ imported: number }>("/api/v1/admin/etf-flows", { method: "POST", body: JSON.stringify({ source, rows }) }); setMsg(`Imported ${r.imported} day(s).`); }
    catch (e) { setMsg((e as Error).message); }
  };
  return (
    <div className="space-y-2 text-sm">
      <p className="text-xs text-zinc-400">Paste published daily totals (US$ millions), one per line: <code>2026-10-05, 412.6</code>. Only enter figures you have from a published source.</p>
      <input value={source} onChange={(e) => setSource(e.target.value)} className="w-full rounded bg-zinc-900 px-2 py-1" />
      <textarea value={text} onChange={(e) => setText(e.target.value)} rows={6} className="w-full rounded bg-zinc-900 p-2 font-mono text-xs" />
      <button onClick={submit} className="rounded-md bg-zinc-700 px-3 py-1">Import</button> <span className="text-zinc-400">{msg}</span>
    </div>
  );
}

function Logs({ call }: { call: Call }) {
  const [logs, setLogs] = useState<{ ts: string; level: string; source: string; message: string }[]>([]);
  useEffect(() => { call<{ logs: typeof logs }>("/api/v1/admin/logs?limit=200").then((d) => setLogs(d.logs)).catch(() => {}); }, [call]);
  return (
    <div className="max-h-[70vh] overflow-auto rounded-xl border border-zinc-800 p-2 font-mono text-xs">
      {logs.length === 0 && <p className="text-zinc-500">No warnings or errors logged.</p>}
      {logs.map((l, i) => (
        <p key={i}><span className="text-zinc-500">{fmtTs(l.ts)}</span> <span className={l.level === "ERROR" ? "text-rose-400" : "text-amber-300"}>{l.level}</span> <span className="text-zinc-400">{l.source}</span> {l.message}</p>
      ))}
    </div>
  );
}

function IndicatorTable({ stats }: { stats: IndicatorStats }) {
  const [split, setSplit] = useState<"all" | "test">("test");
  const rows: [string, IStat | undefined][] = [
    ["FINAL SCORE", stats.final[split]],
    ...Object.entries(stats.categories).map(([k, v]) => [`category ${k}`, v[split]] as [string, IStat | undefined]),
    ...Object.entries(stats.indicators).map(([k, v]) => [k, v[split]] as [string, IStat | undefined]),
  ];
  rows.sort((a, b) => (b[1]?.expectancy_pct ?? -99) - (a[1]?.expectancy_pct ?? -99));
  const f = (v?: number | null, d = 2) => (v == null ? "—" : v.toFixed(d));
  return (
    <div className="mt-3">
      <div className="mb-1 flex flex-wrap items-center gap-2">
        <p className="font-semibold">Indicator backtest ({stats.horizon_h}h horizon, |score| ≥ 15 = call)</p>
        {(["test", "all"] as const).map((k) => (
          <button key={k} onClick={() => setSplit(k)} className={`rounded px-2 ${k === split ? "bg-zinc-700" : "bg-zinc-900 text-zinc-400"}`}>
            {k === "test" ? "out-of-sample" : "full period"}
          </button>
        ))}
      </div>
      <div className="max-h-96 overflow-auto rounded border border-zinc-800">
        <table className="w-full text-left">
          <thead className="sticky top-0 bg-zinc-900 text-zinc-500"><tr>
            <th className="p-1">Indicator</th><th className="p-1">Calls</th><th className="p-1">Acc.</th><th className="p-1">Prec ▲/▼</th>
            <th className="p-1">Recall ▲/▼</th><th className="p-1">Exp. %</th><th className="p-1">MFE/MAE %</th><th className="p-1">Sharpe</th><th className="p-1">MaxDD %</th>
          </tr></thead>
          <tbody>{rows.map(([k, v]) => (
            <tr key={k} className={`border-t border-zinc-800/60 ${k === "FINAL SCORE" ? "font-semibold" : ""}`}>
              <td className="p-1 font-mono">{k}</td><td className="p-1">{v?.n_calls ?? 0}</td>
              <td className={`p-1 ${(v?.accuracy ?? 0) > 0.55 ? "text-emerald-400" : (v?.accuracy ?? 1) < 0.45 ? "text-rose-400" : ""}`}>{v?.n_calls ? pct(v.accuracy) : "—"}</td>
              <td className="p-1">{v?.n_calls ? `${pct(v.precision_bullish ?? undefined)} / ${pct(v.precision_bearish ?? undefined)}` : "—"}</td>
              <td className="p-1">{v?.n_calls ? `${pct(v.recall_bullish ?? undefined)} / ${pct(v.recall_bearish ?? undefined)}` : "—"}</td>
              <td className="p-1">{f(v?.expectancy_pct, 3)}</td><td className="p-1">{f(v?.avg_mfe_pct)} / {f(v?.avg_mae_pct)}</td>
              <td className="p-1">{f(v?.sharpe_annualized)}</td><td className="p-1">{f(v?.max_drawdown_pct)}</td>
            </tr>))}
          </tbody>
        </table>
      </div>
      <p className="mt-1 text-zinc-500">Small samples are noise. Use these numbers to propose weights (Weights tab), never as accuracy claims.</p>
    </div>
  );
}
