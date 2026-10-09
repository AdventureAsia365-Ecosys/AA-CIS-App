"use client";
// app/admin/decisions/page.tsx — AA-660 Jev (TypeSafe) decision layer, v2 (S203 feedback: explain every
// term, filters + sort, sticky tabs/headers, one column per fact, solid status colours, fuller stats).
// API: /admin/decisions/{summary,log,questions/{key}}. Jev spend also appears in External Spend.

import { useCallback, useEffect, useMemo, useState } from "react";
import { BookOpen, ChevronDown, ChevronRight, RefreshCw, Scale } from "lucide-react";
import { Bar, BarChart, CartesianGrid, Legend, ResponsiveContainer, Tooltip, XAxis, YAxis } from "recharts";
import AdminSidebar from "../_components/AdminSidebar";
import { A, alpha, serif, sans, mono, Card, SLabel, Btn, LoadingScreen, TH, TD, CHART_TOOLTIP } from "../_components/adminUi";
import { useChartColors } from "../../_kit/useTheme";

// ── types ──────────────────────────────────────────────────────────────────────────────────────
type Mode = "off" | "shadow" | "enforce";
type Zone = "accept" | "grey" | "reject" | "error" | "skipped";

interface Question {
  question_key: string; stage: string; kind: string; instructions: string; criteria: unknown; mode: Mode;
  accept_floor: number | null; reject_ceiling: number | null; threshold_version: number;
  calibration_ref: string | null; notes: string | null; updated_at: string; updated_by: string;
  verdicts: number; accept: number; grey: number; reject: number; error: number; skipped: number;
  acted: number; cached: number; cost_usd: number; avg_latency_ms: number | null; p50_latency_ms: number | null;
  p95_latency_ms: number | null; avg_probability: number | null; first_used_at: string | null; last_used_at: string | null;
}
interface StageRow {
  stage: string; questions: number; verdicts: number; acted: number; errors: number; skipped: number; cached: number;
  cost_usd: number; avg_latency_ms: number | null; last_used_at: string | null;
}
interface DayRow { day: string; verdicts: number; accept: number; grey: number; reject: number; error: number; skipped: number; cost_usd: number }
interface Summary {
  days: number; questions: Question[]; stages: StageRow[]; daily: DayRow[];
  calls_by_stage: { stage: string; calls: number; cost_usd: number; tokens_in: number }[];
  unregistered: { stage: string; question_key: string; verdicts: number; cost_usd: number }[];
  total_cost_usd: number; total_calls: number;
  tenant_allowlist: { tenant_id: string; slug: string; name: string; reason: string }[];
}
interface Verdict {
  id: number; created_at: string; stage: string; question_key: string; subject_key: string;
  tenant_slug: string | null; job_id: string | null; mode: Mode; zone: Zone; probability: number | null;
  choice: string | null; threshold_version: number | null; latency_ms: number | null; cost_usd: number;
  error: string | null; outcome: string | null; cached?: boolean;
}

// ── meaning of every status, shown in the Guide and as tooltips ────────────────────────────────
const ZONES: Zone[] = ["accept", "grey", "reject", "error", "skipped"];
const ZONE_META: Record<Zone, { label: string; color: string; cssVar: string; help: string }> = {
  accept:  { label: "Accept",  color: "var(--aa-green-deep)",  cssVar: "--aa-green-deep",  help: "Jev is confidently YES (probability ≥ accept floor). In enforce mode the stage acts on it." },
  grey:    { label: "Grey",    color: "var(--aa-neutral-fg2)", cssVar: "--aa-neutral-fg2", help: "Jev is not confident either way. The stage keeps its old rule — nothing changes." },
  reject:  { label: "Reject",  color: "var(--aa-red-strong)",  cssVar: "--aa-red-strong",  help: "Jev is confidently NO (probability ≤ reject ceiling). In enforce mode the stage acts on it (e.g. drops the keyword)." },
  error:   { label: "Error",   color: "var(--aa-amber-strong)",cssVar: "--aa-amber-strong",help: "Jev could not answer (timeout, API error, bad config). Fail-open: the stage keeps its old rule." },
  skipped: { label: "Skipped", color: "var(--aa-blue-link)",   cssVar: "--aa-blue-link",   help: "Not asked: the question is off, or it is tenant content and the tenant is not on the allow-list." },
};
const MODE_META: Record<Mode, { label: string; color: string; help: string }> = {
  off:     { label: "Off",     color: "var(--aa-neutral-fg)",  help: "The question is not asked at all." },
  shadow:  { label: "Shadow",  color: "var(--aa-blue-link)",   help: "Asked and logged, but the pipeline ignores the answer. Used to collect calibration data." },
  enforce: { label: "Enforce", color: "var(--aa-amber-strong)",help: "Asked and ACTED ON when the answer is confident (accept / reject). Needs a calibration record." },
};
const KIND_HELP: Record<string, string> = {
  noul: "Yes/no question — Jev returns the probability that the answer is yes (0–1).",
  choice: "Pick one label from a list — Jev returns the pick and its confidence (0–1).",
  score: "Level on an ordered scale — Jev returns the expected level and its confidence.",
};

// ── small UI pieces ────────────────────────────────────────────────────────────────────────────
const usd = (v: number | null | undefined) => (v == null ? "—" : v < 0.01 ? `$${v.toFixed(5)}` : `$${v.toFixed(3)}`);
// AA-601: a count that arrives null/undefined (a stage seen only in the cache-hit rollup, or a
// FILTERed sum that was NULL) must count as 0 — summing it directly would make the whole total NaN
// (the "Acted on = NaN" bug). The backend now zero-fills every aggregate; this is the FE guard so
// one bad row can never poison a reduce again.
const n0 = (v: number | null | undefined): number => (typeof v === "number" && Number.isFinite(v) ? v : 0);
const when = (s: string | null) => (s ? new Date(s).toLocaleString() : "—");
const num = (v: number | null | undefined, d = 2) => (v == null ? "—" : v.toFixed(d));
const pct = (a: number, b: number) => (b ? `${Math.round((a / b) * 100)}%` : "—");

