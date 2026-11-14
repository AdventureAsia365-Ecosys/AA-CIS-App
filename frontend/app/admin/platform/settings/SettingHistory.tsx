"use client";
// app/admin/platform/settings/SettingHistory.tsx — the history dialog of Platform › Settings (moved out of
// SettingsRegistry.tsx in the AA-1096 S280 UX fix round).
//
// Every version of the key at one scope (the platform, or one tenant: HistoryScope, which also lists the open tenant
// proposals with a Review link), newest first: status (an approved change dated ahead shows "Scheduled"), the value it
// replaced and the new one (in the page's words), "Effective from" ("when approved" for an open proposal without a
// date; a decided one the later of its date and its approval, lib effectiveShown), who proposed it and why, who
// decided it. An open proposal of the scope is approved or rejected here by a person who may (canApprove: the
// administrator; on the CRM tab only a CRM platform administrator).

import { useQuery } from "@tanstack/react-query";
import { useMemo, useState } from "react";
import {
  Badge, Button, DataTable, DateText, ErrorState, Field, K, Modal, Textarea, mono, useT, useToast, type ColumnDef,
  type Tone,
} from "../../../_kit";
import {
  decisionBody, decisionPath, effectiveShown, formatValue, historyPath, historyTenant, isScheduled, type HistoryResp,
  type SettingItem, type SettingRow,
} from "@/lib/settingsRegistry";
import { SETTINGS } from "./messages";
import { ActorName } from "./SettingGuide";
import { HistoryScope } from "./SettingScope";
import { getJson, post } from "./settingsApi";
import { useSettingWords } from "./useSettingWords";

const small = { fontSize: 12, color: K.muted, lineHeight: 1.45 } as const;
type StatusLabel = "statusProposed" | "statusApproved" | "statusRejected" | "statusSuperseded" | "statusScheduled";
const STATUS: Record<SettingRow["status"], { tone: Tone; label: StatusLabel }> = {
  proposed: { tone: "info", label: "statusProposed" },
  approved: { tone: "success", label: "statusApproved" },
  rejected: { tone: "danger", label: "statusRejected" },
  superseded: { tone: "neutral", label: "statusSuperseded" },
};

export function StatusPill({ row }: { row: Pick<SettingRow, "status" | "effective_from"> }) {
  const t = useT(SETTINGS);
  const s = isScheduled(row) ? { tone: "accent" as Tone, label: "statusScheduled" as StatusLabel } : STATUS[row.status];
  return <Badge tone={s.tone} dot={false}>{t(s.label)}</Badge>;
}

function useHistoryColumns(item: SettingItem): ColumnDef<SettingRow, unknown>[] {
  const t = useT(SETTINGS);
  const { words } = useSettingWords();
  return useMemo<ColumnDef<SettingRow, unknown>[]>(() => [
    { id: "version", header: t("colVersion"), accessorKey: "version", meta: { align: "right" } },
    { id: "status", header: t("colStatus"), accessorKey: "status",
      cell: ({ row: { original: r } }) => (
        <span style={{ display: "inline-flex", gap: 4, flexWrap: "wrap" }}>
          <StatusPill row={r} />
          {r.self_approved && <Badge tone="warning" dot={false}>{t("selfApproved")}</Badge>}
        </span>
      ) },
    { id: "was", header: t("colWas"), accessorFn: (r) => formatValue(r.previous_value, item, words),
      cell: ({ getValue }) => <span style={{ fontFamily: mono, fontSize: 12 }}>{String(getValue())}</span> },
    { id: "now", header: t("colNow"), accessorFn: (r) => formatValue(r.value, item, words),
      cell: ({ getValue }) => <span style={{ fontFamily: mono, fontSize: 12 }}>{String(getValue())}</span> },
    { id: "effective", header: t("colEffective"), accessorFn: (r) => effectiveShown(r) ?? "",
      cell: ({ row: { original: r } }) => {
        const at = effectiveShown(r);
        return <span style={small}>
          {at ? <DateText value={at} withTime /> : r.status === "proposed" ? t("whenApproved") : "—"}</span>;
      } },
    { id: "who", header: t("colWho"), accessorFn: (r) => r.proposed_by_name ?? r.proposed_by,
      cell: ({ row: { original: r } }) => <span style={small}><ActorName actor={r.proposed_by}
        name={r.proposed_by_name} /><br /><DateText value={r.proposed_at} withTime /></span> },
    { id: "reason", header: t("colReason"), accessorKey: "reason",
      cell: ({ row: { original: r } }) => <span style={small}>{r.reason}{r.evidence_ref && <><br />{r.evidence_ref}</>}
      </span> },
    { id: "decided", header: t("colDecided"), accessorFn: (r) => r.decided_at ?? "",
      cell: ({ row: { original: r } }) => r.decided_at
        ? <span style={small}><ActorName actor={r.decided_by} name={r.decided_by_name} /><br />
          <DateText value={r.decided_at} withTime />
          {r.decision_note && <><br />{r.decision_note}</>}</span>
        : <span style={small}>—</span> },
  ], [t, item, words]);
}

