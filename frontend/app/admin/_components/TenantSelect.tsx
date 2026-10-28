"use client";
// app/admin/_components/TenantSelect.tsx — AA-919: the one admin tenant picker, and the hook that names tenants.
//
// useTenants()  one cached fetch of the tenant list (lib/tenants.ts: TENANTS_API, TENANTS_QUERY_KEY, shared with
//               /admin/data-erasure) and label(id) = name, else slug, else the first 8 characters of the id.
// TenantSelect  a labelled native <select> of the tenants by name (slug in brackets), active ones only unless
//               includeInactive; a search box over name, slug and id appears above it when there are more than
//               TENANT_SEARCH_MIN. Its value is the tenant id, "" = none picked. Admins never type a tenant UUID.

import { useQuery } from "@tanstack/react-query";
import { useCallback, useId, useMemo, useState } from "react";
import { Button, K, apiGet } from "../../_kit";
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

const fieldStyle: React.CSSProperties = {
  width: "100%", fontSize: 13, padding: "6px 8px", borderRadius: 8, border: `1px solid ${K.line}`,
  background: K.card, color: K.ink, boxSizing: "border-box", maxWidth: "100%", minWidth: 0,
};

export function TenantSelect({
  value, onChange, label = "Tenant", required = false, includeInactive = false,
  placeholder = "Pick a tenant…", disabled = false,
}: {
  value: string;
  onChange: (tenantId: string) => void;
  label?: string;
  required?: boolean;
  /** Also offer suspended and offboarded tenants (their status shows in the option). */
  includeInactive?: boolean;
  placeholder?: string;
  disabled?: boolean;
}) {
  const id = useId();
  const { tenants, byId, isLoading, error, refetch } = useTenants();
  const [query, setQuery] = useState("");
  const offered = useMemo(
    () => tenants.filter((t) => includeInactive || !t.status || t.status === "active"),
    [tenants, includeInactive],
  );
  const searchable = offered.length > TENANT_SEARCH_MIN;
  const shown = useMemo(() => {
    const list = filterTenants(offered, searchable ? query : "");
    // Keep the picked tenant listed while the search hides it, so the select never drops its value.
    const picked = value ? byId.get(value) : undefined;
    return picked && !list.some((t) => t.tenant_id === value) ? [picked, ...list] : list;
  }, [offered, searchable, query, value, byId]);

  return (
    <div style={{ display: "grid", gap: 4, minWidth: 0 }}>
      <label htmlFor={id}>
        {label}{required && <span aria-hidden="true" style={{ color: K.danger }}> *</span>}
      </label>
      {searchable && (
        <input type="search" value={query} onChange={(e) => setQuery(e.target.value)} style={fieldStyle}
          placeholder="Search by name, slug or id" aria-label={`Search ${label.toLowerCase()}`} disabled={disabled} />
      )}
      <select id={id} value={value} onChange={(e) => onChange(e.target.value)} style={fieldStyle}
        required={required} aria-required={required} aria-invalid={required && !value ? true : undefined}
        disabled={disabled || isLoading} data-testid="tenant-select">
        <option value="">{isLoading ? "Loading tenants…" : placeholder}</option>
        {shown.map((t) => <option key={t.tenant_id} value={t.tenant_id}>{tenantOptionText(t)}</option>)}
      </select>
      {error ? (
        <div style={{ display: "flex", gap: 6, alignItems: "center", flexWrap: "wrap", fontSize: 12, color: K.danger }}>
          The tenant list did not load ({error}).
          <Button size="sm" variant="ghost" onClick={() => refetch()}>Retry</Button>
        </div>
      ) : !isLoading && offered.length === 0 ? (
        <div style={{ fontSize: 12, color: K.muted }}>No {includeInactive ? "" : "active "}tenant yet.</div>
      ) : searchable && query.trim() && shown.length === 0 ? (
        <div style={{ fontSize: 12, color: K.muted }}>No tenant matches “{query.trim()}”.</div>
      ) : null}
    </div>
  );
}
