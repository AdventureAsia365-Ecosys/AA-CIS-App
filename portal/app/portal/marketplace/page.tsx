"use client";
// portal/app/portal/marketplace/page.tsx — AA-444.
// No T-number in the route (see MarketplaceTab.tsx header + implementation notes):
// ADR-2026-038 §0.3 frames Marketplace as a view over T4 (Tenant Tour Pool) + T6 (Atom
// Curation), not a distinct pipeline stage of its own.
import MarketplaceTab from "../_components/MarketplaceTab";

export default function MarketplacePage() {
  return <MarketplaceTab />;
}
