"use client";
// app/admin/platform/compliance/page.tsx — AA-1170: Platform › Compliance (readiness per framework). See
// CompliancePage.tsx. compliance.read (lib/rbac.ts PAGE_RULES /admin/platform/compliance; the backend re-checks).
import { Suspense } from "react";
import CompliancePage from "./CompliancePage";

export default function PlatformCompliancePage() {
  // The tab, the framework filter and the open control live in the URL (useSearchParams): a Suspense boundary for
  // `next build`.
  return (
    <Suspense>
      <CompliancePage />
    </Suspense>
  );
}
