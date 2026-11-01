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
// AA-916 WM-07: Reject and Dismiss open a small confirm (ReasonActionModal.tsx) with the reason chip preselected
// from the row's failure codes; Regenerate carries the chip too (RegenerateModal.tsx).

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Ban, CheckCircle, Filter, RotateCcw, X } from "lucide-react";
import { useEffect, useMemo, useRef, useState } from "react";
import {
  K, mono, sans,
  Button,
  Badge,
  type ColumnDef,
  DataTable,
  Drawer,
  PageHeader,
  StatusBadge,
  ToastProvider,
  useAdminIdentity,
  useToast, PageFrame,
  useLang,
  useT,
  Select,
} from "../../_kit";
import { M } from "./messages";
import { can } from "@/lib/rbac";
import { ReviewEditor } from "./ReviewEditor";
import { RegenerateModal } from "./RegenerateModal";
import { ReasonActionModal, type ReasonAction } from "./ReasonActionModal";
import type { ReasonCode } from "@/lib/writingReasons";
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
import { PASS_LINE, SCORE_BANDS } from "@/lib/quality-registry.generated";

// Reviewer identity prompt (temporary until AA-232). Called once on mount so the x-reviewer-id
// header the BFF forwards is populated for the audit trail.
function ensureReviewerId(prompt: string) {
  if (typeof window === "undefined") return;
  const id = window.localStorage.getItem("cis_reviewer_id") || "";
  if (!id) {
    const entered = window.prompt(prompt, "");
    const v = (entered || "").trim();
    if (v) window.localStorage.setItem("cis_reviewer_id", v);
  }
}

// ── Reason chips (color-coded by failure nature) ──────────────────────────────
function ReasonChips({ codes }: { codes: string[] }) {
  if (!codes.length) return <span style={{ fontSize: 11, color: K.muted2 }}>—</span>;
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
      {extra > 0 && <span style={{ fontSize: 10.5, color: K.muted }}>+{extra}</span>}
    </div>
  );
}

const selectStyle: React.CSSProperties = {
  padding: "7px 14px",
  background: K.card,
  border: `1px solid ${K.line}`,
  borderRadius: 8,
  color: K.body,
  fontSize: 13,
  fontFamily: sans,
  cursor: "pointer",
  outline: "none",
  // S248 AA-760 — a <select> sizes itself to its longest option ("7.0+ (held for another reason)",
  // a long country facet); cap it to the row so it can never push the toolbar past a phone screen.
  maxWidth: "100%",
  minWidth: 0,
};

