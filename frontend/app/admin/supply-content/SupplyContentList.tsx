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
// (S240): load on open + Refresh. AA-994: text in the UI language (messages.ts, useSupplyText.ts).

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
  useFormat,
  useT,
  type ColumnDef, PageFrame,
  Select,
} from "../../_kit";
import {
  CONTENT_STATUSES,
  ENTITY_TYPES,
  STATUS_TONE,
  detailHref,
  listUrl,
  supplyErrorText,
  type ContentStatus,
  type EntityType,
  type FeedStatus,
  type ListFilters,
  type ListPage,
  type ListRow,
  type SupplyWords,
} from "@/lib/supplyContent";
import { SC } from "./messages";
import { useSupplyText } from "./useSupplyText";

export const LIST_KEY = ["supply-content-list"] as const;
const PAGE_SIZES = [25, 50, 100];

async function getJson<T>(url: string, w: SupplyWords): Promise<T> {
  const res = await fetch(url, { headers: adminHeaders() });
  let body: unknown = null;
  try { body = await res.json(); } catch { /* non-JSON */ }
  if (!res.ok) throw new Error(supplyErrorText(res.status, body, w).message);
  return body as T;
}

export default function SupplyContentList() {
  const t = useT(SC);
  const st = useSupplyText();
  const { formatDateTime } = useFormat();
  const qc = useQueryClient();
  const router = useRouter();
  const [filters, setFilters] = useState<ListFilters>({ status: "", type: "" });
  const [pageIndex, setPageIndex] = useState(0);
  const [pageSize, setPageSize] = useState(PAGE_SIZES[0]);

  const list = useQuery({
    queryKey: [...LIST_KEY, filters, pageIndex, pageSize],
    queryFn: () => getJson<ListPage>(listUrl(filters, pageIndex, pageSize), st.words),
    placeholderData: keepPreviousData,
    retry: false,
  });
  const feed = useQuery({
    queryKey: [...LIST_KEY, "status"],
    queryFn: () => getJson<FeedStatus>("/api/admin/supply-content/status", st.words),
    retry: false,
  });

  const setFilter = <F extends keyof ListFilters>(key: F, value: ListFilters[F]) => {
    setFilters((f) => ({ ...f, [key]: value }));
    setPageIndex(0);
  };

  const columns = useMemo<ColumnDef<ListRow, unknown>[]>(() => [
    {
      accessorKey: "title", header: t("colHotel"),
      cell: (c) => (
        <span style={{ display: "flex", flexDirection: "column", gap: 2, minWidth: 0 }}>
          <span style={{ fontWeight: 600, color: K.ink, overflowWrap: "anywhere" }}>
            {c.row.original.title || <span style={{ color: K.muted }}>{t("noName")}</span>}
          </span>
          <span style={{ fontSize: 11.5, color: K.muted, overflowWrap: "anywhere" }}>
            {st.type[c.row.original.entity_type]}{c.row.original.service_type ? ` · ${c.row.original.service_type}` : ""}
          </span>
        </span>
      ),
    },
    {
      accessorKey: "content_status", header: t("colContent"),
      cell: (c) => (
        <span style={{ display: "inline-flex", gap: 6, flexWrap: "wrap", alignItems: "center" }}>
          <Badge tone={STATUS_TONE[c.row.original.content_status]}>{st.status[c.row.original.content_status]}</Badge>
          {c.row.original.open_drafts > 0 && (
            <Badge tone="warning" dot={false}>{t("drafts", c.row.original.open_drafts)}</Badge>
          )}
          {!c.row.original.active && <Badge tone="neutral" dot={false}>{t("inactive")}</Badge>}
        </span>
      ),
    },
    { accessorKey: "country_code", header: t("colCountry"), cell: (c) => c.row.original.country_code || "—" },
    { accessorKey: "applied_supply_version", header: t("colFacts"), cell: (c) => `v${c.row.original.applied_supply_version}` },
    { accessorKey: "updated_at", header: t("colUpdated"), cell: (c) => formatDateTime(c.row.original.updated_at) },
  ], [t, st, formatDateTime]);

  const refresh = () => { void qc.invalidateQueries({ queryKey: LIST_KEY }); };
  const counts = list.data?.by_status ?? {};
  const filtered = !!(filters.status || filters.type);

  const filterBar = (
    <div style={{ display: "flex", gap: 8, flexWrap: "wrap", alignItems: "center", minWidth: 0 }}>
      <Select aria-label={t("contentStatus")} value={filters.status}
        onChange={(e) => setFilter("status", e.target.value as ContentStatus | "")} style={selectStyle}>
        <option value="">{t("anyStatus")}</option>
        {CONTENT_STATUSES.map((s) => <option key={s} value={s}>{st.status[s]} ({counts[s] ?? 0})</option>)}
      </Select>
      <Select aria-label={t("type")} value={filters.type}
        onChange={(e) => setFilter("type", e.target.value as EntityType | "")} style={selectStyle}>
        <option value="">{t("hotelsAndServices")}</option>
        {ENTITY_TYPES.map((et) => <option key={et} value={et}>{t("typePlural", st.type[et])}</option>)}
      </Select>
    </div>
  );

  return (
    <PageFrame>
      <PageHeader
        title={t("title")}
        breadcrumbs={[{ label: t("cisContent") }, { label: t("assets") }, { label: t("title") }]}
        description={t("listDescription")}
        actions={
          <Button variant="secondary" size="sm" onClick={refresh} disabled={list.isFetching}>
            <RefreshCw size={13} /> {list.isFetching ? t("loading") : t("refresh")}
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
        error={list.error ? (list.error as Error).message || t("listError") : null}
        onRetry={refresh}
        toolbarExtra={filterBar}
        pageSize={pageSize}
        pageSizeOptions={PAGE_SIZES}
        serverPagination={{
          pageIndex, pageSize, total: list.data?.total ?? 0,
          onChange: (i, size) => { setPageIndex(size === pageSize ? i : 0); setPageSize(size); },
        }}
        emptyTitle={filtered ? t("emptyFiltered") : t("emptyNone")}
        emptyDescription={filtered ? t("emptyFilteredDesc") : t("emptyNoneDesc")}
        onRowClick={(r) => router.push(detailHref(r.id))}
        tableMinWidth={640}
      />
    </PageFrame>
  );
}

const selectStyle: React.CSSProperties = {
  minHeight: 32, padding: "5px 8px", fontSize: 12.5, borderRadius: 8, border: `1px solid ${K.line}`,
  background: K.card, color: K.body, maxWidth: "100%", minWidth: 0,
};

/** The Booking feed at a glance (observability rule): lag, last error, draft jobs and today's LLM cost. */
function FeedBoard({ feed, error }: { feed?: FeedStatus; error: string | null }) {
  const t = useT(SC);
  const { formatDateTime } = useFormat();
  if (error) {
    return <div data-testid="supply-feed" style={{ ...boardStyle, color: K.danger }}>{t("feedUnavailable", error)}</div>;
  }
  if (!feed) return <div data-testid="supply-feed" style={{ ...boardStyle, color: K.muted }}>{t("feedLoading")}</div>;
  const jobs = Object.entries(feed.draft_jobs_7d || {}).map(([s, n]) => `${n} ${s}`).join(" · ") || t("none");
  const purges = Object.entries(feed.photo_purge_jobs_7d || {}).map(([s, n]) => `${n} ${s}`).join(" · ");
  return (
    <div data-testid="supply-feed" style={boardStyle}>
      <Item label={t("bookingFeed")}>
        {feed.feed_available
          ? <>{t("pendingCursor", feed.pending ?? 0, feed.cursor, feed.head != null ? ` / ${feed.head}` : "")}</>
          : <span style={{ color: K.danger }}>{t("notReadable", feed.feed_error ?? t("unknown"))}</span>}
      </Item>
      <Item label={t("lastPull")}>{feed.last_success_at ? formatDateTime(feed.last_success_at) : t("never")}</Item>
      <Item label={t("draftJobs")}>{jobs}</Item>
      {purges && <Item label={t("photoRemovals")}>{purges}</Item>}
      <Item label={t("llmCostToday")}>${(feed.llm_cost_today_usd ?? 0).toFixed(3)}</Item>
      {feed.last_error && (
        <div style={{ flexBasis: "100%", color: K.danger, overflowWrap: "anywhere" }}>
          {t("lastError", feed.last_error_at ? `(${formatDateTime(feed.last_error_at)})` : "", feed.last_error)}
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
