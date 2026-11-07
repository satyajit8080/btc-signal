export const API_URL = (process.env.NEXT_PUBLIC_API_URL || "http://localhost:8000").replace(/\/$/, "");

export type Direction = "BULLISH" | "BEARISH" | "NEUTRAL";

export interface WhyItem {
  engine: string;
  label: string;
  stance: string;
  supports: boolean | null;
  text: string;
  contribution: number;
}

export interface Signal {
  asset: string;
  ts: string;
  price: number | null;
  change_24h_pct: number | null;
  direction: Direction;
  direction_note: string | null;
  score: number;
  confidence: number;
  confidence_class: "HIGH" | "MEDIUM" | "LOW";
  trade: "YES" | "NO";
  entry: { low: number; high: number } | null;
  invalidation: number | null;
  levels_reference_only: boolean;
  regime: string;
  summary: string;
  why: WhyItem[];
  disclaimer: string;
  data_quality: { score: number; label: string; critical_stale: boolean; reasons: string[] };
  trade_blocking?: string[];
  levels?: { target: number | null; target_basis: string | null; reward_risk: number | null; basis: string; atr_pct: number } | null;
  support_resistance?: Record<"nearest_support" | "nearest_resistance" | "major_support" | "major_resistance", Level | null>;
  trade_quality?: { blocking: string[] };
}

export interface Level { price: number; strength: number; sources: string[] }

export interface Indicator {
  name: string;
  timeframe: string;
  raw: Record<string, unknown>;
  score: number;
  signal: "BULLISH" | "BEARISH" | "NEUTRAL";
  confidence: number;
  explanation: string;
  ts: string;
}

export interface Category { available: boolean; score: number; confidence: number; coverage: number; weight: number; contribution?: number }

export interface Component {
  category: string;
  indicators: Indicator[];
  engine: string;
  label: string;
  available: boolean;
  score: number;
  confidence: number;
  stance: string;
  confirmation: string;
  reasons: string[];
  weight: number;
  contribution: number;
  directional: boolean;
  signals: { provider: string; raw: number; weight: number; freshness: number; note: string }[];
}

export interface ComponentsResponse {
  ts: string;
  score: number;
  coverage: number;
  agreement: number;
  categories: Record<string, Category>;
  confidence_parts: Record<string, number>;
  trade_quality: { trade: string; score: number; gates: { gate: string; passed: boolean; reason: string | null }[] };
  components: Record<string, Component>;
}

export interface HistoryRow {
  id: number;
  ts: string;
  price: number | null;
  direction: Direction;
  trade: "YES" | "NO";
  score: number;
  confidence: number;
  data_quality: number;
  outcomes: Record<string, { return_pct: number; mfe_pct: number; mae_pct: number; win: boolean | null }>;
}

export class ApiError extends Error {
  constructor(public status: number, message: string) {
    super(message);
  }
}

export async function api<T>(path: string, init?: RequestInit & { token?: string }): Promise<T> {
  const headers: Record<string, string> = { "Content-Type": "application/json" };
  if (init?.token) headers.Authorization = `Bearer ${init.token}`;
  const res = await fetch(`${API_URL}${path}`, { ...init, headers: { ...headers, ...(init?.headers as object) }, cache: "no-store" });
  if (!res.ok) {
    let msg = res.statusText;
    try {
      const body = await res.json();
      msg = typeof body.detail === "string" ? body.detail : JSON.stringify(body.detail ?? body);
    } catch {}
    throw new ApiError(res.status, msg);
  }
  return res.json() as Promise<T>;
}

export const fmtUsd = (v: number | null | undefined, digits = 0) =>
  v == null ? "—" : `$${v.toLocaleString("en-US", { minimumFractionDigits: digits, maximumFractionDigits: digits })}`;

export const fmtPct = (v: number | null | undefined, digits = 2) =>
  v == null ? "—" : `${v > 0 ? "+" : ""}${v.toFixed(digits)}%`;

export function ago(ts: string, now: number): string {
  const s = Math.max(0, Math.round((now - new Date(ts).getTime()) / 1000));
  if (s < 60) return `${s} seconds ago`;
  if (s < 3600) return `${Math.floor(s / 60)} min ago`;
  return `${Math.floor(s / 3600)} h ago`;
}

export const directionStyle: Record<Direction, { dot: string; text: string; ring: string }> = {
  BULLISH: { dot: "🟢", text: "text-emerald-400", ring: "ring-emerald-500/40" },
  BEARISH: { dot: "🔴", text: "text-rose-400", ring: "ring-rose-500/40" },
  NEUTRAL: { dot: "⚪", text: "text-zinc-300", ring: "ring-zinc-500/30" },
};
