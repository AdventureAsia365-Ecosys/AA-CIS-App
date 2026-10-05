"use client";
// app/(tenant)/portal/_components/ReviewList.tsx — My Content (t10-review).
// AA-669: migrated from an expand-card list to the shared UI-kit DataTable + react-query.
//   - columns: title, channel, goal, status, created, actions
//   - filters: channel, status, goal, search; sort any column; saved views; CSV export
//   - bulk actions: export selected (text), publish selected (eligible pieces)
//   - row click → ReviewDrawer (content with copy-lock, context, flags, inline edit, exports)
// Constraints kept from AA-501/AA-519/AA-614: tenant-safe projection (no gate data), copy-lock,
// ready_state semantics. Deep-link ?piece= still opens that piece's drawer.

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useSearchParams } from "next/navigation";
import { Download, Flag, Send } from "lucide-react";
import { useMemo, useState } from "react";
import { T, sans } from "./ui";
import {
  Badge,
  type ColumnDef,
  DataTable,
  Drawer,
  StatusBadge,
  ToastProvider,
  useToast,
} from "../../../_kit";
import { ReviewDrawer } from "./ReviewDrawer";
import {
  channelLabel,
  exportPiece,
  fetchReviews,
  publishPiece,
  savePieceText,
  type ReviewItem,
} from "./reviewListApi";

const READY_STATUS: Record<string, string> = {
  ready: "approved", // kit StatusBadge tone: success
  in_progress: "running",
  not_ready: "pending",
};

