"use client";
// app/admin/ai-gateway/BatchesTab.tsx — AA-1076: the Batches tab of AI providers (the gateway batch lane).
//
// One read, GET /api/admin/ai-gateway/batches (backend api/services/ai_gateway_batches.py): every gateway batch (an
// `llm_batch` job) with where it runs (provider, model, connection), its status, the outcome of its requests, the
// estimate against its cap, the spend our ledger holds for it, the same requests at the standard price and what that
// saved, and where the replies went (the normal S1 / A3 jobs). Two writes, both ai.admin (usePageAccess "batches"):
// Send a batch (POST /batches; "estimate only" is on by default and costs nothing) and Cancel (POST
// /batches/{job_id}/cancel). Kit components only; the copy is in batches.messages.ts.

import { useCallback, useMemo, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Send, XCircle } from "lucide-react";
import {
  Badge, Button, Checkbox, ConfirmModal, DataTable, DateText, Drawer, ErrorState, Field, Input, K, KpiTile, Section,
  Select, Textarea, apiGet, apiSend, mono, useT, useToast, type ColumnDef,
} from "../../_kit";
import {
  BATCHES_API, BATCHES_KEY, BATCH_DRAFT, batchCancelPath, batchPhaseTone, batchSendBody, isReadOnlyRefusal,
  parseTourIds, usd, type BatchConsumer, type BatchDraft, type BatchRow, type BatchesResp,
} from "@/lib/aiGateway";
import { BATCHES } from "./batches.messages";
import { usePageAccess } from "./usePageAccess";

const small = { fontSize: 12, color: K.muted, lineHeight: 1.45 } as const;
const TABLE_MIN_WIDTH = 1080;

function SendDrawer({ onClose }: { onClose: () => void }) {
  const t = useT(BATCHES);
  const toast = useToast();
  const qc = useQueryClient();
  const [draft, setDraft] = useState<BatchDraft>(BATCH_DRAFT);
  const [confirming, setConfirming] = useState(false);
  const [apiError, setApiError] = useState("");
  const set = (p: Partial<BatchDraft>) => setDraft((d) => ({ ...d, ...p }));
  const body = batchSendBody(draft);
  const tourCount = parseTourIds(draft.tours).ids.length;
  const toursError = body === "tours" && draft.tours.trim() !== "" ? t("errTours")
    : body === "tour_ids_bad" ? t("errToursBad") : body === "too_many" ? t("errTooMany") : undefined;

  const mut = useMutation({
    mutationFn: () => apiSend(BATCHES_API, { method: "POST", body, admin: true }),
    onSuccess: () => {
      toast.success(draft.dryRun ? t("estimateQueued") : t("sent"));
      qc.invalidateQueries({ queryKey: [...BATCHES_KEY] });
      onClose();
    },
    onError: (e) => {
      setConfirming(false);
      setApiError(isReadOnlyRefusal(e) ? t("readOnly") : e instanceof Error ? e.message : String(e));
    },
  });
  const blocked = typeof body === "string" || mut.isPending;

  return (
    <Drawer open onClose={onClose} title={t("sendTitle")} width={560}
      footer={
        <div style={{ display: "flex", gap: 8, justifyContent: "flex-end" }}>
          <Button variant="ghost" onClick={onClose} disabled={mut.isPending}>{t("cancel")}</Button>
          <Button variant="primary" disabled={blocked} testId="batch-send-submit"
            onClick={() => (draft.dryRun ? mut.mutate() : setConfirming(true))}>
            {mut.isPending ? t("sending") : t("send")}
          </Button>
        </div>
      }>
      <div data-testid="batch-send-form" style={{ display: "flex", flexDirection: "column", gap: 14 }}>
        <span style={small}>{t("sendHelp")}</span>
        <Field label={t("fieldConsumer")}>
          <Select value={draft.consumer} disabled={mut.isPending}
            onChange={(e) => set({ consumer: e.target.value as BatchConsumer })}
            options={[{ value: "s1_attempt1", label: t("consumerS1") }, { value: "a3_atomize", label: t("consumerA3") }]} />
        </Field>
        <Field label={t("fieldTours")} hint={t("toursHint")} error={toursError} required>
          <Textarea value={draft.tours} rows={5} disabled={mut.isPending} style={{ fontFamily: mono, fontSize: 12 }}
            onChange={(e) => set({ tours: e.target.value })} data-testid="batch-send-tours" />
        </Field>
        <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fit, minmax(min(100%, 200px), 1fr))", gap: 12 }}>
          <Field label={t("fieldCap")} hint={t("capHint")} error={body === "cap" ? t("errCap") : undefined} required>
            <Input inputMode="decimal" value={draft.cap} disabled={mut.isPending} style={{ fontFamily: mono }}
              onChange={(e) => set({ cap: e.target.value })} data-testid="batch-send-cap" />
          </Field>
          <Field label={t("fieldRunBatch")} hint={t("runBatchHint")}
            error={body === "run_batch" ? t("errRunBatch") : undefined}>
            <Input value={draft.runBatchId} disabled={mut.isPending || draft.consumer !== "s1_attempt1"}
              style={{ fontFamily: mono, fontSize: 12 }} onChange={(e) => set({ runBatchId: e.target.value })} />
          </Field>
        </div>
        <Checkbox checked={draft.dryRun} disabled={mut.isPending} label={t("dryRunLabel")}
          onChange={(e) => set({ dryRun: e.target.checked })} data-testid="batch-send-dry-run" />
        {draft.consumer === "s1_attempt1" && (
          <Checkbox checked={draft.drill} disabled={mut.isPending} label={t("drillLabel")}
            onChange={(e) => set({ drill: e.target.checked })} />
        )}
        {apiError && <span role="alert" style={{ fontSize: 12, color: K.danger }}>{apiError}</span>}
      </div>
      <ConfirmModal open={confirming} onClose={() => setConfirming(false)} onConfirm={() => mut.mutate()}
        title={t("confirmTitle")} body={t("confirmBody", tourCount, usd(Number(draft.cap), 2))}
        confirmLabel={t("confirmSend")} busy={mut.isPending} testId="batch-send-confirm" />
    </Drawer>
  );
}

