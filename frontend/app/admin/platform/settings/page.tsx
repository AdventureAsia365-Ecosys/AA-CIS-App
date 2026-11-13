"use client";
// app/admin/platform/settings/page.tsx — S279 thresholds L1: Platform › Settings (the settings registry).
// See SettingsRegistry.tsx. Admin only (lib/rbac.ts PAGE_RULES /admin/platform; backend ops.read / settings.write /
// settings.approve).
import { ToastProvider } from "../../../_kit";
import SettingsRegistry from "./SettingsRegistry";

export default function PlatformSettingsPage() {
  return (
    <ToastProvider>
      <SettingsRegistry />
    </ToastProvider>
  );
}
