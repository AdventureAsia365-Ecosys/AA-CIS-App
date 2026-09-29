"use client";
// app/admin/decisions/page.tsx — AA-660 Jev (TypeSafe) decision layer: which stage asks which
// question, in what mode (off / shadow / enforce), with what calibrated floors, how its verdicts fall
// (accept / grey / reject / error / skipped), and what it costs — the Jev counterpart of the per-stage
// LLM model settings. API: /admin/decisions/*. Jev spend also appears in External Spend
// (llm_call_log, provider "typesafe").

import { useCallback, useEffect, useState } from "react";
import { RefreshCw, Scale } from "lucide-react";
import AdminSidebar from "../_components/AdminSidebar";
import { A, serif, sans, mono, Card, SLabel, Badge, Btn, LoadingScreen, StatCard, TH, TD } from "../_components/adminUi";

type Mode = "off" | "shadow" | "enforce";
type Zone = "accept" | "grey" | "reject" | "error" | "skipped";

interface Question {
  question_key: string; stage: string; kind: string; instructions: string; mode: Mode;
  accept_floor: number | null; reject_ceiling: number | null; threshold_version: number;
  calibration_ref: string | null; notes: string | null; updated_at: string; updated_by: string;
  verdicts: number; accept: number; grey: number; reject: number; error: number; skipped: number;
  acted: number; cost_usd: number; avg_latency_ms: number | null; last_used_at: string | null;
}
interface Summary {
  days: number; questions: Question[];
  unregistered: { stage: string; question_key: string; verdicts: number; cost_usd: number; last_used_at: string }[];
  calls_by_stage: { stage: string; calls: number; cost_usd: number; tokens_in: number }[];
  total_cost_usd: number; total_calls: number;
  tenant_allowlist: { tenant_id: string; slug: string; name: string; reason: string }[];
}
interface Verdict {
  id: number; created_at: string; stage: string; question_key: string; subject_key: string;
  tenant_slug: string | null; job_id: string | null; mode: Mode; zone: Zone; probability: number | null;
  choice: string | null; threshold_version: number | null; latency_ms: number | null; cost_usd: number;
  error: string | null; outcome: string | null;
}

const ZONES: Zone[] = ["accept", "grey", "reject", "error", "skipped"];
const ZONE_COLOR: Record<Zone, "green" | "gray" | "red" | "amber" | "blue"> =
  { accept: "green", grey: "gray", reject: "red", error: "amber", skipped: "blue" };
const ZONE_BAR: Record<Zone, string> =
  { accept: "#10B981", grey: "#9CA3AF", reject: "#EF4444", error: "#F59E0B", skipped: "#93C5FD" };
const MODE_COLOR: Record<Mode, "gray" | "blue" | "gold"> = { off: "gray", shadow: "blue", enforce: "gold" };

const usd = (v: number) => (v < 0.01 ? `$${v.toFixed(5)}` : `$${v.toFixed(3)}`);
const when = (s: string | null) => (s ? new Date(s).toLocaleString() : "—");
const floor = (v: number | null) => (v == null ? "—" : v.toFixed(2));

function ZoneBar({ q }: { q: Question }) {
  const total = ZONES.reduce((n, z) => n + q[z], 0);
  if (!total) return <span style={{ color: A.muted2, fontSize: 12 }}>no verdicts</span>;
  return (
    <div style={{ minWidth: 150 }}>
      <div style={{ display: "flex", height: 8, borderRadius: 4, overflow: "hidden", background: A.line }}>
        {ZONES.map(z => q[z] ? <div key={z} title={`${z}: ${q[z]}`}
          style={{ width: `${(q[z] / total) * 100}%`, background: ZONE_BAR[z] }} /> : null)}
      </div>
      <div style={{ fontSize: 10.5, color: A.muted, marginTop: 3, fontFamily: mono }}>
        {ZONES.filter(z => q[z]).map(z => `${z} ${q[z]}`).join(" · ")}
      </div>
    </div>
  );
}

