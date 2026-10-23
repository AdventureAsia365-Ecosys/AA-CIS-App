"use client";
// portal/app/portal/_components/PublishedList.tsx — AA-815
//
// The tenant's published posts (and ones they removed), with an Unpublish action. Unpublish is a
// confirm step, then DELETE /api/tenant/v1/publish-log/{publish_id}. The backend removes the post
// from the channel first (WordPress: moved to trash; Facebook: deleted) and only then marks the
// row unpublished; if the channel refuses, the post stays "Published" and the reason is shown.
//
// API: GET /api/tenant/v1/publish-log (tenant-safe rows: no errors/keys/actors).
// UI kit: DataTable + ConfirmModal + Toast + Badge/StatusBadge, react-query key ["publish-log"].

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { ExternalLink, Undo2 } from "lucide-react";
import { useMemo, useState } from "react";
import {
  ApiError,
  Badge,
  ConfirmModal,
  type ColumnDef,
  DataTable,
  StatusBadge,
  ToastProvider,
  apiGet,
  apiSend,
  useToast,
} from "@/app/_kit";
import { T, sans, Card, CardHead } from "./ui";
import { ServedBadge, afterUnpublishSentence, type ServedFields } from "./ServedBadge";
import { formatDate } from "@/app/_kit/tokens";

export interface PublishedRow extends ServedFields {
  publish_id: string;
  piece_id: string;
  title: string;
  channel: string;
  status: "published" | "unpublished";
  external_url: string | null;
  published_at: string | null;
  unpublished_at: string | null;
  /** AA-986 L3 (ADR 0015 Q3): a post still live about a tour AA no longer serves. AA never removes it on its own;
   *  the tenant's one-click Unpublish below does (the real WordPress / Facebook removal). */
  live_post_of_discontinued_tour?: boolean;
}

export const PUBLISH_LOG_QUERY_KEY = ["publish-log"] as const;

const CHANNEL_NAME: Record<string, string> = { blog: "WordPress", facebook: "Facebook" };

export function channelName(channel: string): string {
  return CHANNEL_NAME[channel] ?? channel.charAt(0).toUpperCase() + channel.slice(1);
}

// AA-760 — the shared date format ("9 Oct 2026"), was the browser locale ("10/20/2026").
function fmtDate(iso: string | null): string {
  return formatDate(iso);
}

