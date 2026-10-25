"use client";
// app/admin/supply-content/page.tsx — AA-835: CIS Content › Assets › Supply content (the list). See
// SupplyContentList.tsx. Every staff role reads (lib/rbac.ts PAGE_RULES /admin/supply-content: ALL_STAFF); edits and a
// regenerate need content.write, publish / reject / unpublish review.act (backend api/core/staff_authz.py).
import { ToastProvider } from "../../_kit";
import SupplyContentList from "./SupplyContentList";

export default function SupplyContentPage() {
  return (
    <ToastProvider>
      <SupplyContentList />
    </ToastProvider>
  );
}
