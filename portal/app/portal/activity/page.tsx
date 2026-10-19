"use client";
// portal/app/portal/activity/page.tsx — AA-430 route migration.
// Was the "activity" tab in the old portal/page.tsx. Not a T-stage (utility page), so
// no T-prefix — see AA-430 implementation notes.
import { ActivityLogTab } from "../_components/PlaceholderTabs";
import { usePortalShell } from "../_components/PortalShellContext";

export default function ActivityPage() {
  const { billing } = usePortalShell();
  return <ActivityLogTab activity={billing?.activity ?? []} />;
}
