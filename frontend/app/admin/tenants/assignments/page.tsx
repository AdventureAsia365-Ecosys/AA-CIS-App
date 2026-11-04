"use client";
// app/admin/tenants/assignments/page.tsx — AA-1035 R2 slice 2: Tenants › Assignments. See Assignments.tsx.
// admin, customer_success, support (lib/rbac.ts PAGE_RULES); the backend filters to the caller's scope and only
// tenant.assign (admin) changes an assignment.
import { PageFrame, ToastProvider } from "../../../_kit";
import Assignments from "./Assignments";

export default function AssignmentsPage() {
  return (
    <ToastProvider>
      <PageFrame>
        <Assignments />
      </PageFrame>
    </ToastProvider>
  );
}
