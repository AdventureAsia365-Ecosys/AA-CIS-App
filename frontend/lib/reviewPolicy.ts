// frontend/lib/reviewPolicy.ts
// The Review Queue's policy filter values (GET /admin/review-queue?policy=, api/routers/admin_pipeline/review.py):
// "all" (no filter), "elephant_riding" (only the elephant-riding policy reviews, AA-1049) and "none" (without them,
// AA-1190: My work's "Review Queue: ready to review" tile). Dependency-free (node --test runs lib/myWork.test.mjs on it).

export const POLICY_FILTER = "elephant_riding";
export const NO_POLICY_FILTER = "none";
const POLICY_VALUES: readonly string[] = ["all", POLICY_FILTER, NO_POLICY_FILTER];

/** The policy filter a link asks for (`?policy=`, the My work tiles); "all" for anything else. */
export function policyFromSearch(search: string): string {
  const v = new URLSearchParams(search).get("policy") ?? "all";
  return POLICY_VALUES.includes(v) ? v : "all";
}
