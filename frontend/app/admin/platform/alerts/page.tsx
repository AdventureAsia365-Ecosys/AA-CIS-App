"use client";
// app/admin/platform/alerts/page.tsx — AA-913: Platform › People & access › Alert recipients.
// See AlertRecipients.tsx. Admin only (lib/rbac.ts PAGE_RULES /admin/platform; backend ops.read / alerts.write).
import { ToastProvider } from "../../../_kit";
import AlertRecipients from "./AlertRecipients";

export default function PlatformAlertRecipientsPage() {
  return (
    <ToastProvider>
      <AlertRecipients />
    </ToastProvider>
  );
}
