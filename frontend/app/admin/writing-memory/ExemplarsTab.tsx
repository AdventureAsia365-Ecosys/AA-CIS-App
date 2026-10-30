"use client";
// app/admin/writing-memory/ExemplarsTab.tsx — AA-921 WM-29: Writing memory › Exemplars (app ADR 0010, E3 / D12).
//
// What it shows:
//   Pool        every voice example (masked text) per owner, writer and channel, with its status (candidate, pinned,
//               never use) and whether the next rules draft of that writer and owner would pin it. Filters: writer,
//               status, owner (Platform or one tenant). Header actions: Find candidates (dry run) and Add candidates
//               (the refresh from the "use as example" signals that passed the fact filter; $0, no model call).
//   Next draft  with a writer and an owner picked: what a rules draft would pin now, per channel (the backend's own
//               selection: GET /exemplars/selection), so a pin change shows before the draft is made.
//   Drawer      one example: the masked text, its source, its last change, and Pin / Unpin / Never use with a
//               required reason (each change audited; ids only).
// Every action is a backend route (api/routers/admin_writing_memory.py, ai.admin, a signed-in admin id); the buttons
// show only for a role with ai.admin (lib/rbac.ts, a hint: the backend re-checks every call). Admin only: the text is
// tenant text, so nothing here is ever shown to a tenant.

import { useQuery, useQueryClient } from "@tanstack/react-query";
import { RefreshCw, Search } from "lucide-react";
import { useCallback, useMemo, useState } from "react";
import {
  Badge, Button, DataTable, Drawer, ErrorState, Field, Input, K, Select, TableSkeleton, apiGet, mono,
  useAdminIdentity, useFormat, useT, useToast, type ColumnDef,
} from "../../_kit";
import { can } from "@/lib/rbac";
import { TenantSelect, useTenants } from "../_components/TenantSelect";
import {
  EXEMPLARS_PER_CHANNEL, EXEMPLAR_STATUSES, EXEMPLAR_WRITERS, WM_API, byChannel, exemplarActions, exemplarTone,
  type Exemplar, type ExemplarAction, type ExemplarSelection, type ExemplarsPage, type RefreshResult,
} from "@/lib/writingMemory";
import { EXEMPLAR_STATUS, X } from "./exemplars.messages";
import { useWmText } from "./useWmText";

const PLATFORM = "__platform__";

function short(id: string | null | undefined, n = 8): string {
  return id ? id.slice(0, n) : "—";
}

function ownerQuery(owner: string): string {
  if (!owner) return "";
  return owner === PLATFORM ? "&scope=platform" : `&scope=tenant&tenant_id=${encodeURIComponent(owner)}`;
}

// ── next draft preview ───────────────────────────────────────────────────────────────────────────────────────────

function NextDraft({ writer, owner }: { writer: string; owner: string }) {
  const t = useT(X);
  const ready = !!writer && !!owner;
  const q = useQuery({ queryKey: ["wm", "exemplar-selection", writer, owner], enabled: ready,
    queryFn: () => apiGet<ExemplarSelection>(`${WM_API}/exemplars/selection?writer=${writer}${ownerQuery(owner)}`) });
  return (
    <section style={{ border: `1px solid ${K.line}`, borderRadius: 10, padding: 12, background: K.card }}
      data-testid="wm-next-draft">
      <h2 style={{ fontSize: 14, margin: "0 0 4px" }}>{t("nextTitle")}</h2>
      <div style={{ fontSize: 12.5, color: K.muted, marginBottom: 8 }}>
        {ready ? t("nextHelp", q.data?.per_channel ?? EXEMPLARS_PER_CHANNEL) : t("nextPick")}</div>
      {ready && (q.error ? <ErrorState message={(q.error as Error).message} onRetry={() => q.refetch()} />
        : q.isLoading ? <TableSkeleton rows={2} cols={2} />
        : !q.data?.items.length ? <div style={{ fontSize: 12.5, color: K.muted }}>{t("nextEmpty")}</div>
        : <div style={{ display: "grid", gap: 8 }}>
          {byChannel(q.data.items).map(([channel, items]) => (
            <div key={channel}>
              <div style={{ fontSize: 11.5, color: K.muted, fontFamily: mono, marginBottom: 4 }}>{channel}</div>
              {items.map((e) => (
                <div key={e.exemplar_id} style={{ fontSize: 12.5, lineHeight: 1.45, marginBottom: 4, minWidth: 0,
                  overflowWrap: "anywhere" }}>
                  <strong>{e.position}.</strong> {e.masked_text}
                </div>
              ))}
            </div>
          ))}
        </div>)}
    </section>
  );
}

// ── one example ──────────────────────────────────────────────────────────────────────────────────────────────────

