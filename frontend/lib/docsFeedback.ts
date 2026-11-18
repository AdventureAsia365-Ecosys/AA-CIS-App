// lib/docsFeedback.ts — AA-1133 follow-up (S284 live check): who sees "Was this page helpful?" and what a refused
// answer says. No React here, so node --test can load it (lib/docsFeedback.test.mjs).
//
// The manifest (GET /admin/docs/manifest) answers `can_feedback` for this caller: staff_authz.denial_reason on POST
// /admin/docs/feedback, so the read-only viewer AND a read-only Dev E2E session (whose role cookie says "admin") both
// get false and the page offers no vote the API would refuse. An older API without the field: the role cookie decides
// (the viewer gets no widget). Any refusal that still happens shows a sentence, never the API's code.

import { isReadOnlyRole } from "./rbac.ts";

/** May this session answer the feedback question? The API's own answer when it gave one, else the role cookie. */
export function feedbackAllowed(manifest: { can_feedback?: boolean } | undefined, role: string | null | undefined): boolean {
  if (typeof manifest?.can_feedback === "boolean") return manifest.can_feedback;
  return !isReadOnlyRole(role);
}

/** Why an answer was not saved, as a message key of app/admin/docs/messages.ts. */
export type FeedbackRefusal =
  | "feedbackErrReadOnly"
  | "feedbackErrNotAllowed"
  | "feedbackErrPageGone"
  | "feedbackErrInvalid"
  | "feedbackErrUnavailable"
  | "feedbackErrFailed";

// The API's read-only refusals: the Dev E2E session (api/core/e2e_staff.py E2E_READ_ONLY) and the viewer role
// (staff_authz READ_ONLY_DENIED "Role '<role>' is read-only").
const READ_ONLY_DETAIL = /^e2e_read_only$|read-only/i;

const BY_STATUS: Record<number, FeedbackRefusal> = {
  404: "feedbackErrPageGone",
  422: "feedbackErrInvalid",
  503: "feedbackErrUnavailable",
};

/** The message key for a failed POST /api/admin/docs/feedback (an ApiError carries `status` and the API detail). */
export function feedbackRefusal(err: unknown): FeedbackRefusal {
  const status = (err as { status?: unknown } | null)?.status;
  if (status === 403) {
    const detail = err instanceof Error ? err.message.trim() : "";
    return READ_ONLY_DETAIL.test(detail) ? "feedbackErrReadOnly" : "feedbackErrNotAllowed";
  }
  return (typeof status === "number" && BY_STATUS[status]) || "feedbackErrFailed";
}
