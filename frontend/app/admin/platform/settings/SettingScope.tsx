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
// - HistoryScope: whose history the dialog shows (Platform or one tenant; GET .../history?tenant_id=), and (S280 UX
//   fix round) the open tenant proposals by name with a Review link, so a tenant proposal is decided here even while
//   the platform has one too (the row's "pending" badge counts every scope).
// - S280 fix round, CRM tab: a CRM key an agency may keep its own value of (lib agencyScoped) gets the same count,
//   values and open changes, named by the CRM (agency_name: its agencies are not CIS tenants, so no TenantSelect), and
//   a plain Select of those agencies for the history; Approve / Reject send the agency id (lib decisionBody). A change
//   made here stays a platform change (scopeOptions).
// Every other key renders nothing here and keeps the form as before. Kit + TenantSelect only.

import type { ReactNode } from "react";
import { K, Badge, Field, LinkButton, Select, mono, useT } from "../../../_kit";
import { TenantSelect, useTenants } from "../../_components/TenantSelect";
import {
  agencyNames, agencyScoped, boundsText, formatValue, scopeOptions, scopeTarget, scopeTenant, tenantProposals,
  tenantScoped, type ScopeChoice, type SettingItem, type SettingScope,
} from "@/lib/settingsRegistry";
import { SETTINGS } from "./messages";
import { useSettingWords } from "./useSettingWords";

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
  const { words } = useSettingWords();
  const { label } = useTenants();
  const tenant = scopeTenant(scope);
  const target = scopeTarget(item, tenant);
  const bounds = boundsText(item, words);
  return (
    <div style={small}>
      {tenant ? t("currentFor", label(tenant)) : t("current")}:{" "}
      <strong style={{ fontFamily: mono }}>{formatValue(target.value, item, words)}</strong>
      {target.inherited && <> ({t("inheritsPlatform")})</>}
      {bounds && <> · {t("colBounds")}: {bounds}</>}
    </div>
  );
}

/** The row's count of tenant values in force (a tenant-scoped key), or of agency values (a CRM agency key). */
export function TenantCount({ item }: { item: SettingItem }) {
  const t = useT(SETTINGS);
  const agency = agencyScoped(item);
  if (!tenantScoped(item) && !agency) return null;
  const n = item.tenant_overrides?.length ?? 0;
  return (
    <span data-testid={`settings-tenants-${item.key}`}>
      <Badge tone={n ? "info" : "neutral"} dot={false}>{agency ? t("agencyValues", n) : t("tenantValues", n)}</Badge>
    </span>
  );
}

type NameOf = (tenantId: string) => ReactNode;

/** Each tenant's (or agency's) own value by name with a link to its history. */
function OwnValues({ item, onPick, nameOf, title, empty }: {
  item: SettingItem; onPick: (tenantId: string) => void; nameOf: NameOf; title: string; empty: string;
}) {
  const t = useT(SETTINGS);
  const { words } = useSettingWords();
  const rows = item.tenant_overrides ?? [];
  return (
    <div style={{ display: "grid", gap: 4 }} data-testid="settings-tenant-values">
      <strong style={{ fontSize: 12 }}>{title}</strong>
      {rows.length === 0 && <span style={small}>{empty}</span>}
      {rows.map((o) => (
        <span key={o.tenant_id} style={{ ...small, display: "flex", gap: 8, flexWrap: "wrap", alignItems: "center" }}>
          <span title={o.tenant_id}>{nameOf(o.tenant_id)}</span>
          <strong style={{ fontFamily: mono, color: K.body }}>{formatValue(o.value, item, words)}</strong>
          <span>v{o.version}</span>
          <LinkButton size="sm" onClick={() => onPick(o.tenant_id)}>{t("history")}</LinkButton>
        </span>
      ))}
    </div>
  );
}

/** The open proposals of tenants (or agencies) by name, each with a Review link (the dialog switches to that scope's
 *  rows, where Approve / Reject appear). */
