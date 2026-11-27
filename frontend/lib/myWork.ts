// frontend/lib/myWork.ts
// AA-1156 — My work (/admin/my-work): the landing page of every staff role (AA-1190), one tile per queue. The backend
// (GET /admin/my-work/summary, api/routers/admin_my_work.py) answers the tiles the caller may act on, each with its
// count, the oldest item's time, its band (group), whether it counts toward the total and the target page's filter;
// this file keeps the page path of each tile, builds the link (path + that filter), groups the tiles into bands and
// splits the tiles with work from the empty ones (collapsed into one line). tests/unit/test_aa1156_my_work.py keeps the keys and
// capabilities below equal to the backend's TILES; lib/myWork.test.mjs checks every role that sees a tile may open
// its target page (lib/rbac.ts PAGE_RULES).
//
// Dependency-free, type-level TS only (node --test --experimental-strip-types runs lib/myWork.test.mjs on it).

import type { Capability } from "./rbac";

export type MyWorkKey =
  | "settings_to_approve" | "compliance_exceptions_to_approve" | "erasure_to_approve"
  | "review_waiting" | "supply_drafts" | "supply_no_description" | "aa_content_drafts" | "compliance_holds"
  | "ops_open_problems" | "review_held_by_policy" | "place_cards_to_review";

/** AA-1190: the bands of the page, in display order (the backend sends each tile's band). */
export type MyWorkGroup = "approve" | "work" | "platform" | "watch";
export const MY_WORK_GROUPS: readonly MyWorkGroup[] = ["approve", "work", "platform", "watch"];

export interface MyWorkTileDef {
  key: MyWorkKey;
  /** The capability that shows the tile (the backend filters the same way). */
  capability: Capability;
  /** The target page; the backend's `filter` becomes its query string. */
  path: string;
}

/** Display order = the backend's TILES order. */
export const MY_WORK_TILES: readonly MyWorkTileDef[] = [
  { key: "settings_to_approve", capability: "settings.approve", path: "/admin/platform/settings" },
  { key: "compliance_exceptions_to_approve", capability: "compliance.write", path: "/admin/platform/compliance" },
  { key: "erasure_to_approve", capability: "tenant.write", path: "/admin/tenants/data-erasure" },
  { key: "review_waiting", capability: "review.act", path: "/admin/review" },
  { key: "supply_drafts", capability: "content.write", path: "/admin/supply-content" },
  { key: "supply_no_description", capability: "content.write", path: "/admin/supply-content" },
  { key: "aa_content_drafts", capability: "content.write", path: "/admin/aa-content" },
  { key: "compliance_holds", capability: "quality.write", path: "/admin/tenant-qa/compliance" },
  { key: "ops_open_problems", capability: "ops.write", path: "/admin/overview" },
  { key: "review_held_by_policy", capability: "review.act", path: "/admin/review" },
  { key: "place_cards_to_review", capability: "quality.write", path: "/admin/places/cards" },
];

/** One tile of GET /api/admin/my-work/summary. */
export interface MyWorkTile {
  key: string;
  capability: string;
  /** AA-1190: the band (approve, work, platform, watch); a build that does not know it draws the tile in "work". */
  group?: string;
  /** AA-1190: false for backlog and policy decisions (shown, never added to the total). */
  counts_toward_total?: boolean;
  filter: Record<string, string>;
  /** false: its table is not there yet (a migration not applied); count and oldest_at are null. */
  available: boolean;
  count: number | null;
  oldest_at: string | null;
}

export interface MyWorkSummary {
  role: string | null;
  tiles: MyWorkTile[];
  generated_at: string;
}

export const MY_WORK_API = "/api/admin/my-work/summary";

/** The tile definition of a key the backend sent; undefined for a key this build does not know (not drawn). */
export function tileDef(key: string): MyWorkTileDef | undefined {
  return MY_WORK_TILES.find((t) => t.key === key);
}

/** The link of a tile: its page with the backend's filter as the query string (keys sorted, so it is stable). */
export function tileHref(def: Pick<MyWorkTileDef, "path">, filter: Record<string, string> | null | undefined): string {
  const q = new URLSearchParams();
  for (const k of Object.keys(filter ?? {}).sort()) q.set(k, String(filter![k]));
  const qs = q.toString();
  return qs ? `${def.path}?${qs}` : def.path;
}

/** The tiles the page draws: known keys only, in the backend's order. */
export function knownTiles(summary: MyWorkSummary | null | undefined): { tile: MyWorkTile; def: MyWorkTileDef }[] {
  const out: { tile: MyWorkTile; def: MyWorkTileDef }[] = [];
  for (const tile of summary?.tiles ?? []) {
    const def = tileDef(tile.key);
    if (def) out.push({ tile, def });
  }
  return out;
}

/** Total of the counted tiles (unavailable tiles and the tiles that do not count toward the total count 0). */
export function totalWaiting(tiles: readonly Pick<MyWorkTile, "count" | "counts_toward_total">[]): number {
  return tiles.reduce((n, t) => n + (t.counts_toward_total === false ? 0 : (t.count ?? 0)), 0);
}

/** The band of a tile (an unknown or missing band reads as "work"). */
export function groupOf(tile: Pick<MyWorkTile, "group">): MyWorkGroup {
  return (MY_WORK_GROUPS as readonly string[]).includes(tile.group ?? "") ? (tile.group as MyWorkGroup) : "work";
}

/** A tile with something to show: an available count above zero, or not available (it says so). */
export function hasWork(tile: Pick<MyWorkTile, "available" | "count">): boolean {
  return !tile.available || (tile.count ?? 0) > 0;
}

export interface MyWorkBand<T> { group: MyWorkGroup; tiles: T[] }

/** AA-1190: the bands of the page with the tiles that have work, in band order (a band with none is left out), and
 *  the empty tiles of every band, collapsed into one line at the end ("Nothing waiting in N queues"). */
export function splitBands<T extends { tile: Pick<MyWorkTile, "group" | "available" | "count"> }>(
  rows: readonly T[],
): { bands: MyWorkBand<T>[]; empty: T[] } {
  const bands: MyWorkBand<T>[] = [];
  for (const group of MY_WORK_GROUPS) {
    const tiles = rows.filter((r) => groupOf(r.tile) === group && hasWork(r.tile));
    if (tiles.length) bands.push({ group, tiles });
  }
  return { bands, empty: rows.filter((r) => !hasWork(r.tile)) };
}
