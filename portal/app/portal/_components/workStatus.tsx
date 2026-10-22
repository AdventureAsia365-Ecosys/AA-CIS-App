// portal/app/portal/_components/workStatus.tsx
// AA-760 — ONE status model for tenant-visible work (a tour rewrite, a written piece), shared by
// Browse Tours, My Tours, My Content, the Writer panel and the live view:
//
//   queued   — the job is waiting for a worker            (tour: job_status "queued"; live: no step yet)
//   writing  — the job is running                           (tour: pending + job not ended; piece: in_progress)
//   checking — the job runs its quality checks (live view only: the "check" / "revise" step is active)
//   done     — finished with content to use                 (tour: any finished version; piece: ready)
//   failed   — the job ended without content → Retry        (tour: failed / job failed|cancelled|stopped_budget;
//                                                            piece: not_ready)
//
// The status always comes from what the API reads off the job (`shared.job`): a tour version's
// `job_status` + `status` (v1 my-versions / versions/{id}), a piece's `ready_state` (the API maps
// the piece's job outcome), the live snapshot's steps. It is never inferred from row flags alone
// (the AA-760 "Writing…" bug: Browse Tours read `already_rewritten` and showed "Writing…" forever
// for a failed or truncated-list tour).

import { SkeletonBar, SkeletonStyle, StatusBadge } from "@/app/_kit";
import { isAiWriting, isRewriteFailed, type Version } from "./catalogApi";

export type WorkStatus = "queued" | "writing" | "checking" | "done" | "failed";

export const WORK_STATUS_LABEL: Record<WorkStatus, string> = {
  queued: "Queued",
  writing: "Writing",
  checking: "Checking",
  done: "Ready",
  failed: "Writing failed",
};

/** Still moving: poll while any row is in one of these. */
export function isActiveWork(s: WorkStatus): boolean {
  return s === "queued" || s === "writing" || s === "checking";
}

/** A tour version (T2 rewrite) — from its own status + the job status the API joins in. */
export function tourWorkStatus(v: Pick<Version, "status" | "edit_source" | "job_status">): WorkStatus {
  if (isRewriteFailed(v)) return "failed";
  if (isAiWriting(v)) return v.job_status === "queued" ? "queued" : "writing";
  return "done";
}

/** A written piece (T9) — from the tenant-safe `ready_state` the API derives from the job. */
export function pieceWorkStatus(p: { ready_state: "in_progress" | "ready" | "not_ready" | string }): WorkStatus {
  if (p.ready_state === "in_progress") return "writing";
  if (p.ready_state === "not_ready") return "failed";
  return "done";
}

/** The live view's snapshot (GET /v1/progress/{kind}/{id}): the active step names the stage. */
export function liveWorkStatus(snap: {
  found: boolean; status?: string; steps?: { key: string; state: string }[];
} | null): WorkStatus {
  if (!snap || !snap.found) return "queued";
  if (snap.status === "done") return "done";
  if (snap.status === "failed") return "failed";
  const active = (snap.steps ?? []).find((s) => s.state === "active")?.key;
  return active === "check" || active === "revise" ? "checking" : "writing";
}

/** Kit StatusBadge vocabulary for each state (running spins). */
const BADGE_STATUS: Record<WorkStatus, string> = {
  queued: "queued",
  writing: "running",
  checking: "running",
  done: "approved",
  failed: "failed",
};

export function WorkStatusBadge({ status, label }: { status: WorkStatus; label?: string }) {
  return <StatusBadge status={BADGE_STATUS[status]} label={label ?? WORK_STATUS_LABEL[status]} />;
}

/** The latest version per published tour (one row per tour, highest version_number). */
export function latestVersionByTour(list: Version[]): Map<string, Version> {
  const byTour = new Map<string, Version>();
  for (const v of list) {
    if (!v.published_tour_id) continue;
    const cur = byTour.get(v.published_tour_id);
    if (!cur || v.version_number > cur.version_number) byTour.set(v.published_tour_id, v);
  }
  return byTour;
}

/** Poll interval for a list of work rows: 5 s while anything is active, off once all settled. */
export function activePollInterval(statuses: WorkStatus[], ms = 5000): number | false {
  return statuses.some(isActiveWork) ? ms : false;
}

// ── Activity feed (dashboard, Activity page, bell) ───────────────────────────
// `/v1/billing` activity rows carry only the version's own status (no job). The shell joins each
// row to the same version in the ["my-versions"] query (newest first, so the 5 newest activity rows
// are always on its first page) and reads the job state from there. Until that query answers, a
// row has no state (null) rather than a guessed "Writing".
export type ActivityState = WorkStatus | "approved" | "rejected" | "edited";

export const ACTIVITY_LABEL: Record<ActivityState, string> = {
  ...WORK_STATUS_LABEL,
  done: "Ready to review",
  approved: "Approved",
  rejected: "Rejected",
  edited: "Edited",
};

export function activityState(
  a: { id: string; status: string; edit_source: string },
  version: Pick<Version, "status" | "edit_source" | "job_status"> | undefined,
): ActivityState | null {
  if (a.status === "approved") return "approved";
  if (a.status === "rejected") return "rejected";
  if (a.status === "failed") return "failed";
  if (!version) return null;
  const s = tourWorkStatus(version);
  if (s !== "done") return s;
  return a.edit_source === "ai_generated" ? "done" : "edited";
}

/** Badge vocabulary per activity state (kit StatusBadge). */
const ACTIVITY_BADGE: Record<ActivityState, string> = {
  queued: "queued", writing: "running", checking: "running", done: "pending",
  failed: "failed", approved: "approved", rejected: "rejected", edited: "dismissed",
};

/** AA-760 — while the state is unknown (versions not loaded yet) a neutral placeholder pill, not
 *  nothing (a row without a badge next to an amber icon read as "still running"). */
export function ActivityStateBadge({ state }: { state: ActivityState | null }) {
  if (!state) {
    return (
      <span aria-label="Loading status" style={{ display: "inline-flex" }}>
        <SkeletonStyle />
        <SkeletonBar w={88} h={20} r={999} />
      </span>
    );
  }
  return <StatusBadge status={ACTIVITY_BADGE[state]} label={ACTIVITY_LABEL[state]} />;
}

/** AA-760 — the state of an activity row whose version is not in the loaded ["my-versions"] list
 *  (deleted or archived since): a finished row, never "unknown" forever. */
export function settledActivityState(a: { status: string; edit_source: string }): ActivityState {
  if (a.status === "approved") return "approved";
  if (a.status === "rejected") return "rejected";
  if (a.status === "failed") return "failed";
  return a.edit_source === "ai_generated" ? "done" : "edited";
}
