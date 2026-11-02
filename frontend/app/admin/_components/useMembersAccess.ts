"use client";
// app/admin/_components/useMembersAccess.ts — AA-1031 (S264 live check F3): which Content studio member writes this
// session may use on one tenant (GET /api/admin/tenants/{id}/members/access, one read per tenant in the react-query
// cache). Used by Tenant 360 › Members and by the agency drawer's "also invite to the Content studio" box.
// The API answers with its own route policy, so a read-only session (the Dev E2E admin, whose role cookie says
// "admin") sees no write control; nothing is offered while the read is in flight, and only a FAILED read falls back
// to the role cookie (lib/rbac can "tenant.write"). Same pattern as the AI providers page (ai-gateway/usePageAccess).

import { useQuery } from "@tanstack/react-query";
import { useMemo } from "react";
import { apiGet, useAdminIdentity } from "../../_kit";
import { can } from "@/lib/rbac";
import { memberPermissions, membersAccessPath, type MemberPermissions, type MembersAccess } from "@/lib/tenantMembers";

export const membersAccessKey = (tenantId: string) => ["tenant360", tenantId, "members-access"] as const;

/** `tenantId` null / empty: no read (nothing offered). */
export function useMembersAccess(tenantId: string | null | undefined): MemberPermissions {
  const role = useAdminIdentity().role;
  const id = tenantId ?? "";
  const q = useQuery({
    queryKey: membersAccessKey(id),
    queryFn: () => apiGet<MembersAccess>(membersAccessPath(id), { admin: true }),
    enabled: !!id,
    staleTime: 5 * 60_000,
    retry: false,
  });
  return useMemo(() => memberPermissions(q.data, q.isError, can(role, "tenant.write")), [q.data, q.isError, role]);
}