function ExemplarDrawer({ ex, onClose, canAdmin, ownerOf }: {
  ex: Exemplar | null; onClose: () => void; canAdmin: boolean; ownerOf: (e: Exemplar) => string;
}) {
  const t = useT(X);
  const status = useT(EXEMPLAR_STATUS);
  const toast = useToast();
  const qc = useQueryClient();
  const wm = useWmText();
  const { formatDateTime } = useFormat();
  const [reason, setReason] = useState("");
  const [busy, setBusy] = useState<ExemplarAction | null>(null);
  const close = useCallback(() => { setReason(""); onClose(); }, [onClose]);

  async function act(action: ExemplarAction) {
    if (!ex) return;
    setBusy(action);
    const r = await wm.send<Exemplar>(`/exemplars/${ex.exemplar_id}/${action}`, "POST", { reason: reason.trim() });
    setBusy(null);
    if (r.ok) {
      toast.success(action === "pin" ? t("pinned") : action === "unpin" ? t("unpinned") : t("nevered"));
      qc.invalidateQueries({ queryKey: ["wm"] });
      close();
    } else toast.error(r.message);
  }

  const label = { pin: t("pin"), unpin: t("unpin"), never: t("never") };
  return (
    <Drawer open={!!ex} onClose={close} width={560} title={ex ? t("drawerTitle", ex.writer, ex.channel) : ""}>
      {ex && (
        <div style={{ display: "grid", gap: 12, fontSize: 13 }}>
          <div style={{ display: "flex", gap: 6, flexWrap: "wrap", alignItems: "center" }}>
            <Badge tone={ex.scope === "platform" ? "purple" : "info"}>{ownerOf(ex)}</Badge>
            <Badge tone={exemplarTone(ex.status)} dot={false}>{status(ex.status)}</Badge>
            {ex.in_next_draft && <Badge tone="success" dot={false}>{t("inNextDraft")}</Badge>}
            <span style={{ color: K.muted, fontSize: 12 }}>{t("words", ex.word_count)}</span>
          </div>
          <div style={{ lineHeight: 1.55, overflowWrap: "anywhere", background: K.panel, borderRadius: 8,
            padding: 10 }}>{ex.masked_text}</div>
          <div style={{ fontSize: 12, color: K.muted }}>
            {t("source")}: {ex.source_table} <span style={{ fontFamily: mono }}>{short(ex.source_id)}</span>
            {" · "}{formatDateTime(ex.created_at)}
          </div>
          {ex.status_at && (
            <div style={{ fontSize: 12, color: K.muted }}>
              {t("lastChange")} ({formatDateTime(ex.status_at)}): {t("lastChangeBy", ex.status_by ?? "—",
                ex.status_reason ?? "—")}
            </div>
          )}
          {canAdmin ? (
            <div style={{ display: "grid", gap: 8 }}>
              <Field label={t("reasonLabel")} required hint={t("reasonHint")}>
                <Input value={reason} onChange={(e) => setReason(e.target.value)} maxLength={500} />
              </Field>
              <div style={{ display: "flex", gap: 6, flexWrap: "wrap" }}>
                {exemplarActions(ex.status).map((a) => (
                  <Button key={a} size="sm" variant={a === "never" ? "danger" : a === "pin" ? "primary" : "secondary"}
                    disabled={reason.trim().length < 3 || !!busy} onClick={() => act(a)}>{label[a]}</Button>
                ))}
              </div>
            </div>
          ) : <div style={{ fontSize: 12, color: K.muted }}>{t("readOnly")}</div>}
        </div>
      )}
    </Drawer>
  );
}

// ── the tab ──────────────────────────────────────────────────────────────────────────────────────────────────────

