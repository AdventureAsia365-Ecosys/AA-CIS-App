"use client";
// app/admin/ai-gateway/usePageAccess.ts — AA-1025 C1 follow-up: which write controls of the AI providers page this
// session may use (GET /api/admin/ai-gateway/access, one read per page in the react-query cache). The API answers
// with its own route policy, so a read-only session (the Dev E2E admin, whose role cookie says "admin") sees no
// write button. When the read fails the role cookie decides (lib/rbac can); the API still refuses what it must.

import { useQuery } from "@tanstack/react-query";
import { useCallback } from "react";
import { apiGet, useAdminIdentity } from "../../_kit";
import { ACCESS_API, ACTION_CAPABILITY, mayUse, type PageAccess, type PageAction } from "@/lib/aiGateway";
import { can } from "@/lib/rbac";

export const ACCESS_KEY = ["ai-gateway", "access"] as const;

export function usePageAccess(): { can: (action: PageAction) => boolean; readOnly: boolean } {
  const role = useAdminIdentity().role;
  const q = useQuery({
    queryKey: ACCESS_KEY,
    queryFn: () => apiGet<PageAccess>(ACCESS_API, { admin: true }),
    staleTime: 5 * 60_000,
    retry: false,
  });
  const allowed = useCallback(
    (action: PageAction) => mayUse(q.data, action, q.isError, (a) => can(role, ACTION_CAPABILITY[a])),
    [q.data, q.isError, role],
  );
  return { can: allowed, readOnly: q.data?.read_only === true };
}