function ReviewListInner() {
  const toast = useToast();
  const qc = useQueryClient();
  const searchParams = useSearchParams();
  const focusPieceId = searchParams.get("piece");

  const [channelFilter, setChannelFilter] = useState("all");
  const [statusFilter, setStatusFilter] = useState("all");
  const [goalFilter, setGoalFilter] = useState("all");
  // Open drawer: initialise from ?piece= as a lazy value (client-only), no setState-in-effect.
  const [openPieceId, setOpenPieceId] = useState<string | null>(() => focusPieceId);

  const { data: items = [], isLoading, isError, error, refetch } = useQuery({
    queryKey: ["content-reviews"],
    queryFn: fetchReviews,
    refetchInterval: (query) => {
      const rows = query.state.data ?? [];
      return rows.some((r) => r.ready_state === "in_progress") ? 5000 : false;
    },
  });

  const invalidate = () => qc.invalidateQueries({ queryKey: ["content-reviews"] });

  const saveM = useMutation({
    mutationFn: (vars: { pieceId: string; text: string }) => savePieceText(vars.pieceId, vars.text),
    onSuccess: () => {
      toast.success("Saved");
      invalidate();
    },
    onError: () => toast.error("Couldn't save — try again."),
  });

  const channels = useMemo(() => [...new Set(items.map((i) => i.channel))], [items]);
  const goals = useMemo(
    () => [...new Set(items.map((i) => i.goal?.label).filter(Boolean))] as string[],
    [items],
  );

  const visible = useMemo(
    () =>
      items.filter((it) => {
        const mc = channelFilter === "all" || it.channel === channelFilter;
        const ms = statusFilter === "all" || it.ready_state === statusFilter;
        const mg = goalFilter === "all" || (it.goal?.label ?? "") === goalFilter;
        return mc && ms && mg;
      }),
    [items, channelFilter, statusFilter, goalFilter],
  );

  const openItem = useMemo(
    () => (openPieceId ? items.find((i) => i.piece_id === openPieceId) ?? null : null),
    [items, openPieceId],
  );

  async function onExport(item: ReviewItem, format: "text" | "html", mode?: "document" | "fragment") {
    try {
      await exportPiece(item, format, mode);
    } catch {
      toast.error("Export failed — try again.");
    }
  }

  async function bulkExport(rows: ReviewItem[]) {
    const exportable = rows.filter((r) => r.content_text !== null);
    if (exportable.length === 0) {
      toast.info("No exportable pieces selected.");
      return;
    }
    for (const r of exportable) {
      await onExport(r, "text");
    }
    toast.success(`Exported ${exportable.length} piece(s)`);
  }

  async function bulkPublish(rows: ReviewItem[]) {
    const eligible = rows.filter((r) => r.ready_state === "ready");
    if (eligible.length === 0) {
      toast.info("Only ready pieces can be published — none selected.");
      return;
    }
    let ok = 0;
    const failures: string[] = [];
    for (const r of eligible) {
      try {
        await publishPiece(r.piece_id);
        ok += 1;
      } catch (e) {
        failures.push(`${r.angle?.name || r.channel}: ${e instanceof Error ? e.message : "failed"}`);
      }
    }
    invalidate();
    if (ok > 0) toast.success(`Published ${ok} piece(s)`);
    if (failures.length > 0) toast.error(`${failures.length} could not publish`);
  }

  const columns = useMemo<ColumnDef<ReviewItem, unknown>[]>(
    () => [
      {
        id: "title",
        accessorFn: (r) => r.angle?.name || "Untitled",
        header: "Title",
        cell: (c) => {
          const it = c.row.original;
          return (
            <div style={{ minWidth: 0 }}>
              <div style={{ fontWeight: 600, color: T.ink, display: "flex", alignItems: "center", gap: 6 }}>
                {it.angle?.name || "Untitled"}
                {it.flags.length > 0 && <Flag size={12} color={T.amber} />}
              </div>
              {it.tour && (
                <div style={{ fontSize: 11.5, color: T.muted2 }}>
                  {it.tour.name} · {it.tour.destination}
                </div>
              )}
            </div>
          );
        },
      },
      {
        accessorKey: "channel",
        header: "Channel",
        cell: (c) => <Badge tone="neutral">{channelLabel(c.row.original.channel)}</Badge>,
      },
      {
        id: "goal",
        accessorFn: (r) => r.goal?.label ?? "—",
        header: "Goal",
        cell: (c) => <span style={{ color: T.body }}>{c.row.original.goal?.label ?? "—"}</span>,
      },
      {
        id: "status",
        accessorFn: (r) => r.ready_state,
        header: "Status",
        cell: (c) => {
          const rs = c.row.original.ready_state;
          const label = rs === "ready" ? "Ready" : rs === "in_progress" ? "Writing…" : "Not ready";
          return <StatusBadge status={READY_STATUS[rs] ?? "pending"} label={label} />;
        },
      },
      {
        id: "created",
        accessorFn: (r) => (r.created_at ? new Date(r.created_at).getTime() : 0),
        header: "Created",
        cell: (c) => (
          <span style={{ color: T.muted, fontSize: 12 }}>
            {c.row.original.created_at ? new Date(c.row.original.created_at).toLocaleDateString() : "—"}
          </span>
        ),
      },
    ],
    [],
  );

  return (
    <>
      <DataTable<ReviewItem>
        data={visible}
        columns={columns}
        tableId="my-content-pieces"
        getRowId={(r) => r.piece_id}
        loading={isLoading}
        error={isError ? (error instanceof Error ? error.message : "Could not load your content") : null}
        onRetry={() => refetch()}
        searchable
        searchPlaceholder="Search title, tour…"
        enableSelection
        enableSavedViews
        enableCsv
        csvFilename="my-content"
        pageSize={25}
        emptyTitle="Nothing written yet"
        emptyDescription="Once you've written content from Social Content, it'll show up here to review before publishing."
        onRowClick={(r) => setOpenPieceId(r.piece_id)}
        toolbarExtra={
          <div style={{ display: "flex", gap: 8, alignItems: "center" }}>
            <select value={channelFilter} onChange={(e) => setChannelFilter(e.target.value)} style={selectStyle}>
              <option value="all">All channels</option>
              {channels.map((c) => (
                <option key={c} value={c}>{channelLabel(c)}</option>
              ))}
            </select>
            <select value={statusFilter} onChange={(e) => setStatusFilter(e.target.value)} style={selectStyle}>
              <option value="all">All statuses</option>
              <option value="ready">Ready</option>
              <option value="in_progress">Writing…</option>
              <option value="not_ready">Not ready</option>
            </select>
            {goals.length > 0 && (
              <select value={goalFilter} onChange={(e) => setGoalFilter(e.target.value)} style={selectStyle}>
                <option value="all">All goals</option>
                {goals.map((g) => (
                  <option key={g} value={g}>{g}</option>
                ))}
              </select>
            )}
          </div>
        }
        bulkActions={({ selectedRows, clearSelection }) => (
          <>
            <button style={bulkBtn} onClick={() => bulkExport(selectedRows)}>
              <Download size={13} /> Export selected
            </button>
            <button style={bulkBtn} onClick={() => { bulkPublish(selectedRows); clearSelection(); }}>
              <Send size={13} /> Publish selected
            </button>
          </>
        )}
      />

      <Drawer
        open={!!openItem}
        onClose={() => setOpenPieceId(null)}
        width={720}
        title={
          openItem ? (
            <span style={{ display: "inline-flex", alignItems: "center", gap: 8 }}>
              {openItem.angle?.name || "Untitled"}
              <Badge tone="neutral">{channelLabel(openItem.channel)}</Badge>
            </span>
          ) : (
            ""
          )
        }
      >
        {openItem && (
          <ReviewDrawer
            key={openItem.piece_id}
            item={openItem}
            saving={saveM.isPending}
            onSave={(pieceId, text) => saveM.mutateAsync({ pieceId, text }).then(() => undefined)}
            onExport={onExport}
            onCopyAttempt={() => toast.info("Use Export to download this content.")}
          />
        )}
      </Drawer>

      {/* StatusBadge "running" uses animation: spin */}
      <style>{`@keyframes spin{to{transform:rotate(360deg)}}`}</style>
    </>
  );
}

const selectStyle: React.CSSProperties = {
  padding: "7px 12px",
  background: T.card,
  border: `1px solid ${T.line}`,
  borderRadius: 8,
  color: T.body,
  fontSize: 13,
  fontFamily: sans,
  cursor: "pointer",
  outline: "none",
};

const bulkBtn: React.CSSProperties = {
  display: "inline-flex",
  alignItems: "center",
  gap: 6,
  padding: "7px 12px",
  borderRadius: 999,
  border: `1px solid ${T.gold}`,
  background: T.goldTint,
  color: T.gold,
  fontSize: 12,
  fontWeight: 600,
  cursor: "pointer",
  fontFamily: sans,
};

export function ReviewList() {
  return (
    <ToastProvider>
      <ReviewListInner />
    </ToastProvider>
  );
}
