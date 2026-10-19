"use client";
// portal/app/portal/settings/page.tsx — AA-430 route migration.
// Was the "settings" tab in the old portal/page.tsx. Not a T-stage (utility page), so
// no T-prefix — see AA-430 implementation notes.
import { SettingsTab } from "../_components/PlaceholderTabs";

export default function SettingsPage() {
  return <SettingsTab />;
}