function ReviewQueueInner() {
  const toast = useToast();
  const t = useT(M);
  const lang = useLang();
  const qc = useQueryClient();
  // AA-878 — the read-only viewer sees the queue and the editor, without the actions (the BFF and the
  // backend refuse its writes anyway).
  const role = useAdminIdentity().role;
  const canAct = can(role, "review.act");

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
  // AA-916 — the Reject / Dismiss waiting for its reason chip
  const [reasonAction, setReasonAction] = useState<{ action: ReasonAction; item: ReviewItem } | null>(null);
  // review ids whose regenerate job is in flight (client-only flag → poll + spinner)
  const [regenerating, setRegenerating] = useState<Set<string>>(new Set());
  const initRef = useRef(false);

  useEffect(() => {
    if (initRef.current) return;
    initRef.current = true;
    // Prompt for a reviewer name once (writes localStorage; no React state involved).
    ensureReviewerId(t("reviewerPrompt"));
  }, [t]);

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

  const baseItems: ReviewItem[] = useMemo(
    () => (data?.data || []).map((r) =>
      mapRow(r, { untitledTour: t("untitledTour"), unknownCountry: t("unknownCountry"), lang })),
    [data, t, lang],
  );

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
      toast.success(t("approved"));
      invalidate();
    },
    onError: (e) => toast.error(e instanceof Error ? e.message : t("approveFailed")),
  });
  const rejectM = useMutation({
    mutationFn: ({ id, reason }: { id: string; reason: ReasonCode }) => rejectReview(id, reason),
    onSuccess: () => {
      toast.success(t("rejected"));
      setReasonAction(null);
      invalidate();
    },
    onError: (e) => toast.error(e instanceof Error ? e.message : t("rejectFailed")),
  });
  const dismissM = useMutation({
    mutationFn: ({ id, reason }: { id: string; reason: ReasonCode }) => dismissReview(id, reason),
    onSuccess: () => {
      toast.success(t("dismissed"));
      setReasonAction(null);
      invalidate();
    },
    onError: (e) => toast.error(e instanceof Error ? e.message : t("dismissFailed")),
  });

  // ── Filters run on the server (AA-739); the country list comes from the server facets, so it
  // lists every country in this status, not only the ones on the loaded page. ──
  const countries = data?.facets?.countries ?? [];
  const filtered = items;

  // ── Columns ──
  const canApprove = (item: ReviewItem) => {
    const failCount = (item.raw.failures || []).length;
    return item.human_edited ? item.revalidate_passed === true : item.score >= PASS_LINE && failCount === 0;
  };

  const columns = useMemo<ColumnDef<ReviewItem, unknown>[]>(
    () => [
      {
        accessorKey: "name",
        header: t("colTour"),
        cell: (c) => (
          <div style={{ display: "flex", alignItems: "center", gap: 8, maxWidth: 180 }}>
            <span
              title={c.row.original.name}
              style={{
                fontWeight: 600,
                color: K.ink,
                overflow: "hidden",
                textOverflow: "ellipsis",
                whiteSpace: "nowrap",
              }}
            >
              {c.row.original.name}
            </span>
            {c.row.original.human_edited && <Badge tone="neutral">{t("edited")}</Badge>}
          </div>
        ),
      },
      { accessorKey: "country", header: t("colCountry"), cell: (c) => <span style={{ color: K.muted }}>{c.row.original.country}</span> },
      {
        accessorKey: "version_num",
        header: t("colVersion"),
        cell: (c) =>
          c.row.original.version_num != null ? (
            <Badge tone="info">v{c.row.original.version_num}</Badge>
          ) : (
            <span style={{ color: K.muted2 }}>—</span>
          ),
      },
      {
        accessorKey: "created_at_ms",
        header: t("colWritten"),
        cell: (c) => <span style={{ color: K.muted, fontSize: 12 }}>{c.row.original.date || "—"}</span>,
      },
      {
        accessorKey: "score",
        header: t("colScore"),
        cell: (c) => {
          const s = c.row.original.score;
          const kind = c.row.original.block?.kind;
          // AA-739: a high score on a blocked row is not the story — show it quietly so the
          // block reason in the next column reads as the headline.
          if (kind === "hard" || kind === "needs_human") {
            return (
              <span style={{ fontFamily: mono, fontSize: 12, color: K.muted2 }} title={t("scoreNotWhy")}>
                {s.toFixed(1)}
              </span>
            );
          }
          const col = s >= SCORE_BANDS.pass ? K.success : s >= SCORE_BANDS.low ? K.amber : K.danger;
          return (
            <span style={{ fontFamily: mono, fontWeight: 700, fontSize: 14, color: col }}>
              {s.toFixed(1)}
            </span>
          );
        },
      },
      {
        id: "reasons",
        header: t("colReasons"),
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
              {/* AA-728 — when Regenerate cannot fix this row, tell the reviewer what to do. */}
              {!c.row.original.retryable && c.row.original.hint && (
                <span style={{ fontSize: 10.5, color: K.muted, lineHeight: 1.4, maxWidth: 360 }}>
                  {c.row.original.hint}
                </span>
              )}
            </div>
          );
        },
      },
      {
        id: "actions",
        header: t("colActions"),
        enableSorting: false,
        enableHiding: false,
        cell: (c) => {
          const item = c.row.original;
          const ok = canApprove(item);
          const busy = item.regenerating;
          if (!canAct) {
            return <span style={{ fontSize: 11.5, color: K.muted }}>{t("viewOnly")}</span>;
          }
          return (
            <div style={{ display: "flex", flexDirection: "column", gap: 6, alignItems: "flex-start" }} onClick={(e) => e.stopPropagation()}>
              {/* AA-728 (feedback r1) — two-row Actions cell so it still fits 1440px with the full
                  TEXT Regenerate button restored (#605's icon-only button was too small to read).
                  Row 1: Regenerate, only when the server marked the row retryable. Row 2: the
                  always-present Dismiss · Reject · Approve. */}
              {item.retryable && (
                <Button variant="primary" size="sm" disabled={busy} onClick={() => setRegenItems([item])}
                  title={t("regenerateTitle")}>
                  <RotateCcw size={12} /> {t("regenerate")}
                </Button>
              )}
              <div style={{ display: "flex", gap: 6, alignItems: "center", whiteSpace: "nowrap", flexWrap: "nowrap" }}>
                <button
                  onClick={() => setReasonAction({ action: "dismiss", item })}
                  disabled={busy}
                  title={t("dismissTitle")}
                  aria-label={t("dismiss")}
                  style={{
                    padding: 7,
                    border: `1px solid ${K.line}`,
                    borderRadius: 8,
                    background: "none",
                    cursor: busy ? "not-allowed" : "pointer",
                    color: K.muted,
                    display: "flex",
                    opacity: busy ? 0.5 : 1,
                  }}
                >
                  <Ban size={13} />
                </button>
                <Button variant="danger" size="sm" disabled={busy} onClick={() => setReasonAction({ action: "reject", item })}>
                  {t("reject")}
                </Button>
                <Button variant={ok ? "primary" : "ghost"} size="sm" disabled={!ok || busy} onClick={() => approveM.mutate(item.id)}>
                  {t("approve")}
                </Button>
              </div>
            </div>
          );
        },
      },
    ],
    [approveM, canAct, t],
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
        ? t("queuedMany", reviewIds.length)
        : t("queuedOne"),
    );
    setRegenItems([]);
  }

  return (
    <PageFrame>
      <style>{`@keyframes spin{to{transform:rotate(360deg)}}.spin{animation:spin .8s linear infinite}`}</style>

      <PageHeader
        title={t("title")}
        description={t("description")}
        actions={
          <div style={{ textAlign: "center" }}>
            <div
              style={{
                fontFamily: sans,
                fontVariantNumeric: "tabular-nums",
                fontSize: 22,
                fontWeight: 600,
                color: K.accent,
                letterSpacing: "-0.02em",
              }}
            >
              {total}
            </div>
            <div style={{ fontSize: 11, color: K.muted }}>
              {filterStatus === "approved" ? t("statusApproved") : filterStatus === "rejected" ? t("statusRejected")
                : filterStatus === "pending" ? t("statusPending") : t("total")}
            </div>
          </div>
        }
      />

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
            background: K.accentTint,
            border: `1px solid ${K.accent}`,
            fontSize: 12,
            color: K.ink,
          }}
        >
          <Filter size={13} style={{ color: K.accent }} />
          <span>
            {t("showingFor")}<strong>{tourFilterName || t("oneSelectedTour")}</strong>{t("only")}
          </span>
          <button
            onClick={() => { setTourFilter(null); setPageIndex(0); }}
            style={{
              marginLeft: "auto",
              border: `1px solid ${K.line}`,
              borderRadius: 6,
              background: K.card,
              cursor: "pointer",
              padding: "4px 10px",
              fontSize: 12,
              color: K.body,
              display: "inline-flex",
              alignItems: "center",
              gap: 5,
            }}
          >
            <X size={12} /> {t("clearFilter")}
          </button>
        </div>
      )}

      {/* Legend */}
      {!isLoading && filtered.length > 0 && (
        <div style={{ display: "flex", flexWrap: "wrap", columnGap: 16, rowGap: 6, alignItems: "center", marginBottom: 12, fontSize: 11, color: K.muted }}>
          <span style={{ fontWeight: 600 }}>{t("reasonColors")}</span>
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
              {sev === "red" ? t("sevRed") : sev === "amber" ? t("sevAmber") : t("sevGray")}
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
        error={isError ? (error instanceof Error ? error.message : t("loadFailed")) : null}
        onRetry={() => refetch()}
        searchable
        searchPlaceholder={t("searchTours")}
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
        emptyTitle={t("emptyTitle")}
        emptyDescription={t("emptyBody")}
        // AA-601 — the Tour column is capped (names ellipsize) so the 4-button Actions cell fits a
        // 1440px screen; narrower screens scroll the table inside its wrapper.
        // S248 AA-760 — the filter group wraps: at 400 px the three selects + Refresh (~480 px) ran past
        // the screen and were cut off by the scrolling <main>. The group may shrink (minWidth 0) so the
        // kit toolbar can put it on its own line, then the selects wrap inside it.
        toolbarExtra={
          <div style={{ display: "flex", flexWrap: "wrap", gap: 8, alignItems: "center", minWidth: 0, maxWidth: "100%" }}>
            <Select value={filterStatus} onChange={(e) => setFilterStatus(e.target.value)} style={selectStyle}>
              <option value="pending">{t("statusPending")}</option>
              <option value="approved">{t("statusApproved")}</option>
              <option value="rejected">{t("statusRejected")}</option>
              <option value="all">{t("statusAll")}</option>
            </Select>
            <Select value={filterCountry} onChange={(e) => setCountry(e.target.value)} style={selectStyle}>
              <option value="all">{t("allCountries")}</option>
              {countries.map((c) => (
                <option key={c.country} value={c.country}>
                  {c.country} ({c.n})
                </option>
              ))}
            </Select>
            <Select value={filterScore} onChange={(e) => setScore(e.target.value)} style={selectStyle}>
              <option value="all">{t("allScores")}</option>
              <option value="critical">{t("critical", SCORE_BANDS.low.toFixed(1))}</option>
              <option value="low">{t("low", SCORE_BANDS.low.toFixed(1), (PASS_LINE - 0.1).toFixed(1))}</option>
              <option value="ok">{t("ok", PASS_LINE.toFixed(1))}</option>
            </Select>
            <Button variant="ghost" size="sm" onClick={() => refetch()}>
              <RotateCcw size={12} className={isFetching ? "spin" : undefined} /> {t("refresh")}
            </Button>
          </div>
        }
        onRowClick={(row) => setEditorItem(row)}
        bulkActions={!canAct ? undefined : ({ selectedRows, clearSelection }) => {
          // AA-728 — Regenerate only the retryable rows; non-retryable rows (raw too thin, needs a
          // person, or Sonnet already failed) are skipped. If none are retryable the action is
          // disabled with a tooltip.
          const retryable = selectedRows.filter((r) => r.retryable);
          const skipped = selectedRows.length - retryable.length;
          const noneRetryable = retryable.length === 0;
          return (
            <Button
              variant="primary"
              size="sm"
              disabled={noneRetryable}
              title={
                noneRetryable
                  ? t("noneRetryable")
                  : skipped > 0
                    ? t("regenSkip", retryable.length, skipped)
                    : t("regenSelectedTitle")
              }
              onClick={() => {
                setRegenItems(retryable);
                if (skipped > 0) {
                  toast.info(t("regenSkipped", retryable.length, skipped));
                }
                clearSelection();
              }}
            >
              <RotateCcw size={12} /> {t("regenSelected")}
              {skipped > 0 ? ` (${retryable.length})` : ""}
            </Button>
          );
        }}
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
            readOnly={!can(role, "content.write")}
            canLabel={canAct}
          />
        )}
      </Drawer>

      {reasonAction && (
        <ReasonActionModal
          key={`${reasonAction.action}-${reasonAction.item.id}`}
          action={reasonAction.action}
          item={reasonAction.item}
          busy={rejectM.isPending || dismissM.isPending}
          onClose={() => setReasonAction(null)}
          onConfirm={(reason) => (reasonAction.action === "reject" ? rejectM : dismissM).mutate({ id: reasonAction.item.id, reason })}
        />
      )}

      {regenItems.length > 0 && (
        <RegenerateModal items={regenItems} onClose={() => setRegenItems([])} onEnqueued={onEnqueued} />
      )}

      {!isLoading && filtered.length === 0 && !isError && (
        <div style={{ textAlign: "center", padding: "20px 0" }}>
          <CheckCircle size={32} style={{ margin: "0 auto 8px", color: K.success, display: "block" }} />
        </div>
      )}
    </PageFrame>
  );
}

export default function AdminReviewPage() {
  return (
    <ToastProvider>
      <ReviewQueueInner />
    </ToastProvider>
  );
}
