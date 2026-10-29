"use client";
// app/admin/_components/TenantSelect.tsx — AA-919: the one admin tenant picker, and the hook that names tenants.
//
// useTenants()  one cached fetch of the tenant list (lib/tenants.ts: TENANTS_API, TENANTS_QUERY_KEY) and
//               label(id) = name, else slug, else the first 8 characters of the id.
// TenantName    how a page prints a tenant id it holds: label(id), with the full id as the hover title.
// TenantSelect  a labelled native <select> of the tenants by name (slug in brackets), active ones only unless
//               includeInactive; a search box over name, slug and id appears beside it when there are more than
//               TENANT_SEARCH_MIN. Its value is the tenant id, "" = none picked (or "All tenants" in a filter: pass
//               that as the placeholder). Admins never type a tenant UUID. Every admin page that lets staff choose a
//               tenant uses it (S254): forms take the stacked layout, filter bars `inline`; `offer` narrows the list
//               (only offboarded tenants, only tenants with data), `extraOptions` adds a non-tenant choice after them.

import { useQuery } from "@tanstack/react-query";
import { useCallback, useId, useMemo, useState } from "react";
import { Button, K, apiGet, useT } from "../../_kit";
import { M } from "./components.messages";
import {
  TENANTS_API, TENANTS_QUERY_KEY, TENANT_SEARCH_MIN, filterTenants, tenantLabel, tenantOptionText,
  type TenantRef,
} from "@/lib/tenants";

export function useTenants() {
  const q = useQuery({
    queryKey: TENANTS_QUERY_KEY,
    queryFn: () => apiGet<{ tenants: TenantRef[] }>(TENANTS_API),
    staleTime: 5 * 60_000,
  });
  const tenants = useMemo(() => q.data?.tenants ?? [], [q.data]);
  const byId = useMemo(() => new Map(tenants.map((t) => [t.tenant_id, t])), [tenants]);
  const label = useCallback((id: string | null | undefined) => tenantLabel(id, byId), [byId]);
  return {
    tenants, byId, label, isLoading: q.isLoading,
    error: q.error ? (q.error as Error).message : null, refetch: q.refetch,
  };
}

/** A tenant id printed by name (useTenants().label), the full id on hover. */
export function TenantName({ id, style }: { id: string | null | undefined; style?: React.CSSProperties }) {
  const { label } = useTenants();
  if (!id) return <span style={style}>—</span>;
  return <span title={id} style={{ overflowWrap: "anywhere", ...style }}>{label(id)}</span>;
}

const fieldStyle: React.CSSProperties = {
  width: "100%", fontSize: 13, padding: "6px 8px", borderRadius: 8, border: `1px solid ${K.line}`,
  background: K.card, color: K.ink, boxSizing: "border-box", maxWidth: "100%", minWidth: 0,
};
// Filter bars: a compact box that grows with the names, never wider than the bar.
const inlineFieldStyle: React.CSSProperties = { ...fieldStyle, width: "auto", minWidth: 180, fontSize: 12.5 };
const inlineSearchStyle: React.CSSProperties = { ...fieldStyle, width: 170, fontSize: 12.5 };
// The label stays for screen readers when a toolbar shows no visible labels (`hideLabel`).
const srOnly: React.CSSProperties = {
  position: "absolute", width: 1, height: 1, padding: 0, margin: -1, overflow: "hidden", clip: "rect(0 0 0 0)",
  whiteSpace: "nowrap", border: 0,
};

