"use client";
// app/admin/tenants/data-erasure/page.tsx — AA-839: Platform › Jobs › Data erasure. See DataErasure.tsx.
// Admin only (lib/rbac.ts PAGE_RULES /admin/tenants; backend tenant.read / tenant.write; an apply also needs
// ERASURE_APPLY_ENABLED=true on the API and worker and a Cognito staff session).
import { Suspense } from "react";
import { PageFrame, ToastProvider } from "../../../_kit";
import DataErasure from "./DataErasure";

export default function DataErasurePage() {
  return (
    <ToastProvider>
      <PageFrame>
        {/* AA-1190: the tab and the selected jobs are read with useSearchParams: a Suspense boundary for `next build`. */}
        <Suspense>
          <DataErasure />
        </Suspense>
      </PageFrame>
    </ToastProvider>
  );
}
