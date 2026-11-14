// lib/tenants.ts — AA-919: the admin tenant list, shared by every page that picks a tenant or prints one.
//
// One source: GET /admin/tenant-names (api/routers/admin.py list_tenant_names) through the BFF. AA-1035: names only
// (id, name, slug, derived status), filtered to the caller's tenant scope, open to every role whose pages pick a
// tenant (the Tenants directory GET /admin/tenants is not: content_lead, tenant_qa and ai_ops are refused there). It
// returns active, suspended and offboarded tenants, so a page that only needs to NAME a tenant (a release of an
// offboarded tenant still has an owner) and a picker that offers only active ones read the same cached query
// (TENANTS_QUERY_KEY, also the key of /admin/tenants/data-erasure). The hook and the picker live in
// app/admin/_components/TenantSelect.tsx; this file holds the pure parts so node --test can load them.

export interface TenantRef {
  tenant_id: string;
  name: string;
  slug: string;
  /** Derived by the API: active | suspended | offboarded. */
  status?: string;
  cancelled_at?: string | null;
}

export const TENANTS_API = "/api/admin/tenant-names";
export const TENANTS_QUERY_KEY = ["admin-tenants", "include-inactive"] as const;

/** Above this many choices the picker shows a search box over name, slug and id. */
export const TENANT_SEARCH_MIN = 10;

export function shortId(id: string | null | undefined, n = 8): string {
  return id ? id.slice(0, n) : "—";
}

/** How a page prints a tenant: its name, else its slug, else the first 8 characters of its id. */
export function tenantLabel(
  id: string | null | undefined,
  byId: ReadonlyMap<string, Pick<TenantRef, "name" | "slug">> | undefined,
): string {
  if (!id) return "—";
  const t = byId?.get(id);
  return t?.name?.trim() || t?.slug?.trim() || shortId(id);
}

/** The text of one picker option: "Name (slug)", plus the status when it is not active. */
export function tenantOptionText(t: TenantRef): string {
  const shown = t.name?.trim() || t.slug?.trim() || shortId(t.tenant_id);
  const slug = t.slug && t.slug !== shown ? ` (${t.slug})` : "";
  const status = t.status && t.status !== "active" ? ` · ${t.status}` : "";
  return `${shown}${slug}${status}`;
}

/** Case-insensitive match on name, slug or id; an empty query keeps every tenant. Sorted by name. */
export function filterTenants(tenants: readonly TenantRef[], query: string): TenantRef[] {
  const q = query.trim().toLowerCase();
  return tenants
    .filter((t) => !q || [t.name, t.slug, t.tenant_id].some((v) => (v ?? "").toLowerCase().includes(q)))
    .slice()
    .sort((a, b) => (a.name || a.slug).localeCompare(b.name || b.slug));
}

/** S280 (AA-1096) — the text a kit DataTable `searchText` matches for a row that names a tenant: its name, slug and
 *  id (from the tenant list), plus any extra values of the row (a request id, an e-mail). Staff search by the slug or
 *  an id they were sent, not only by the display name the cell shows. */
export function tenantSearchText(
  tenantId: string | null | undefined,
  byId: ReadonlyMap<string, Pick<TenantRef, "name" | "slug">> | undefined,
  ...extra: (string | number | null | undefined)[]
): string {
  const t = tenantId ? byId?.get(tenantId) : undefined;
  return [t?.name, t?.slug, tenantId, ...extra].filter((v) => v != null && v !== "").join(" ");
}
