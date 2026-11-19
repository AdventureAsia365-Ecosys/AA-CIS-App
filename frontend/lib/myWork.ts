// frontend/lib/myWork.ts
// AA-1156 — My work (/admin/my-work): the content queues of the signed-in role, one tile per queue. The backend
// (GET /admin/my-work/summary, api/routers/admin_my_work.py) answers the tiles the caller's role may work, each with
// its count, the oldest item's time and the target page's filter; this file keeps the page path of each tile, builds
// the link (path + that filter) and orders / labels nothing else. tests/unit/test_aa1156_my_work.py keeps the keys and
// capabilities below equal to the backend's TILES; lib/myWork.test.mjs checks every role that sees a tile may open
// its target page (lib/rbac.ts PAGE_RULES).
//
// Dependency-free, type-level TS only (node --test --experimental-strip-types runs lib/myWork.test.mjs on it).

import type { Capability } from "./rbac";

export type MyWorkKey =
  | "review_waiting" | "supply_drafts" | "supply_no_description" | "aa_content_drafts"
  | "place_cards_to_review" | "compliance_holds";

export interface MyWorkTileDef {
  key: MyWorkKey;
  /** The capability that shows the tile (the backend filters the same way). */
  capability: Capability;
  /** The target page; the backend's `filter` becomes its query string. */
  path: string;
}

/** Display order = the backend's TILES order. */
export const MY_WORK_TILES: readonly MyWorkTileDef[] = [
  { key: "review_waiting", capability: "review.act", path: "/admin/review" },
  { key: "supply_drafts", capability: "content.write", path: "/admin/supply-content" },
  { key: "supply_no_description", capability: "content.write", path: "/admin/supply-content" },
  { key: "aa_content_drafts", capability: "content.write", path: "/admin/aa-content" },
  { key: "place_cards_to_review", capability: "quality.write", path: "/admin/places/cards" },
  { key: "compliance_holds", capability: "quality.write", path: "/admin/tenant-qa/compliance" },
];

/** One tile of GET /api/admin/my-work/summary. */
export interface MyWorkTile {
  key: string;
  capability: string;
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

/** Total of the counted tiles (unavailable tiles count 0). */
export function totalWaiting(tiles: readonly Pick<MyWorkTile, "count">[]): number {
  return tiles.reduce((n, t) => n + (t.count ?? 0), 0);
}
