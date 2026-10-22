// app/admin/data-model/erd.ts — AA-894 scope 2: types and pure helpers for the Data model page.
//
// The model is erd.json (format 1) written by the nightly DB Schema Dump (AA-CIS-Infra db/gen_erd.py) and served by
// GET /api/admin/data-model (backend api/routers/admin_data_model.py, api/services/data_model.py). This repo is
// public: nothing here names a real schema or table; every name comes from the response at run time.

export interface ErdColumn {
  name: string;
  type: string;
  not_null: boolean;
  pk: boolean;
  fk: boolean;
  uk: boolean;
  tag?: string; // DM-6.1 tag, e.g. "pii:contact" or "conf:internal"
  also?: string[];
}

export interface ErdForeignKey {
  name: string;
  columns: string[];
  ref_schema: string;
  ref_table: string;
  ref_columns: string[];
  validated: boolean;
}

export interface ErdTable {
  name: string;
  kind: string | null;
  owner_role: string | null;
  rls: boolean;
  rls_forced: boolean;
  is_partition: boolean;
  primary_key: string[];
  unique_keys: string[][];
  columns: ErdColumn[];
  foreign_keys: ErdForeignKey[];
}

export interface ErdSchema {
  schema: string;
  owner_app: string;
  owner_role: string | null;
  tables: ErdTable[];
}

export interface ErdEdge {
  name: string;
  from: string; // "schema.table"
  from_columns: string[];
  to: string;
  to_columns: string[];
  validated: boolean;
  cross_schema: boolean;
  cross_owner: boolean;
}

export interface ErdModel {
  format: number;
  fingerprint: string;
  generated_at: string | null;
  source?: string;
  migrations_latest_per_owner: Record<string, string>;
  schemas: ErdSchema[];
  edges: ErdEdge[];
}

export interface TableDiff {
  columns_added?: string[];
  columns_removed?: string[];
  columns_changed?: { name: string; changes: string[] }[];
  primary_key?: { from: string[]; to: string[] };
  fks_added?: string[];
  fks_removed?: string[];
  rls?: { from: [boolean, boolean]; to: [boolean, boolean] };
}

export interface ErdDiff {
  tables_added: string[];
  tables_removed: string[];
  tables_changed: Record<string, TableDiff>;
  edges_added: string[]; // "schema.table:constraint"
  edges_removed: string[];
}

export interface DataModelResp {
  model: ErdModel;
  previous: { fingerprint: string; generated_at: string | null; migrations_latest_per_owner: Record<string, string> } | null;
  diff: ErdDiff | null;
  loaded_at: string;
  cache_ttl_s: number;
}

/** One table of the model with its schema, keyed "schema.table". */
export interface TableRef {
  id: string;
  schema: string;
  ownerApp: string;
  table: ErdTable;
}

export type ChangeKind = "added" | "changed";

export function flattenTables(model: ErdModel): TableRef[] {
  const out: TableRef[] = [];
  for (const s of model.schemas) {
    for (const t of s.tables) out.push({ id: `${s.schema}.${t.name}`, schema: s.schema, ownerApp: s.owner_app, table: t });
  }
  return out;
}

/** Every word of `query` appears in the table id or in one of its column names (case-insensitive). */
export function matchesQuery(ref: TableRef, query: string): boolean {
  const words = query.toLowerCase().split(/\s+/).filter(Boolean);
  if (!words.length) return true;
  const hay = [ref.id, ...ref.table.columns.map((c) => c.name)].join(" ").toLowerCase();
  return words.every((w) => hay.includes(w));
}

/** Does any column name (not only the table name) match? Used to say why a table is listed. */
export function columnHits(ref: TableRef, query: string): string[] {
  const words = query.toLowerCase().split(/\s+/).filter(Boolean);
  if (!words.length) return [];
  return ref.table.columns.map((c) => c.name).filter((n) => words.some((w) => n.toLowerCase().includes(w)));
}

export interface Neighbours {
  outgoing: ErdEdge[]; // this table -> parent (FK out)
  incoming: ErdEdge[]; // child -> this table (FK in)
  ids: Set<string>; // the table itself + every +1 neighbour
}

export function neighbours(model: ErdModel, id: string): Neighbours {
  const outgoing = model.edges.filter((e) => e.from === id);
  const incoming = model.edges.filter((e) => e.to === id && e.from !== id);
  const ids = new Set<string>([id]);
  for (const e of outgoing) ids.add(e.to);
  for (const e of incoming) ids.add(e.from);
  return { outgoing, incoming, ids };
}

export function changeOf(diff: ErdDiff | null, id: string): ChangeKind | null {
  if (!diff) return null;
  if (diff.tables_added.includes(id)) return "added";
  if (diff.tables_changed[id]) return "changed";
  return null;
}

/** Column-level change of `column` in `id`: "added", the list of changes, or null. */
export function columnChange(diff: ErdDiff | null, id: string, column: string): "added" | string[] | null {
  const d = diff?.tables_changed[id];
  if (!d) return null;
  if (d.columns_added?.includes(column)) return "added";
  return d.columns_changed?.find((c) => c.name === column)?.changes ?? null;
}

export function piiColumns(t: ErdTable): ErdColumn[] {
  return t.columns.filter((c) => c.tag?.startsWith("pii:"));
}

export function taggedColumns(t: ErdTable): ErdColumn[] {
  return t.columns.filter((c) => !!c.tag);
}

/** "a, b" -> "(a, b)"; one column stays bare. */
export function colList(cols: string[]): string {
  return cols.length === 1 ? cols[0] : `(${cols.join(", ")})`;
}

export function countChanges(diff: ErdDiff | null): number {
  if (!diff) return 0;
  return (
    diff.tables_added.length +
    diff.tables_removed.length +
    Object.keys(diff.tables_changed).length +
    diff.edges_added.length +
    diff.edges_removed.length
  );
}
