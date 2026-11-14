"use client";
// app/admin/platform/settings/page.tsx — S279 thresholds L1: Platform › Settings (the settings registry).
// See SettingsRegistry.tsx. Admin only (lib/rbac.ts PAGE_RULES /admin/platform; backend ops.read / settings.write /
// settings.approve).
import { Suspense } from "react";
import { ToastProvider } from "../../../_kit";
import SettingsRegistry from "./SettingsRegistry";

export default function PlatformSettingsPage() {
  return (
    <ToastProvider>
      {/* AA-1106: the registry reads ?app= / ?area= (useSearchParams): a Suspense boundary for `next build`. */}
      <Suspense>
        <SettingsRegistry />
      </Suspense>
    </ToastProvider>
  );
}
