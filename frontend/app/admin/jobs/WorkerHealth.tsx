"use client";
// app/admin/jobs/WorkerHealth.tsx — AA-721 worker panel. Jobs run in a SEPARATE ECS service
// (aa-cis-dev-worker, `python -m shared.jobs.worker`), not inside the API container
// (JOB_WORKER_IN_API=false). The /workers endpoint returns up to 20 rows, most of them dead
// history, so by default we show live workers only, grouped by task_revision (a deploy brings up a
// newer revision; the old one drains, AA-711). A "show stopped (n)" toggle reveals the rest.
// Queue depth + running per worker + reaper totals complete the picture. shared.job_worker (mig 178).

import { useState } from "react";
import { Server } from "lucide-react";
import { A, mono, Card, SLabel, Badge } from "../_components/adminUi";
import { ago, fmtSeconds, fmtTime, type Kind, type Worker, type WorkerHealthResp } from "./jobsShared";

function WorkerRow({ w, now }: { w: Worker; now: number }) {
  const reaped = [...(w.last_reaped?.requeued ?? []), ...(w.last_reaped?.failed ?? [])];
  return (
    <div style={{ fontSize: 12.5, color: A.body, marginBottom: 8 }}>
      <div style={{ display: "flex", gap: 8, alignItems: "center", flexWrap: "wrap" }}>
        <span
          style={{ width: 8, height: 8, borderRadius: 4, background: w.alive ? A.green : A.muted2, display: "inline-block" }}
          aria-label={w.alive ? "alive" : "stopped"}
        />
        <span style={{ fontFamily: mono, color: A.ink3 }}>{w.host}</span>
        {w.alive ? (
          <span style={{ color: A.muted }}>
            seen {ago(w.last_seen_at, now)} · up {fmtSeconds((now - new Date(w.started_at).getTime()) / 1000)}
          </span>
        ) : (
          <span style={{ color: A.muted }}>
            {w.stopped_at ? `stopped ${ago(w.stopped_at, now)}` : `silent since ${fmtTime(w.last_seen_at)}`}
          </span>
        )}
        <Badge color={w.running_jobs >= w.max_parallel ? "amber" : "gray"}>{w.running_jobs}/{w.max_parallel} running</Badge>
      </div>
      <div style={{ color: A.muted, fontSize: 11.5, marginTop: 3, marginLeft: 16 }}>
        Reaper since start: {w.reaped_requeued} re-queued · {w.reaped_failed} failed
        {w.last_reap_at && <> · last {fmtTime(w.last_reap_at)}{reaped.length > 0 && ` (${reaped.map(id => id.slice(0, 8)).join(", ")})`}</>}
      </div>
    </div>
  );
}

