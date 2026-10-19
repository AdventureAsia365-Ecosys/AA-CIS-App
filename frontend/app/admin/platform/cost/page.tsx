"use client";
// app/admin/platform/cost/page.tsx — AA-884 (S241 GĐ10): Platform › AI & spend › Cost per app.
// See CostByApp.tsx. Admin only (lib/rbac.ts PAGE_RULES /admin/platform).
import CostByApp from "./CostByApp";

export default function PlatformCostPage() {
  return <CostByApp />;
}
