"use client";
// app/admin/platform/settings/ScopeWarning.tsx — AA-963 / AA-970 (S282): the warning of the change dialog when an
// agency feature switch that is off by default (crm.search.enabled, crm.release.preview; lib agencyFeatureSwitch) is
// about to be changed for the whole platform: a platform value reaches every agency without its own value, the pilot
// included. Nothing for any other key or scope (lib platformSwitchWarning).
// Imports the kit core directly (useT, K) so lib/settingsScopeWarning.test.mjs renders it with react-dom/server.

import { K } from "../../../_kit/core/tokens";
import { useT } from "../../../_kit/core/useI18n";
import { platformSwitchWarning, type ScopeChoice, type SettingDefinition } from "@/lib/settingsRegistry";
import { SETTINGS } from "./messages";

export function PlatformSwitchWarning({ item, scope }: {
  item: Pick<SettingDefinition, "scope_kind" | "app" | "value_type" | "default_value">; scope: ScopeChoice;
}) {
  const t = useT(SETTINGS);
  if (!platformSwitchWarning(item, scope)) return null;
  return (
    <div role="alert" data-testid="settings-scope-platform-warning"
      style={{ fontSize: 12, lineHeight: 1.45, color: K.body, background: K.amberSoft,
        border: `1px solid ${K.amber}`, borderRadius: 8, padding: "8px 10px" }}>
      {t("platformSwitchWarn")}
    </div>
  );
}
