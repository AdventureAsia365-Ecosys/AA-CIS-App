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

import { useQuery } from "@tanstack/react-query";
import { apiGet } from "@/app/_kit";
import { fetchMyVersions } from "../_components/catalogApi";
import { countUniqueTours } from "../_components/ui";
import type { ShellBilling } from "../_components/PortalShellContext";

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

export function useCatalogCount(): number {
  const { data } = useQuery({ queryKey: MY_VERSIONS_KEY, queryFn: fetchMyVersions });
  return data ? countUniqueTours(data) : 0;
}

export function useBilling(): ShellBilling | null {
  const { data } = useQuery({
    queryKey: ["tenant-billing"],
    queryFn: () => apiGet<ShellBilling>("/api/tenant/v1/billing"),
  });
  return data ?? null;
}
