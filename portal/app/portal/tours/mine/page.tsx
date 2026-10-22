"use client";
// portal/app/portal/tours/mine/page.tsx — My Tours (T4 — the tenant's own rewritten versions).
// AA-759: was /portal/t4-pool ("My Catalog Tours"; 308 from the old slug, next.config.ts).
// CatalogTab reads useSearchParams() (?tour_id=, also the ⌘K "My tours" target), so it needs a
// Suspense boundary or `next build` fails prerendering this route.
import { Suspense } from "react";
import { PageHeader } from "@/app/_kit";
import CatalogTab from "../../_components/CatalogTab";

export default function MyToursPage() {
  return (
    <>
      <PageHeader title="My Tours"
        description="Your rewritten tours. Review, edit and export them from here." />
      <Suspense>
        <CatalogTab />
      </Suspense>
    </>
  );
}
