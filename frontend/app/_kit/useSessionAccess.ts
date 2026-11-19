"use client";
// app/_kit/useSessionAccess.ts
// AA-1127 C3 — may this staff session change data at all? The API answers with its own rules (GET /admin/session:
// a read-only role, or a Dev-only E2E identity whose role cookie says "admin" but whose every write the API refuses
// with 403 e2e_read_only). One read per session in the react-query cache (5 min). AdminShell hands the answer to the
// kit core <ReadOnlyProvider>, so every page reads it with useReadOnly() / <WriteOnly>. While the read is pending or
// when it fails, the role cookie decides (lib/rbac isReadOnlyRole); the API still refuses what it must.

import { useQuery } from "@tanstack/react-query";
import { isReadOnlyRole } from "@/lib/rbac";
import { sessionReadOnly, SESSION_API, type SessionHint } from "@/lib/sessionAccess";
import { apiGet } from "./api";
import { useAdminIdentity } from "./useAdminIdentity";

export const SESSION_ACCESS_KEY = ["admin", "session"] as const;

export function useSessionAccess(): { readOnly: boolean; reason: SessionHint["reason"] } {
  const role = useAdminIdentity().role;
  const q = useQuery({
    queryKey: SESSION_ACCESS_KEY,
    queryFn: () => apiGet<SessionHint>(SESSION_API, { admin: true }),
    staleTime: 5 * 60_000,
    retry: false,
    enabled: !!role,
  });
  return { readOnly: sessionReadOnly(q.data, isReadOnlyRole(role)), reason: q.data?.reason ?? null };
}