function QuestionEditor({ q, onSaved, onCancel }: { q: Question; onSaved: () => void; onCancel: () => void }) {
  const [mode, setMode] = useState<Mode>(q.mode);
  const [accept, setAccept] = useState(q.accept_floor?.toString() ?? "");
  const [reject, setReject] = useState(q.reject_ceiling?.toString() ?? "");
  const [calib, setCalib] = useState(q.calibration_ref ?? "");
  const [err, setErr] = useState<string | null>(null);
  const [saving, setSaving] = useState(false);
  const num = (s: string) => (s.trim() === "" ? null : Number(s));

  async function save() {
    setSaving(true); setErr(null);
    try {
      const r = await fetch(`/api/admin/decisions/questions/${encodeURIComponent(q.question_key)}`, {
        method: "PUT", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ mode, accept_floor: num(accept), reject_ceiling: num(reject),
                               calibration_ref: calib.trim() || null }),
      });
      if (!r.ok) { const b = await r.json().catch(() => ({})); setErr(String(b.detail ?? r.status)); return; }
      onSaved();
    } finally { setSaving(false); }
  }

  const input: React.CSSProperties = { padding: "6px 8px", border: `1px solid ${A.line}`, borderRadius: 6, fontSize: 12.5, fontFamily: sans };
  return (
    <div style={{ display: "flex", gap: 10, alignItems: "flex-end", flexWrap: "wrap", padding: "10px 0" }}>
      <label style={{ fontSize: 11, color: A.muted }}>Mode<br />
        <select value={mode} onChange={e => setMode(e.target.value as Mode)} style={input}>
          <option value="off">off</option><option value="shadow">shadow</option><option value="enforce">enforce</option>
        </select>
      </label>
      <label style={{ fontSize: 11, color: A.muted }}>Accept floor<br />
        <input value={accept} onChange={e => setAccept(e.target.value)} placeholder="e.g. 0.85" style={{ ...input, width: 90 }} />
      </label>
      <label style={{ fontSize: 11, color: A.muted }}>Reject ceiling<br />
        <input value={reject} onChange={e => setReject(e.target.value)} placeholder="e.g. 0.15" style={{ ...input, width: 90 }} />
      </label>
      <label style={{ fontSize: 11, color: A.muted, flex: 1, minWidth: 220 }}>Calibration record (required for enforce)<br />
        <input value={calib} onChange={e => setCalib(e.target.value)} placeholder="docs/calibration/<question>.md" style={{ ...input, width: "100%" }} />
      </label>
      <Btn variant="primary" onClick={save} disabled={saving}>{saving ? "Saving…" : "Save"}</Btn>
      <Btn onClick={onCancel}>Cancel</Btn>
      {err && <div style={{ width: "100%", color: A.red, fontSize: 12 }}>{err}</div>}
    </div>
  );
}

