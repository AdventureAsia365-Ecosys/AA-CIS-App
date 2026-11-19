"use client";
// app/admin/lab/writing-memory/useWmAdmin.ts — AA-1127 C3: may this session use the Writing memory run / write /
// paid controls? The ai.admin permission AND a session that may change data. The role cookie alone is not enough: a
// read-only session (a read-only role, or the Dev-only E2E identity whose role cookie says admin) gets no button the
// API would then refuse with 403. A display hint only; the API still refuses every write it must.

import { useAdminIdentity, useReadOnly } from "../../../_kit";
import { can } from "@/lib/rbac";

export function useWmAdmin(): boolean {
  const role = useAdminIdentity().role;
  const readOnly = useReadOnly();
  return can(role, "ai.admin") && !readOnly;
}
