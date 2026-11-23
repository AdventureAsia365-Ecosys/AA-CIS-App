"use client";
// app/admin/platform/compliance/page.tsx — AA-1170: Platform › Compliance (readiness per framework). See
// CompliancePage.tsx. compliance.read (lib/rbac.ts PAGE_RULES /admin/platform/compliance; the backend re-checks).
import { Suspense } from "react";
import { ToastProvider } from "../../../_kit";
import CompliancePage from "./CompliancePage";

export default function PlatformCompliancePage() {
  // The tab, the framework filter and the open control live in the URL (useSearchParams): a Suspense boundary for
  // `next build`. ToastProvider: the page shows the Refresh evidence result as a toast (useToast).
  return (
    <ToastProvider>
      <Suspense>
        <CompliancePage />
      </Suspense>
    </ToastProvider>
  );
}
