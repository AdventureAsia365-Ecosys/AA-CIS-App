"use client";
// app/admin/tenants/view-as/page.tsx — AA-1035 R2 slice 2: Tenants › View as partner (read only). See ViewAsPartner.tsx.
// admin, customer_success, support (lib/rbac.ts PAGE_RULES); the backend checks tenant.view_as and the tenant scope.
import { PageFrame, ToastProvider } from "../../../_kit";
import ViewAsPartner from "./ViewAsPartner";

export default function ViewAsPage() {
  return (
    <ToastProvider>
      <PageFrame>
        <ViewAsPartner />
      </PageFrame>
    </ToastProvider>
  );
}
