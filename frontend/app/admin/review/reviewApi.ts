// app/admin/review/reviewApi.ts
// AA-662 PR 2 — review-queue data layer, used with react-query.
// All calls go through the BFF proxy (/api/admin/*) with the reviewer-id header. These are plain
// functions; the page wires them into useQuery/useMutation.

import { adminHeaders, apiGet, apiSend } from "../../_kit";

export type ReviewRaw = Record<string, unknown> & {
  id: string | number;
  tour_id: string;
  generated_content_id: string;
};

export type ReviewQueueResponse = {
  data: ReviewRaw[];
  pagination?: { total?: number };
};

export function fetchReviewQueue(status: string): Promise<ReviewQueueResponse> {
  return apiGet<ReviewQueueResponse>(`/api/admin/review-queue?status=${encodeURIComponent(status)}`, {
    admin: true,
  });
}

export function approveReview(id: string): Promise<unknown> {
  return apiSend(`/api/admin/review-queue/${id}/approve`, { method: "POST", admin: true });
}

export function rejectReview(id: string): Promise<unknown> {
  return apiSend(`/api/admin/review-queue/${id}/reject`, { method: "POST", admin: true });
}

export function dismissReview(id: string): Promise<unknown> {
  return apiSend(`/api/admin/review-queue/${id}/dismiss`, { method: "POST", admin: true });
}

/** PATCH edited fields on a generated_content version. */
export function patchGenerated(
  tourId: string,
  generatedContentId: string,
  body: Record<string, unknown>,
): Promise<unknown> {
  return apiSend(`/api/admin/tours/${tourId}/generated/${generatedContentId}`, {
    method: "PATCH",
    body,
    admin: true,
  });
}

export type RevalidateStart = { job_id: string };

/** Start a re-validation job; backend returns 202 + job_id. */
export async function startRevalidate(
  tourId: string,
  generatedContentId: string,
): Promise<RevalidateStart> {
  const res = await fetch(
    `/api/admin/tours/${tourId}/generated/${generatedContentId}/revalidate`,
    { method: "POST", headers: adminHeaders() },
  );
  if (res.status !== 202) {
    const e = await res.json().catch(() => ({}));
    throw new Error(e.detail || `Could not start re-validation (${res.status})`);
  }
  return res.json();
}

/**
 * Enqueue a full-pipeline regenerate for one tour. Backend returns 202 + job_id immediately
 * (async), so the caller does NOT block on completion (AA-719: non-blocking regenerate).
 */
export async function startRegenerate(args: {
  tourId: string;
  modelTier: string;
}): Promise<{ job_id: string }> {
  const res = await fetch(`/api/admin/run-tour-async`, {
    method: "POST",
    headers: adminHeaders(true),
    body: JSON.stringify({
      tour_id: args.tourId,
      batch_id: crypto.randomUUID(),
      tenant_id: "00000000-0000-0000-0000-000000000001",
      model_tier: args.modelTier,
      allow_auto_upgrade: false,
    }),
  });
  if (res.status !== 202) {
    const e = await res.json().catch(() => ({}));
    throw new Error(e.detail || `Could not start regeneration (${res.status})`);
  }
  return res.json();
}

export type JobOutcome = "succeeded" | "failed" | "interrupted" | "timeout";

/** Poll a job to a terminal state. Used by Re-validate (which must read the row verdict after). */
export async function pollJob(jobId: string, ceilingMs = 120_000): Promise<JobOutcome> {
  const deadline = Date.now() + ceilingMs;
  while (Date.now() < deadline) {
    await new Promise((r) => setTimeout(r, 2000));
    try {
      const job = await apiGet<{ status: string }>(`/api/admin/jobs/${jobId}`, { admin: true });
      if (job.status === "succeeded") return "succeeded";
      if (job.status === "failed") return "failed";
      if (job.status === "interrupted") return "interrupted";
    } catch {
      /* transient — keep polling */
    }
  }
  return "timeout";
}

/** Read one row's current revalidate_passed from the queue (verdict lives on the row). */
export async function fetchRevalidateState(reviewId: string): Promise<boolean | null> {
  try {
    const d = await apiGet<ReviewQueueResponse>(`/api/admin/review-queue`, { admin: true });
    const row = (d.data || []).find((r) => String(r.id) === reviewId);
    if (!row) return null;
    return row.revalidate_passed === true ? true : row.revalidate_passed === false ? false : null;
  } catch {
    return null;
  }
}
