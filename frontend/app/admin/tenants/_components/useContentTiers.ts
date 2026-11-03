"use client";
// app/admin/tenants/_components/useContentTiers.ts — AA-1028 R1 slice 3: the content tiers staff may put a tenant on,
// read from the plan catalogue (GET /api/admin/plans?product=content: the live public content plans with a tier).
// Replaces the UI's hard-coded PLAN_OPTIONS / PLAN_CHOICES; the API re-checks the tier on every write.
import { useQuery } from "@tanstack/react-query";
import { PLANS_API, PLANS_KEY, sendJson, tierChoices, type PlansResp } from "@/lib/tenantPlans";

export function useContentTiers(current?: string | null): { tiers: string[]; loading: boolean } {
  const q = useQuery({
    queryKey: [...PLANS_KEY, "content"],
    queryFn: () => sendJson<PlansResp>(`${PLANS_API}?product=content`, "GET"),
    staleTime: 60_000,
  });
  return { tiers: tierChoices(q.data?.plans, current), loading: q.isPending };
}
