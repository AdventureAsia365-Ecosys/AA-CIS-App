"use client";
// portal/app/portal/tours/marketplace/page.tsx — AA-444; titled "Tour overview" since AA-760 (the
// nav said "Marketplace"; the page lists the tenant's own written tours, nothing to find or buy).
// AA-759: was /portal/marketplace (308 from the old slug, next.config.ts). The route keeps its
// slug so bookmarks work. No T-number in the route (see MarketplaceTab.tsx header): ADR-2026-038
// §0.3 frames it as a view over the tenant's tour versions, not a pipeline stage of its own.
import { PageHeader } from "@/app/_kit";
import MarketplaceTab from "../../_components/MarketplaceTab";

export default function MarketplacePage() {
  return (
    <>
      <PageHeader title="Tour overview"
        description="Every tour you've written, with its price and status. To pick a new tour, use Browse Tours." />
      <MarketplaceTab />
    </>
  );
}
