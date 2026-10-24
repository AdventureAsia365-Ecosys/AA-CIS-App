"use client";
// app/admin/platform/agencies/page.tsx — AA-941 (CRM-13a, screen S-01): Platform › Tenants › Agencies (CRM).
// See AgenciesConsole.tsx. Admin only (lib/rbac.ts PAGE_RULES /admin/platform; backend tenant.read / tenant.write,
// and the CRM's own crm-platform-admin group for writes).
import { ToastProvider } from "../../../_kit";
import AgenciesConsole from "./AgenciesConsole";

export default function PlatformAgenciesPage() {
  return (
    <ToastProvider>
      <AgenciesConsole />
    </ToastProvider>
  );
}
