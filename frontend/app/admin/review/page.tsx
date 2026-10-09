"use client";
// app/admin/review/page.tsx — HITL review queue (AA-234 Part C / AA-241).
// AA-662 PR 2: migrated to the UI kit (DataTable, Drawer, Modal, Toast, StatusBadge) and
// react-query. AA-719: multi-select bulk regenerate + non-blocking regenerate (modal closes
// immediately, the row shows "regenerating", react-query polls the queue until jobs land).
//
// Lifecycle unchanged: edit → PATCH (sets human_edited, resets revalidate_passed=NULL) → POST
// revalidate (202 + job) → poll → Approve gated on revalidate_passed===true. The editor and its
// re-validate flow live in ReviewEditor.tsx; the data layer in reviewApi.ts; the field/severity
// model in reviewModel.ts.

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Ban, CheckCircle, Filter, RotateCcw, X } from "lucide-react";
import { useEffect, useMemo, useRef, useState } from "react";
import AdminSidebar from "../_components/AdminSidebar";
import { A, serif, mono, sans, Btn } from "../_components/adminUi";
import {
  Badge,
  type ColumnDef,
  DataTable,
  Drawer,
  StatusBadge,
  ToastProvider,
  useToast,
} from "../../_kit";
import { ReviewEditor } from "./ReviewEditor";
import { RegenerateModal } from "./RegenerateModal";
import {
  approveReview,
  dismissReview,
  fetchReviewQueue,
  rejectReview,
} from "./reviewApi";
import {
  SEV_STYLE,
  codeSeverity,
  mapRow,
  BLOCK_STYLE,
  type ReviewItem,
} from "./reviewModel";

// Reviewer identity prompt (temporary until AA-232). Called once on mount so the x-reviewer-id
// header the BFF forwards is populated for the audit trail.
function ensureReviewerId() {
  if (typeof window === "undefined") return;
  const id = window.localStorage.getItem("cis_reviewer_id") || "";
  if (!id) {
    const entered = window.prompt("Reviewer name (for the edit audit trail):", "");
    const v = (entered || "").trim();
    if (v) window.localStorage.setItem("cis_reviewer_id", v);
  }
}

// ── Reason chips (color-coded by failure nature) ──────────────────────────────
function ReasonChips({ codes }: { codes: string[] }) {
  if (!codes.length) return <span style={{ fontSize: 11, color: A.muted2 }}>—</span>;
  const shown = codes.slice(0, 3);
  const extra = codes.length - shown.length;
  return (
    <div style={{ display: "flex", flexWrap: "wrap", gap: 5, alignItems: "center" }}>
      {shown.map((c, i) => {
        const s = SEV_STYLE[codeSeverity(c)];
        return (
          <span
            key={i}
            title={c}
            style={{
              fontSize: 10,
              fontWeight: 600,
              padding: "2px 8px",
              borderRadius: 20,
              background: s.bg,
              color: s.color,
              border: `1px solid ${s.border}`,
              whiteSpace: "nowrap",
            }}
          >
            {c}
          </span>
        );
      })}
      {extra > 0 && <span style={{ fontSize: 10.5, color: A.muted }}>+{extra}</span>}
    </div>
  );
}

const selectStyle: React.CSSProperties = {
  padding: "7px 14px",
  background: A.card,
  border: `1px solid ${A.line}`,
  borderRadius: 8,
  color: A.body,
  fontSize: 13,
  fontFamily: sans,
  cursor: "pointer",
  outline: "none",
};

