"use client";
// app/admin/platform/apps/page.tsx — AA-884 (S241 GĐ7): Platform › Apps › Apps.
// See AppsBoard.tsx. Admin only (lib/rbac.ts PAGE_RULES /admin/platform).
import AppsBoard from "./AppsBoard";

export default function PlatformAppsPage() {
  return <AppsBoard />;
}
