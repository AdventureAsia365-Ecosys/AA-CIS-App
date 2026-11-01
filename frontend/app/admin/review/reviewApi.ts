// app/admin/review/reviewApi.ts
// AA-662 PR 2 — review-queue data layer, used with react-query.
// All calls go through the BFF proxy (/api/admin/*) with the reviewer-id header. These are plain
// functions; the page wires them into useQuery/useMutation.

import { adminHeaders, apiGet, apiSend } from "../../_kit";
import type { ReasonCode } from "@/lib/writingReasons";

export type ReviewRaw = Record<string, unknown> & {
  id: string | number;
  tour_id: string;
  generated_content_id: string;
};

export type ReviewQueueResponse = {
  data: ReviewRaw[];
  pagination?: { page?: number; page_size?: number; total?: number };
  // AA-739: every country present for this status, independent of the current page/filters.
  facets?: { countries?: { country: string; n: number }[] };
};

export type ReviewQueueQuery = {
  status: string;
  page: number; // 1-based
  pageSize: number;
  country?: string; // "all" = no filter
  score?: string; // all | critical | low | ok
  tourId?: string | null;
};

// AA-739: paging and filters run on the server (the page used to filter a 20-row slice).
export function fetchReviewQueue(q: ReviewQueueQuery): Promise<ReviewQueueResponse> {
  const qs = new URLSearchParams({
    status: q.status,
    page: String(q.page),
    page_size: String(q.pageSize),
  });
  if (q.country && q.country !== "all") qs.set("country", q.country);
  if (q.score && q.score !== "all") qs.set("score", q.score);
  if (q.tourId) qs.set("tour_id", q.tourId);
  return apiGet<ReviewQueueResponse>(`/api/admin/review-queue?${qs.toString()}`, { admin: true });
}

export function approveReview(id: string): Promise<unknown> {
  return apiSend(`/api/admin/review-queue/${id}/approve`, { method: "POST", admin: true });
}

// AA-916 WM-07 — Reject / Dismiss carry the reason chip (`reason_code`, a reason-only writing signal). The body is
// optional on the API, so a call without a code keeps the old behaviour.
export function rejectReview(id: string, reasonCode?: ReasonCode): Promise<unknown> {
  return apiSend(`/api/admin/review-queue/${id}/reject`, {
    method: "POST", admin: true, body: reasonCode ? { reason_code: reasonCode } : undefined,
  });
}

export function dismissReview(id: string, reasonCode?: ReasonCode): Promise<unknown> {
  return apiSend(`/api/admin/review-queue/${id}/dismiss`, {
    method: "POST", admin: true, body: reasonCode ? { reason_code: reasonCode } : undefined,
  });
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
  // AA-994: the caller's UI-language message when the API sends no detail.
  failMessage: (status: number) => string = (s) => `Could not start re-validation (${s})`,
): Promise<RevalidateStart> {
  const res = await fetch(
    `/api/admin/tours/${tourId}/generated/${generatedContentId}/revalidate`,
    { method: "POST", headers: adminHeaders() },
  );
  if (res.status !== 202) {
    const e = await res.json().catch(() => ({}));
    throw new Error(e.detail || failMessage(res.status));
  }
  return res.json();
}

/**
 * Enqueue a full-pipeline regenerate for one tour. Backend returns 202 + job_id immediately
 * (async), so the caller does NOT block on completion (AA-719: non-blocking regenerate).
 */
export async function startRegenerate(args: {
  tourId: string;
  // AA-916 WM-07 — the review row and the chip: the API writes one reason-only signal per job it creates
  // (a dedup double click writes none); neither goes into the job payload.
  reviewId?: string;
  reasonCode?: ReasonCode;
  // AA-994: the caller's UI-language message when the API sends no detail.
  failMessage?: (status: number) => string;
}): Promise<{ job_id: string }> {
  const res = await fetch(`/api/admin/run-tour-async`, {
    method: "POST",
    headers: adminHeaders(true),
    body: JSON.stringify({
      tour_id: args.tourId,
      batch_id: crypto.randomUUID(),
      tenant_id: "00000000-0000-0000-0000-000000000001",
      // AA-1024 A2: no model_tier: the run uses the s1_generate route (AI providers › Routing); the API refuses one.
      allow_auto_upgrade: false,
      ...(args.reviewId && args.reasonCode ? { review_id: args.reviewId, reason_code: args.reasonCode } : {}),
    }),
  });
  if (res.status !== 202) {
    const e = await res.json().catch(() => ({}));
    throw new Error(e.detail || (args.failMessage ?? ((s) => `Could not start regeneration (${s})`))(res.status));
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

// AA-845 — per-sentence source check. Each grounded sentence of the row's version, its latest Jev
// a1_claim_supported verdict and the reviewer's label. "Mark unsupported" stamps truth=False on that
// sentence only (a whole-version reject never labels sentences).
export type ClaimVerdict = { zone: string; mode: string; probability: number | null; at: string | null };
export type ClaimLabel = { truth: boolean | null; source: string | null; at: string | null; by: string | null };
export type ReviewClaim = {
  index: number;
  field: string;
  day: number | null;
  sentence: string;
  subject_key: string;
  verdict: ClaimVerdict | null;
  promise_probability: number | null;
  label: ClaimLabel | null;
  marked_unsupported: boolean;
};
export type ReviewClaimsResponse = {
  review_id: string;
  generated_content_id: string;
  review_status: string;
  human_edited: boolean;
  claims: ReviewClaim[];
  with_verdict: number;
  marked_unsupported: number;
};

export function fetchReviewClaims(reviewId: string): Promise<ReviewClaimsResponse> {
  return apiGet<ReviewClaimsResponse>(`/api/admin/review-queue/${reviewId}/claims`, { admin: true });
}

export function labelReviewClaim(
  reviewId: string,
  subjectKey: string,
  unsupported: boolean,
): Promise<{ rows_updated: number }> {
  return apiSend(`/api/admin/review-queue/${reviewId}/claims/label`, {
    method: "POST",
    body: { subject_key: subjectKey, unsupported },
    admin: true,
  });
}