/** The rows of the scope, an empty note, or the load error with a retry. */
function HistoryRows({ item, rows, error, onRetry }: {
  item: SettingItem; rows: SettingRow[] | undefined; error: Error | null; onRetry: () => void;
}) {
  const t = useT(SETTINGS);
  const columns = useHistoryColumns(item);
  if (error) return <ErrorState message={`${t("loadError")}: ${error.message}`} onRetry={onRetry} />;
  if (!rows) return null;
  if (rows.length === 0) return <div style={small}>{t("noHistory")}</div>;
  return (
    <DataTable<SettingRow> tableId="settings-history-table" data={rows} columns={columns}
      getRowId={(r) => r.id} searchable={false} paginate={false} compact tableMinWidth={860} />
  );
}

function DecisionButtons({ busy, note, onDecide }: { busy: boolean; note: string; onDecide: (approve: boolean) => void }) {
  const t = useT(SETTINGS);
  return (
    <>
      <Button variant="secondary" disabled={busy || note.trim().length < 3} onClick={() => onDecide(false)}>
        {t("reject")}</Button>
      <Button variant="primary" disabled={busy} onClick={() => onDecide(true)} testId="settings-approve">
        {t("approve")}</Button>
    </>
  );
}

export function HistoryModal({ item, canApprove, onClose, onDone }: {
  item: SettingItem; canApprove: boolean; onClose: () => void; onDone: () => void;
}) {
  const t = useT(SETTINGS);
  const toast = useToast();
  // AA-1096 (S280): whose rows (the platform, or one tenant); Review opens on the scope of the open proposal.
  const [tenant, setTenant] = useState<string | null>(() => historyTenant(item));
  const q = useQuery({ queryKey: ["settings-history", item.key, tenant],
    queryFn: () => getJson<HistoryResp>(historyPath(item.key, tenant)) });
  const [note, setNote] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const open = (q.data?.history ?? []).find((r) => r.status === "proposed");
  const decide = async (approve: boolean) => {
    if (!open || busy) return;
    setBusy(true);
    setError(null);
    const out = await post(decisionPath(open.id, approve), decisionBody(item, open, note),
      { network: t("network"), failed: (s) => t("failed", s), readOnly: t("readOnly") });
    setBusy(false);
    if (!out.ok) {
      setError(out.message);
      return;
    }
    toast.success(approve ? t("approved") : t("rejected"), 6000);
    onDone();
  };
  const deciding = !!open && canApprove;
  return (
    <Modal open onClose={onClose} title={t("historyTitle", item.label)} width={960} pinFooter testId="settings-history"
      footer={<>
        {deciding && <DecisionButtons busy={busy} note={note} onDecide={(approve) => void decide(approve)} />}
        <Button variant="secondary" onClick={onClose}>{t("close")}</Button>
      </>}>
      <div style={{ display: "grid", gridTemplateColumns: "minmax(0, 1fr)", gap: 12 }}>
        <div style={small}>{t("keyLabel")}: <span style={{ fontFamily: mono }}>{item.key}</span>
          {item.env_override && <> · {t("envLabel", item.env_override)}</>}</div>
        <HistoryScope item={item} tenant={tenant} onTenant={setTenant} />
        <HistoryRows item={item} rows={q.data?.history} error={q.isError ? (q.error as Error) : null}
          onRetry={() => void q.refetch()} />
        {deciding && (
          <Field label={t("note")} hint={t("noteHint")}>
            <Textarea rows={2} value={note} onChange={(e) => setNote(e.target.value)} />
          </Field>
        )}
        {error && <div role="alert" style={{ ...small, color: K.danger }}>{error}</div>}
      </div>
    </Modal>
  );
}
