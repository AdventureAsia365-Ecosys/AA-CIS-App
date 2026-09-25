"use client";
// app/(tenant)/portal/t4-pool/page.tsx — AA-430 route migration.
// Was the "catalog" tab in the old portal/page.tsx (T4 — Tenant Tour Pool: tenant's
// own rewritten versions, approve/reject/edit). Route slug confirmed against the
// ADR-2026-038 mapping — NOT T3 (T3 is the tenant-facing QA-failure view, which has no
// UI yet, see AA-430 implementation notes).
import { Suspense } from "react";
import CatalogTab from "../_components/CatalogTab";
import { PageHeader } from "../_components/ui";

// AA-454 — CatalogTab now reads useSearchParams() (?tour_id= from AtomsTab's nav link),
// which requires a Suspense boundary or `next build` fails prerendering this route
// (same pattern as frontend/app/admin/curation/page.tsx).
export default function T4PoolPage() {
  return (
    <>
      <PageHeader title="My Catalog Tours"
        sub="Your rewritten tours. Review, edit, approve and export them from here." />
      <Suspense>
        <CatalogTab />
      </Suspense>
    </>
  );
}
