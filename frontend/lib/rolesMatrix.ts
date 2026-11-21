// lib/rolesMatrix.ts — AA-1155 spec task T10 (R5.4): Platform › Roles & permissions (app/admin/platform/roles), a
// read-only page of every app's staff roles. The answer of GET /api/admin/platform/roles (backend
// api/core/staff_roles.roles_catalogue, staff.read; the Staff page pickers read the same answer) and the pure helpers
// that turn it into the page's rows.
//
// Rows: CIS built-in roles (with their capabilities), CIS custom roles, Booking built-in roles (their capabilities are
// the Booking code's), Booking custom roles, the three CRM staff groups as static rows (no CRM role editor) and
// TripPlanner as "no staff roles". Never a person, never an agent or E2E identity, never an issue id: the API sends
// none, and visibleRows() drops or cleans anything that looks like one (a custom role someone named after a test).

export interface CatalogueRole {
  code: string;
  label: string;
  capabilities?: string[];
  based_on?: string;
  archived?: boolean;
  assignable?: boolean;
}

export interface RolesCatalogue {
  cis: { builtin: CatalogueRole[]; custom: CatalogueRole[]; custom_available: boolean };
  booking: { builtin: CatalogueRole[]; custom: CatalogueRole[]; custom_available: boolean };
  crm: { groups: CatalogueRole[] };
  tripplanner: { staff_roles: boolean };
}

export const ROLES_PATH = "/api/admin/platform/roles";

export type RoleApp = "cis" | "booking" | "crm" | "tripplanner";
export type RoleKind = "builtin" | "custom" | "group" | "none";

export interface MatrixRow {
  id: string;
  app: RoleApp;
  kind: RoleKind;
  code: string;
  label: string;
  /** null = the capabilities live in another app's code (Booking built-in roles, CRM groups). */
  capabilities: string[] | null;
  basedOn: string | null;
  archived: boolean;
}

// A row that names a test or an agent identity is never shown (R5.4), and an issue id never reaches the UI.
const HIDDEN_ROW = /(?:^|[^a-z0-9])(?:e2e|agents?)(?:[^a-z0-9]|$)/i;
const ISSUE_ID = /\bAA-\d+\b/g;

function clean(label: string): string {
  return label.replace(ISSUE_ID, "").replace(/\s{2,}/g, " ").replace(/\s+([,.)])/g, "$1").trim();
}

function rowsOf(app: RoleApp, kind: RoleKind, roles: CatalogueRole[] | undefined, withCaps: boolean): MatrixRow[] {
  return (roles ?? []).map((r) => ({
    id: `${app}:${r.code}`,
    app,
    kind,
    code: r.code,
    label: r.label,
    capabilities: withCaps ? [...(r.capabilities ?? [])].sort() : null,
    basedOn: r.based_on ?? null,
    archived: !!r.archived,
  }));
}

/** Every row of the page, in app order (CIS, Booking, CRM, TripPlanner), built-in before custom. */
export function matrixRows(data: RolesCatalogue): MatrixRow[] {
  const rows = [
    ...rowsOf("cis", "builtin", data.cis?.builtin, true),
    ...rowsOf("cis", "custom", data.cis?.custom, true),
    ...rowsOf("booking", "builtin", data.booking?.builtin, false),
    ...rowsOf("booking", "custom", data.booking?.custom, true),
    ...rowsOf("crm", "group", data.crm?.groups, false),
  ];
  if (!data.tripplanner?.staff_roles) {
    rows.push({ id: "tripplanner:none", app: "tripplanner", kind: "none", code: "", label: "", capabilities: null,
      basedOn: null, archived: false });
  }
  return visibleRows(rows);
}

/** Drops rows that name a test or agent identity and removes issue ids from labels (R5.4). */
export function visibleRows(rows: MatrixRow[]): MatrixRow[] {
  return rows
    .filter((r) => !HIDDEN_ROW.test(r.code) && !HIDDEN_ROW.test(r.label))
    .map((r) => ({ ...r, label: clean(r.label), code: r.code.replace(ISSUE_ID, "") }));
}

/** The rows of one app. */
export function rowsForApp(rows: MatrixRow[], app: RoleApp): MatrixRow[] {
  return rows.filter((r) => r.app === app);
}

/** The capabilities shown as matrix columns for CIS: every capability any CIS row holds, sorted. */
export function cisCapabilityColumns(rows: MatrixRow[]): string[] {
  const all = new Set<string>();
  for (const r of rowsForApp(rows, "cis")) for (const c of r.capabilities ?? []) all.add(c);
  return [...all].sort();
}

/** A capability's area (the part before the first dot), for grouping the matrix columns. */
export function capabilityArea(capability: string): string {
  return capability.split(".", 1)[0];
}

/** Text a filter matches on: code, label and capabilities. */
export function matchesFilter(row: MatrixRow, query: string): boolean {
  const q = query.trim().toLowerCase();
  if (!q) return true;
  return [row.code, row.label, ...(row.capabilities ?? [])].some((v) => v.toLowerCase().includes(q));
}