function PublishedListInner({ onUnpublished }: { onUnpublished?: () => void }) {
  const toast = useToast();
  const qc = useQueryClient();
  const [confirmRow, setConfirmRow] = useState<PublishedRow | null>(null);

  const { data: rows = [], isLoading, isError, error, refetch } = useQuery({
    queryKey: PUBLISH_LOG_QUERY_KEY,
    queryFn: () => apiGet<{ data?: PublishedRow[] }>("/api/tenant/v1/publish-log").then(d => d.data ?? []),
  });

  const unpublishM = useMutation({
    mutationFn: (row: PublishedRow) =>
      apiSend<{ status: string }>(`/api/tenant/v1/publish-log/${row.publish_id}`, { method: "DELETE" }),
    onSuccess: (_d, row) => {
      toast.success(`Removed from ${channelName(row.channel)}.`);
      setConfirmRow(null);
      qc.invalidateQueries({ queryKey: PUBLISH_LOG_QUERY_KEY });
      onUnpublished?.();
    },
    onError: (e, row) => {
      // 404 = already unpublished elsewhere (refresh shows it); anything else = still live.
      const msg = e instanceof Error ? e.message : "Unpublish failed.";
      toast.error(e instanceof ApiError && e.status === 404
        ? "This post was already unpublished."
        : `Still published on ${channelName(row.channel)} — ${msg}`, 8000);
      setConfirmRow(null);
      qc.invalidateQueries({ queryKey: PUBLISH_LOG_QUERY_KEY });
    },
  });

  const columns = useMemo<ColumnDef<PublishedRow, unknown>[]>(
    () => [
      {
        accessorKey: "title",
        header: "Title",
        cell: (c) => {
          const r = c.row.original;
          return (
            <div>
              <span style={{ fontWeight: 600, color: T.ink }}>{r.title}</span>
              {r.served_notice && <div style={{ marginTop: 4 }}><ServedBadge fields={r} /></div>}
              {r.live_post_of_discontinued_tour && (
                <div style={{ marginTop: 4, fontSize: 11.5, color: T.red, fontWeight: 600 }}>
                  Tour discontinued: remove or edit this post
                </div>
              )}
            </div>
          );
        },
      },
      {
        accessorKey: "channel",
        header: "Channel",
        cell: (c) => <Badge tone="neutral">{channelName(c.row.original.channel)}</Badge>,
      },
      {
        accessorKey: "status",
        header: "Status",
        cell: (c) => c.row.original.status === "published"
          ? <StatusBadge status="approved" label="Published" />
          : <StatusBadge status="pending" label="Unpublished" />,
      },
      {
        id: "date",
        accessorFn: (r) => new Date(r.unpublished_at ?? r.published_at ?? 0).getTime(),
        header: "Date",
        cell: (c) => {
          const r = c.row.original;
          return (
            <span style={{ color: T.muted, fontSize: 12 }}>
              {r.status === "unpublished" ? `Removed ${fmtDate(r.unpublished_at)}` : fmtDate(r.published_at)}
            </span>
          );
        },
      },
      {
        id: "actions",
        header: "",
        enableSorting: false,
        cell: (c) => {
          const r = c.row.original;
          return (
            <div style={{ display: "flex", gap: 12, alignItems: "center", justifyContent: "flex-end" }}
              onClick={(e) => e.stopPropagation()}>
              {r.status === "published" && r.external_url && (
                <a href={r.external_url} target="_blank" rel="noreferrer" style={linkStyle}>
                  View <ExternalLink size={11} />
                </a>
              )}
              {r.status === "published" && (
                <button style={unpublishBtn} onClick={() => setConfirmRow(r)}>
                  <Undo2 size={12} /> {r.live_post_of_discontinued_tour ? "Remove post" : "Unpublish"}
                </button>
              )}
            </div>
          );
        },
      },
    ],
    [],
  );

  return (
    <Card>
      <CardHead title="Published" />
      <DataTable<PublishedRow>
        data={rows}
        columns={columns}
        tableId="portal-published-posts"
        getRowId={(r) => r.publish_id}
        loading={isLoading}
        error={isError ? (error instanceof Error ? error.message : "Could not load published posts") : null}
        onRetry={() => refetch()}
        searchable
        searchPlaceholder="Search title…"
        pageSize={10}
        tableMinWidth={560}
        emptyTitle="Nothing published yet"
        emptyDescription="Posts you publish to WordPress or Facebook show up here, where you can also unpublish them."
      />
      <ConfirmModal
        open={!!confirmRow}
        onClose={() => { if (!unpublishM.isPending) setConfirmRow(null); }}
        onConfirm={() => { if (confirmRow) unpublishM.mutate(confirmRow); }}
        destructive
        busy={unpublishM.isPending}
        title="Unpublish this post?"
        confirmLabel={unpublishM.isPending ? "Removing…" : "Unpublish"}
        body={confirmRow ? (
          <div style={{ fontSize: 13, lineHeight: 1.6, color: T.body }}>
            <p style={{ margin: "0 0 10px" }}>
              <strong>{confirmRow.title}</strong> will be removed from {channelName(confirmRow.channel)}
              {confirmRow.channel === "blog" ? " (moved to your WordPress trash)" : ""}.
            </p>
            <p style={{ margin: 0, color: T.muted }}>
              {afterUnpublishSentence(confirmRow)}
            </p>
          </div>
        ) : null}
      />
    </Card>
  );
}

const linkStyle: React.CSSProperties = {
  display: "inline-flex", alignItems: "center", gap: 4, fontSize: 12, fontWeight: 600,
  color: T.ink3, textDecoration: "none", fontFamily: sans, whiteSpace: "nowrap",
};

const unpublishBtn: React.CSSProperties = {
  display: "inline-flex", alignItems: "center", gap: 5, padding: "5px 12px", borderRadius: 999,
  border: `1px solid ${T.redBorder}`, background: T.redSoft, color: T.red, fontSize: 11,
  fontWeight: 600, cursor: "pointer", fontFamily: sans, whiteSpace: "nowrap",
};

export function PublishedList({ onUnpublished }: { onUnpublished?: () => void }) {
  return (
    <ToastProvider>
      <PublishedListInner onUnpublished={onUnpublished} />
    </ToastProvider>
  );
}
