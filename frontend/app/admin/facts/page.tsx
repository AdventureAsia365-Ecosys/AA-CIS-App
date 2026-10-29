"use client";
// app/admin/facts/page.tsx — AA-814: CIS Content › Assets › Facts (platform Facts Entries the T9 writer cites).
// See FactsConsole.tsx. Every staff role reads (lib/rbac.ts PAGE_RULES /admin/facts: ALL_STAFF); add / edit / delete
// need quality.write (admin; backend api/core/staff_authz.py, BFF API_WRITE_RULES "facts"; can() hides them).
import { ToastProvider } from "../../_kit";
import FactsConsole from "./FactsConsole";

export default function FactsPage() {
  return (
    <ToastProvider>
      <FactsConsole />
    </ToastProvider>
  );
}
