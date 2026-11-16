"use client";
// app/admin/platform/delivery/page.tsx — AA-1108: Platform › Delivery (CI/CD and DORA per app). See DeliveryPage.tsx.
// Admin only (lib/rbac.ts PAGE_RULES /admin/platform; backend ops.read).
import { Suspense } from "react";
import DeliveryPage from "./DeliveryPage";

export default function PlatformDeliveryPage() {
  // The app tab lives in the URL (useSearchParams): a Suspense boundary for `next build`.
  return (
    <Suspense>
      <DeliveryPage />
    </Suspense>
  );
}