export default function WorkerHealth({ health, kinds, now }: { health: WorkerHealthResp | null; kinds: Kind[]; now: number }) {
  const [showStopped, setShowStopped] = useState(false);
  if (!health) return null;

  const alive = health.workers.filter(w => w.alive);
  const stopped = health.workers.filter(w => !w.alive);

  // Group the shown workers by their ECS task-definition revision (AA-711): a deploy brings up a
  // newer revision that takes over while the old one drains, so a transient two-revision view is
  // expected right after a deploy.
  const shown = showStopped ? health.workers : alive;
  const byRevision = new Map<number | null, Worker[]>();
  for (const w of shown) {
    const key = w.task_revision;
    (byRevision.get(key) ?? byRevision.set(key, []).get(key)!).push(w);
  }
  const revisions = Array.from(byRevision.keys()).sort((a, b) => (b ?? -1) - (a ?? -1));

  // Totals for the per-kind queue table (running per kind across all live workers, queue depth).
  const runningByKind: Record<string, number> = {};
  for (const r of health.running) runningByKind[r.kind] = (runningByKind[r.kind] ?? 0) + r.n;
  const queuedByKind = Object.fromEntries(health.queued.map(q => [q.kind, q]));
  const kindNames = Array.from(new Set([...kinds.map(k => k.kind), ...Object.keys(runningByKind), ...Object.keys(queuedByKind)]));
  const orphanRunning = health.running.filter(r => !r.worker_id || !alive.some(w => w.worker_id === r.worker_id));

  return (
    <Card style={{ padding: 16, marginBottom: 16 }}>
      <div style={{ display: "flex", gap: 8, alignItems: "center", marginBottom: 10, flexWrap: "wrap" }}>
        <Server size={14} style={{ color: A.muted }} />
        <span style={{ fontSize: 12, color: A.muted }}>
          Jobs run in a separate worker ECS service (<span style={{ fontFamily: mono, color: A.ink3 }}>aa-cis-dev-worker</span>),
          not inside the API — a deploy or restart re-queues work instead of losing it.
        </span>
      </div>

      <div style={{ display: "flex", gap: 24, flexWrap: "wrap" }}>
        <div style={{ flex: "1 1 340px", minWidth: 0 }}>
          <div style={{ display: "flex", alignItems: "center", gap: 10, marginBottom: 4 }}>
            <SLabel>Workers</SLabel>
            {stopped.length > 0 && (
              <button
                onClick={() => setShowStopped(v => !v)}
                style={{ marginLeft: "auto", fontSize: 11.5, padding: "3px 9px", border: `1px solid ${A.line}`, borderRadius: 6, background: A.card, color: A.muted, cursor: "pointer" }}
              >
                {showStopped ? "hide stopped" : `show stopped (${stopped.length})`}
              </button>
            )}
          </div>

          {alive.length === 0 && !showStopped ? (
            <div style={{ fontSize: 12.5, color: A.red }}>
              No live worker — nothing will be claimed. (A worker writes every ~15 s; silent for {health.alive_seconds}s counts as gone.)
            </div>
          ) : (
            revisions.map(rev => {
              const list = byRevision.get(rev) ?? [];
              return (
                <div key={String(rev)} style={{ marginBottom: 10 }}>
                  <div style={{ fontSize: 11, color: A.muted2, marginBottom: 4 }}>
                    task-def {rev == null ? "unknown" : <span style={{ fontFamily: mono, color: A.ink3 }}>:{rev}</span>}
                    {" · "}{list.length} worker{list.length === 1 ? "" : "s"}
                  </div>
                  {list.map(w => <WorkerRow key={w.worker_id} w={w} now={now} />)}
                </div>
              );
            })
          )}

          {orphanRunning.length > 0 && (
            <div style={{ fontSize: 11.5, color: A.amber, marginTop: 6 }}>
              {orphanRunning.reduce((a, r) => a + r.n, 0)} running job(s) held by a worker that is not live — the reaper re-queues them when the lease (90 s) expires.
            </div>
          )}
        </div>

        <div style={{ flex: "1 1 340px", minWidth: 0 }}>
          <SLabel>Queue by kind</SLabel>
          <table style={{ borderCollapse: "collapse", fontSize: 12, width: "100%" }}>
            <thead>
              <tr style={{ color: A.muted2, textAlign: "left" }}>
                <th style={{ fontWeight: 500, padding: "2px 8px 4px 0" }}>Kind</th>
                <th style={{ fontWeight: 500, padding: "2px 8px 4px 0" }}>Running / cap</th>
                <th style={{ fontWeight: 500, padding: "2px 8px 4px 0" }}>Queued (ready)</th>
                <th style={{ fontWeight: 500, padding: "2px 0 4px 0" }}>Oldest queued</th>
              </tr>
            </thead>
            <tbody>
              {kindNames.map(k => {
                const cap = kinds.find(x => x.kind === k)?.concurrency;
                const q = queuedByKind[k];
                const run = runningByKind[k] ?? 0;
                return (
                  <tr key={k}>
                    <td style={{ fontFamily: mono, color: A.ink3, padding: "3px 8px 3px 0" }}>{k}</td>
                    <td style={{ fontFamily: mono, padding: "3px 8px 3px 0", color: cap != null && run >= cap ? A.amber : A.body }}>{run}/{cap ?? "?"}</td>
                    <td style={{ fontFamily: mono, padding: "3px 8px 3px 0" }}>{q ? `${q.n} (${q.ready})` : "0"}</td>
                    <td style={{ padding: "3px 0", color: A.muted }}>{q?.oldest_created_at ? ago(q.oldest_created_at, now) : "—"}</td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
      </div>
    </Card>
  );
}
