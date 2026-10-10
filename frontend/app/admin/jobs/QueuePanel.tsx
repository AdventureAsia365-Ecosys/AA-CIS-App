"use client";
// app/admin/jobs/QueuePanel.tsx — AA-755. The "Workers & queue" tab: the worker-liveness panel
// (separate aa-cis-dev-worker ECS service, AA-687) over the registered-kind table. Split out of
// page.tsx so the jobs table and the operational view live in two tabs instead of one long scroll.
// Pure view — the page owns the 10 s poll and hands the panels their data.

import WorkerHealth from "./WorkerHealth";
import JobKinds from "./JobKinds";
import type { Count, Kind, WorkerHealthResp } from "./jobsShared";

export default function QueuePanel({
  health, kinds, counts, now, onPick,
}: {
  health: WorkerHealthResp | null;
  kinds: Kind[];
  counts: Count[];
  now: number;
  onPick: (kind: string, status: string) => void;
}) {
  return (
    <div>
      <WorkerHealth health={health} kinds={kinds} now={now} />
      <JobKinds kinds={kinds} counts={counts} onPick={onPick} />
    </div>
  );
}