export default function DecisionsPage() {
  const [days, setDays] = useState(7);
  const [data, setData] = useState<Summary | null>(null);
  const [log, setLog] = useState<Verdict[]>([]);
  const [fQuestion, setFQuestion] = useState("");
  const [fZone, setFZone] = useState("");
  const [fSubject, setFSubject] = useState("");
  const [editing, setEditing] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);

  const load = useCallback(async () => {
    const qs = new URLSearchParams({ limit: "200" });
    if (fQuestion) qs.set("question_key", fQuestion);
    if (fZone) qs.set("zone", fZone);
    if (fSubject) qs.set("subject", fSubject);
    try {
      const [s, l] = await Promise.all([
        fetch(`/api/admin/decisions/summary?days=${days}`).then(r => r.ok ? r.json() : Promise.reject(r.status)),
        fetch(`/api/admin/decisions/log?${qs}`).then(r => r.ok ? r.json() : Promise.reject(r.status)),
      ]);
      setData(s); setLog(l.decisions ?? []); setError(null);
    } catch (e) {
      setError(`Could not load decisions (${e})`);
    }
  }, [days, fQuestion, fZone, fSubject]);

  useEffect(() => {
    // eslint-disable-next-line react-hooks/set-state-in-effect -- initial + filter-change fetch, same pattern as every admin page
    load();
  }, [load]);

  const stages = data ? Array.from(new Set(data.questions.map(q => q.stage))) : [];
  const modeCount = (m: Mode) => data?.questions.filter(q => q.mode === m).length ?? 0;
  const select: React.CSSProperties = { padding: "6px 8px", border: `1px solid ${A.line}`, borderRadius: 6, fontSize: 12.5, fontFamily: sans, background: "#fff" };

  return (
    <div style={{ display: "flex", height: "100vh", background: A.bg, fontFamily: sans }}>
      <AdminSidebar />
      <main style={{ flex: 1, padding: "32px 36px", minWidth: 0, overflowY: "auto" }}>
        <div style={{ display: "flex", alignItems: "center", gap: 12, marginBottom: 24 }}>
          <div style={{ width: 36, height: 36, borderRadius: 9, background: `${A.accent}15`, color: A.accent, display: "grid", placeItems: "center" }}>
            <Scale size={18} />
          </div>
          <div style={{ flex: 1 }}>
            <h1 style={{ fontFamily: serif, fontSize: 22, fontWeight: 500, color: A.ink, margin: 0 }}>Jev Decisions</h1>
            <div style={{ fontSize: 11.5, color: A.muted2, marginTop: 2 }}>
              Questions each stage asks Jev (TypeSafe): mode, calibrated floors, verdicts and cost. Only an
              <b> enforce</b> question with a confident verdict changes what a stage does.
            </div>
          </div>
          <select value={days} onChange={e => setDays(Number(e.target.value))} style={select}>
            <option value={1}>Last 24 h</option><option value={7}>Last 7 days</option><option value={30}>Last 30 days</option>
          </select>
          <Btn onClick={load}><RefreshCw size={13} /> Refresh</Btn>
        </div>

        {error && <Card style={{ color: A.red, marginBottom: 16 }}>{error}</Card>}
        {!data && !error && <LoadingScreen msg="Loading decisions…" />}

        {data && (
          <>
            <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fit, minmax(180px, 1fr))", gap: 14, marginBottom: 20 }}>
              <StatCard label={`Jev calls · ${data.days}d`} value={data.total_calls.toLocaleString()} sub="one call per decide()" />
              <StatCard label={`Jev cost · ${data.days}d`} value={usd(data.total_cost_usd)} sub="also in External Spend" />
              <StatCard label="Questions" value={`${data.questions.length}`}
                sub={`${modeCount("enforce")} enforce · ${modeCount("shadow")} shadow · ${modeCount("off")} off`} />
              <StatCard label="Tenant allow-list" value={`${data.tenant_allowlist.length}`}
                sub={data.tenant_allowlist.map(t => t.slug).join(", ") || "platform content only"} />
            </div>

            <Card style={{ marginBottom: 20, padding: 0, overflowX: "auto" }}>
              <div style={{ padding: "16px 18px 6px" }}><SLabel>Questions by stage</SLabel></div>
              <table style={{ width: "100%", borderCollapse: "collapse" }}>
                <thead><tr>
                  {["Stage / question", "Mode", "Floors (accept / reject)", "Calibration", "Verdicts", "Acted", "Avg latency", "Cost", "Last used", ""]
                    .map(h => <th key={h} style={TH}>{h}</th>)}
                </tr></thead>
                <tbody>
                  {stages.map(stage => data.questions.filter(q => q.stage === stage).map((q, i) => (
                    <FragmentRows key={q.question_key} first={i === 0} q={q} editing={editing === q.question_key}
                      onEdit={() => setEditing(q.question_key)} onCancel={() => setEditing(null)}
                      onSaved={() => { setEditing(null); load(); }} />
                  )))}
                  {!data.questions.length && (
                    <tr><td style={TD} colSpan={10}>No questions yet — stages add them as Jev is wired in (AA-690…AA-696).</td></tr>
                  )}
                </tbody>
              </table>
            </Card>

            {(data.calls_by_stage.length > 0 || data.unregistered.length > 0) && (
              <Card style={{ marginBottom: 20 }}>
                <SLabel>Billed calls per stage (llm_call_log)</SLabel>
                <div style={{ display: "flex", gap: 18, flexWrap: "wrap", fontSize: 12.5 }}>
                  {data.calls_by_stage.map(c => (
                    <span key={c.stage}><span style={{ fontFamily: mono }}>{c.stage}</span>: {c.calls} calls · {c.tokens_in.toLocaleString()} tokens · {usd(c.cost_usd)}</span>
                  ))}
                </div>
                {data.unregistered.length > 0 && (
                  <div style={{ fontSize: 11.5, color: A.muted, marginTop: 8 }}>
                    Verdicts for questions with no config row (ad-hoc scripts): {data.unregistered.map(u => `${u.stage}/${u.question_key} (${u.verdicts})`).join(", ")}
                  </div>
                )}
              </Card>
            )}

            <Card style={{ padding: 0, overflowX: "auto" }}>
              <div style={{ padding: "16px 18px 10px", display: "flex", gap: 10, alignItems: "center", flexWrap: "wrap" }}>
                <SLabel style={{ marginBottom: 0, flex: 1 }}>Recent verdicts</SLabel>
                <select value={fQuestion} onChange={e => setFQuestion(e.target.value)} style={select}>
                  <option value="">All questions</option>
                  {data.questions.map(q => <option key={q.question_key} value={q.question_key}>{q.question_key}</option>)}
                </select>
                <select value={fZone} onChange={e => setFZone(e.target.value)} style={select}>
                  <option value="">All zones</option>
                  {ZONES.map(z => <option key={z} value={z}>{z}</option>)}
                </select>
                <input value={fSubject} onChange={e => setFSubject(e.target.value)} placeholder="Subject contains…" style={{ ...select, width: 180 }} />
              </div>
              <table style={{ width: "100%", borderCollapse: "collapse" }}>
                <thead><tr>
                  {["Time", "Stage", "Question", "Subject", "Tenant", "Mode", "Zone", "p / pick", "Latency", "Cost", "Note"].map(h => <th key={h} style={TH}>{h}</th>)}
                </tr></thead>
                <tbody>
                  {log.map(v => (
                    <tr key={v.id}>
                      <td style={{ ...TD, whiteSpace: "nowrap" }}>{when(v.created_at)}</td>
                      <td style={{ ...TD, fontFamily: mono }}>{v.stage}</td>
                      <td style={{ ...TD, fontFamily: mono }}>{v.question_key}</td>
                      <td style={{ ...TD, maxWidth: 260, overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }} title={v.subject_key}>{v.subject_key}</td>
                      <td style={TD}>{v.tenant_slug ?? "platform"}</td>
                      <td style={TD}><Badge color={MODE_COLOR[v.mode]}>{v.mode}</Badge></td>
                      <td style={TD}><Badge color={ZONE_COLOR[v.zone]}>{v.zone}</Badge></td>
                      <td style={{ ...TD, fontFamily: mono }}>{v.choice ?? (v.probability != null ? v.probability.toFixed(3) : "—")}</td>
                      <td style={{ ...TD, fontFamily: mono }}>{v.latency_ms != null ? `${v.latency_ms} ms` : "—"}</td>
                      <td style={{ ...TD, fontFamily: mono }}>{usd(v.cost_usd ?? 0)}</td>
                      <td style={{ ...TD, fontSize: 11.5, color: A.muted, maxWidth: 220 }}>{v.error ?? v.outcome ?? ""}</td>
                    </tr>
                  ))}
                  {!log.length && <tr><td style={TD} colSpan={11}>No verdicts in this filter.</td></tr>}
                </tbody>
              </table>
            </Card>
          </>
        )}
      </main>
    </div>
  );
}

