// lib/contentTrace.ts — paging, header counts and column sort of /admin/tenant-activity (06 · Content Trace).
// AA-1006 follow-up (S263 F3).
//
// The page used to load the newest 200 rows (the API's default `limit`) and show "Total pieces" as the number of rows
// it had loaded, so the header said 200 whatever the real count was, and there was no way to reach row 201. The API
// now pages by `offset` and answers the real `total` and `by_channel` counts for the filters; the page keeps the
// paging state here:
//
// - `traceQuery()` — the filter query plus `limit` / `offset` of the page asked for;
// - `pageFor()` — the page index belongs to one filter set: change a filter and the list starts at the first page
//   again, without a reset effect (React Compiler rule, frontend/AGENTS.md);
// - `traceStats()` — the header tiles from the answer's own counts (the loaded rows only as a fallback for an
//   older API answer that has none);
// - `traceSortValue()` — the table shows seven columns (it had ten and did not fit its pane at 1440): Tenant / tour,
//   Status (+ publish state) and Gates (+ retries) each sort by their first value, then by the second.

export const TRACE_PAGE_SIZES = [50, 100, 200] as const;
export const TRACE_DEFAULT_PAGE_SIZE = 50;

export interface TraceFilters {
  tenantId?: string;
  channel?: string;
  status?: string;
  published?: string;
  dateFrom?: string;
  dateTo?: string;
}

/** The filter part of the query (no paging): also the key a page index belongs to. */
export function traceFilterQuery(f: TraceFilters): string {
  const p = new URLSearchParams();
  if (f.tenantId) p.set("tenant_id", f.tenantId);
  if (f.channel) p.set("channel", f.channel);
  if (f.status) p.set("status", f.status);
  if (f.published) p.set("published", f.published);
  if (f.dateFrom) p.set("date_from", f.dateFrom);
  if (f.dateTo) p.set("date_to", f.dateTo);
  return p.toString();
}

/** The full query for one page: the filters, then `limit` (page size) and `offset` (rows before the page). */
export function traceQuery(filterQuery: string, pageIndex: number, pageSize: number): string {
  const p = new URLSearchParams(filterQuery);
  const size = Math.max(1, Math.floor(pageSize));
  p.set("limit", String(size));
  p.set("offset", String(Math.max(0, Math.floor(pageIndex)) * size));
  return p.toString();
}

/** A page index remembered together with the filter query it was chosen under. */
export interface TracePage {
  filterQuery: string;
  index: number;
}

/** The page index to show for the current filters: the remembered one, or 0 once the filters changed. */
export function pageFor(page: TracePage, filterQuery: string): number {
  return page.filterQuery === filterQuery ? page.index : 0;
}

/** The API answer of GET /admin/a4/content-log (the parts the paging and the header read). */
export interface TraceAnswer<Row extends { channel: string }> {
  data: Row[];
  total?: number;
  offset?: number;
  limit?: number;
  by_channel?: Record<string, number>;
}

/** The header tiles: every match and the matches per channel (largest first), from the answer's own counts. */
export function traceStats<Row extends { channel: string }>(
  answer: TraceAnswer<Row> | null | undefined,
): { total: number; byChannel: [string, number][] } {
  const rows = answer?.data ?? [];
  let byChannel: Record<string, number>;
  if (answer?.by_channel) {
    byChannel = answer.by_channel;
  } else {
    byChannel = {};
    for (const r of rows) byChannel[r.channel] = (byChannel[r.channel] ?? 0) + 1;
  }
  const counted = Object.values(byChannel).reduce((a, b) => a + b, 0);
  const total = typeof answer?.total === "number" ? answer.total : counted;
  return {
    total,
    byChannel: Object.entries(byChannel)
      .filter(([, n]) => n > 0)
      .sort((a, b) => b[1] - a[1] || a[0].localeCompare(b[0])),
  };
}

/** The fields of a Content Trace row the column sort reads. */
export interface TraceSortRow {
  topic: string | null;
  tenant_name: string | null;
  tour: { name: string } | null;
  channel: string;
  angles: { name: string; chosen: boolean }[];
  status: string | null;
  publish_status: "published" | "pending_publish" | "n/a";
  gate_pass_count: number;
  retry_count: number;
  created_at: string;
}

export type TraceSortKey = "topic" | "tenant" | "channel" | "angle" | "status" | "gate" | "created_at";

/** Pipeline order of the publish state (not alphabetical): not ready → waiting → live. */
export const PUBLISH_RANK: Record<TraceSortRow["publish_status"], number> = {
  "n/a": 0, pending_publish: 1, published: 2,
};

/** The sort value of a column. Two-part columns compare their first part, then the second. */
export function traceSortValue(r: TraceSortRow, key: TraceSortKey, channelLabel: (code: string) => string = (c) => c): string | number {
  switch (key) {
    case "topic": return r.topic ?? "";
    // "\u0001" sorts before every printable character, so "Ann" + tour comes before "Anna" + tour.
    case "tenant": return `${r.tenant_name ?? ""}\u0001${r.tour?.name ?? ""}`;
    case "channel": return channelLabel(r.channel);
    case "angle": return r.angles.find((a) => a.chosen)?.name ?? "";
    case "status": return `${r.status ?? ""}\u0001${PUBLISH_RANK[r.publish_status] ?? 0}`;
    case "gate": return r.gate_pass_count * 1000 + Math.min(999, Math.max(0, r.retry_count));
    case "created_at": return new Date(r.created_at).getTime();
  }
}
