"use client";
// app/admin/data-erasure/page.tsx — AA-839: Platform › Jobs › Data erasure. See DataErasure.tsx.
// Admin only (lib/rbac.ts PAGE_RULES /admin/data-erasure; backend tenant.read / tenant.write; an apply also needs
// ERASURE_APPLY_ENABLED=true on the API and worker and a Cognito staff session).
import { PageFrame, ToastProvider } from "../../_kit";
import DataErasure from "./DataErasure";

export default function DataErasurePage() {
  return (
    <ToastProvider>
      <PageFrame>
        <DataErasure />
      </PageFrame>
    </ToastProvider>
  );
}