function FragmentRows({ q, first, editing, onEdit, onCancel, onSaved }: {
  q: Question; first: boolean; editing: boolean; onEdit: () => void; onCancel: () => void; onSaved: () => void;
}) {
  return (
    <>
      <tr style={first ? { borderTop: `2px solid ${A.line}` } : undefined}>
        <td style={TD}>
          <div style={{ fontFamily: mono, fontSize: 11, color: A.muted }}>{q.stage}</div>
          <div style={{ fontFamily: mono, fontWeight: 600 }}>{q.question_key}</div>
          <div style={{ fontSize: 11.5, color: A.muted, maxWidth: 360 }} title={q.instructions}>{q.kind} · {q.instructions}</div>
        </td>
        <td style={TD}><Badge color={MODE_COLOR[q.mode]}>{q.mode}</Badge></td>
        <td style={{ ...TD, fontFamily: mono }}>{floor(q.accept_floor)} / {floor(q.reject_ceiling)}<div style={{ fontSize: 10.5, color: A.muted2 }}>v{q.threshold_version}</div></td>
        <td style={{ ...TD, fontSize: 11.5 }}>{q.calibration_ref ?? <span style={{ color: A.muted2 }}>not calibrated</span>}</td>
        <td style={TD}><ZoneBar q={q} /></td>
        <td style={{ ...TD, fontFamily: mono }}>{q.acted}</td>
        <td style={{ ...TD, fontFamily: mono }}>{q.avg_latency_ms != null ? `${Math.round(q.avg_latency_ms)} ms` : "—"}</td>
        <td style={{ ...TD, fontFamily: mono }}>{usd(q.cost_usd)}</td>
        <td style={{ ...TD, whiteSpace: "nowrap", fontSize: 12 }}>{when(q.last_used_at)}</td>
        <td style={TD}>{!editing && <Btn size="sm" onClick={onEdit}>Edit</Btn>}</td>
      </tr>
      {editing && (
        <tr><td style={TD} colSpan={10}><QuestionEditor q={q} onSaved={onSaved} onCancel={onCancel} /></td></tr>
      )}
    </>
  );
}