export default function ExemplarsTab() {
  const t = useT(X);
  const status = useT(EXEMPLAR_STATUS);
  const toast = useToast();
  const qc = useQueryClient();
  const wm = useWmText();
  const { formatDateTime } = useFormat();
  const canAdmin = can(useAdminIdentity().role, "ai.admin");
  const { byId, label } = useTenants();
  const ownerOf = useCallback((e: Exemplar) => wm.owner(e.scope, e.tenant_id,
    e.tenant_id && byId.has(e.tenant_id) ? label(e.tenant_id) : undefined), [byId, label, wm]);
  const [writer, setWriter] = useState("");
  const [statusFilter, setStatusFilter] = useState("");
  const [owner, setOwner] = useState("");
  const [open, setOpen] = useState<Exemplar | null>(null);
  const [busy, setBusy] = useState(false);
  const list = useQuery({ queryKey: ["wm", "exemplars", writer, statusFilter, owner],
    queryFn: () => apiGet<ExemplarsPage>(`${WM_API}/exemplars?limit=200${writer ? `&writer=${writer}` : ""}`
      + `${statusFilter ? `&status=${statusFilter}` : ""}${ownerQuery(owner)}`) });

  async function refresh(dryRun: boolean) {
    setBusy(true);
    const r = await wm.send<RefreshResult>("/exemplars/refresh", "POST", { dry_run: dryRun });
    setBusy(false);
    if (r.ok) {
      toast.success(dryRun ? t("refreshDry", r.data.added, r.data.marked_never)
        : t("refreshDone", r.data.added, r.data.marked_never));
      if (!dryRun) qc.invalidateQueries({ queryKey: ["wm"] });
    } else toast.error(r.message);
  }

  const cols = useMemo<ColumnDef<Exemplar, unknown>[]>(() => [
    { id: "owner", accessorFn: (e) => ownerOf(e), header: t("colOwner"),
      cell: (c) => <Badge tone={c.row.original.scope === "platform" ? "purple" : "info"}>
        {ownerOf(c.row.original)}</Badge> },
    { accessorKey: "writer", header: t("colWriter"),
      cell: (c) => <span style={{ fontFamily: mono }}>{c.row.original.writer}</span> },
    { accessorKey: "channel", header: t("colChannel"),
      cell: (c) => <span style={{ fontFamily: mono }}>{c.row.original.channel}</span> },
    { id: "text", header: t("colExample"), enableSorting: false,
      cell: (c) => <div style={{ fontSize: 12, lineHeight: 1.45, maxWidth: 520, overflowWrap: "anywhere" }}>
        {c.row.original.masked_text.length > 220 ? `${c.row.original.masked_text.slice(0, 220)}…`
          : c.row.original.masked_text}</div> },
    { accessorKey: "status", header: t("colStatus"),
      cell: (c) => <Badge tone={exemplarTone(c.row.original.status)} dot={false}>
        {status(c.row.original.status)}</Badge> },
    { id: "next", accessorFn: (e) => (e.in_next_draft ? 1 : 0), header: t("colNextDraft"),
      cell: (c) => (c.row.original.in_next_draft ? <Badge tone="success" dot={false}>{t("inNextDraft")}</Badge>
        : t("notInNextDraft")) },
    { accessorKey: "created_at", header: t("colAdded"), cell: (c) => formatDateTime(c.row.original.created_at) },
  ], [t, status, ownerOf, formatDateTime]);

  return (
    <div style={{ display: "grid", gap: 14, minWidth: 0 }}>
      <div style={{ display: "flex", gap: 8, flexWrap: "wrap", alignItems: "center", justifyContent: "space-between" }}>
        <div style={{ fontSize: 12.5, color: K.muted, maxWidth: 760 }}>{t("help")}</div>
        {canAdmin && (
          <div style={{ display: "flex", gap: 6, flexWrap: "wrap" }}>
            <Button variant="ghost" onClick={() => refresh(true)} disabled={busy} title={t("refreshDryRunTitle")}>
              <Search size={14} /> {t("refreshDryRun")}</Button>
            <Button onClick={() => refresh(false)} disabled={busy} title={t("refreshTitle")}>
              <RefreshCw size={14} /> {t("refresh")}</Button>
          </div>
        )}
      </div>
      <NextDraft writer={writer} owner={owner} />
      {list.error ? <ErrorState message={(list.error as Error).message} onRetry={() => list.refetch()} /> : (
        <DataTable tableId="wm-exemplars" data={list.data?.items ?? []} columns={cols}
          getRowId={(e) => e.exemplar_id} loading={list.isLoading} onRowClick={(e) => setOpen(e)} tableMinWidth={980}
          emptyTitle={t("emptyTitle")} emptyDescription={t("emptyDesc")}
          toolbarExtra={<div style={{ display: "flex", gap: 6, flexWrap: "wrap", alignItems: "center" }}>
            <Select size="sm" value={writer} onChange={(e) => setWriter(e.target.value)} aria-label={t("writerAria")}
              placeholder={t("everyWriter")} options={EXEMPLAR_WRITERS.map((w) => ({ value: w, label: w }))} />
            <Select size="sm" value={statusFilter} onChange={(e) => setStatusFilter(e.target.value)}
              aria-label={t("statusAria")} placeholder={t("everyStatus")}
              options={EXEMPLAR_STATUSES.map((s) => ({ value: s, label: status(s) }))} />
            <TenantSelect inline hideLabel label={t("ownerLabel")} value={owner} onChange={(id) => setOwner(id)}
              placeholder={t("everyOwner")} extraOptions={[{ value: PLATFORM, label: t("platform") }]}
              testId="wm-exemplar-owner" />
          </div>} />
      )}
      <ExemplarDrawer ex={open} onClose={() => setOpen(null)} canAdmin={canAdmin} ownerOf={ownerOf} />
    </div>
  );
}