function Pill({ color, children, title }: { color: string; children: React.ReactNode; title?: string }) {
  return (
    <span title={title} style={{
      display: "inline-block", padding: "3px 10px", borderRadius: 999, background: color, color: "var(--aa-on-solid)",
      fontSize: 11, fontWeight: 700, letterSpacing: "0.03em", whiteSpace: "nowrap", cursor: title ? "help" : "default",
    }}>{children}</span>
  );
}
const ZonePill = ({ z }: { z: Zone }) => <Pill color={ZONE_META[z].color} title={ZONE_META[z].help}>{ZONE_META[z].label}</Pill>;
const ModePill = ({ m }: { m: Mode }) => <Pill color={MODE_META[m].color} title={MODE_META[m].help}>{MODE_META[m].label}</Pill>;

function Kpi({ label, value, sub, color = A.ink }: { label: string; value: string; sub?: string; color?: string }) {
  return (
    <div style={{ background: A.card, border: `1px solid ${A.line}`, borderRadius: 10, padding: "14px 16px" }}>
      <div style={{ fontSize: 11, fontWeight: 700, textTransform: "uppercase", letterSpacing: "0.08em", color: A.muted }}>{label}</div>
      <div style={{ fontSize: 26, fontWeight: 700, color, marginTop: 4, fontVariantNumeric: "tabular-nums" }}>{value}</div>
      {sub && <div style={{ fontSize: 11.5, color: A.muted, marginTop: 2 }}>{sub}</div>}
    </div>
  );
}

const STICKY_TH: React.CSSProperties = { ...TH, position: "sticky", top: 0, zIndex: 2, background: "var(--aa-bg)", whiteSpace: "nowrap" };
const TABLE_BOX: React.CSSProperties = { overflow: "auto", maxHeight: "62vh", border: `1px solid ${A.line}`, borderRadius: 10, background: A.card };
const input: React.CSSProperties = { padding: "7px 9px", border: `1px solid ${A.line}`, borderRadius: 7, fontSize: 12.5, fontFamily: sans, background: A.card };

function SortTh<K extends string>({ label, k, sort, setSort, title }: {
  label: string; k: K; sort: { key: K; dir: "asc" | "desc" }; setSort: (s: { key: K; dir: "asc" | "desc" }) => void; title?: string;
}) {
  const active = sort.key === k;
  return (
    <th title={title} style={{ ...STICKY_TH, cursor: "pointer", color: active ? A.ink : undefined }}
      onClick={() => setSort({ key: k, dir: active && sort.dir === "desc" ? "asc" : "desc" })}>
      {label}{active ? (sort.dir === "desc" ? " ▼" : " ▲") : ""}
    </th>
  );
}

function ZoneBar({ counts }: { counts: Record<Zone, number> }) {
  const total = ZONES.reduce((n, z) => n + counts[z], 0);
  if (!total) return <span style={{ color: A.muted2, fontSize: 12 }}>no verdicts yet</span>;
  return (
    <div>
      <div style={{ display: "flex", height: 12, borderRadius: 6, overflow: "hidden", background: A.line }}>
        {ZONES.map(z => counts[z] ? <div key={z} title={`${ZONE_META[z].label}: ${counts[z]}`}
          style={{ width: `${(counts[z] / total) * 100}%`, background: ZONE_META[z].color }} /> : null)}
      </div>
      <div style={{ display: "flex", gap: 12, flexWrap: "wrap", marginTop: 6, fontSize: 12 }}>
        {ZONES.map(z => (
          <span key={z} style={{ display: "inline-flex", alignItems: "center", gap: 5 }} title={ZONE_META[z].help}>
            <span style={{ width: 10, height: 10, borderRadius: 3, background: ZONE_META[z].color }} />
            {ZONE_META[z].label} <b>{counts[z]}</b> <span style={{ color: A.muted }}>({pct(counts[z], total)})</span>
          </span>
        ))}
      </div>
    </div>
  );
}

