"use client";
// app/_kit/useReviewPendingCount.ts
// S241 — the Review Queue pending count shown as a badge by the Sidebar (desktop + phone drawer) and
// the BottomNav (phone). One hook, one query key, so both share a single cached request instead of
// two pollers (the S240 Vercel audit counted 16.0k proxied review-queue calls in 13 days from the
// two 60 s keys). Polls every 180 s, paused while the tab is hidden; undefined on error (the badge is
// simply hidden — never a blocking failure for the shell). Only a role that may open the Review page
// asks (lib/rbac.ts canSeePage): the others (customer_success, support, finance) hold no content.read,
// so the backend answers 403 and the badge would never show anyway (AA-1035 cs-scope evidence, S270).

import { useQuery } from "@tanstack/react-query";
import { apiGet } from "./api";
import { useAdminIdentity } from "./useAdminIdentity";
import { canSeePage } from "@/lib/rbac";

interface ReviewTotal {
  pagination?: { total?: number };
  total?: number;
}

/** Shared react-query key. It sits under the "review-queue" prefix on purpose: the Review Queue
 * page's mutations invalidate `["review-queue"]`, so an approve/reject refreshes the badge at once
 * instead of waiting up to 180 s. */
export const REVIEW_PENDING_KEY = ["review-queue", "shell-pending-count"] as const;
const REVIEW_PENDING_MS = 180_000;

export function useReviewPendingCount(): number | undefined {
  const { role } = useAdminIdentity();
  const { data } = useQuery({
    queryKey: REVIEW_PENDING_KEY,
    enabled: canSeePage(role, "/admin/review"),
    queryFn: () =>
      apiGet<ReviewTotal>("/api/admin/review-queue?status=pending&page=1&page_size=1", { admin: true }),
    staleTime: REVIEW_PENDING_MS,
    refetchInterval: REVIEW_PENDING_MS,
    refetchIntervalInBackground: false,
    retry: false,
  });
  return data?.pagination?.total ?? data?.total;
}
