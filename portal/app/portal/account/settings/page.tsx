"use client";
// portal/app/portal/account/settings/page.tsx — AA-430 route migration.
// AA-759: was /portal/settings (308 from the old slug, next.config.ts).
// Was the "settings" tab in the old portal/page.tsx. Not a T-stage (utility page), so
// no T-prefix — see AA-430 implementation notes.
import { SettingsTab } from "../../_components/PlaceholderTabs";

export default function SettingsPage() {
  return <SettingsTab />;
}
