"use client";
// app/admin/jobs/page.tsx — AA-650 durable job queue (shared.job): what is queued/running, what
// finished, what it cost, and cancel/retry. API: /admin/job-runner/* (not /admin/jobs, which is the
// older AA-223 run-tour poll endpoint).

import { Fragment, useCallback, useEffect, useState } from "react";
import { ChevronDown, ChevronRight, ListChecks, RefreshCw } from "lucide-react";
import AdminSidebar from "../_components/AdminSidebar";
import { A, serif, sans, mono, Card, SLabel, Badge, Btn, LoadingScreen, TH, TD } from "../_components/adminUi";

interface Job {
  id: string;
  kind: string;
  status: string;
  attempt: number;
  max_attempts: number;
  payload: Record<string, unknown> | null;
  progress: Record<string, unknown> | null;
  result: Record<string, unknown> | null;
  cost_usd: number;
  error: string | null;
  cancel_requested: boolean;
  created_by: string | null;
  run_after: string | null;
  created_at: string;
  started_at: string | null;
  finished_at: string | null;
}
interface Count { kind: string; status: string; n: number; cost_usd: number }
interface Kind { kind: string; concurrency: number; max_attempts: number }

const STATUSES = ["queued", "running", "succeeded", "failed", "stopped_budget", "cancelled"] as const;
const STATUS_COLOR: Record<string, "red" | "green" | "amber" | "gray" | "gold" | "blue" | "purple"> = {
  queued: "gray", running: "blue", succeeded: "green", failed: "red", stopped_budget: "amber",
  cancelled: "purple",
};
const REFRESH_MS = 10_000;

function fmtTime(iso: string | null): string {
  if (!iso) return "—";
  return new Date(iso).toLocaleString(undefined, { month: "short", day: "numeric", hour: "2-digit", minute: "2-digit", second: "2-digit" });
}

function duration(job: Job): string {
  if (!job.started_at) return "—";
  const end = job.finished_at ? new Date(job.finished_at).getTime() : Date.now();
  const s = Math.max(0, Math.round((end - new Date(job.started_at).getTime()) / 1000));
  return s < 60 ? `${s}s` : `${Math.floor(s / 60)}m ${s % 60}s`;
}

function progressText(p: Record<string, unknown> | null): string {
  if (!p) return "";
  const phase = typeof p.phase === "string" ? p.phase : "";
  const extra = Object.entries(p).filter(([k]) => k !== "phase" && k !== "released").length;
  return [phase, p.released ? "released once" : "", extra ? `+${extra} fields` : ""].filter(Boolean).join(" · ");
}

function Json({ label, value }: { label: string; value: unknown }) {
  if (value == null || (typeof value === "object" && Object.keys(value as object).length === 0)) return null;
  return (
    <div style={{ minWidth: 0 }}>
      <div style={{ fontSize: 10.5, color: A.muted2, textTransform: "uppercase", letterSpacing: "0.06em", marginBottom: 4 }}>{label}</div>
      <pre style={{ margin: 0, fontFamily: mono, fontSize: 11, color: A.ink3, background: A.bg, border: `1px solid ${A.line}`, borderRadius: 6, padding: 10, maxHeight: 260, overflow: "auto", whiteSpace: "pre-wrap", wordBreak: "break-word" }}>
        {JSON.stringify(value, null, 2)}
      </pre>
    </div>
  );
}

