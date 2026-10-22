"use client";
// portal/app/portal/account/billing/page.tsx — AA-430 route migration.
// AA-759: was /portal/billing (308 from the old slug, next.config.ts).
// Was the "billing" tab in the old portal/page.tsx. Not a T-stage (utility page), so
// no T-prefix — see AA-430 implementation notes.
import { BillingTab } from "../../_components/PlaceholderTabs";
import { usePortalShell } from "../../_components/PortalShellContext";

export default function BillingPage() {
  const { billing } = usePortalShell();
  return <BillingTab billing={billing} />;
}