// ── Guide ──────────────────────────────────────────────────────────────────────────────────────
function Guide() {
  const term = (t: string, d: React.ReactNode) => (
    <div style={{ padding: "10px 0", borderTop: `1px solid ${A.line}` }}>
      <div style={{ fontWeight: 700, color: A.ink, fontSize: 13 }}>{t}</div>
      <div style={{ fontSize: 12.5, color: A.body, marginTop: 3, lineHeight: 1.55 }}>{d}</div>
    </div>
  );
  return (
    <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fit, minmax(340px, 1fr))", gap: 16 }}>
      <Card>
        <SLabel>What this page is</SLabel>
        <p style={{ fontSize: 13, lineHeight: 1.6, margin: 0 }}>
          <b>Jev</b> (TypeSafe) is a small, cheap model that only answers typed questions (yes/no, pick one) — it never
          writes text. Pipeline stages ask it questions such as <i>&quot;is this keyword about this place?&quot;</i> before spending money
          or storing data. This page shows <b>which stage asks which question</b>, whether the answer is <b>used</b>, how
          the verdicts fall, and what it costs.
        </p>
        <SLabel style={{ marginTop: 14 }}>How an answer becomes an action</SLabel>
        <ol style={{ fontSize: 12.5, lineHeight: 1.7, margin: 0, paddingLeft: 18 }}>
          <li>The stage sends the data + question to Jev → Jev returns a probability (0–1).</li>
          <li>The probability is placed in a <b>zone</b> using the question&apos;s two floors: ≥ accept floor → <ZonePill z="accept" />, ≤ reject ceiling → <ZonePill z="reject" />, in between → <ZonePill z="grey" />.</li>
          <li>Only if the question is in <ModePill m="enforce" /> mode <b>and</b> the zone is Accept/Reject does the stage change what it does. Everything else keeps the old rule.</li>
        </ol>
      </Card>
      <Card>
        <SLabel>Terms</SLabel>
        {term("Stage", "The pipeline step that asks, e.g. a3_research (segment keyword research), a0_ingest (Excel upload).")}
        {term("Question", "The exact question text and its key (e.g. a3_keyword_belongs). One stage can ask several.")}
        {term("Type", Object.entries(KIND_HELP).map(([k, v]) => <div key={k}><b>{k}</b>: {v}</div>))}
        {term("Mode", (["off", "shadow", "enforce"] as Mode[]).map(m => <div key={m} style={{ marginTop: 3 }}><ModePill m={m} /> {MODE_META[m].help}</div>))}
      </Card>
      <Card>
        <SLabel>Zones (one per answer)</SLabel>
        {ZONES.map(z => <div key={z} style={{ padding: "6px 0" }}><ZonePill z={z} /> <span style={{ fontSize: 12.5 }}>{ZONE_META[z].help}</span></div>)}
        {term("Accept floor / Reject ceiling", "The two probability limits that define the zones. They come from calibration: we label ~200 real examples and pick limits where Jev is right ≥ 95% of the time.")}
        {term("Calibration", "The record (docs/calibration/<question>.md) proving the floors. A question cannot be set to Enforce without one.")}
        {term("Verdict", "Jev's answer to one question about one subject (a keyword, a row, a sentence). Every Verdict is kept.")}
        {term("Cached", "A Verdict reused from an earlier answer to the same question wording and subject — no new Jev call, no cost. The zone is recomputed with today's floors. Changing a question's wording starts fresh.")}
        {term("Acted", "How many Verdicts actually changed what the stage did (Enforce + Accept/Reject).")}
        {term("Cost", "TypeSafe charges $0.042 per 1M input tokens (output free). Every call is also in External Spend under “Jev · TypeSafe”.")}
      </Card>
    </div>
  );
}

// ── Question editor ────────────────────────────────────────────────────────────────────────────
function QuestionEditor({ q, onSaved }: { q: Question; onSaved: () => void }) {
  const [mode, setMode] = useState<Mode>(q.mode);
  const [accept, setAccept] = useState(q.accept_floor?.toString() ?? "");
  const [reject, setReject] = useState(q.reject_ceiling?.toString() ?? "");
  const [calib, setCalib] = useState(q.calibration_ref ?? "");
  const [err, setErr] = useState<string | null>(null);
  const [saving, setSaving] = useState(false);
  const toNum = (s: string) => (s.trim() === "" ? null : Number(s));
  async function save() {
    if (mode === "enforce" && !window.confirm(`Set ${q.question_key} to ENFORCE? The ${q.stage} stage will start acting on confident answers.`)) return;
    setSaving(true); setErr(null);
    try {
      const r = await fetch(`/api/admin/decisions/questions/${encodeURIComponent(q.question_key)}`, {
        method: "PUT", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ mode, accept_floor: toNum(accept), reject_ceiling: toNum(reject), calibration_ref: calib.trim() || null }),
      });
      if (!r.ok) { const b = await r.json().catch(() => ({})); setErr(String(b.detail ?? r.status)); return; }
      onSaved();
    } finally { setSaving(false); }
  }
  return (
    <div style={{ display: "flex", gap: 12, alignItems: "flex-end", flexWrap: "wrap" }}>
      <label style={{ fontSize: 11.5, color: A.muted }}>Mode<br />
        <select value={mode} onChange={e => setMode(e.target.value as Mode)} style={input}>
          <option value="off">Off</option><option value="shadow">Shadow</option><option value="enforce">Enforce</option>
        </select>
      </label>
      <label style={{ fontSize: 11.5, color: A.muted }}>Accept floor (≥)<br /><input value={accept} onChange={e => setAccept(e.target.value)} placeholder="0.95" style={{ ...input, width: 90 }} /></label>
      <label style={{ fontSize: 11.5, color: A.muted }}>Reject ceiling (≤)<br /><input value={reject} onChange={e => setReject(e.target.value)} placeholder="0.30" style={{ ...input, width: 90 }} /></label>
      <label style={{ fontSize: 11.5, color: A.muted, flex: 1, minWidth: 240 }}>Calibration record (required for Enforce)<br />
        <input value={calib} onChange={e => setCalib(e.target.value)} placeholder="docs/calibration/<question>.md" style={{ ...input, width: "100%" }} />
      </label>
      <Btn variant="primary" onClick={save} disabled={saving}>{saving ? "Saving…" : "Save"}</Btn>
      {err && <div style={{ width: "100%", color: A.red, fontSize: 12 }}>{err}</div>}
    </div>
  );
}

// ── page ───────────────────────────────────────────────────────────────────────────────────────
type Tab = "overview" | "questions" | "verdicts" | "guide";
type QSortKey = "stage" | "question_key" | "mode" | "verdicts" | "acted" | "reject" | "error" | "cost_usd" | "avg_latency_ms" | "last_used_at";
type SSortKey = "stage" | "verdicts" | "acted" | "errors" | "cost_usd" | "avg_latency_ms" | "last_used_at";
type VSortKey = "created_at" | "probability" | "latency_ms" | "cost_usd";
const PAGE = 50;