export default function JobsPage() {
  const [jobs, setJobs] = useState<Job[] | null>(null);
  const [counts, setCounts] = useState<Count[]>([]);
  const [kinds, setKinds] = useState<Kind[]>([]);
  const [kind, setKind] = useState("");
  const [status, setStatus] = useState("");
  const [open, setOpen] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState<string | null>(null);

  const load = useCallback(async () => {
    const qs = new URLSearchParams({ limit: "100" });
    if (kind) qs.set("kind", kind);
    if (status) qs.set("status", status);
    try {
      const [j, s] = await Promise.all([
        fetch(`/api/admin/job-runner/jobs?${qs}`).then(r => r.ok ? r.json() : Promise.reject(r.status)),
        fetch(`/api/admin/job-runner/summary`).then(r => r.ok ? r.json() : Promise.reject(r.status)),
      ]);
      setJobs(j.jobs ?? []);
      setCounts(s.counts ?? []);
      setKinds(s.kinds ?? []);
      setError(null);
    } catch (e) {
      setError(`Could not load jobs (${e})`);
      setJobs(prev => prev ?? []);
    }
  }, [kind, status]);

  useEffect(() => {
    // eslint-disable-next-line react-hooks/set-state-in-effect -- initial + filter-change fetch, same pattern as every admin page
    load();
    const t = setInterval(load, REFRESH_MS);
    return () => clearInterval(t);
  }, [load]);

  async function act(job: Job, action: "cancel" | "retry") {
    const verb = action === "cancel" ? "Cancel" : "Retry";
    if (!window.confirm(`${verb} ${job.kind} job ${job.id.slice(0, 8)}?`)) return;
    setBusy(job.id);
    try {
      const r = await fetch(`/api/admin/job-runner/jobs/${job.id}/${action}`, { method: "POST" });
      if (!r.ok) {
        const body = await r.json().catch(() => ({}));
        setError(`${verb} failed: ${body.detail ?? r.status}`);
      }
      await load();
    } finally {
      setBusy(null);
    }
  }

  const byStatus = STATUSES.map(s => ({
    status: s,
    n: counts.filter(c => c.status === s && (!kind || c.kind === kind)).reduce((a, c) => a + c.n, 0),
  }));
  const cost30d = counts.filter(c => !kind || c.kind === kind).reduce((a, c) => a + c.cost_usd, 0);

  if (jobs === null) return <LoadingScreen msg="Loading jobs..." />;

  return (
    <div style={{ display: "flex", height: "100vh", background: A.bg, fontFamily: sans }}>
      <AdminSidebar />
      <main style={{ flex: 1, padding: "32px 36px", minWidth: 0, minHeight: 0, overflowY: "auto" }}>
        <div style={{ display: "flex", alignItems: "center", gap: 12, marginBottom: 20, flexWrap: "wrap" }}>
          <div style={{ width: 36, height: 36, borderRadius: 9, background: `${A.accent}15`, color: A.accent, display: "grid", placeItems: "center" }}>
            <ListChecks size={18} />
          </div>
          <div>
            <h1 style={{ fontFamily: serif, fontSize: 22, fontWeight: 500, color: A.ink, letterSpacing: "-0.02em", margin: 0 }}>Jobs</h1>
            <div style={{ fontSize: 11.5, color: A.muted2, marginTop: 2 }}>
              Background work that survives deploys — queued, running, finished. Refreshes every 10 s.
            </div>
          </div>
          <div style={{ marginLeft: "auto", display: "flex", gap: 8, alignItems: "center" }}>
            <select value={kind} onChange={e => setKind(e.target.value)} aria-label="Kind"
                    style={{ fontSize: 12, padding: "6px 8px", border: `1px solid ${A.line}`, borderRadius: 6, background: A.card, color: A.ink3 }}>
              <option value="">All kinds</option>
              {kinds.map(k => <option key={k.kind} value={k.kind}>{k.kind}</option>)}
            </select>
            <select value={status} onChange={e => setStatus(e.target.value)} aria-label="Status"
                    style={{ fontSize: 12, padding: "6px 8px", border: `1px solid ${A.line}`, borderRadius: 6, background: A.card, color: A.ink3 }}>
              <option value="">All statuses</option>
              {STATUSES.map(s => <option key={s} value={s}>{s}</option>)}
            </select>
            <Btn size="sm" onClick={load}><RefreshCw size={12} style={{ marginRight: 4 }} />Refresh</Btn>
          </div>
        </div>

        {error && (
          <div style={{ marginBottom: 14, padding: "10px 12px", borderRadius: 8, background: A.redSoft, color: A.red, border: `1px solid ${A.redBorder}`, fontSize: 12.5 }}>
            {error}
          </div>
        )}

        <div style={{ display: "flex", gap: 8, flexWrap: "wrap", marginBottom: 16 }}>
          {byStatus.map(s => (
            <button key={s.status} onClick={() => setStatus(status === s.status ? "" : s.status)}
                    style={{ cursor: "pointer", border: `1px solid ${status === s.status ? A.accent : A.line}`, background: A.card, borderRadius: 8, padding: "8px 12px", display: "flex", gap: 8, alignItems: "center" }}>
              <Badge color={STATUS_COLOR[s.status]}>{s.status}</Badge>
              <span style={{ fontFamily: mono, fontSize: 13, color: A.ink }}>{s.n}</span>
            </button>
          ))}
          <div style={{ marginLeft: "auto", fontSize: 12, color: A.muted, alignSelf: "center" }}>
            Cost, last 30 days: <span style={{ fontFamily: mono, color: A.ink }}>${cost30d.toFixed(4)}</span>
          </div>
        </div>

        <Card style={{ padding: 0, overflow: "hidden" }}>
          {jobs.length === 0 ? (
            <div style={{ padding: 28, textAlign: "center", color: A.muted, fontSize: 13 }}>
              No jobs match these filters yet.
            </div>
          ) : (
            <div style={{ overflowX: "auto" }}>
              <table style={{ width: "100%", borderCollapse: "collapse", fontSize: 12.5 }}>
                <thead>
                  <tr>
                    <th style={TH} />
                    <th style={TH}>Created</th>
                    <th style={TH}>Kind</th>
                    <th style={TH}>Status</th>
                    <th style={TH}>Attempt</th>
                    <th style={TH}>Progress</th>
                    <th style={TH}>Duration</th>
                    <th style={{ ...TH, textAlign: "right" }}>Cost</th>
                    <th style={TH}>By</th>
                    <th style={TH} />
                  </tr>
                </thead>
                <tbody>
                  {jobs.map(j => (
                    <Fragment key={j.id}>
                      <tr style={{ cursor: "pointer" }} onClick={() => setOpen(open === j.id ? null : j.id)}>
                        <td style={{ ...TD, width: 24, color: A.muted }}>{open === j.id ? <ChevronDown size={14} /> : <ChevronRight size={14} />}</td>
                        <td style={{ ...TD, whiteSpace: "nowrap" }}>{fmtTime(j.created_at)}</td>
                        <td style={{ ...TD, fontFamily: mono }}>{j.kind}</td>
                        <td style={TD}>
                          <Badge color={STATUS_COLOR[j.status] ?? "gray"}>{j.status}</Badge>
                          {j.cancel_requested && j.status === "running" && <span style={{ marginLeft: 6, fontSize: 11, color: A.muted }}>cancelling…</span>}
                        </td>
                        <td style={{ ...TD, fontFamily: mono }}>{j.attempt}/{j.max_attempts}</td>
                        <td style={{ ...TD, color: A.muted, maxWidth: 260, overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>
                          {j.error && j.status !== "succeeded" ? <span style={{ color: A.red }}>{j.error}</span> : progressText(j.progress)}
                        </td>
                        <td style={{ ...TD, fontFamily: mono }}>{duration(j)}</td>
                        <td style={{ ...TD, fontFamily: mono, textAlign: "right" }}>${(j.cost_usd ?? 0).toFixed(4)}</td>
                        <td style={{ ...TD, color: A.muted }}>{j.created_by ?? "—"}</td>
                        <td style={{ ...TD, whiteSpace: "nowrap" }} onClick={e => e.stopPropagation()}>
                          {(j.status === "queued" || (j.status === "running" && !j.cancel_requested)) && (
                            <Btn size="sm" variant="danger" disabled={busy === j.id} onClick={() => act(j, "cancel")}>Cancel</Btn>
                          )}
                          {(j.status === "failed" || j.status === "stopped_budget" || j.status === "cancelled") && (
                            <Btn size="sm" disabled={busy === j.id} onClick={() => act(j, "retry")}>Retry</Btn>
                          )}
                        </td>
                      </tr>
                      {open === j.id && (
                        <tr>
                          <td colSpan={10} style={{ ...TD, background: A.card }}>
                            <div style={{ fontSize: 11, color: A.muted, marginBottom: 8, fontFamily: mono }}>
                              {j.id} · started {fmtTime(j.started_at)} · finished {fmtTime(j.finished_at)}
                              {j.status === "queued" && j.run_after ? ` · runs after ${fmtTime(j.run_after)}` : ""}
                            </div>
                            {j.error && <div style={{ fontSize: 12, color: A.red, marginBottom: 8, whiteSpace: "pre-wrap" }}>{j.error}</div>}
                            <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fit, minmax(260px, 1fr))", gap: 12 }}>
                              <Json label="Payload" value={j.payload} />
                              <Json label="Progress" value={j.progress} />
                              <Json label="Result" value={j.result} />
                            </div>
                          </td>
                        </tr>
                      )}
                    </Fragment>
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </Card>

        {kinds.length > 0 && (
          <div style={{ marginTop: 16 }}>
            <SLabel>Registered kinds</SLabel>
            <div style={{ fontSize: 12, color: A.muted, display: "flex", gap: 16, flexWrap: "wrap" }}>
              {kinds.map(k => (
                <span key={k.kind}><span style={{ fontFamily: mono, color: A.ink3 }}>{k.kind}</span> · max {k.concurrency} at a time · {k.max_attempts} attempts</span>
              ))}
            </div>
          </div>
        )}
      </main>
    </div>
  );
}
