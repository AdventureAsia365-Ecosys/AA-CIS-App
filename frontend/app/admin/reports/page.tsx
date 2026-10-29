"use client";
// app/admin/reports/page.tsx — AA-1003: Platform › Overview › Reports (the report register and its runs).
// See ReportsBoard.tsx. Admin only (lib/rbac.ts PAGE_RULES /admin/reports; backend ops.read / ops.write).
import { ToastProvider } from "../../_kit";
import ReportsBoard from "./ReportsBoard";

export default function ReportsPage() {
  return (
    <ToastProvider>
      <ReportsBoard />
    </ToastProvider>
  );
}
