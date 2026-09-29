"use client";
// app/admin/jobs/page.tsx — AA-650 durable job queue (shared.job): what is queued/running, what
// finished, what it cost, and cancel/retry. API: /admin/job-runner/* (not /admin/jobs, which is the
// older AA-223 run-tour poll endpoint). AA-687: worker health panel, progress + ETA, long-running /
// near-release flags, and a detail drawer (lifecycle, cost split, LLM calls, what the job wrote).

import { useCallback, useEffect, useRef, useState } from "react";
import { ListChecks, RefreshCw } from "lucide-react";
import AdminSidebar from "../_components/AdminSidebar";
import { A, serif, sans, mono, Card, SLabel, Badge, Btn, LoadingScreen, TH, TD } from "../_components/adminUi";
import JobDrawer from "./JobDrawer";
import WorkerHealth from "./WorkerHealth";
import {
  STATUSES, STATUS_COLOR, fmtSeconds, fmtTime, releases, runSeconds, stepProgress, usd,
  type Count, type Job, type Kind, type WorkerHealthResp,
} from "./jobsShared";

const REFRESH_MS = 10_000;

/** First sample seen per (job, step): the ETA is (total - done) / observed rate since then. */
type Sample = { step: string; done: number; t: number };

function etaFor(job: Job, first: Sample | undefined, now: number): string | null {
  const p = stepProgress(job);
  if (!p || !first || first.step !== p.step || p.done <= first.done) return null;
  const rate = (p.done - first.done) / ((now - first.t) / 1000);
  return rate > 0 ? fmtSeconds((p.total - p.done) / rate) : null;
}

function progressCell(job: Job, eta: string | null) {
  const p = stepProgress(job);
  const phase = typeof job.progress?.phase === "string" ? job.progress.phase : "";
  if (!p) return <span>{phase}</span>;
  const pct = Math.min(100, (p.done / p.total) * 100);
  return (
    <div style={{ minWidth: 160 }}>
      <div style={{ fontSize: 11.5 }}>
        {p.step || phase} · <span style={{ fontFamily: mono }}>{p.done}/{p.total}</span>
        {job.status === "running" && eta && <span> · ETA {eta}</span>}
      </div>
      <div style={{ height: 4, background: A.line2, borderRadius: 2, overflow: "hidden", marginTop: 3 }}>
        <div style={{ width: `${pct}%`, height: "100%", background: A.accent }} />
      </div>
    </div>
  );
}

