"use client";
// app/admin/aa-content/page.tsx — AA-977 (CRM-B5, slice S3): CIS Content › Assets › AA content (CRM).
// See AaContentConsole.tsx. Every staff role reads (lib/rbac.ts PAGE_RULES /admin/aa-content: ALL_STAFF); drafts need
// content.write, publish / retire / restore need master.admin (backend api/core/staff_authz.py; can() hides them).
import { ToastProvider } from "../../_kit";
import AaContentConsole from "./AaContentConsole";

export default function AaContentPage() {
  return (
    <ToastProvider>
      <AaContentConsole />
    </ToastProvider>
  );
}
