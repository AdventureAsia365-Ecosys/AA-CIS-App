"use client";
// app/admin/jobs/JobDrawer.tsx — AA-687 job detail: lifecycle (attempts, releases, lease), cost split
// (LLM from llm_call_log vs what the handler reported), the job's LLM calls, and what it wrote.

import { useCallback, useEffect, useState } from "react";
import { X } from "lucide-react";
import { A, serif, mono, SLabel, Badge, Btn, TH, TD } from "../_components/adminUi";
import { fmtTime, fmtSeconds, releases, runSeconds, stepProgress, usd, STATUS_COLOR, type Job } from "./jobsShared";

interface LlmCall {
  created_at: string; stage: string; role: string; model: string; account: string | null;
  provider: string | null; fallback_used: boolean | null; tokens_in: number | null;
  tokens_out: number | null; cost_usd: number | null; texts: number | null;
}
interface LlmCalls {
  calls: LlmCall[];
  totals: { calls: number; cost_usd: number; tokens_in: number; tokens_out: number; first_at: string | null; last_at: string | null };
  by_stage: { stage: string; model: string; calls: number; cost_usd: number }[];
  truncated: boolean;
}

function Row({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <div style={{ display: "flex", gap: 12, fontSize: 12.5, padding: "4px 0", borderBottom: `1px solid ${A.line2}` }}>
      <div style={{ width: 130, flexShrink: 0, color: A.muted }}>{label}</div>
      <div style={{ minWidth: 0, color: A.body, wordBreak: "break-word" }}>{children}</div>
    </div>
  );
}

function Json({ label, value }: { label: string; value: unknown }) {
  if (value == null || (typeof value === "object" && Object.keys(value as object).length === 0)) return null;
  return (
    <div style={{ marginTop: 12 }}>
      <SLabel>{label}</SLabel>
      <pre style={{ margin: 0, fontFamily: mono, fontSize: 11, color: A.ink3, background: A.bg, border: `1px solid ${A.line}`, borderRadius: 6, padding: 10, maxHeight: 220, overflow: "auto", whiteSpace: "pre-wrap", wordBreak: "break-word" }}>
        {JSON.stringify(value, null, 2)}
      </pre>
    </div>
  );
}

