"use client";
// app/(tenant)/portal/t0-brand/page.tsx — AA-430 route migration.
// Was the "brand" tab in the old portal/page.tsx (T0 — Brand Identity Setup).
//
// AA-754: the "Competitors" sub-tab (AA-445-02, backed by acp_silver_s2.competitor_inputs and
// the T5 distinctiveness scoring path) was removed when the distinctiveness feature was dropped
// end-to-end. T0 is now just the Brand Identity setup it originally was.
import BrandTab from "../_components/BrandTab";

export default function T0BrandPage() {
  return <BrandTab />;
}
