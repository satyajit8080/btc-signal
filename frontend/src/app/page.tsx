"use client";

import { useEffect, useState } from "react";
import {
  API_URL, ApiError, ComponentsResponse, Signal, ago, api, directionStyle, fmtPct, fmtUsd,
} from "@/lib/api";

function useNow(ms = 1000) {
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => {
    const t = setInterval(() => setNow(Date.now()), ms);
    return () => clearInterval(t);
  }, [ms]);
  return now;
}

function ScoreBar({ score }: { score: number }) {
  const pct = Math.min(100, Math.abs(score));
  return (
    <div className="relative mx-auto mt-2 h-2 w-56 rounded-full bg-zinc-800" aria-hidden>
      <div className="absolute left-1/2 top-0 h-2 w-px bg-zinc-500" />
      <div
        className={`absolute top-0 h-2 rounded-full ${score >= 0 ? "bg-emerald-500" : "bg-rose-500"}`}
        style={score >= 0 ? { left: "50%", width: `${pct / 2}%` } : { right: "50%", width: `${pct / 2}%` }}
      />
    </div>
  );
}

function WhyPanel() {
  const [open, setOpen] = useState(false);
  const [data, setData] = useState<ComponentsResponse | null>(null);
  const [err, setErr] = useState<string | null>(null);

  useEffect(() => {
    if (!open) return;
    api<ComponentsResponse>("/api/v1/btc/components").then(setData).catch((e) => setErr(String(e.message)));
  }, [open]);

  const comps = data ? Object.values(data.components) : [];
  const cats = data ? Object.entries(data.categories).sort((a, b) => b[1].weight - a[1].weight) : [];
  const context = comps.filter((c) => c.category === "CONTEXT" && c.available);
  const stanceColor = (s: string) => (s === "BULLISH" ? "text-emerald-400" : s === "BEARISH" ? "text-rose-400" : "text-zinc-500");
  return (
    <section className="rounded-2xl border border-zinc-800 bg-zinc-900/60">
      <button onClick={() => setOpen(!open)} className="flex w-full items-center justify-between px-5 py-4 text-left font-semibold">
        WHY THIS SIGNAL?
        <span className="text-zinc-500">{open ? "−" : "+"}</span>
      </button>
      {open && (
        <div className="space-y-2 px-5 pb-5 text-sm">
          {err && <p className="text-rose-400">{err}</p>}
          {!data && !err && <p className="text-zinc-500">Loading…</p>}
          {data && (
            <p className="text-xs text-zinc-500">
              Indicators are grouped into categories first, so correlated indicators are not counted twice. Coverage{" "}
              {(data.coverage * 100).toFixed(0)}% · category agreement {(data.agreement * 100).toFixed(0)}%
            </p>
          )}
          {context.map((c) => (
            <p key={c.engine} className="text-xs text-zinc-400"><span className="text-zinc-500">{c.label}:</span> {c.reasons.join(" · ")}</p>
          ))}
          {cats.map(([name, cat]) => {
            const members = comps.filter((c) => c.category === name);
            const stance = !cat.available ? "UNAVAILABLE" : cat.score >= 15 ? "BULLISH" : cat.score <= -15 ? "BEARISH" : "NEUTRAL";
            return (
              <details key={name} className="rounded-lg bg-zinc-950/60 px-3 py-2">
                <summary className="flex cursor-pointer list-none items-center justify-between gap-2">
                  <span className="font-medium">{name}</span>
                  <span className="flex items-center gap-3">
                    <span className={stanceColor(stance)}>{stance}</span>
                    <span className="w-14 text-right font-mono text-xs text-zinc-400">
                      {cat.available && cat.contribution != null ? `${cat.contribution > 0 ? "+" : ""}${cat.contribution.toFixed(1)}` : "n/a"}
                    </span>
                  </span>
                </summary>
                <div className="mt-2 space-y-2">
                  {members.map((c) => (
                    <div key={c.engine} className="border-l border-zinc-800 pl-2 text-xs">
                      <p className="flex justify-between">
                        <span className="text-zinc-300">{c.label}</span>
                        <span className={stanceColor(c.stance)}>{c.available ? `${c.stance} ${c.score > 0 ? "+" : ""}${c.score.toFixed(0)}` : "no data"}</span>
                      </p>
                      {!c.available && <p className="text-zinc-500">{c.reasons[0]}</p>}
                      {c.indicators.map((i, k) => (
                        <p key={k} className="text-zinc-400">
                          <span className={stanceColor(i.signal)}>●</span> {i.explanation}
                          <span className="text-zinc-600"> · {i.score > 0 ? "+" : ""}{i.score.toFixed(0)}, conf {(i.confidence * 100).toFixed(0)}%</span>
                        </p>
                      ))}
                    </div>
                  ))}
                </div>
              </details>
            );
          })}
          {data && (
            <div className="pt-2">
              <p className="mb-1 font-semibold">Trade checks</p>
              <ul className="space-y-1 text-xs">
                {data.trade_quality.gates.map((g) => (
                  <li key={g.gate} className={g.passed ? "text-zinc-400" : "text-amber-400"}>
                    {g.passed ? "✓" : "✗"} {g.gate.replace(/_/g, " ")}{g.reason ? ` — ${g.reason}` : ""}
                  </li>
                ))}
              </ul>
            </div>
          )}
        </div>
      )}
    </section>
  );
}

