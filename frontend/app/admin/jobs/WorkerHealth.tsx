"use client";
// app/admin/jobs/WorkerHealth.tsx — AA-687 worker health panel: is the in-API worker alive, what it
// runs against the per-kind caps, queue depth, and what the reaper did (shared.job_worker, mig 178).

import { A, mono, Card, SLabel, Badge } from "../_components/adminUi";
import { ago, fmtSeconds, fmtTime, type Kind, type WorkerHealthResp } from "./jobsShared";

export default function WorkerHealth({ health, kinds, now }: { health: WorkerHealthResp | null; kinds: Kind[]; now: number }) {
  if (!health) return null;
  const alive = health.workers.filter(w => w.alive);
  const recent = health.workers.filter(w => !w.alive).slice(0, 3);

  const runningByKind: Record<string, number> = {};
  for (const r of health.running) runningByKind[r.kind] = (runningByKind[r.kind] ?? 0) + r.n;
  const queuedByKind = Object.fromEntries(health.queued.map(q => [q.kind, q]));
  const kindNames = Array.from(new Set([...kinds.map(k => k.kind), ...Object.keys(runningByKind), ...Object.keys(queuedByKind)]));
  const orphanRunning = health.running.filter(r => !r.worker_id || !alive.some(w => w.worker_id === r.worker_id));

  return (
    <Card style={{ padding: 16, marginBottom: 16 }}>
      <div style={{ display: "flex", gap: 24, flexWrap: "wrap" }}>
        <div style={{ flex: "1 1 340px", minWidth: 0 }}>
          <SLabel>Workers</SLabel>
          {alive.length === 0 ? (
            <div style={{ fontSize: 12.5, color: A.red }}>
              No live worker — nothing will be claimed. (A worker writes every ~15 s; silent for {health.alive_seconds}s counts as gone.)
            </div>
          ) : alive.map(w => (
            <div key={w.worker_id} style={{ fontSize: 12.5, color: A.body, marginBottom: 8 }}>
              <div style={{ display: "flex", gap: 8, alignItems: "center", flexWrap: "wrap" }}>
                <span style={{ width: 8, height: 8, borderRadius: 4, background: A.green, display: "inline-block" }} aria-label="alive" />
                <span style={{ fontFamily: mono, color: A.ink3 }}>{w.host}</span>
                <span style={{ color: A.muted }}>seen {ago(w.last_seen_at, now)} · up {fmtSeconds((now - new Date(w.started_at).getTime()) / 1000)}</span>
                <Badge color={w.running_jobs >= w.max_parallel ? "amber" : "gray"}>{w.running_jobs}/{w.max_parallel} running</Badge>
              </div>
              <div style={{ color: A.muted, fontSize: 11.5, marginTop: 3, marginLeft: 16 }}>
                Reaper since start: {w.reaped_requeued} re-queued · {w.reaped_failed} failed
                {w.last_reap_at && <> · last {fmtTime(w.last_reap_at)} ({[...(w.last_reaped?.requeued ?? []), ...(w.last_reaped?.failed ?? [])].map(id => id.slice(0, 8)).join(", ")})</>}
              </div>
            </div>
          ))}
          {recent.length > 0 && (
            <div style={{ fontSize: 11.5, color: A.muted, marginTop: 4 }}>
              Recently stopped: {recent.map(w => `${w.host} (${w.stopped_at ? `stopped ${ago(w.stopped_at, now)}` : `silent since ${fmtTime(w.last_seen_at)}`})`).join(" · ")}
            </div>
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
