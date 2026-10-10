"use client";
// app/admin/jobs/QueuePanel.tsx — AA-755. The "Queue & workers" tab: the worker-liveness panel
// (separate aa-cis-dev-worker ECS service, AA-687) plus the queue-by-kind view it already renders.
// Split out of page.tsx; the registered-kind table ("Job kinds") is now its own third tab, so this
// panel is just the worker/queue view. Pure view — the page owns the 10 s poll.

import WorkerHealth from "./WorkerHealth";
import type { Kind, WorkerHealthResp } from "./jobsShared";

export default function QueuePanel({
  health, kinds, now,
}: {
  health: WorkerHealthResp | null;
  kinds: Kind[];
  now: number;
}) {
  return <WorkerHealth health={health} kinds={kinds} now={now} />;
}
