"use client";
// app/admin/platform/roles/page.tsx — Platform › Roles & permissions (AA-1155 spec task T10, R5.4), read-only.
// See RolesMatrix.tsx. Admin only (lib/rbac.ts PAGE_RULES /admin/platform: staff.read; backend staff.read).
import RolesMatrix from "./RolesMatrix";

export default function PlatformRolesPage() {
  return <RolesMatrix />;
}
