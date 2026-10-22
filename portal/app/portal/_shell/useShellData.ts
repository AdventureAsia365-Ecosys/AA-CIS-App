"use client";
// portal/app/portal/_shell/useShellData.ts
// AA-759 — the shell's data, all through react-query (was four raw fetches in a mount effect).
//
//   - tenant identity: GET /api/tenant/me (the tenant cookies are httpOnly, AA-427);
//   - Browse Tours total: GET /v1/tours/pool?page_size=1 → pagination.total;
//   - My Tours count: the SAME query as the My Tours page (key ["my-versions"], fetchMyVersions),
//     counted as unique tours (AA-566 Phần A) — one reading of one endpoint, so the sidebar badge
//     and the page heading can never disagree, and a page refetch refreshes the badge;
//   - billing + activity: GET /v1/billing (tenant-scoped, AA-496).

import { useMemo } from "react";
import { useQuery } from "@tanstack/react-query";
import { apiGet } from "@/app/_kit";
import { fetchMyVersions, type Version } from "../_components/catalogApi";
import { countUniqueTours } from "../_components/ui";
import { activePollInterval, activityState, tourWorkStatus } from "../_components/workStatus";
import type { ShellActivityView, ShellBilling } from "../_components/PortalShellContext";

export const MY_VERSIONS_KEY = ["my-versions"] as const;

interface TenantMe { tenant_name?: string; plan_tier?: string }

export function useTenantMe(): { tenantName: string; planTier: string } {
  const { data } = useQuery({
    queryKey: ["tenant-me"],
    queryFn: () => apiGet<TenantMe>("/api/tenant/me"),
    staleTime: 5 * 60_000,
  });
  return { tenantName: data?.tenant_name || "Partner", planTier: data?.plan_tier || "" };
}

export function usePoolTotal(): number {
  const { data } = useQuery({
    queryKey: ["pool-total"],
    queryFn: () => apiGet<{ pagination?: { total?: number } }>("/api/tenant/v1/tours/pool?page_size=1"),
    staleTime: 5 * 60_000,
  });
  return data?.pagination?.total ?? 0;
}

/** The tenant's versions (shared ["my-versions"] query, polled while a rewrite runs) + the count. */
export function useCatalog(): { versions: Version[] | undefined; count: number } {
  // AA-760 — the badge was stale after a rewrite finished in the background (it only refreshed when
  // the My Tours page refetched). The shell's own observer now polls the shared query while any
  // version is queued / writing, and stops once all are terminal (shared work-status model).
  const { data } = useQuery({
    queryKey: MY_VERSIONS_KEY,
    queryFn: fetchMyVersions,
    refetchInterval: (q) => activePollInterval((q.state.data ?? []).map(tourWorkStatus), 10_000),
  });
  return { versions: data, count: data ? countUniqueTours(data) : 0 };
}

/** AA-760 — the activity feed with each row's state read from its version's job. */
export function useActivityView(billing: ShellBilling | null, versions: Version[] | undefined): ShellActivityView[] {
  return useMemo(() => {
    const byId = new Map((versions ?? []).map((v) => [v.id, v]));
    return (billing?.activity ?? []).map((a) => ({ ...a, state: activityState(a, byId.get(a.id)) }));
  }, [billing, versions]);
}

export function useBilling(): ShellBilling | null {
  const { data } = useQuery({
    queryKey: ["tenant-billing"],
    queryFn: () => apiGet<ShellBilling>("/api/tenant/v1/billing"),
  });
  return data ?? null;
}
