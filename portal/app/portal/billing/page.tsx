"use client";
// portal/app/portal/billing/page.tsx — AA-430 route migration.
// Was the "billing" tab in the old portal/page.tsx. Not a T-stage (utility page), so
// no T-prefix — see AA-430 implementation notes.
import { BillingTab } from "../_components/PlaceholderTabs";
import { usePortalShell } from "../_components/PortalShellContext";

export default function BillingPage() {
  const { billing } = usePortalShell();
  return <BillingTab billing={billing} />;
}
