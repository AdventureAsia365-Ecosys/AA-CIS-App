"use client";
// app/admin/reference-prices/page.tsx — AA-1091: AA Catalogue › Reference prices (the tours' structured reference
// prices). See ReferencePricesConsole.tsx. Readers: lib/rbac.ts PAGE_RULES /admin/reference-prices; writes need
// reference_price.write (admin, content_lead, customer_success; backend api/core/staff_authz.py, BFF API_WRITE_RULES
// "reference-prices"; can() hides them).
import { ToastProvider } from "../../_kit";
import ReferencePricesConsole from "./ReferencePricesConsole";

export default function ReferencePricesPage() {
  return (
    <ToastProvider>
      <ReferencePricesConsole />
    </ToastProvider>
  );
}