export default function Home() {
  const [sig, setSig] = useState<Signal | null>(null);
  const [error, setError] = useState<string | null>(null);
  const now = useNow();

  useEffect(() => {
    const load = () =>
      api<Signal>("/api/v1/btc/signal")
        .then((d) => {
          setSig(d);
          setError(null);
        })
        .catch((e) =>
          setError(e instanceof ApiError && e.status === 503 ? "Engine warming up — first signal in under a minute." : `Backend unreachable (${API_URL}).`),
        );
    load();
    let es: EventSource | null = null;
    try {
      es = new EventSource(`${API_URL}/api/v1/btc/stream`);
      es.addEventListener("signal", (ev) => {
        const d = JSON.parse((ev as MessageEvent).data);
        setSig({ ...d, trade_blocking: d.trade_quality?.blocking ?? [] });
        setError(null);
      });
    } catch {}
    const poll = setInterval(load, 60_000); // fallback if SSE is blocked by a proxy
    return () => {
      es?.close();
      clearInterval(poll);
    };
  }, []);

  if (!sig) {
    return <div className="py-24 text-center text-zinc-400">{error ?? "Loading signal…"}</div>;
  }

  const st = directionStyle[sig.direction];
  const blocking = sig.trade_blocking ?? [];
  const ageS = (now - new Date(sig.ts).getTime()) / 1000;

  return (
    <div className="space-y-5">
      {error && <p className="rounded-lg bg-amber-500/10 px-3 py-2 text-sm text-amber-300">{error} Showing last known signal.</p>}

      <section className={`rounded-2xl border border-zinc-800 bg-zinc-900/60 px-5 py-8 text-center ring-1 ${st.ring}`}>
        <p className="text-sm tracking-[0.3em] text-zinc-400">BITCOIN</p>
        <p className="mt-2 text-4xl font-bold">{fmtUsd(sig.price)}</p>
        <p className={`text-sm ${(sig.change_24h_pct ?? 0) >= 0 ? "text-emerald-400" : "text-rose-400"}`}>{fmtPct(sig.change_24h_pct)} 24h</p>

        <p className={`mt-6 text-3xl font-extrabold ${st.text}`}>{st.dot} {sig.direction}</p>
        <p className="mt-3 text-sm text-zinc-400">SCORE <span className="font-mono text-zinc-100">{sig.score > 0 ? "+" : ""}{sig.score.toFixed(0)}</span> / ±100</p>
        <ScoreBar score={sig.score} />

        <p className={`mt-6 text-2xl font-bold ${sig.trade === "YES" ? "text-emerald-400" : "text-zinc-300"}`}>TRADE: {sig.trade}</p>
        <p className="mt-1 text-sm text-zinc-400">
          CONFIDENCE: <span className="text-zinc-100">{sig.confidence.toFixed(0)}%</span> ({sig.confidence_class})
        </p>
        {sig.direction_note && <p className="mt-2 text-xs text-amber-300">{sig.direction_note}</p>}
      </section>

      <section className="rounded-2xl border border-zinc-800 bg-zinc-900/60 px-5 py-4">
        <p className="mb-2 font-semibold">WHY?</p>
        <p className="mb-3 text-sm text-zinc-400">{sig.summary}</p>
        <ul className="space-y-1.5 text-sm">
          {sig.why.map((w) => (
            <li key={w.engine} className="flex gap-2">
              <span className={w.supports === false ? "text-amber-400" : w.stance === "BULLISH" ? "text-emerald-400" : w.stance === "BEARISH" ? "text-rose-400" : "text-zinc-400"}>
                {w.supports === false ? "⚠" : "✓"}
              </span>
              <span><span className="text-zinc-400">{w.label}:</span> {w.text}</span>
            </li>
          ))}
          {sig.why.length === 0 && <li className="text-zinc-500">No engine has a strong reading right now.</li>}
        </ul>
      </section>

      <section className="grid grid-cols-2 gap-3">
        <div className="rounded-2xl border border-zinc-800 bg-zinc-900/60 px-4 py-4">
          <p className="text-xs tracking-widest text-zinc-500">ENTRY</p>
          <p className={`mt-1 font-mono ${sig.levels_reference_only ? "text-zinc-500" : ""}`}>
            {sig.entry ? `${fmtUsd(sig.entry.low)} – ${fmtUsd(sig.entry.high)}` : "No setup"}
          </p>
        </div>
        <div className="rounded-2xl border border-zinc-800 bg-zinc-900/60 px-4 py-4">
          <p className="text-xs tracking-widest text-zinc-500">INVALIDATION</p>
          <p className={`mt-1 font-mono ${sig.levels_reference_only ? "text-zinc-500" : ""}`}>{sig.invalidation ? fmtUsd(sig.invalidation) : "—"}</p>
        </div>
        {sig.levels && (
          <p className="col-span-2 px-1 text-xs text-zinc-500">
            Stop from {sig.levels.basis} (ATR {sig.levels.atr_pct.toFixed(2)}%)
            {sig.levels.target ? ` · next target ${fmtUsd(sig.levels.target)} (${sig.levels.target_basis}) · reward/risk ${sig.levels.reward_risk?.toFixed(2)}` : " · no significant level within 10%"}
          </p>
        )}
        {sig.support_resistance && (
          <div className="col-span-2 grid grid-cols-2 gap-x-3 gap-y-1 rounded-2xl border border-zinc-800 bg-zinc-900/60 px-4 py-3 text-xs">
            {(["nearest_resistance", "major_resistance", "nearest_support", "major_support"] as const).map((k) => {
              const lv = sig.support_resistance?.[k];
              return (
                <p key={k} className="flex justify-between gap-2">
                  <span className="text-zinc-500">{k.replace("_", " ")}</span>
                  <span className={`font-mono ${k.includes("support") ? "text-emerald-300" : "text-rose-300"}`} title={lv?.sources.join(", ")}>
                    {lv ? fmtUsd(lv.price) : "—"}
                  </span>
                </p>
              );
            })}
          </div>
        )}
        {sig.trade === "NO" && (
          <div className="col-span-2 rounded-2xl border border-zinc-800 bg-zinc-900/60 px-4 py-3 text-xs text-zinc-400">
            <p className="mb-1 font-semibold text-zinc-300">No trade because</p>
            <ul className="space-y-0.5">{blocking.map((b) => <li key={b}>• {b}</li>)}</ul>
            {sig.entry && <p className="mt-1 text-zinc-500">Levels shown for reference only.</p>}
          </div>
        )}
      </section>

      <section className="flex items-center justify-between rounded-2xl border border-zinc-800 bg-zinc-900/60 px-5 py-4 text-sm">
        <div>
          <p className="text-xs tracking-widest text-zinc-500">DATA QUALITY</p>
          <p className={`text-lg font-semibold ${sig.data_quality.score >= 80 ? "text-emerald-400" : sig.data_quality.score >= 60 ? "text-amber-300" : "text-rose-400"}`}>
            {sig.data_quality.score.toFixed(0)}/100
          </p>
        </div>
        <div className="text-right text-zinc-400">
          <p>Updated {ago(sig.ts, now)}</p>
          <p className="text-xs text-zinc-500">{sig.regime}</p>
          {ageS > 300 && <p className="text-xs text-amber-400">Signal is stale</p>}
        </div>
      </section>
      {sig.data_quality.reasons.length > 0 && (
        <p className="px-1 text-xs text-zinc-500">{sig.data_quality.reasons.join(" · ")}</p>
      )}

      <WhyPanel />
    </div>
  );
}