export default function JobDrawer({ jobId, tick, expectedSeconds, maxReleases, eta, onClose, onAct, busy }: {
  jobId: string;
  tick: number;                       // parent refresh counter — re-fetch on every page refresh
  expectedSeconds?: number;
  maxReleases: number;
  eta: string | null;
  onClose: () => void;
  onAct: (job: Job, action: "cancel" | "retry") => void;
  busy: boolean;
}) {
  const [job, setJob] = useState<Job | null>(null);
  const [llm, setLlm] = useState<LlmCalls | null>(null);
  const [error, setError] = useState<string | null>(null);

  const load = useCallback(async () => {
    try {
      const [j, l] = await Promise.all([
        fetch(`/api/admin/job-runner/jobs/${jobId}`).then(r => r.ok ? r.json() : Promise.reject(r.status)),
        fetch(`/api/admin/job-runner/jobs/${jobId}/llm-calls?limit=200`).then(r => r.ok ? r.json() : Promise.reject(r.status)),
      ]);
      setJob(j);
      setLlm(l);
      setError(null);
    } catch (e) {
      setError(`Could not load job (${e})`);
    }
  }, [jobId]);

  useEffect(() => {
    // eslint-disable-next-line react-hooks/set-state-in-effect -- fetch on open and on each page refresh tick
    load();
  }, [load, tick]);

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => { if (e.key === "Escape") onClose(); };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [onClose]);

  const secs = job ? runSeconds(job) : null;
  const rel = job ? releases(job) : 0;
  const prog = job ? stepProgress(job) : null;
  const llmCost = llm?.totals.cost_usd ?? job?.llm_cost_usd ?? 0;
  const total = job?.cost_usd ?? 0;
  const links = job?.links;

  return (
    <>
      <div onClick={onClose} style={{ position: "fixed", inset: 0, background: "rgba(15,23,42,0.25)", zIndex: 40 }} />
      <aside role="dialog" aria-label="Job detail"
             style={{ position: "fixed", top: 0, right: 0, bottom: 0, width: "min(760px, 100vw)", background: A.card, borderLeft: `1px solid ${A.line}`, zIndex: 41, overflowY: "auto", padding: "20px 22px" }}>
        <div style={{ display: "flex", alignItems: "center", gap: 10, marginBottom: 14 }}>
          <h2 style={{ fontFamily: serif, fontSize: 18, fontWeight: 500, color: A.ink, margin: 0 }}>{job?.kind ?? "Job"}</h2>
          {job && <Badge color={STATUS_COLOR[job.status] ?? "gray"}>{job.status}</Badge>}
          <div style={{ marginLeft: "auto", display: "flex", gap: 8 }}>
            {job && (job.status === "queued" || (job.status === "running" && !job.cancel_requested)) && (
              <Btn size="sm" variant="danger" disabled={busy} onClick={() => onAct(job, "cancel")}>Cancel</Btn>
            )}
            {job && (job.status === "failed" || job.status === "stopped_budget" || job.status === "cancelled") && (
              <Btn size="sm" disabled={busy} onClick={() => onAct(job, "retry")}>Retry</Btn>
            )}
            <button onClick={onClose} aria-label="Close" style={{ border: "none", background: "transparent", cursor: "pointer", color: A.muted }}><X size={18} /></button>
          </div>
        </div>

        {error && <div style={{ fontSize: 12.5, color: A.red, marginBottom: 10 }}>{error}</div>}
        {!job ? <div style={{ fontSize: 12.5, color: A.muted }}>Loading…</div> : (
          <>
            {job.error && job.status !== "succeeded" && (
              <div style={{ fontSize: 12.5, color: A.red, background: A.redSoft, border: `1px solid ${A.redBorder}`, borderRadius: 8, padding: "8px 10px", marginBottom: 12, whiteSpace: "pre-wrap" }}>{job.error}</div>
            )}

            {prog && (
              <div style={{ marginBottom: 14 }}>
                <SLabel>Progress</SLabel>
                <div style={{ fontSize: 12.5, color: A.body, marginBottom: 4 }}>
                  {prog.step || "working"} · <span style={{ fontFamily: mono }}>{prog.done}/{prog.total}</span>
                  {job.status === "running" && eta && <span style={{ color: A.muted }}> · ETA {eta}</span>}
                </div>
                <div style={{ height: 6, background: A.line2, borderRadius: 3, overflow: "hidden" }}>
                  <div style={{ width: `${Math.min(100, (prog.done / prog.total) * 100)}%`, height: "100%", background: A.accent }} />
                </div>
              </div>
            )}

            <SLabel>Lifecycle</SLabel>
            <Row label="Job id"><span style={{ fontFamily: mono }}>{job.id}</span></Row>
            <Row label="Attempt">{job.attempt}/{job.max_attempts}</Row>
            <Row label="Released on shutdown">
              <span style={{ color: rel >= maxReleases - 1 && rel > 0 ? A.red : A.body }}>{rel}/{maxReleases}</span>
              {rel > 0 && <span style={{ color: A.muted }}> — a deploy/restart handed it back to the queue; at {maxReleases} it fails</span>}
            </Row>
            <Row label="Created">{fmtTime(job.created_at)}{job.created_by ? ` by ${job.created_by}` : ""}</Row>
            <Row label="Started / finished">{fmtTime(job.started_at)} → {fmtTime(job.finished_at)}</Row>
            <Row label="Duration">
              {secs == null ? "—" : fmtSeconds(secs)}
              {expectedSeconds && secs != null && secs > expectedSeconds && job.status === "running" && (
                <span style={{ color: A.amber }}> — longer than expected for this kind ({fmtSeconds(expectedSeconds)})</span>
              )}
            </Row>
            {job.status === "running" && <Row label="Lease">{job.locked_by ?? "—"} · until {fmtTime(job.locked_until)}</Row>}
            {job.status === "queued" && job.run_after && <Row label="Runs after">{fmtTime(job.run_after)}</Row>}

            <div style={{ marginTop: 16 }}>
              <SLabel>Cost</SLabel>
              <div style={{ display: "flex", gap: 20, fontSize: 12.5, flexWrap: "wrap" }}>
                <div>Total <span style={{ fontFamily: mono, color: A.ink }}>{usd(total)}</span></div>
                <div>LLM <span style={{ fontFamily: mono, color: A.ink }}>{usd(llmCost)}</span> <span style={{ color: A.muted }}>({llm?.totals.calls ?? 0} calls)</span></div>
                <div>Other (DataForSEO…) <span style={{ fontFamily: mono, color: A.ink }}>{usd(Math.max(0, total - llmCost))}</span></div>
              </div>
            </div>

            {llm && llm.totals.calls > 0 && (
              <div style={{ marginTop: 16 }}>
                <SLabel>LLM calls by stage</SLabel>
                <table style={{ width: "100%", borderCollapse: "collapse", fontSize: 12 }}>
                  <thead><tr><th style={TH}>Stage</th><th style={TH}>Model</th><th style={{ ...TH, textAlign: "right" }}>Calls</th><th style={{ ...TH, textAlign: "right" }}>Cost</th></tr></thead>
                  <tbody>
                    {llm.by_stage.map(s => (
                      <tr key={`${s.stage}:${s.model}`}>
                        <td style={{ ...TD, fontFamily: mono }}>{s.stage}</td>
                        <td style={{ ...TD, fontFamily: mono }}>{s.model}</td>
                        <td style={{ ...TD, fontFamily: mono, textAlign: "right" }}>{s.calls}</td>
                        <td style={{ ...TD, fontFamily: mono, textAlign: "right" }}>{usd(s.cost_usd, 5)}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>

                <div style={{ marginTop: 12 }}>
                  <SLabel>Recent calls{llm.truncated ? ` (latest ${llm.calls.length} of ${llm.totals.calls})` : ""}</SLabel>
                  <div style={{ maxHeight: 260, overflowY: "auto", border: `1px solid ${A.line2}`, borderRadius: 6 }}>
                    <table style={{ width: "100%", borderCollapse: "collapse", fontSize: 11.5 }}>
                      <thead><tr><th style={TH}>Time</th><th style={TH}>Stage</th><th style={TH}>Model / account</th><th style={{ ...TH, textAlign: "right" }}>Tokens in/out</th><th style={{ ...TH, textAlign: "right" }}>Cost</th></tr></thead>
                      <tbody>
                        {llm.calls.map((c, i) => (
                          <tr key={i}>
                            <td style={{ ...TD, whiteSpace: "nowrap" }}>{fmtTime(c.created_at)}</td>
                            <td style={{ ...TD, fontFamily: mono }}>{c.stage}{c.texts && c.texts > 1 ? ` ×${c.texts}` : ""}</td>
                            <td style={{ ...TD, fontFamily: mono }}>{c.model}{c.account ? ` · ${c.account}` : ""}{c.fallback_used ? " · fallback" : ""}</td>
                            <td style={{ ...TD, fontFamily: mono, textAlign: "right" }}>{c.tokens_in ?? 0}/{c.tokens_out ?? 0}</td>
                            <td style={{ ...TD, fontFamily: mono, textAlign: "right" }}>{usd(c.cost_usd, 6)}</td>
                          </tr>
                        ))}
                      </tbody>
                    </table>
                  </div>
                </div>
              </div>
            )}

            {links && (links.tour_versions.length > 0 || links.content_pieces.length > 0 || links.tour_id) && (
              <div style={{ marginTop: 16 }}>
                <SLabel>What this job wrote</SLabel>
                {links.tour_id && (
                  <Row label="Tour">
                    <a href={`/admin/review?tour_id=${links.tour_id}`} style={{ color: A.accent, fontFamily: mono }}>{links.tour_id}</a>
                    {links.version_id && <span style={{ color: A.muted }}> · version <span style={{ fontFamily: mono }}>{links.version_id.slice(0, 8)}</span></span>}
                  </Row>
                )}
                {links.tour_versions.map(v => (
                  <Row key={v.version_id} label="Tenant version">
                    <span style={{ fontFamily: mono }}>v{v.version_number} · {v.version_id.slice(0, 8)}</span> · {v.status}
                    <span style={{ color: A.muted }}> · tenant <a href="/admin/tenants" style={{ color: A.accent, fontFamily: mono }}>{v.tenant_id.slice(0, 8)}</a></span>
                  </Row>
                ))}
                {links.content_pieces.map(p => (
                  <Row key={p.piece_id} label="Content piece">
                    <span style={{ fontFamily: mono }}>{p.piece_id.slice(0, 8)}</span> · {p.status}
                    <span style={{ color: A.muted }}> · tenant <a href="/admin/tenants" style={{ color: A.accent, fontFamily: mono }}>{p.tenant_id.slice(0, 8)}</a></span>
                  </Row>
                ))}
              </div>
            )}

            <Json label="Payload" value={job.payload} />
            <Json label="Progress" value={job.progress} />
            <Json label="Result" value={job.result} />
          </>
        )}
      </aside>
    </>
  );
}
