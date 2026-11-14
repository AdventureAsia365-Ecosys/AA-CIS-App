"use client";
// app/admin/platform/catalogue/page.tsx — AA-1106: Platform › Product map (the root repo product catalogue).
// See ProductMap.tsx. Admin only (lib/rbac.ts PAGE_RULES /admin/platform; backend ops.read).
import { Suspense } from "react";
import ProductMap from "./ProductMap";

export default function PlatformCataloguePage() {
  // The filters and the tab live in the URL (useSearchParams): a Suspense boundary for `next build`.
  return (
    <Suspense>
      <ProductMap />
    </Suspense>
  );
}
