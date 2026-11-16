"use client";
// app/admin/platform/settings/useCrmAgencies.ts — AA-963 / AA-970 (S282): the CRM agencies the change form of a CRM
// agency key offers (lib/settingsRegistry allAgencies: every page of the CIS relay of crm.ops.v1 GET /tenants, read
// with the staff member's own token, the list the Platform › Tenants › Agencies console reads). One cached read per
// page visit; only a dialog of a CRM agency key asks for it.

import { useQuery } from "@tanstack/react-query";
import { useMemo } from "react";
import { allAgencies, type AgencyRef } from "@/lib/settingsRegistry";
import { getJson } from "./settingsApi";

export const CRM_AGENCIES_KEY = ["settings-crm-agencies"] as const;

export function useCrmAgencies(enabled = true) {
  const q = useQuery({
    queryKey: CRM_AGENCIES_KEY,
    queryFn: () => allAgencies((path) => getJson<{ items: AgencyRef[]; next_cursor: string | null }>(path)),
    staleTime: 5 * 60_000,
    enabled,
  });
  const agencies = useMemo(() => q.data ?? [], [q.data]);
  const byId = useMemo(() => new Map(agencies.map((a) => [a.tenant_id, a])), [agencies]);
  return {
    agencies, byId, isLoading: q.isLoading, error: q.error ? (q.error as Error).message : null, refetch: q.refetch,
  };
}