export default function DecisionsPage() {
  const [tab, setTab] = useState<Tab>("overview");
  const [days, setDays] = useState(7);
  const [data, setData] = useState<Summary | null>(null);
  const [error, setError] = useState<string | null>(null);
  // AA-601 part B — recharts fill/stroke are SVG attributes; resolve zone colours + grid to concrete
  // values for portability (Chromium resolves var() in SVG attrs, other engines/exports do not).
  const chartColors = useChartColors({
    accept: ZONE_META.accept.cssVar, grey: ZONE_META.grey.cssVar, reject: ZONE_META.reject.cssVar,
    error: ZONE_META.error.cssVar, skipped: ZONE_META.skipped.cssVar, line: "--aa-line",
  });
  // questions tab
  const [qSearch, setQSearch] = useState(""); const [qStage, setQStage] = useState(""); const [qMode, setQMode] = useState("");
  const [qSort, setQSort] = useState<{ key: QSortKey; dir: "asc" | "desc" }>({ key: "stage", dir: "asc" });
  const [open, setOpen] = useState<string | null>(null);
  const [sSort, setSSort] = useState<{ key: SSortKey; dir: "asc" | "desc" }>({ key: "verdicts", dir: "desc" });
  // verdicts tab
  const [vStage, setVStage] = useState(""); const [vQuestion, setVQuestion] = useState(""); const [vZone, setVZone] = useState("");
  const [vMode, setVMode] = useState(""); const [vTenant, setVTenant] = useState(""); const [vSubject, setVSubject] = useState("");
  const [vSort, setVSort] = useState<{ key: VSortKey; dir: "asc" | "desc" }>({ key: "created_at", dir: "desc" });
  const [vPage, setVPage] = useState(0);
  const [log, setLog] = useState<{ rows: Verdict[]; total: number }>({ rows: [], total: 0 });

  const loadSummary = useCallback(async () => {
    try {
      const r = await fetch(`/api/admin/decisions/summary?days=${days}`);
      if (!r.ok) throw r.status;
      setData(await r.json()); setError(null);
    } catch (e) { setError(`Could not load summary (${e})`); }
  }, [days]);

  const loadLog = useCallback(async () => {
    const qs = new URLSearchParams({ days: String(days), limit: String(PAGE), offset: String(vPage * PAGE), sort: vSort.key, direction: vSort.dir });
    if (vStage) qs.set("stage", vStage); if (vQuestion) qs.set("question_key", vQuestion); if (vZone) qs.set("zone", vZone);
    if (vMode) qs.set("mode", vMode); if (vTenant) qs.set("tenant", vTenant); if (vSubject) qs.set("subject", vSubject);
    try {
      const r = await fetch(`/api/admin/decisions/log?${qs}`);
      if (!r.ok) throw r.status;
      const d = await r.json(); setLog({ rows: d.decisions ?? [], total: d.total ?? 0 });
    } catch (e) { setError(`Could not load verdicts (${e})`); }
  }, [days, vStage, vQuestion, vZone, vMode, vTenant, vSubject, vSort, vPage]);

  // eslint-disable-next-line react-hooks/set-state-in-effect -- fetch on window change, same pattern as every admin page
  useEffect(() => { loadSummary(); }, [loadSummary]);
  // eslint-disable-next-line react-hooks/set-state-in-effect -- fetch on filter change
  useEffect(() => { if (tab === "verdicts") loadLog(); }, [tab, loadLog]);

  const qs = useMemo(() => data?.questions ?? [], [data]);
  const openQ = qs.find(q => q.question_key === open) ?? null;
  const stagesAll = useMemo(() => Array.from(new Set([...(data?.stages ?? []).map(s => s.stage), ...qs.map(q => q.stage)])).sort(), [data, qs]);
  const totals = useMemo(() => {
    const t = { accept: 0, grey: 0, reject: 0, error: 0, skipped: 0 } as Record<Zone, number>;
    for (const d of data?.daily ?? []) for (const z of ZONES) t[z] += n0(d[z]);
    return t;
  }, [data]);
  const verdictTotal = ZONES.reduce((n, z) => n + n0(totals[z]), 0);
  const acted = (data?.stages ?? []).reduce((n, s) => n + n0(s.acted), 0);
  const latencies = (data?.stages ?? []).filter(s => s.avg_latency_ms != null);
  const avgLatency = latencies.length ? latencies.reduce((n, s) => n + n0(s.avg_latency_ms) * n0(s.verdicts), 0) / Math.max(1, latencies.reduce((n, s) => n + n0(s.verdicts), 0)) : null;

  const filteredQs = useMemo(() => {
    const s = qSearch.toLowerCase();
    const rows = qs.filter(q => (!qStage || q.stage === qStage) && (!qMode || q.mode === qMode)
      && (!s || q.question_key.toLowerCase().includes(s) || q.instructions.toLowerCase().includes(s)));
    const dir = qSort.dir === "asc" ? 1 : -1;
    return [...rows].sort((a, b) => {
      const x = a[qSort.key] ?? ""; const y = b[qSort.key] ?? "";
      return (x < y ? -1 : x > y ? 1 : 0) * dir;
    });
  }, [qs, qSearch, qStage, qMode, qSort]);

  const stageRows = useMemo(() => {
    const dir = sSort.dir === "asc" ? 1 : -1;
    return [...(data?.stages ?? [])].sort((a, b) => {
      const x = a[sSort.key] ?? ""; const y = b[sSort.key] ?? "";
      return (x < y ? -1 : x > y ? 1 : 0) * dir;
    });
  }, [data, sSort]);

  const tabBtn = (t: Tab, label: string) => (
    <button onClick={() => setTab(t)} style={{
      padding: "9px 16px", border: "none", borderBottom: `3px solid ${tab === t ? A.accent : "transparent"}`,
      background: "none", fontFamily: sans, fontSize: 13.5, fontWeight: tab === t ? 700 : 500,
      color: tab === t ? A.ink : A.muted, cursor: "pointer",
    }}>{label}</button>
  );
  const resetPage = <T,>(set: (v: T) => void) => (v: T) => { set(v); setVPage(0); };

  return (
    <div style={{ display: "flex", height: "100vh", background: A.bg, fontFamily: sans }}>
      <AdminSidebar />
      <main className="aa-admin-main" style={{ flex: 1, minWidth: 0, overflowY: "auto" }}>
        {/* Sticky header + tabs */}
        <div style={{ position: "sticky", top: 0, zIndex: 5, background: A.bg, padding: "22px 32px 0", borderBottom: `1px solid ${A.line}` }}>
          <div style={{ display: "flex", alignItems: "center", gap: 12 }}>
            <div style={{ width: 36, height: 36, borderRadius: 9, background: alpha(A.accent, 13), color: A.accent, display: "grid", placeItems: "center" }}><Scale size={18} /></div>
            <div style={{ flex: 1, minWidth: 0 }}>
              <h1 style={{ fontFamily: serif, fontSize: 22, fontWeight: 600, color: A.ink, margin: 0 }}>Jev Decisions</h1>
              <div style={{ fontSize: 12, color: A.muted, marginTop: 2 }}>
                Which pipeline stage asks Jev which question, whether the answer is used, and what it costs.
                New here? Open <a onClick={() => setTab("guide")} style={{ color: A.accentDeep, fontWeight: 600, cursor: "pointer" }}>Guide</a>.
              </div>
            </div>
            <select value={days} onChange={e => { setDays(Number(e.target.value)); setVPage(0); }} style={input} title="Time window for all numbers on this page">
              <option value={1}>Last 24 h</option><option value={7}>Last 7 days</option><option value={30}>Last 30 days</option><option value={90}>Last 90 days</option>
            </select>
            <Btn onClick={() => { loadSummary(); if (tab === "verdicts") loadLog(); }}><RefreshCw size={13} /> Refresh</Btn>
          </div>
          <div style={{ display: "flex", gap: 4, marginTop: 12 }}>
            {tabBtn("overview", "Overview")}{tabBtn("questions", `Questions (${qs.length})`)}{tabBtn("verdicts", "Verdicts")}{tabBtn("guide", "Guide")}
          </div>
        </div>

        <div style={{ padding: "20px 32px 40px" }}>
          {error && <Card style={{ color: A.red, marginBottom: 16 }}>{error}</Card>}
          {!data && !error && tab !== "guide" && <LoadingScreen msg="Loading…" />}

          {tab === "guide" && <Guide />}

          {data && tab === "overview" && (
            <>
              <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fit, minmax(160px, 1fr))", gap: 12, marginBottom: 18 }}>
                <Kpi label={`Jev calls · ${data.days}d`} value={data.total_calls.toLocaleString()} sub="billed API calls" />
                <Kpi label={`Cost · ${data.days}d`} value={usd(data.total_cost_usd)} sub="also in External Spend" />
                <Kpi label="Verdicts" value={verdictTotal.toLocaleString()} sub={`one per question asked · ${(data.stages ?? []).reduce((n, s) => n + (s.cached ?? 0), 0)} from cache`} />
                <Kpi label="Acted on" value={acted.toLocaleString()} sub={`${pct(acted, verdictTotal)} of verdicts changed a stage`} color={acted ? MODE_META.enforce.color : A.ink} />
                <Kpi label="Errors" value={totals.error.toLocaleString()} sub={`${pct(totals.error, verdictTotal)} — fail-open`} color={totals.error ? ZONE_META.error.color : A.ink} />
                <Kpi label="Avg latency" value={avgLatency != null ? `${Math.round(avgLatency)} ms` : "—"} sub="per call" />
                <Kpi label="Questions" value={`${qs.length}`} sub={`${qs.filter(q => q.mode === "enforce").length} enforce · ${qs.filter(q => q.mode === "shadow").length} shadow · ${qs.filter(q => q.mode === "off").length} off`} />
                <Kpi label="Tenant allow-list" value={`${data.tenant_allowlist.length}`} sub={data.tenant_allowlist.map(t => t.slug).join(", ") || "platform content only"} />
              </div>

              <Card style={{ marginBottom: 18 }}>
                <SLabel>How the verdicts fell ({data.days} days)</SLabel>
                <ZoneBar counts={totals} />
              </Card>

              <Card style={{ marginBottom: 18 }}>
                <SLabel>Verdicts per day, by zone</SLabel>
                {(data.daily ?? []).length === 0 ? <div style={{ fontSize: 12.5, color: A.muted }}>No verdicts in this window.</div> : (
                  <div style={{ height: 240 }}>
                    <ResponsiveContainer width="100%" height="100%">
                      <BarChart data={data.daily ?? []}>
                        <CartesianGrid strokeDasharray="3 3" stroke={chartColors.line} />
                        <XAxis dataKey="day" fontSize={11} /><YAxis fontSize={11} allowDecimals={false} />
                        <Tooltip {...CHART_TOOLTIP} />
                        <Legend />
                        {ZONES.map(z => <Bar key={z} dataKey={z} name={ZONE_META[z].label} stackId="z" fill={chartColors[z]} />)}
                      </BarChart>
                    </ResponsiveContainer>
                  </div>
                )}
              </Card>

              <SLabel>By stage</SLabel>
              <div style={{ ...TABLE_BOX, maxHeight: "none", marginBottom: 12 }}>
                <table style={{ width: "100%", borderCollapse: "collapse", fontSize: 12.5 }}>
                  <thead><tr>
                    <SortTh label="Stage" k="stage" sort={sSort} setSort={setSSort} title="Pipeline step that asks Jev" />
                    <th style={STICKY_TH} title="Distinct questions asked by this stage">Questions</th>
                    <SortTh label="Verdicts" k="verdicts" sort={sSort} setSort={setSSort} />
                    <SortTh label="Acted" k="acted" sort={sSort} setSort={setSSort} title="Verdicts that changed what the stage did" />
                    <SortTh label="Errors" k="errors" sort={sSort} setSort={setSSort} />
                    <th style={STICKY_TH}>Skipped</th>
                    <th style={STICKY_TH} title="Billed calls in llm_call_log">Calls</th>
                    <SortTh label="Cost" k="cost_usd" sort={sSort} setSort={setSSort} />
                    <SortTh label="Avg latency" k="avg_latency_ms" sort={sSort} setSort={setSSort} />
                    <SortTh label="Last used" k="last_used_at" sort={sSort} setSort={setSSort} />
                  </tr></thead>
                  <tbody>
                    {stageRows.map(s => {
                      const calls = (data.calls_by_stage ?? []).find(c => c.stage === s.stage);
                      return (
                        <tr key={s.stage}>
                          <td style={{ ...TD, fontFamily: mono, fontWeight: 700 }}>{s.stage}</td>
                          <td style={TD}>{n0(s.questions)}</td>
                          <td style={TD}>{n0(s.verdicts).toLocaleString()}</td>
                          <td style={{ ...TD, fontWeight: 700, color: s.acted ? MODE_META.enforce.color : A.muted }}>{n0(s.acted)}</td>
                          <td style={{ ...TD, color: s.errors ? ZONE_META.error.color : A.muted }}>{n0(s.errors)} <span style={{ color: A.muted }}>({pct(n0(s.errors), n0(s.verdicts))})</span></td>
                          <td style={TD}>{n0(s.skipped)}</td>
                          <td style={TD}>{calls?.calls ?? "—"}</td>
                          <td style={{ ...TD, fontFamily: mono }}>{usd(calls?.cost_usd ?? s.cost_usd)}</td>
                          <td style={{ ...TD, fontFamily: mono }}>{s.avg_latency_ms != null ? `${Math.round(s.avg_latency_ms)} ms` : "—"}</td>
                          <td style={{ ...TD, whiteSpace: "nowrap" }}>{when(s.last_used_at)}</td>
                        </tr>
                      );
                    })}
                    {!stageRows.length && <tr><td style={TD} colSpan={10}>No stage has asked Jev in this window.</td></tr>}
                  </tbody>
                </table>
              </div>
              <div style={{ fontSize: 11.5, color: A.muted }}>
                Stages starting with <span style={{ fontFamily: mono }}>adhoc_</span> are one-off scripts (smoke tests, calibration runs), not the pipeline.
              </div>
            </>
          )}

          {data && tab === "questions" && (
            <>
              <div style={{ display: "flex", gap: 10, flexWrap: "wrap", marginBottom: 12 }}>
                <input value={qSearch} onChange={e => setQSearch(e.target.value)} placeholder="Search question…" style={{ ...input, width: 220 }} />
                <select value={qStage} onChange={e => setQStage(e.target.value)} style={input}><option value="">All stages</option>{stagesAll.map(s => <option key={s} value={s}>{s}</option>)}</select>
                <select value={qMode} onChange={e => setQMode(e.target.value)} style={input}><option value="">All modes</option><option value="off">Off</option><option value="shadow">Shadow</option><option value="enforce">Enforce</option></select>
                <span style={{ fontSize: 12, color: A.muted, alignSelf: "center" }}>{filteredQs.length} of {qs.length} · click a row — details and settings open below the table</span>
              </div>
              <div style={TABLE_BOX}>
                <table style={{ width: "100%", borderCollapse: "collapse", fontSize: 12.5 }}>
                  <thead><tr>
                    <th style={STICKY_TH} />
                    <SortTh label="Stage" k="stage" sort={qSort} setSort={setQSort} />
                    <SortTh label="Question" k="question_key" sort={qSort} setSort={setQSort} />
                    <th style={STICKY_TH} title="noul = yes/no · choice = pick one">Type</th>
                    <SortTh label="Mode" k="mode" sort={qSort} setSort={setQSort} title="Off / Shadow (logged only) / Enforce (acted on)" />
                    <th style={STICKY_TH} title="Probability at or above which Jev's YES is trusted">Accept ≥</th>
                    <th style={STICKY_TH} title="Probability at or below which Jev's NO is trusted">Reject ≤</th>
                    <th style={STICKY_TH}>Calibrated</th>
                    <SortTh label="Verdicts" k="verdicts" sort={qSort} setSort={setQSort} />
                    <th style={STICKY_TH} title="Verdicts reused from an earlier answer to the same wording and subject (no Jev call, no cost)">Cached</th>
                    <th style={{ ...STICKY_TH, color: ZONE_META.accept.color }}>Accept</th>
                    <th style={STICKY_TH}>Grey</th>
                    <SortTh label="Reject" k="reject" sort={qSort} setSort={setQSort} />
                    <SortTh label="Error" k="error" sort={qSort} setSort={setQSort} />
                    <th style={STICKY_TH}>Skipped</th>
                    <SortTh label="Acted" k="acted" sort={qSort} setSort={setQSort} />
                    <th style={STICKY_TH} title="Average probability / confidence">Avg p</th>
                    <SortTh label="Latency p50 / p95" k="avg_latency_ms" sort={qSort} setSort={setQSort} />
                    <SortTh label="Cost" k="cost_usd" sort={qSort} setSort={setQSort} />
                    <SortTh label="Last used" k="last_used_at" sort={qSort} setSort={setQSort} />
                  </tr></thead>
                  <tbody>
                    {filteredQs.map(q => {
                      const isOpen = open === q.question_key;
                      return (
                        <tr key={q.question_key} onClick={() => setOpen(isOpen ? null : q.question_key)} style={{ cursor: "pointer", background: isOpen ? A.accentTint : undefined }}>
                          <td style={TD}>{isOpen ? <ChevronDown size={14} /> : <ChevronRight size={14} />}</td>
                          <td style={{ ...TD, fontFamily: mono }}>{q.stage}</td>
                          <td style={{ ...TD, fontFamily: mono, fontWeight: 700 }} title={q.instructions}>{q.question_key}</td>
                          <td style={TD} title={KIND_HELP[q.kind]}>{q.kind}</td>
                          <td style={TD}><ModePill m={q.mode} /></td>
                          <td style={{ ...TD, fontFamily: mono }}>{num(q.accept_floor)}</td>
                          <td style={{ ...TD, fontFamily: mono }}>{num(q.reject_ceiling)}</td>
                          <td style={TD}>{q.calibration_ref ? <Pill color="var(--aa-green-deep)">Yes</Pill> : <Pill color="var(--aa-muted2)">No</Pill>}</td>
                          <td style={{ ...TD, fontWeight: 700 }}>{q.verdicts}</td>
                          <td style={TD}>{q.cached ?? 0}</td>
                          <td style={{ ...TD, color: ZONE_META.accept.color, fontWeight: 600 }}>{q.accept}</td>
                          <td style={TD}>{q.grey}</td>
                          <td style={{ ...TD, color: ZONE_META.reject.color, fontWeight: 600 }}>{q.reject}</td>
                          <td style={{ ...TD, color: q.error ? ZONE_META.error.color : undefined }}>{q.error}</td>
                          <td style={TD}>{q.skipped}</td>
                          <td style={{ ...TD, fontWeight: 700, color: q.acted ? MODE_META.enforce.color : A.muted }}>{q.acted}</td>
                          <td style={{ ...TD, fontFamily: mono }}>{num(q.avg_probability)}</td>
                          <td style={{ ...TD, fontFamily: mono, whiteSpace: "nowrap" }}>{q.p50_latency_ms != null ? `${Math.round(q.p50_latency_ms)} / ${Math.round(q.p95_latency_ms ?? 0)} ms` : "—"}</td>
                          <td style={{ ...TD, fontFamily: mono }}>{usd(q.cost_usd)}</td>
                          <td style={{ ...TD, whiteSpace: "nowrap" }}>{when(q.last_used_at)}</td>
                        </tr>
                      );
                    })}
                    {!filteredQs.length && <tr><td style={TD} colSpan={20}>No questions match.</td></tr>}
                  </tbody>
                </table>
              </div>
              {openQ && (() => { const q = openQ; return (
                <Card style={{ marginTop: 14, borderLeft: `4px solid ${A.accent}` }}>
                  <div style={{ display: "flex", alignItems: "center", gap: 10, marginBottom: 12 }}>
                    <span style={{ fontFamily: mono, fontWeight: 700, fontSize: 14 }}>{q.question_key}</span>
                    <ModePill m={q.mode} /><span style={{ fontSize: 12, color: A.muted }}>stage {q.stage}</span>
                    <span style={{ flex: 1 }} /><Btn size="sm" onClick={() => setOpen(null)}>Close</Btn>
                  </div>
<div style={{ display: "grid", gridTemplateColumns: "minmax(260px, 1fr) minmax(260px, 1fr)", gap: 18 }}>
                              <div>
                                <SLabel>Question asked</SLabel>
                                <div style={{ fontSize: 13.5, fontWeight: 600, color: A.ink }}>{q.instructions}</div>
                                <div style={{ fontSize: 12, color: A.muted, marginTop: 4 }}>{KIND_HELP[q.kind]}</div>
                                {q.criteria != null && (
                                  <>
                                    <SLabel style={{ marginTop: 12 }}>{q.kind === "noul" ? "Meaning of yes / no" : "Options"}</SLabel>
                                    <ul style={{ margin: 0, paddingLeft: 18, fontSize: 12.5 }}>
                                      {Object.entries(q.criteria as Record<string, unknown>).map(([k, v]) => <li key={k}><b>{k}</b>{v ? ` — ${String(v)}` : ""}</li>)}
                                    </ul>
                                  </>
                                )}
                                {q.notes && <div style={{ fontSize: 12, color: A.muted, marginTop: 10 }}>{q.notes}</div>}
                              </div>
                              <div>
                                <SLabel>Verdicts ({data.days} days)</SLabel>
                                <ZoneBar counts={{ accept: q.accept, grey: q.grey, reject: q.reject, error: q.error, skipped: q.skipped }} />
                                <div style={{ fontSize: 12, color: A.muted, marginTop: 10 }}>
                                  Floors v{q.threshold_version} · calibration: {q.calibration_ref ?? "none yet (cannot enforce)"} ·
                                  first used {when(q.first_used_at)} · updated {when(q.updated_at)} by {q.updated_by}
                                </div>
                                <Btn size="sm" style={{ marginTop: 10 }} onClick={() => { setVQuestion(q.question_key); setVPage(0); setTab("verdicts"); }}>See its verdicts →</Btn>
                              </div>
                            </div>
                            <div style={{ marginTop: 16 }}><SLabel>Settings</SLabel><QuestionEditor q={q} onSaved={() => { setOpen(null); loadSummary(); }} /></div>
                                          </Card>
              ); })()}
            </>
          )}

          {data && tab === "verdicts" && (
            <>
              <div style={{ display: "flex", gap: 10, flexWrap: "wrap", marginBottom: 12 }}>
                <select value={vStage} onChange={e => resetPage(setVStage)(e.target.value)} style={input}><option value="">All stages</option>{stagesAll.map(s => <option key={s} value={s}>{s}</option>)}</select>
                <select value={vQuestion} onChange={e => resetPage(setVQuestion)(e.target.value)} style={input}><option value="">All questions</option>{qs.map(q => <option key={q.question_key} value={q.question_key}>{q.question_key}</option>)}</select>
                <select value={vZone} onChange={e => resetPage(setVZone)(e.target.value)} style={input}><option value="">All zones</option>{ZONES.map(z => <option key={z} value={z}>{ZONE_META[z].label}</option>)}</select>
                <select value={vMode} onChange={e => resetPage(setVMode)(e.target.value)} style={input}><option value="">All modes</option><option value="off">Off</option><option value="shadow">Shadow</option><option value="enforce">Enforce</option></select>
                <select value={vTenant} onChange={e => resetPage(setVTenant)(e.target.value)} style={input}>
                  <option value="">All content</option><option value="platform">Platform</option>
                  {data.tenant_allowlist.map(t => <option key={t.slug} value={t.slug}>{t.slug}</option>)}
                </select>
                <input value={vSubject} onChange={e => resetPage(setVSubject)(e.target.value)} placeholder="Subject contains…" style={{ ...input, width: 200 }} />
              </div>
              <div style={TABLE_BOX}>
                <table style={{ width: "100%", borderCollapse: "collapse", fontSize: 12.5 }}>
                  <thead><tr>
                    <SortTh label="Time" k="created_at" sort={vSort} setSort={s => { setVSort(s); setVPage(0); }} />
                    <th style={STICKY_TH}>Stage</th>
                    <th style={STICKY_TH}>Question</th>
                    <th style={STICKY_TH} title="What was judged (keyword, row, atom…)">Subject</th>
                    <th style={STICKY_TH} title="Whose content: platform or a tenant">Content</th>
                    <th style={STICKY_TH}>Mode</th>
                    <th style={STICKY_TH}>Zone</th>
                    <SortTh label="Probability / pick" k="probability" sort={vSort} setSort={s => { setVSort(s); setVPage(0); }} />
                    <th style={STICKY_TH} title="Did this answer change what the stage did?">Acted</th>
                    <SortTh label="Latency" k="latency_ms" sort={vSort} setSort={s => { setVSort(s); setVPage(0); }} />
                    <SortTh label="Cost" k="cost_usd" sort={vSort} setSort={s => { setVSort(s); setVPage(0); }} />
                    <th style={STICKY_TH}>Note</th>
                  </tr></thead>
                  <tbody>
                    {log.rows.map(v => {
                      const actedOn = v.mode === "enforce" && (v.zone === "accept" || v.zone === "reject");
                      return (
                        <tr key={v.id}>
                          <td style={{ ...TD, whiteSpace: "nowrap" }}>{when(v.created_at)}</td>
                          <td style={{ ...TD, fontFamily: mono }}>{v.stage}</td>
                          <td style={{ ...TD, fontFamily: mono }}>{v.question_key}</td>
                          <td style={{ ...TD, maxWidth: 280, overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }} title={v.subject_key}>{v.subject_key}</td>
                          <td style={TD}>{v.tenant_slug ?? "platform"}</td>
                          <td style={TD}><ModePill m={v.mode} /></td>
                          <td style={TD}><ZonePill z={v.zone} /></td>
                          <td style={{ ...TD, fontFamily: mono, fontWeight: 700 }}>{v.choice ? `${v.choice} (${num(v.probability)})` : num(v.probability, 3)}</td>
                          <td style={{ ...TD, fontWeight: 700, color: actedOn ? MODE_META.enforce.color : A.muted }}>{actedOn ? "Yes" : "No"}</td>
                          <td style={{ ...TD, fontFamily: mono }}>{v.latency_ms != null ? `${v.latency_ms} ms` : "—"}</td>
                          <td style={{ ...TD, fontFamily: mono }}>{usd(v.cost_usd ?? 0)}</td>
                          <td style={{ ...TD, fontSize: 11.5, color: A.muted, maxWidth: 240 }}>{v.error ?? v.outcome ?? ""}</td>
                        </tr>
                      );
                    })}
                    {!log.rows.length && <tr><td style={TD} colSpan={12}>No verdicts match these filters.</td></tr>}
                  </tbody>
                </table>
              </div>
              <div style={{ display: "flex", alignItems: "center", gap: 10, marginTop: 10, fontSize: 12.5 }}>
                <span style={{ color: A.muted }}>
                  {log.total ? `${vPage * PAGE + 1}–${Math.min((vPage + 1) * PAGE, log.total)} of ${log.total.toLocaleString()}` : "0 verdicts"}
                </span>
                <Btn size="sm" disabled={vPage === 0} onClick={() => setVPage(p => p - 1)}>← Prev</Btn>
                <Btn size="sm" disabled={(vPage + 1) * PAGE >= log.total} onClick={() => setVPage(p => p + 1)}>Next →</Btn>
              </div>
            </>
          )}

          {tab !== "guide" && (
            <div style={{ marginTop: 22, fontSize: 12, color: A.muted, display: "flex", alignItems: "center", gap: 6 }}>
              <BookOpen size={13} /> Colours: {ZONES.map(z => <ZonePill key={z} z={z} />)} · modes: {(["off", "shadow", "enforce"] as Mode[]).map(m => <ModePill key={m} m={m} />)}
            </div>
          )}
        </div>
      </main>
    </div>
  );
}