export function TenantSelect({
  value, onChange, label: labelProp, required = false, includeInactive = false,
  placeholder: placeholderProp, disabled = false, inline = false, hideLabel = false, offer,
  optionText = tenantOptionText, extraOptions, hint, testId = "tenant-select",
}: {
  value: string;
  /** The picked id ("" = none), and its tenant row when it is one (not for "" or an extra option). */
  onChange: (tenantId: string, tenant?: TenantRef) => void;
  label?: string;
  required?: boolean;
  /** Also offer suspended and offboarded tenants (their status shows in the option). */
  includeInactive?: boolean;
  /** Text of the "" option: "Pick a tenant…" in a form, "All tenants" in a filter. */
  placeholder?: string;
  disabled?: boolean;
  /** Filter-bar layout: "Label:" left of a compact select, the search box beside it, no "no tenant yet" line. */
  inline?: boolean;
  /** Keep the label for screen readers only (a toolbar whose other filters show no label either). */
  hideLabel?: boolean;
  /** Narrows the offered tenants further, after includeInactive (e.g. only offboarded ones). */
  offer?: (t: TenantRef) => boolean;
  /** The text of one option; default "Name (slug)" plus the status when it is not active. */
  optionText?: (t: TenantRef) => string;
  /** Non-tenant choices listed after the tenants (e.g. "Another tenant id…"); the search never hides them. */
  extraOptions?: readonly { value: string; label: string }[];
  /** One help line under the select (stacked layout). */
  hint?: React.ReactNode;
  testId?: string;
}) {
  const t = useT(M);
  const label = labelProp ?? t("tenant");
  const placeholder = placeholderProp ?? t("pickTenant");
  const id = useId();
  const { tenants, byId, isLoading, error, refetch } = useTenants();
  const [query, setQuery] = useState("");
  const offered = useMemo(
    () => tenants.filter((tn) => (includeInactive || !tn.status || tn.status === "active") && (!offer || offer(tn))),
    [tenants, includeInactive, offer],
  );
  const searchable = offered.length > TENANT_SEARCH_MIN;
  const shown = useMemo(() => {
    const list = filterTenants(offered, searchable ? query : "");
    // Keep the picked tenant listed while the search hides it, so the select never drops its value.
    const picked = value ? byId.get(value) : undefined;
    return picked && !list.some((tn) => tn.tenant_id === value) ? [picked, ...list] : list;
  }, [offered, searchable, query, value, byId]);

  const labelEl = (
    <label htmlFor={id} style={hideLabel ? srOnly : inline ? { fontSize: 12, color: K.muted } : undefined}>
      {label}{inline && !hideLabel ? ":" : ""}
      {required && <span aria-hidden="true" style={{ color: K.danger }}> *</span>}
    </label>
  );
  const search = searchable && (
    <input type="search" value={query} onChange={(e) => setQuery(e.target.value)}
      style={inline ? inlineSearchStyle : fieldStyle} placeholder={t("searchTenants")}
      aria-label={t("searchLabel", label)} disabled={disabled} />
  );
  const select = (
    <select id={id} value={value} onChange={(e) => onChange(e.target.value, byId.get(e.target.value))} style={inline ? inlineFieldStyle : fieldStyle}
      required={required} aria-required={required} aria-invalid={required && !value ? true : undefined}
      disabled={disabled || isLoading} data-testid={testId}>
      <option value="">{isLoading ? t("loadingTenants") : placeholder}</option>
      {shown.map((tn) => <option key={tn.tenant_id} value={tn.tenant_id}>{optionText(tn)}</option>)}
      {extraOptions?.map((o) => <option key={`extra:${o.value}`} value={o.value}>{o.label}</option>)}
    </select>
  );
  const loadError = error ? (
    <div style={{ display: "flex", gap: 6, alignItems: "center", flexWrap: "wrap", fontSize: 12, color: K.danger }}>
      {t("tenantListFailed", String(error))}
      <Button size="sm" variant="ghost" onClick={() => refetch()}>{t("retry")}</Button>
    </div>
  ) : null;

  if (inline) {
    return (
      <div style={{ display: "flex", gap: 6, alignItems: "center", flexWrap: "wrap", minWidth: 0, maxWidth: "100%" }}>
        {labelEl}{search}{select}{loadError}
      </div>
    );
  }
  return (
    <div style={{ display: "grid", gap: 4, minWidth: 0 }}>
      {labelEl}{search}{select}
      {loadError ?? (!isLoading && offered.length === 0 ? (
        <div style={{ fontSize: 12, color: K.muted }}>{includeInactive || offer ? t("noTenantYet") : t("noActiveTenantYet")}</div>
      ) : searchable && query.trim() && shown.length === 0 ? (
        <div style={{ fontSize: 12, color: K.muted }}>{t("noTenantMatches", query.trim())}</div>
      ) : null)}
      {hint && <div style={{ fontSize: 12, color: K.muted }}>{hint}</div>}
    </div>
  );
}
