"use client";
// portal/app/portal/account/activity/page.tsx — AA-430 route migration.
// AA-759: was /portal/activity (308 from the old slug, next.config.ts).
// Was the "activity" tab in the old portal/page.tsx. Not a T-stage (utility page), so
// no T-prefix — see AA-430 implementation notes.
import { ActivityLogTab } from "../../_components/PlaceholderTabs";
import { usePortalShell } from "../../_components/PortalShellContext";

export default function ActivityPage() {
  // AA-760 — the shell's activity view (each row's state read from its version's job).
  const { activity } = usePortalShell();
  return <ActivityLogTab activity={activity} />;
}
