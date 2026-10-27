"use client";
// app/admin/supply-content/SupplyContentList.tsx
// AA-835 — CIS Content › Assets › Supply content: the AA-Booking hotels and services whose customer content CIS
// writes and reviews (root ADR 0004a). Booking emits a facts change on its supply feed; CIS pulls it, an LLM draft is
// written, a reviewer publishes it, and Booking reads the published text back (catalog.v_supply_content).
//
// API (CIS BFF → api/routers/admin_supply_content.py):
//   GET /api/admin/supply-content?status=&entity_type=&limit=&offset=   one server page + counts per status
//   GET /api/admin/supply-content/status                                the feed board: cursor, lag, last error,
//                                                                       draft jobs (7 days), LLM cost today
// A row opens /admin/supply-content/<id> (SupplyContentDetail.tsx), the page Booking's "Edit in CIS" link opens.
//
// Every staff role reads (lib/rbac.ts PAGE_RULES: ALL_STAFF; backend reads are open to every staff role). No polling
// (S240): load on open + Refresh. English only (pages default to English).

import { keepPreviousData, useQuery, useQueryClient } from "@tanstack/react-query";
import { RefreshCw } from "lucide-react";
import { useRouter } from "next/navigation";
import { useMemo, useState } from "react";
import {
  Badge,
  Button,
  DataTable,
  K,
  PageHeader,
  adminHeaders,
  formatDateTime,
  type ColumnDef, AdminMain,
} from "../../_kit";
import {
  CONTENT_STATUSES,
  ENTITY_TYPES,
  STATUS_LABEL,
  STATUS_TONE,
  TYPE_LABEL,
  detailHref,
  listUrl,
  supplyErrorText,
  type ContentStatus,
  type EntityType,
  type FeedStatus,
  type ListFilters,
  type ListPage,
  type ListRow,
} from "@/lib/supplyContent";

export const LIST_KEY = ["supply-content-list"] as const;
const PAGE_SIZES = [25, 50, 100];

async function getJson<T>(url: string): Promise<T> {
  const res = await fetch(url, { headers: adminHeaders() });
  let body: unknown = null;
  try { body = await res.json(); } catch { /* non-JSON */ }
  if (!res.ok) throw new Error(supplyErrorText(res.status, body).message);
  return body as T;
}

export default function SupplyContentList() {
  const qc = useQueryClient();
  const router = useRouter();
  const [filters, setFilters] = useState<ListFilters>({ status: "", type: "" });
  const [pageIndex, setPageIndex] = useState(0);
  const [pageSize, setPageSize] = useState(PAGE_SIZES[0]);

  const list = useQuery({
    queryKey: [...LIST_KEY, filters, pageIndex, pageSize],
    queryFn: () => getJson<ListPage>(listUrl(filters, pageIndex, pageSize)),
    placeholderData: keepPreviousData,
    retry: false,
  });
  const feed = useQuery({
    queryKey: [...LIST_KEY, "status"],
    queryFn: () => getJson<FeedStatus>("/api/admin/supply-content/status"),
    retry: false,
  });

  const setFilter = <F extends keyof ListFilters>(key: F, value: ListFilters[F]) => {
    setFilters((f) => ({ ...f, [key]: value }));
    setPageIndex(0);
  };

  const columns = useMemo<ColumnDef<ListRow, unknown>[]>(() => [
    {
      accessorKey: "title", header: "Hotel / service",
      cell: (c) => (
        <span style={{ display: "flex", flexDirection: "column", gap: 2, minWidth: 0 }}>
          <span style={{ fontWeight: 600, color: K.ink, overflowWrap: "anywhere" }}>
            {c.row.original.title || <span style={{ color: K.muted }}>(no name)</span>}
          </span>
          <span style={{ fontSize: 11.5, color: K.muted, overflowWrap: "anywhere" }}>
            {TYPE_LABEL[c.row.original.entity_type]}{c.row.original.service_type ? ` · ${c.row.original.service_type}` : ""}
          </span>
        </span>
      ),
    },
    {
      accessorKey: "content_status", header: "Content",
      cell: (c) => (
        <span style={{ display: "inline-flex", gap: 6, flexWrap: "wrap", alignItems: "center" }}>
          <Badge tone={STATUS_TONE[c.row.original.content_status]}>{STATUS_LABEL[c.row.original.content_status]}</Badge>
          {c.row.original.open_drafts > 0 && (
            <Badge tone="warning" dot={false}>{c.row.original.open_drafts} draft{c.row.original.open_drafts === 1 ? "" : "s"}</Badge>
          )}
          {!c.row.original.active && <Badge tone="neutral" dot={false}>inactive</Badge>}
        </span>
      ),
    },
    { accessorKey: "country_code", header: "Country", cell: (c) => c.row.original.country_code || "—" },
    { accessorKey: "applied_supply_version", header: "Facts", cell: (c) => `v${c.row.original.applied_supply_version}` },
    { accessorKey: "updated_at", header: "Updated", cell: (c) => formatDateTime(c.row.original.updated_at) },
  ], []);

  const refresh = () => { void qc.invalidateQueries({ queryKey: LIST_KEY }); };
  const counts = list.data?.by_status ?? {};
  const filtered = !!(filters.status || filters.type);

  const filterBar = (
    <div style={{ display: "flex", gap: 8, flexWrap: "wrap", alignItems: "center", minWidth: 0 }}>
      <select aria-label="Content status" value={filters.status}
        onChange={(e) => setFilter("status", e.target.value as ContentStatus | "")} style={selectStyle}>
        <option value="">Any status</option>
        {CONTENT_STATUSES.map((s) => <option key={s} value={s}>{STATUS_LABEL[s]} ({counts[s] ?? 0})</option>)}
      </select>
      <select aria-label="Type" value={filters.type}
        onChange={(e) => setFilter("type", e.target.value as EntityType | "")} style={selectStyle}>
        <option value="">Hotels and services</option>
        {ENTITY_TYPES.map((t) => <option key={t} value={t}>{TYPE_LABEL[t]}s</option>)}
      </select>
    </div>
  );

  return (
    <AdminMain>
      <PageHeader
        title="Supply content"
        breadcrumbs={[{ label: "CIS Content" }, { label: "Assets" }, { label: "Supply content" }]}
        description="Customer-facing text and photos CIS writes for AA-Booking hotels and services. Booking shows only a published version; a facts change in Booking makes it stale until a new version is published."
        actions={
          <Button variant="secondary" size="sm" onClick={refresh} disabled={list.isFetching}>
            <RefreshCw size={13} /> {list.isFetching ? "Loading…" : "Refresh"}
          </Button>
        }
      />

      <FeedBoard feed={feed.data} error={feed.error ? (feed.error as Error).message : null} />

      <DataTable<ListRow>
        tableId="supply-content"
        data={list.data?.items ?? []}
        columns={columns}
        getRowId={(r) => r.id}
        loading={list.isLoading}
        error={list.error ? (list.error as Error).message || "Could not load the supply content." : null}
        onRetry={refresh}
        toolbarExtra={filterBar}
        pageSize={pageSize}
        pageSizeOptions={PAGE_SIZES}
        serverPagination={{
          pageIndex, pageSize, total: list.data?.total ?? 0,
          onChange: (i, size) => { setPageIndex(size === pageSize ? i : 0); setPageSize(size); },
        }}
        emptyTitle={filtered ? "Nothing matches these filters" : "No hotels or services yet"}
        emptyDescription={filtered ? "Clear a filter to see more."
          : "A hotel or service shows here once Booking emits it on the supply feed (a change in Booking starts it)."}
        onRowClick={(r) => router.push(detailHref(r.id))}
        tableMinWidth={640}
      />
    </AdminMain>
  );
}