export default function JobsPage() {
  const [jobs, setJobs] = useState<Job[] | null>(null);
  const [counts, setCounts] = useState<Count[]>([]);
  const [kinds, setKinds] = useState<Kind[]>([]);
  const [maxReleases, setMaxReleases] = useState(3);
  const [health, setHealth] = useState<WorkerHealthResp | null>(null);
  const [kind, setKind] = useState("");
  const [status, setStatus] = useState("");
  const [tourId, setTourId] = useState("");
  const [open, setOpen] = useState<string | null>(null);
  const [ready, setReady] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState<string | null>(null);
  const [tick, setTick] = useState(0);
  const [now, setNow] = useState(() => Date.now());
  const samples = useRef<Map<string, Sample>>(new Map());
  const [etas, setEtas] = useState<Record<string, string | null>>({});

  // AA-687 deep links from domain pages: ?job=<id> opens its drawer; ?kind= / ?status= /
  // ?tour_id= pre-filter the list. Read once on mount (no useSearchParams, same as review/page.tsx).
  useEffect(() => {
    const q = new URLSearchParams(window.location.search);
    /* eslint-disable react-hooks/set-state-in-effect -- one-time read of the deep-link params */
    setKind(q.get("kind") ?? "");
    setStatus(q.get("status") ?? "");
    setTourId(q.get("tour_id") ?? "");
    setOpen(q.get("job"));
    setReady(true);
    /* eslint-enable react-hooks/set-state-in-effect */
  }, []);

  const load = useCallback(async () => {
    const qs = new URLSearchParams({ limit: "100" });
    if (kind) qs.set("kind", kind);
    if (status) qs.set("status", status);
    if (tourId) qs.set("tour_id", tourId);
    try {
      const [j, s, w] = await Promise.all([
        fetch(`/api/admin/job-runner/jobs?${qs}`).then(r => r.ok ? r.json() : Promise.reject(r.status)),
        fetch(`/api/admin/job-runner/summary`).then(r => r.ok ? r.json() : Promise.reject(r.status)),
        // The health panel is optional: an older API (before AA-687) has no /workers.
        fetch(`/api/admin/job-runner/workers`).then(r => r.ok ? r.json() : null).catch(() => null),
      ]);
      const t = Date.now();
      const nextEtas: Record<string, string | null> = {};
      for (const job of (j.jobs ?? []) as Job[]) {
        const p = stepProgress(job);
        const prev = samples.current.get(job.id);
        if (p && (!prev || prev.step !== p.step || p.done < prev.done)) {
          samples.current.set(job.id, { step: p.step, done: p.done, t });
        }
        nextEtas[job.id] = etaFor(job, samples.current.get(job.id), t);
      }
      setEtas(nextEtas);
      setJobs(j.jobs ?? []);
      setCounts(s.counts ?? []);
      setKinds(s.kinds ?? []);
      if (typeof s.max_releases === "number") setMaxReleases(s.max_releases);
      setHealth(w);
      setNow(t);
      setTick(x => x + 1);
      setError(null);
    } catch (e) {
      setError(`Could not load jobs (${e})`);
      setJobs(prev => prev ?? []);
    }
  }, [kind, status, tourId]);

  useEffect(() => {
    if (!ready) return;
    // eslint-disable-next-line react-hooks/set-state-in-effect -- initial + filter-change fetch, same pattern as every admin page
    load();
    const t = setInterval(load, REFRESH_MS);
    return () => clearInterval(t);
  }, [load, ready]);

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

  const expected = (k: string) => kinds.find(x => x.kind === k)?.expected_seconds;
  const byStatus = STATUSES.map(s => ({
    status: s,
    n: counts.filter(c => c.status === s && (!kind || c.kind === kind)).reduce((a, c) => a + c.n, 0),
  }));
  const cost30d = counts.filter(c => !kind || c.kind === kind).reduce((a, c) => a + c.cost_usd, 0);
  const openJob = jobs?.find(j => j.id === open);

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
            {tourId && (
              <button onClick={() => setTourId("")} title="Clear the tour filter"
                      style={{ fontSize: 12, padding: "6px 8px", border: `1px solid ${A.accent}`, borderRadius: 6, background: A.card, color: A.ink3, cursor: "pointer", fontFamily: mono }}>
                tour {tourId.slice(0, 8)} ×
              </button>
            )}
            <Btn size="sm" onClick={load}><RefreshCw size={12} style={{ marginRight: 4 }} />Refresh</Btn>
          </div>
        </div>

        {error && (
          <div style={{ marginBottom: 14, padding: "10px 12px", borderRadius: 8, background: A.redSoft, color: A.red, border: `1px solid ${A.redBorder}`, fontSize: 12.5 }}>
            {error}
          </div>
        )}

        <WorkerHealth health={health} kinds={kinds} now={now} />

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
                  {jobs.map(j => {
                    const secs = runSeconds(j, now);
                    const exp = expected(j.kind);
                    const slow = j.status === "running" && exp != null && secs != null && secs > exp;
                    const rel = releases(j);
                    const eta = etas[j.id] ?? null;
                    return (
                      <tr key={j.id} style={{ cursor: "pointer", background: open === j.id ? A.accentTint : undefined }} onClick={() => setOpen(j.id)}>
                        <td style={{ ...TD, whiteSpace: "nowrap" }}>{fmtTime(j.created_at)}</td>
                        <td style={{ ...TD, fontFamily: mono }}>{j.kind}</td>
                        <td style={{ ...TD, whiteSpace: "nowrap" }}>
                          <Badge color={STATUS_COLOR[j.status] ?? "gray"}>{j.status}</Badge>
                          {j.cancel_requested && j.status === "running" && <span style={{ marginLeft: 6, fontSize: 11, color: A.muted }}>cancelling…</span>}
                          {slow && <span title={`Expected under ${fmtSeconds(exp!)}`} style={{ marginLeft: 6 }}><Badge color="amber">slow</Badge></span>}
                          {rel > 0 && (j.status === "running" || j.status === "queued") && (
                            <span title="Handed back to the queue by a deploy/restart" style={{ marginLeft: 6 }}>
                              <Badge color={rel >= maxReleases - 1 ? "red" : "gray"}>released {rel}/{maxReleases}</Badge>
                            </span>
                          )}
                        </td>
                        <td style={{ ...TD, fontFamily: mono }}>{j.attempt}/{j.max_attempts}</td>
                        <td style={{ ...TD, color: A.muted, maxWidth: 280, overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>
                          {j.error && j.status !== "succeeded" ? <span style={{ color: A.red }}>{j.error}</span> : progressCell(j, eta)}
                        </td>
                        <td style={{ ...TD, fontFamily: mono, color: slow ? A.amber : undefined }}>{secs == null ? "—" : fmtSeconds(secs)}</td>
                        <td style={{ ...TD, fontFamily: mono, textAlign: "right" }}>{usd(j.cost_usd)}</td>
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
                    );
                  })}
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
                <span key={k.kind}>
                  <span style={{ fontFamily: mono, color: A.ink3 }}>{k.kind}</span> · max {k.concurrency} at a time · {k.max_attempts} attempts
                  {k.expected_seconds ? ` · slow after ${fmtSeconds(k.expected_seconds)}` : ""}
                </span>
              ))}
            </div>
          </div>
        )}

        {open && (
          <JobDrawer
            jobId={open}
            tick={tick}
            expectedSeconds={openJob ? expected(openJob.kind) : undefined}
            maxReleases={maxReleases}
            eta={open ? etas[open] ?? null : null}
            onClose={() => setOpen(null)}
            onAct={act}
            busy={busy === open}
          />
        )}
      </main>
    </div>
  );
}
