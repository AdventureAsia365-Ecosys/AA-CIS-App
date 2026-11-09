"use client";
// app/admin/tenants/plans/page.tsx — AA-1028 R1 slice 3: Tenants › Plans & add-ons. See PlansCatalogue.tsx.
// Admin only (lib/rbac.ts PAGE_RULES /admin/tenants; backend tenant.read / tenant.write, re-checked per write).
import { PageFrame, ToastProvider } from "../../../_kit";
import PlansCatalogue from "./PlansCatalogue";

export default function PlansPage() {
  return (
    <ToastProvider>
      <PageFrame>
        <PlansCatalogue />
      </PageFrame>
    </ToastProvider>
  );
}
