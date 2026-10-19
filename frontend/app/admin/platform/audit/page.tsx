"use client";
// app/admin/platform/audit/page.tsx — AA-884 (S241 GĐ8): Platform › People & access › Audit log.
// See AuditLog.tsx. Admin only (lib/rbac.ts PAGE_RULES /admin/platform).
import AuditLog from "./AuditLog";

export default function PlatformAuditPage() {
  return <AuditLog />;
}
