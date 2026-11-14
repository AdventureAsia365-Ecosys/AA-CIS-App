"use client";
// app/admin/platform/settings/SettingScope.tsx — AA-1096 (S280, tech lead: "the change form has no tenant scope"): the
// tenant scope of a setting on Platform › Settings.
//
// A setting whose definition allows a tenant value (scope_kind "platform+tenant", lib/settingsRegistry scopeOptions;
// never a CRM key: its agencies live in the CRM) gets:
// - ScopeFields: "Applies to" Platform (default) or One tenant, the tenant picked with the admin's one tenant picker
//   (TenantSelect: name and slug, search over both). The value the form starts from and the version it names follow
//   the scope (lib scopeTarget).
// - ScopeCurrent: the "In force now" line of the scope (a tenant without a value of its own uses the platform value).
// - TenantCount: the row's count of tenant values in force (the list's tenant_overrides).
// - TenantValues: each tenant's own value by name, with a History link (the history dialog).
// - HistoryScope: whose history the dialog shows (Platform or one tenant; GET .../history?tenant_id=).
// Every other key renders nothing here and keeps the form as before. Kit + TenantSelect only.

import { K, Badge, Field, LinkButton, Select, mono, useT } from "../../../_kit";
import { TenantSelect, useTenants } from "../../_components/TenantSelect";
import {
  boundsText, formatValue, scopeOptions, scopeTarget, scopeTenant, tenantScoped, type ScopeChoice, type SettingItem,
  type SettingScope,
} from "@/lib/settingsRegistry";
import { SETTINGS } from "./messages";

const small = { fontSize: 12, color: K.muted, lineHeight: 1.45 } as const;

export function ScopeFields({ item, value, onChange }: {
  item: SettingItem; value: ScopeChoice; onChange: (c: ScopeChoice) => void;
}) {
  const t = useT(SETTINGS);
  if (!tenantScoped(item)) return null;
  const label = (s: SettingScope) => (s === "tenant" ? t("scopeTenant") : t("scopePlatform"));
  return (
    <div style={{ display: "grid", gap: 8 }} data-testid="settings-scope">
      <Field label={t("appliesTo")} hint={t("scopeTenantHint")}>
        <Select value={value.kind} onChange={(e) => onChange({ kind: e.target.value as SettingScope, tenantId: "" })}
          options={scopeOptions(item).map((s) => ({ value: s, label: label(s) }))} data-testid="settings-scope-kind" />
      </Field>
      {value.kind === "tenant" && (
        <TenantSelect value={value.tenantId} onChange={(id) => onChange({ kind: "tenant", tenantId: id })}
          label={t("tenantPick")} placeholder={t("pickTenant")} required testId="settings-scope-tenant" />
      )}
    </div>
  );
}

/** "In force now: <value> · Allowed: <bounds>" of the scope being changed. */
export function ScopeCurrent({ item, scope }: { item: SettingItem; scope: ScopeChoice }) {
  const t = useT(SETTINGS);
  const { label } = useTenants();
  const tenant = scopeTenant(scope);
  const target = scopeTarget(item, tenant);
  const bounds = boundsText(item);
  return (
    <div style={small}>
      {tenant ? t("currentFor", label(tenant)) : t("current")}:{" "}
      <strong style={{ fontFamily: mono }}>{formatValue(target.value, item)}</strong>
      {target.inherited && <> ({t("inheritsPlatform")})</>}
      {bounds && <> · {t("colBounds")}: {bounds}</>}
    </div>
  );
}

/** The row's count of tenant values in force (a tenant-scoped key only). */
export function TenantCount({ item }: { item: SettingItem }) {
  const t = useT(SETTINGS);
  if (!tenantScoped(item)) return null;
  const n = item.tenant_overrides?.length ?? 0;
  return (
    <span data-testid={`settings-tenants-${item.key}`}>
      <Badge tone={n ? "info" : "neutral"} dot={false}>{t("tenantValues", n)}</Badge>
    </span>
  );
}

/** Each tenant's own value by name (slug in brackets) with a link to its history. */
export function TenantValues({ item, onPick }: { item: SettingItem; onPick: (tenantId: string) => void }) {
  const t = useT(SETTINGS);
  const { byId, label } = useTenants();
  const rows = item.tenant_overrides ?? [];
  return (
    <div style={{ display: "grid", gap: 4 }} data-testid="settings-tenant-values">
      <strong style={{ fontSize: 12 }}>{t("tenantValuesTitle")}</strong>
      {rows.length === 0 && <span style={small}>{t("noTenantValues")}</span>}
      {rows.map((o) => {
        const slug = byId.get(o.tenant_id)?.slug;
        return (
          <span key={o.tenant_id} style={{ ...small, display: "flex", gap: 8, flexWrap: "wrap", alignItems: "center" }}>
            <span title={o.tenant_id}>{label(o.tenant_id)}{slug && slug !== label(o.tenant_id) && ` (${slug})`}</span>
            <strong style={{ fontFamily: mono, color: K.body }}>{formatValue(o.value, item)}</strong>
            <span>v{o.version}</span>
            <LinkButton size="sm" onClick={() => onPick(o.tenant_id)}>{t("history")}</LinkButton>
          </span>
        );
      })}
    </div>
  );
}

/** Whose history the dialog shows: the platform ("") or one tenant, and the tenant values in force. */
export function HistoryScope({ item, tenant, onTenant }: {
  item: SettingItem; tenant: string | null; onTenant: (tenantId: string | null) => void;
}) {
  const t = useT(SETTINGS);
  if (!tenantScoped(item)) return null;
  return (
    <div style={{ display: "grid", gap: 10, border: `1px solid ${K.line}`, borderRadius: 8, padding: 10 }}>
      <TenantValues item={item} onPick={onTenant} />
      <TenantSelect value={tenant ?? ""} onChange={(id) => onTenant(id || null)} label={t("historyOf")}
        placeholder={t("historyPlatform")} includeInactive testId="settings-history-scope" />
    </div>
  );
}