function outcomeText(o: Record<string, number>): string {
  const parts = Object.entries(o).filter(([, n]) => n > 0).map(([k, n]) => `${n} ${k}`);
  return parts.length ? parts.join(" · ") : "—";
}

export default function BatchesTab() {
  const t = useT(BATCHES);
  const toast = useToast();
  const qc = useQueryClient();
  const canSend = usePageAccess().can("batches");
  const [sending, setSending] = useState(false);
  const [cancelling, setCancelling] = useState<BatchRow | null>(null);
  const q = useQuery({
    queryKey: [...BATCHES_KEY],
    queryFn: () => apiGet<BatchesResp>(BATCHES_API, { admin: true }),
    refetchInterval: 60_000,
    refetchIntervalInBackground: false,
  });
  const cancel = useMutation({
    mutationFn: (row: BatchRow) => apiSend(batchCancelPath(row.job_id), { method: "POST", admin: true }),
    onSuccess: () => {
      toast.success(t("cancelled"));
      setCancelling(null);
      qc.invalidateQueries({ queryKey: [...BATCHES_KEY] });
    },
    onError: (e) => {
      setCancelling(null);
      toast.error(isReadOnlyRefusal(e) ? t("readOnly") : e instanceof Error ? e.message : String(e));
    },
  });

  const basisText = useCallback((r: BatchRow) => {
    if (r.estimate_basis !== "ledger_p95") return t("basisWorst");
    return r.worst_case_usd == null ? t("basisMeasured")
      : `${t("basisMeasured")} · ${t("worstCase", usd(r.worst_case_usd))}`;
  }, [t]);

  const columns = useMemo<ColumnDef<BatchRow, unknown>[]>(() => {
    const cols: ColumnDef<BatchRow, unknown>[] = [
      { id: "batch", header: t("colBatch"), accessorFn: (r) => r.consumer ?? "",
        cell: ({ row: { original: r } }) => (
          <div style={{ display: "grid", gap: 2, minWidth: 150 }}>
            <span style={{ fontFamily: mono, fontSize: 12, color: K.ink }}>{r.consumer ?? "—"}</span>
            <span style={small}>{t("tours", r.tours)}{r.dry_run ? ` · ${t("dryRun")}` : ""}</span>
          </div>
        ) },
      { id: "where", header: t("colWhere"), accessorFn: (r) => r.model_key ?? "",
        cell: ({ row: { original: r } }) => (
          <div style={{ display: "grid", gap: 2, minWidth: 150 }}>
            <span style={{ fontFamily: mono, fontSize: 12, color: K.ink }}>{r.model_key ?? "—"}</span>
            <span style={small}>{[r.stage, r.connection_key].filter(Boolean).join(" · ") || "—"}</span>
          </div>
        ) },
      { id: "phase", header: t("colPhase"), accessorFn: (r) => r.phase ?? r.job_status,
        cell: ({ row: { original: r } }) => (
          <div style={{ display: "grid", gap: 2, minWidth: 130 }}>
            <Badge tone={batchPhaseTone(r)} dot={false} style={{ whiteSpace: "nowrap", justifySelf: "start" }}>
              {r.phase ?? r.job_status}</Badge>
            {r.provider_status && <span style={small}>{r.provider_status}</span>}
            {r.next_check_at && (
              <span style={small}>{t("nextCheck")} <DateText value={r.next_check_at} relative /></span>
            )}
            {(r.reason || r.error) && <span style={{ ...small, color: K.danger }}>{r.reason ?? r.error}</span>}
          </div>
        ) },
      { id: "requests", header: t("colRequests"), accessorFn: (r) => r.requests ?? -1, meta: { align: "right" },
        cell: (c) => <span style={{ fontFamily: mono }}>{c.row.original.requests ?? "—"}</span> },
      { id: "outcomes", header: t("colOutcomes"), enableSorting: false,
        cell: (c) => <span style={small}>{outcomeText(c.row.original.outcomes)}</span> },
      { id: "cap", header: t("colCap"), accessorFn: (r) => r.estimate_usd ?? -1, meta: { align: "right" },
        cell: ({ row: { original: r } }) => (
          <div style={{ display: "grid", gap: 2, justifyItems: "end" }}>
            <span style={{ fontFamily: mono, whiteSpace: "nowrap" }}>{usd(r.estimate_usd)} / {usd(r.cap_usd)}</span>
            {r.estimate_basis && <span style={small}>{basisText(r)}</span>}
          </div>
        ) },
      { id: "spend", header: t("colSpend"), accessorFn: (r) => r.spend_usd, meta: { align: "right" },
        cell: ({ row: { original: r } }) => (
          <div style={{ display: "grid", gap: 2, justifyItems: "end" }}>
            <span style={{ fontFamily: mono, color: K.ink }}>{usd(r.spend_usd)}</span>
            {r.spend_usd > 0 && <span style={small}>{usd(r.standard_usd)} · −{usd(r.saved_usd)}</span>}
          </div>
        ) },
      { id: "handoff", header: t("colHandoff"), enableSorting: false,
        cell: ({ row: { original: r } }) => {
          const h = r.handoff ?? {};
          const text = h.kind === "s1_rewrite" ? t("handoffS1", h.seeded ?? 0, h.on_demand ?? 0)
            : h.kind === "a3_atomize" ? t("handoffA3", h.seeded_days ?? 0) : "—";
          return (
            <div style={{ display: "grid", gap: 2, minWidth: 140 }}>
              <span style={small}>{text}</span>
              {h.drill_withheld && h.drill_withheld.length > 0 && (
                <Badge tone="info" dot={false} style={{ justifySelf: "start" }}>{t("drillWithheld")}</Badge>
              )}
            </div>
          );
        } },
      { id: "when", header: t("colWhen"), accessorFn: (r) => r.created_at ?? "",
        cell: (c) => <span style={{ whiteSpace: "nowrap" }}><DateText value={c.row.original.created_at} relative /></span> },
    ];
    if (canSend) {
      cols.push({ id: "actions", header: "", enableSorting: false, enableHiding: false, meta: { align: "right" },
        cell: ({ row: { original: r } }) => (["queued", "running"].includes(r.job_status) ? (
          <Button variant="ghost" size="sm" onClick={() => setCancelling(r)} testId={`batch-cancel-${r.job_id}`}>
            <XCircle size={14} /> {t("cancelBatch")}</Button>
        ) : null) });
    }
    return cols;
  }, [t, canSend, basisText]);

  const totals = q.data?.totals;
  const loading = q.isLoading;
  return (
    <div data-testid="ai-gateway-batches-ready" style={{ display: "grid", gridTemplateColumns: "minmax(0, 1fr)", gap: 16 }}>
      <p style={{ fontSize: 12.5, color: K.muted, margin: 0 }}>{t("intro")}</p>
      {q.data?.lane_closed_reason && (
        <div role="status" style={{ ...small, color: K.danger }}>{t("laneOff", q.data.lane_closed_reason)}</div>
      )}
      <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fit, minmax(min(100%, 180px), 1fr))", gap: 12 }}>
        <KpiTile size="sm" label={t("totalSpend")} info={t("infoSpend")}
          value={loading ? undefined : usd(totals?.spend_usd ?? 0)} />
        <KpiTile size="sm" label={t("totalStandard")} info={t("infoStandard")}
          value={loading ? undefined : usd(totals?.standard_usd ?? 0)} />
        <KpiTile size="sm" label={t("totalSaved")} valueTone="success"
          value={loading ? undefined : usd(totals?.saved_usd ?? 0)} />
        <KpiTile size="sm" label={t("totalOpen")} value={loading ? undefined : (totals?.open ?? 0)} />
      </div>
      <Section title={t("tableTitle")} description={t("tableHelp")} padded={false}
        actions={canSend ? (
          <Button variant="primary" size="sm" onClick={() => setSending(true)} testId="batch-send-open">
            <Send size={14} /> {t("send")}</Button>
        ) : undefined}>
        {q.error ? (
          <ErrorState message={`${t("loadError")}: ${(q.error as Error).message}`} onRetry={() => q.refetch()} />
        ) : (
          <DataTable<BatchRow>
            tableId="ai-gateway-batches" data={q.data?.batches ?? []} columns={columns} getRowId={(r) => r.job_id}
            loading={loading} searchable={false} pageSize={25} pageSizeOptions={[25, 50, 100]} stickyHeader={false}
            tableMinWidth={TABLE_MIN_WIDTH} emptyTitle={t("empty")} emptyDescription={t("emptyBody")} />
        )}
      </Section>
      {sending && <SendDrawer onClose={() => setSending(false)} />}
      <ConfirmModal open={cancelling != null} onClose={() => setCancelling(null)}
        onConfirm={() => cancelling && cancel.mutate(cancelling)} title={t("cancelTitle")} body={t("cancelBody")}
        confirmLabel={t("cancelConfirm")} busy={cancel.isPending} destructive testId="batch-cancel-confirm" />
    </div>
  );
}