const selectStyle: React.CSSProperties = {
  minHeight: 32, padding: "5px 8px", fontSize: 12.5, borderRadius: 8, border: `1px solid ${K.line}`,
  background: K.card, color: K.body, maxWidth: "100%", minWidth: 0,
};

/** The Booking feed at a glance (observability rule): lag, last error, draft jobs and today's LLM cost. */
function FeedBoard({ feed, error }: { feed?: FeedStatus; error: string | null }) {
  if (error) {
    return <div data-testid="supply-feed" style={{ ...boardStyle, color: K.danger }}>Feed status unavailable: {error}</div>;
  }
  if (!feed) return <div data-testid="supply-feed" style={{ ...boardStyle, color: K.muted }}>Loading the feed status…</div>;
  const jobs = Object.entries(feed.draft_jobs_7d || {}).map(([s, n]) => `${n} ${s}`).join(" · ") || "none";
  const purges = Object.entries(feed.photo_purge_jobs_7d || {}).map(([s, n]) => `${n} ${s}`).join(" · ");
  return (
    <div data-testid="supply-feed" style={boardStyle}>
      <Item label="Booking feed">
        {feed.feed_available
          ? <>{feed.pending ?? 0} pending · cursor {feed.cursor}{feed.head != null ? ` / ${feed.head}` : ""}</>
          : <span style={{ color: K.danger }}>not readable ({feed.feed_error ?? "unknown"})</span>}
      </Item>
      <Item label="Last pull">{feed.last_success_at ? formatDateTime(feed.last_success_at) : "never"}</Item>
      <Item label="Draft jobs (7 days)">{jobs}</Item>
      {purges && <Item label="Photo removals (7 days)">{purges}</Item>}
      <Item label="LLM cost today">${(feed.llm_cost_today_usd ?? 0).toFixed(3)}</Item>
      {feed.last_error && (
        <div style={{ flexBasis: "100%", color: K.danger, overflowWrap: "anywhere" }}>
          Last error {feed.last_error_at ? `(${formatDateTime(feed.last_error_at)})` : ""}: {feed.last_error}
        </div>
      )}
    </div>
  );
}

const boardStyle: React.CSSProperties = {
  display: "flex", flexWrap: "wrap", gap: "8px 22px", fontSize: 12.5, color: K.body, background: K.card,
  border: `1px solid ${K.line}`, borderRadius: 10, padding: "10px 14px", marginBottom: 16,
};

function Item({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <span style={{ display: "inline-flex", flexDirection: "column", gap: 2, minWidth: 0 }}>
      <span style={{ fontSize: 10.5, fontWeight: 700, letterSpacing: "0.06em", textTransform: "uppercase", color: K.muted }}>{label}</span>
      <span style={{ overflowWrap: "anywhere" }}>{children}</span>
    </span>
  );
}