function ReviewQueueInner() {
  const toast = useToast();
  const qc = useQueryClient();

  const [filterStatus, setFilterStatusRaw] = useState("pending");
  const [filterCountry, setCountryRaw] = useState("all");
  const [filterScore, setScoreRaw] = useState("all");
  // AA-739: server-side paging. Any filter change goes back to the first page.
  const [pageIndex, setPageIndex] = useState(0);
  const [pageSize, setPageSize] = useState(25);
  const setFilterStatus = (v: string) => { setFilterStatusRaw(v); setPageIndex(0); };
  const setCountry = (v: string) => { setCountryRaw(v); setPageIndex(0); };
  const setScore = (v: string) => { setScoreRaw(v); setPageIndex(0); };
  // Deep-link ?tour_id= from Master Content ("N failed" badge). Read once from the URL as a lazy
  // initial value (client-only; null on the server) rather than setting state in an effect, which
  // the React Compiler forbids (AGENTS.md). Null-safe for SSR/prerender.
  const [tourFilter, setTourFilter] = useState<string | null>(() => {
    if (typeof window === "undefined") return null;
    return new URLSearchParams(window.location.search).get("tour_id");
  });
  const [editorItem, setEditorItem] = useState<ReviewItem | null>(null);
  const [regenItems, setRegenItems] = useState<ReviewItem[]>([]);
  // review ids whose regenerate job is in flight (client-only flag → poll + spinner)
  const [regenerating, setRegenerating] = useState<Set<string>>(new Set());
  const initRef = useRef(false);

  useEffect(() => {
    if (initRef.current) return;
    initRef.current = true;
    // Prompt for a reviewer name once (writes localStorage; no React state involved).
    ensureReviewerId();
  }, []);

  // ── Query: the review queue for the selected status ──
  const { data, isLoading, isError, error, refetch, isFetching } = useQuery({
    queryKey: ["review-queue", filterStatus, filterCountry, filterScore, tourFilter, pageIndex, pageSize],
    queryFn: () =>
      fetchReviewQueue({
        status: filterStatus,
        page: pageIndex + 1,
        pageSize,
        country: filterCountry,
        score: filterScore,
        tourId: tourFilter,
      }),
    placeholderData: (prev) => prev, // keep the current page visible while the next one loads
    // Poll while any row we kicked off is still present in the queue (AA-719). Reading the live
    // result here (not derived state) keeps this self-stopping without an effect: once every
    // regenerating row has left the queue, the interval returns false and polling stops.
    refetchInterval: (query) => {
      if (regenerating.size === 0) return false;
      const rows = query.state.data?.data ?? [];
      const present = new Set(rows.map((r) => String(r.id)));
      return [...regenerating].some((id) => present.has(id)) ? 5000 : false;
    },
  });

  const baseItems: ReviewItem[] = useMemo(() => (data?.data || []).map(mapRow), [data]);

  // A regenerate flag is only "active" while its row is still in the loaded list. When the row
  // leaves (published on success, or superseded), it drops out of this derived set on its own —
  // no effect/setState needed (React Compiler forbids setState-in-effect, AGENTS.md).
  const activeRegenerating = useMemo(() => {
    if (regenerating.size === 0) return regenerating;
    const present = new Set(baseItems.map((i) => i.id));
    const next = new Set<string>();
    for (const id of regenerating) if (present.has(id)) next.add(id);
    return next;
  }, [baseItems, regenerating]);

  const items: ReviewItem[] = useMemo(
    () => baseItems.map((r) => (activeRegenerating.has(r.id) ? { ...r, regenerating: true } : r)),
    [baseItems, activeRegenerating],
  );

  const total = data?.pagination?.total ?? items.length;

  // Derive the deep-link banner name from the loaded rows (no state, no effect).
  const tourFilterName = useMemo(() => {
    if (!tourFilter) return null;
    return items.find((i) => String(i.raw.tour_id) === tourFilter)?.name ?? null;
  }, [tourFilter, items]);

  // ── Mutations ──
  const invalidate = () => qc.invalidateQueries({ queryKey: ["review-queue"] });

  const approveM = useMutation({
    mutationFn: approveReview,
    onSuccess: () => {
      toast.success("Approved");
      invalidate();
    },
    onError: (e) => toast.error(e instanceof Error ? e.message : "Approve failed"),
  });
  const rejectM = useMutation({
    mutationFn: rejectReview,
    onSuccess: () => {
      toast.success("Rejected");
      invalidate();
    },
    onError: (e) => toast.error(e instanceof Error ? e.message : "Reject failed"),
  });
  const dismissM = useMutation({
    mutationFn: dismissReview,
    onSuccess: () => {
      toast.success("Dismissed");
      invalidate();
    },
    onError: (e) => toast.error(e instanceof Error ? e.message : "Dismiss failed"),
  });

  // ── Filters run on the server (AA-739); the country list comes from the server facets, so it
  // lists every country in this status, not only the ones on the loaded page. ──
  const countries = data?.facets?.countries ?? [];
  const filtered = items;

  // ── Columns ──
  const canApprove = (item: ReviewItem) => {
    const failCount = (item.raw.failures || []).length;
    return item.human_edited ? item.revalidate_passed === true : item.score >= 7 && failCount === 0;
  };

  const columns = useMemo<ColumnDef<ReviewItem, unknown>[]>(
    () => [
      {
        accessorKey: "name",
        header: "Tour",
        cell: (c) => (
          <div style={{ display: "flex", alignItems: "center", gap: 8, maxWidth: 180 }}>
            <span
              title={c.row.original.name}
              style={{
                fontWeight: 600,
                color: A.ink,
                overflow: "hidden",
                textOverflow: "ellipsis",
                whiteSpace: "nowrap",
              }}
            >
              {c.row.original.name}
            </span>
            {c.row.original.human_edited && <Badge tone="neutral">edited</Badge>}
          </div>
        ),
      },
      { accessorKey: "country", header: "Country", cell: (c) => <span style={{ color: A.muted }}>{c.row.original.country}</span> },
      {
        accessorKey: "version_num",
        header: "Version",
        cell: (c) =>
          c.row.original.version_num != null ? (
            <Badge tone="info">v{c.row.original.version_num}</Badge>
          ) : (
            <span style={{ color: A.muted2 }}>—</span>
          ),
      },
      {
        accessorKey: "created_at_ms",
        header: "Written",
        cell: (c) => <span style={{ color: A.muted, fontSize: 12 }}>{c.row.original.date || "—"}</span>,
      },
      {
        accessorKey: "score",
        header: "Score",
        cell: (c) => {
          const s = c.row.original.score;
          const kind = c.row.original.block?.kind;
          // AA-739: a high score on a blocked row is not the story — show it quietly so the
          // block reason in the next column reads as the headline.
          if (kind === "hard" || kind === "needs_human") {
            return (
              <span style={{ fontFamily: mono, fontSize: 12, color: A.muted2 }} title="Score is not why this tour is held — see Reasons">
                {s.toFixed(1)}
              </span>
            );
          }
          const col = s >= 7 ? A.green : s >= 5 ? A.amber : A.red;
          return (
            <span style={{ fontFamily: mono, fontWeight: 700, fontSize: 14, color: col }}>
              {s.toFixed(1)}
            </span>
          );
        },
      },
      {
        id: "reasons",
        header: "Reasons",
        enableSorting: false,
        cell: (c) => {
          if (c.row.original.regenerating) return <StatusBadge status="regenerating" />;
          const b = c.row.original.block;
          return (
            <div style={{ display: "flex", flexDirection: "column", gap: 4, alignItems: "flex-start" }}>
              {b && (
                <span
                  style={{
                    fontSize: 11.5,
                    fontWeight: 700,
                    padding: "2px 8px",
                    borderRadius: 6,
                    background: BLOCK_STYLE[b.kind].bg,
                    color: BLOCK_STYLE[b.kind].color,
                    border: `1px solid ${BLOCK_STYLE[b.kind].border}`,
                    maxWidth: 360,
                  }}
                >
                  {b.label}
                </span>
              )}
              <ReasonChips codes={c.row.original.codes} />
            </div>
          );
        },
      },
      {
        id: "actions",
        header: "Actions",
        enableSorting: false,
        enableHiding: false,
        cell: (c) => {
          const item = c.row.original;
          const ok = canApprove(item);
          const busy = item.regenerating;
          return (
            <div style={{ display: "flex", gap: 6, alignItems: "center", whiteSpace: "nowrap", flexWrap: "nowrap" }} onClick={(e) => e.stopPropagation()}>
              {/* AA-601 — icon-only (like Dismiss) so all 4 actions fit a 1440px screen. */}
              <Btn variant="primary" size="sm" disabled={busy} onClick={() => setRegenItems([item])}
                title="Regenerate — rewrite this tour again" ariaLabel="Regenerate">
                <RotateCcw size={13} />
              </Btn>
              <button
                onClick={() => dismissM.mutate(item.id)}
                disabled={busy}
                title="Dismiss — drop this stale failed version from the queue (no edit, no publish)"
                style={{
                  padding: 7,
                  border: `1px solid ${A.line}`,
                  borderRadius: 8,
                  background: "none",
                  cursor: busy ? "not-allowed" : "pointer",
                  color: A.muted,
                  display: "flex",
                  opacity: busy ? 0.5 : 1,
                }}
              >
                <Ban size={13} />
              </button>
              <Btn variant="danger" size="sm" disabled={busy} onClick={() => rejectM.mutate(item.id)}>
                Reject
              </Btn>
              <Btn variant={ok ? "primary" : "ghost"} size="sm" disabled={!ok || busy} onClick={() => approveM.mutate(item.id)}>
                Approve
              </Btn>
            </div>
          );
        },
      },
    ],
    [approveM, rejectM, dismissM],
  );

  // ── Non-blocking regenerate: mark rows regenerating, close modal, let the poll refresh ──
  function onEnqueued(_tourIds: string[], reviewIds: string[]) {
    setRegenerating((prev) => {
      const next = new Set(prev);
      reviewIds.forEach((id) => next.add(id));
      return next;
    });
    toast.success(
      reviewIds.length > 1
        ? `Queued ${reviewIds.length} regenerate jobs — the queue updates as they finish`
        : "Regenerate queued — the queue updates when it finishes",
    );
    setRegenItems([]);
  }

  return (
    <main className="aa-admin-main" style={{ flex: 1, minWidth: 0, minHeight: 0, overflowY: "auto", padding: "32px 36px 56px" }}>
      <style>{`@keyframes spin{to{transform:rotate(360deg)}}.spin{animation:spin .8s linear infinite}`}</style>

      <div style={{ display: "flex", alignItems: "flex-start", justifyContent: "space-between", marginBottom: 24 }}>
        <div>
          <div style={{ fontFamily: serif, fontSize: 26, fontWeight: 500, color: A.ink, letterSpacing: "-0.02em" }}>
            Review Queue
          </div>
          <div style={{ fontSize: 13, color: A.muted, marginTop: 4 }}>
            Edit, re-validate, then approve. Select rows to regenerate in bulk; regenerate runs in the
            background without freezing the screen.
          </div>
        </div>
        <div style={{ textAlign: "center" }}>
          <div
            style={{
              fontFamily: sans,
              fontVariantNumeric: "tabular-nums",
              fontSize: 22,
              fontWeight: 600,
              color: A.gold,
              letterSpacing: "-0.02em",
            }}
          >
            {total}
          </div>
          <div style={{ fontSize: 11, color: A.muted }}>
            {filterStatus === "all" ? "Total" : filterStatus[0].toUpperCase() + filterStatus.slice(1)}
          </div>
        </div>
      </div>

      {/* status/country/score filters — rendered as DataTable toolbarExtra */}
      {tourFilter && (
        <div
          style={{
            display: "flex",
            alignItems: "center",
            gap: 10,
            marginBottom: 16,
            padding: "8px 14px",
            borderRadius: 8,
            background: A.goldTint,
            border: `1px solid ${A.gold}`,
            fontSize: 12,
            color: A.ink,
          }}
        >
          <Filter size={13} style={{ color: A.gold }} />
          <span>
            Showing failed versions for <strong>{tourFilterName || "one selected tour"}</strong> only.
          </span>
          <button
            onClick={() => { setTourFilter(null); setPageIndex(0); }}
            style={{
              marginLeft: "auto",
              border: `1px solid ${A.line}`,
              borderRadius: 6,
              background: A.card,
              cursor: "pointer",
              padding: "4px 10px",
              fontSize: 12,
              color: A.body,
              display: "inline-flex",
              alignItems: "center",
              gap: 5,
            }}
          >
            <X size={12} /> Clear filter
          </button>
        </div>
      )}

      {/* Legend */}
      {!isLoading && filtered.length > 0 && (
        <div style={{ display: "flex", gap: 16, alignItems: "center", marginBottom: 12, fontSize: 11, color: A.muted }}>
          <span style={{ fontWeight: 600 }}>Reason colors:</span>
          {(["red", "amber", "gray"] as const).map((sev) => (
            <span key={sev} style={{ display: "inline-flex", alignItems: "center", gap: 5 }}>
              <span
                style={{
                  width: 10,
                  height: 10,
                  borderRadius: 3,
                  background: SEV_STYLE[sev].bg,
                  border: `1px solid ${SEV_STYLE[sev].border}`,
                  display: "inline-block",
                }}
              />
              {sev === "red" ? "Product-truth / structural" : sev === "amber" ? "Brand / style / SEO" : "Other"}
            </span>
          ))}
        </div>
      )}

      <DataTable<ReviewItem>
        data={filtered}
        columns={columns}
        tableId="review-queue"
        getRowId={(r) => r.id}
        userId={(typeof window !== "undefined" && window.localStorage.getItem("cis_reviewer_id")) || "admin"}
        loading={isLoading}
        error={isError ? (error instanceof Error ? error.message : "Could not load the review queue") : null}
        onRetry={() => refetch()}
        searchable
        searchPlaceholder="Search tours…"
        enableSelection
        enableSavedViews
        enableCsv
        csvFilename="review-queue"
        pageSize={pageSize}
        pageSizeOptions={[25, 50, 100, 200]}
        serverPagination={{
          pageIndex,
          pageSize,
          total,
          onChange: (pi, ps) => {
            setPageIndex(ps !== pageSize ? 0 : pi);
            setPageSize(ps);
          },
        }}
        emptyTitle="Review queue is empty"
        emptyDescription="Every tour has been reviewed."
        // AA-601 — the Tour column is capped (names ellipsize) so the 4-button Actions cell fits a
        // 1440px screen; narrower screens scroll the table inside its wrapper.
        toolbarExtra={
          <div style={{ display: "flex", gap: 8, alignItems: "center" }}>
            <select value={filterStatus} onChange={(e) => setFilterStatus(e.target.value)} style={selectStyle}>
              <option value="pending">Pending</option>
              <option value="approved">Approved</option>
              <option value="rejected">Rejected</option>
              <option value="all">All</option>
            </select>
            <select value={filterCountry} onChange={(e) => setCountry(e.target.value)} style={selectStyle}>
              <option value="all">All countries</option>
              {countries.map((c) => (
                <option key={c.country} value={c.country}>
                  {c.country} ({c.n})
                </option>
              ))}
            </select>
            <select value={filterScore} onChange={(e) => setScore(e.target.value)} style={selectStyle}>
              <option value="all">All scores</option>
              <option value="critical">Critical (&lt;5.0)</option>
              <option value="low">Low (5.0–6.9)</option>
              <option value="ok">7.0+ (held for another reason)</option>
            </select>
            <Btn variant="ghost" size="sm" onClick={() => refetch()}>
              <RotateCcw size={12} className={isFetching ? "spin" : undefined} /> Refresh
            </Btn>
          </div>
        }
        onRowClick={(row) => setEditorItem(row)}
        bulkActions={({ selectedRows, clearSelection }) => (
          <Btn
            variant="primary"
            size="sm"
            onClick={() => {
              setRegenItems(selectedRows);
              clearSelection();
            }}
          >
            <RotateCcw size={12} /> Regenerate selected
          </Btn>
        )}
      />

      {/* Clicking a row (except the actions cell) opens the field editor in a drawer. */}
      <Drawer
        open={!!editorItem}
        onClose={() => setEditorItem(null)}
        title={editorItem ? editorItem.name : ""}
        width={760}
      >
        {editorItem && (
          <ReviewEditor
            key={editorItem.id}
            item={editorItem}
            onSaved={() => invalidate()}
            onRevalidated={() => invalidate()}
          />
        )}
      </Drawer>

      {regenItems.length > 0 && (
        <RegenerateModal items={regenItems} onClose={() => setRegenItems([])} onEnqueued={onEnqueued} />
      )}

      {!isLoading && filtered.length === 0 && !isError && (
        <div style={{ textAlign: "center", padding: "20px 0" }}>
          <CheckCircle size={32} style={{ margin: "0 auto 8px", color: A.green, display: "block" }} />
        </div>
      )}
    </main>
  );
}

export default function AdminReviewPage() {
  return (
    <div style={{ display: "flex", height: "100vh", fontFamily: sans, background: A.bg }}>
      <AdminSidebar />
      <ToastProvider>
        <ReviewQueueInner />
      </ToastProvider>
    </div>
  );
}
