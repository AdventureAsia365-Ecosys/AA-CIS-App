"use client";
// app/admin/platform/staff/page.tsx — AA-883 (S241 GĐ2): Platform › People & access › Staff.
// See StaffDirectory.tsx. Admin only (lib/rbac.ts PAGE_RULES /admin/platform).
import StaffDirectory from "./StaffDirectory";

export default function StaffPage() {
  return <StaffDirectory />;
}