function OpenProposals({ item, onPick, nameOf, title }: {
  item: SettingItem; onPick: (tenantId: string) => void; nameOf: NameOf; title: string;
}) {
  const t = useT(SETTINGS);
  const { words } = useSettingWords();
  const rows = tenantProposals(item);
  if (rows.length === 0) return null;
  return (
    <div style={{ display: "grid", gap: 4 }} data-testid="settings-tenant-proposals">
      <strong style={{ fontSize: 12 }}>{title}</strong>
      {rows.map((p) => (
        <span key={p.id} style={{ ...small, display: "flex", gap: 8, flexWrap: "wrap", alignItems: "center" }}>
          <span title={p.tenant_id ?? ""}>{nameOf(p.tenant_id ?? "")}</span>
          <strong style={{ fontFamily: mono, color: K.body }}>{formatValue(p.value, item, words)}</strong>
          <span>v{p.version}</span>
          <LinkButton size="sm" onClick={() => onPick(p.tenant_id ?? "")}>{t("reviewTenant")}</LinkButton>
        </span>
      ))}
    </div>
  );
}

const box = { display: "grid", gap: 10, border: `1px solid ${K.line}`, borderRadius: 8, padding: 10 } as const;

/** CIS / TripPlanner: the tenants by name and slug (the admin's tenant list), and the tenant picker. */
function TenantHistoryScope({ item, tenant, onTenant }: {
  item: SettingItem; tenant: string | null; onTenant: (tenantId: string | null) => void;
}) {
  const t = useT(SETTINGS);
  const { byId, label } = useTenants();
  const nameOf = (id: string) => {
    const slug = byId.get(id)?.slug;
    return <>{label(id)}{slug && slug !== label(id) && ` (${slug})`}</>;
  };
  return (
    <div style={box}>
      <OwnValues item={item} onPick={onTenant} nameOf={nameOf} title={t("tenantValuesTitle")}
        empty={t("noTenantValues")} />
      <OpenProposals item={item} onPick={onTenant} nameOf={(id) => label(id)} title={t("tenantProposalsTitle")} />
      <TenantSelect value={tenant ?? ""} onChange={(id) => onTenant(id || null)} label={t("historyOf")}
        placeholder={t("historyPlatform")} includeInactive testId="settings-history-scope" />
    </div>
  );
}

/** CRM tab: the agencies by the CRM's names (unknown name: the agency id), and a Select of those agencies. */
function AgencyHistoryScope({ item, tenant, onTenant }: {
  item: SettingItem; tenant: string | null; onTenant: (tenantId: string | null) => void;
}) {
  const t = useT(SETTINGS);
  const names = agencyNames(item);
  const nameOf = (id: string) => names.get(id) ?? <span style={{ fontFamily: mono }}>{id}</span>;
  const ids = [...new Set([...(item.tenant_overrides ?? []).map((o) => o.tenant_id),
    ...tenantProposals(item).map((p) => p.tenant_id ?? ""), ...(tenant ? [tenant] : [])])].filter(Boolean);
  return (
    <div style={box}>
      <OwnValues item={item} onPick={onTenant} nameOf={nameOf} title={t("agencyValuesTitle")}
        empty={t("noAgencyValues")} />
      <OpenProposals item={item} onPick={onTenant} nameOf={nameOf} title={t("agencyProposalsTitle")} />
      <Field label={t("historyOf")}>
        <Select value={tenant ?? ""} onChange={(e) => onTenant(e.target.value || null)}
          data-testid="settings-history-scope"
          options={[{ value: "", label: t("historyPlatform") },
            ...ids.map((id) => ({ value: id, label: names.get(id) ?? id }))]} />
      </Field>
    </div>
  );
}

/** Whose history the dialog shows: the platform or one tenant (agency on the CRM tab), the values of the tenants in
 *  force and their open proposals. */
export function HistoryScope({ item, tenant, onTenant }: {
  item: SettingItem; tenant: string | null; onTenant: (tenantId: string | null) => void;
}) {
  if (agencyScoped(item)) return <AgencyHistoryScope item={item} tenant={tenant} onTenant={onTenant} />;
  if (!tenantScoped(item)) return null;
  return <TenantHistoryScope item={item} tenant={tenant} onTenant={onTenant} />;
}
