"use client";
// app/admin/ai-gateway/ProviderCreditPanel.tsx — AA-1025 B5: the Credit view of AI providers › Providers.
//
// The vendor APIs (OpenAI, Anthropic) have no balance API the gateway may call, so an admin records balance reads and
// top-ups here (GET / POST / DELETE /api/admin/ai-gateway/credit…, backend shared/provider_credit.py) and the page
// shows the estimate: the newest balance read (or the first top-up) plus the top-ups after it, minus the spend logged
// since then. The low-balance threshold is the connection's low_balance_usd, saved through the existing
// PATCH /api/admin/llm-connections/{key}. Below it the backend raises one bell and one e-mail a day (also from the
// api sampler every 30 minutes). A backend without the ledger (migration 342) answers 503: a clean notice.
// Jev keeps its own ledger (JevCreditPanel).
// Like every write control of the page (AA-1025 C1 follow-up), the add form, the delete buttons and Check now show only
// when GET /api/admin/ai-gateway/access says this session may write the ledger (usePageAccess "credit", spend.write on
// the API), and the threshold form only when it may edit connections ("connections", ai.admin): a read-only session
// (the Dev E2E admin, a viewer) sees the figures and one line saying why. The API still answers 403 to a write it refuses.

import { useMemo, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { BellRing, Plus, Trash2, Wallet } from "lucide-react";
import {
  ApiError, Button, ConfirmModal, DataTable, DateField, EmptyState, ErrorState, Field, IconButton, Input, K, KpiTile,
  Section, SegmentedControl, Spinner, apiGet, apiSend, mono, useFormat, useT, useToast, type ColumnDef,
} from "../../_kit";
import {
  CREDIT_API, creditEntryBody, thresholdValue, usd, type CreditConnection, type CreditEntry, type CreditEntryKind,
  type CreditResp,
} from "@/lib/aiGateway";
import { CREDIT } from "./credit.messages";
import { usePageAccess } from "./usePageAccess";
// The Providers overview query (ProvidersTab PROVIDERS_KEY), named here to keep the two modules free of a cycle.
const PROVIDERS_KEY = ["ai-gateway", "providers"] as const;

export const CREDIT_KEY = ["ai-gateway", "credit"] as const;

const small = { fontSize: 12, color: K.muted2, lineHeight: 1.5 } as const;

function useRefresh() {
  const qc = useQueryClient();
  return () => {
    qc.invalidateQueries({ queryKey: [...CREDIT_KEY] });
    qc.invalidateQueries({ queryKey: [...PROVIDERS_KEY] });
    qc.invalidateQueries({ queryKey: ["llm-connections"] });
  };
}

// S280 (AA-1096): the placeholder says "no alert" (it showed 5.00, read as an alert in force), the stored value is shown
// beside the field, Save needs a change, and a tighten-only role (S279: lowering or removing a low-balance alert
// loosens it) is told so before the API refuses it.
function thresholdState(text: string, stored: number | null | undefined, tightenOnly: boolean) {
  const value = thresholdValue(text);
  const now = stored ?? null;
  const changed = value !== undefined && value !== now;
  // A tighten-only role may raise the alert floor only (S279: lowering or removing it loosens the alert).
  const loosens = tightenOnly && now != null && value !== undefined && (value === null || value < now);
  return { value, changed, loosens };
}

function useThresholdHint(stored: number | null | undefined, tightenOnly: boolean): string {
  const t = useT(CREDIT);
  const now = t("thresholdNow", stored == null ? t("noAlert") : usd(stored));
  return `${now} ${tightenOnly && stored != null ? t("thresholdTighten") : t("thresholdHint")}`;
}

function ThresholdForm({ c }: { c: CreditConnection }) {
  const t = useT(CREDIT);
  const toast = useToast();
  const refresh = useRefresh();
  const { tightenOnly } = usePageAccess();
  const [text, setText] = useState(c.low_balance_usd == null ? "" : String(c.low_balance_usd));
  const [error, setError] = useState("");
  const { value, changed, loosens } = thresholdState(text, c.low_balance_usd, tightenOnly);
  const hint = useThresholdHint(c.low_balance_usd, tightenOnly);
  const mut = useMutation({
    mutationFn: (value: number | null) => apiSend(`/api/admin/llm-connections/${encodeURIComponent(c.connection_key)}`,
      { method: "PATCH", body: { low_balance_usd: value }, admin: true }),
    onSuccess: () => { toast.success(t("thresholdSaved")); setError(""); refresh(); },
    onError: (e) => setError(e instanceof Error ? e.message : t("saveFailed")),
  });
  function save() {
    if (value === undefined) { setError(t("thresholdBad")); return; }
    if (loosens) { setError(t("thresholdTighten")); return; }
    mut.mutate(value);
  }
  return (
    <div style={{ display: "flex", gap: 12, flexWrap: "wrap", alignItems: "flex-end" }}>
      <Field label={t("threshold")} style={{ flex: "0 1 240px" }} hint={hint}>
        <Input type="number" min={0} step="0.01" value={text} inputMode="decimal" placeholder={t("noAlert")}
          onChange={(e) => setText(e.target.value)} disabled={mut.isPending} style={{ fontFamily: mono }}
          data-testid={`credit-threshold-${c.connection_key}`} />
      </Field>
      <Button variant="secondary" disabled={mut.isPending || !changed || loosens} onClick={save}>
        {mut.isPending ? <Spinner size={12} /> : null}{t("saveThreshold")}
      </Button>
      {error && <span role="alert" style={{ fontSize: 12, color: K.danger }}>{error}</span>}
    </div>
  );
}

function AddEntryForm({ c }: { c: CreditConnection }) {
  const t = useT(CREDIT);
  const toast = useToast();
  const refresh = useRefresh();
  const [kind, setKind] = useState<CreditEntryKind>(c.entries.length ? "topup" : "opening_balance");
  const [date, setDate] = useState("");
  const [amount, setAmount] = useState("");
  const [note, setNote] = useState("");
  const [error, setError] = useState("");
  const mut = useMutation({
    mutationFn: (body: object) => apiSend<{ check: { alerted?: boolean } | null }>(
      `${CREDIT_API}/${encodeURIComponent(c.connection_key)}/entries`, { method: "POST", body, admin: true }),
    onSuccess: (r) => {
      toast.success(r?.check?.alerted ? t("entryAddedAlert") : t("entryAdded"));
      setAmount(""); setNote(""); setError("");
      refresh();
    },
    onError: (e) => setError(e instanceof Error ? e.message : t("saveFailed")),
  });
  function submit() {
    const body = creditEntryBody(kind, amount, date, note);
    if (body === "amount") { setError(t("amountBad")); return; }
    if (body === "date") { setError(t("dateBad")); return; }
    setError("");
    mut.mutate(body);
  }
  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 10 }}>
      <SegmentedControl<CreditEntryKind> label={t("kindLabel")} value={kind} onChange={setKind} size="sm"
        testId={`credit-kind-${c.connection_key}`}
        options={[{ value: "opening_balance", label: t("kindOpening") }, { value: "topup", label: t("kindTopup") }]} />
      <div style={{ display: "flex", gap: 12, flexWrap: "wrap", alignItems: "flex-end" }}>
        <Field label={t("date")} style={{ flex: "0 1 170px" }}>
          <DateField value={date} onChange={setDate} disabled={mut.isPending} aria-label={t("date")} />
        </Field>
        <Field label={t("amountUsd")} style={{ flex: "0 1 140px" }}>
          <Input type="number" min={0} step="0.01" value={amount} placeholder="10.00" inputMode="decimal"
            onChange={(e) => setAmount(e.target.value)} disabled={mut.isPending} style={{ fontFamily: mono }}
            data-testid={`credit-amount-${c.connection_key}`} />
        </Field>
        <Field label={t("noteOptional")} style={{ flex: "1 1 200px" }}>
          <Input value={note} onChange={(e) => setNote(e.target.value)} disabled={mut.isPending}
            placeholder={t("notePlaceholder")} maxLength={500} />
        </Field>
        <Button variant="primary" disabled={mut.isPending} onClick={submit} testId={`credit-add-${c.connection_key}`}>
          {mut.isPending ? <Spinner size={12} /> : <Plus size={13} />}
          {mut.isPending ? t("saving") : t("add")}
        </Button>
      </div>
      {error && <span role="alert" style={{ fontSize: 12, color: K.danger }}>{error}</span>}
    </div>
  );
}

function EntriesTable({ c, canEdit }: { c: CreditConnection; canEdit: boolean }) {
  const t = useT(CREDIT);
  const { formatDateTime } = useFormat();
  const toast = useToast();
  const refresh = useRefresh();
  const [confirming, setConfirming] = useState<CreditEntry | null>(null);
  const del = useMutation({
    mutationFn: (id: number) => apiSend(`${CREDIT_API}/entries/${id}`, { method: "DELETE", admin: true }),
    onSuccess: () => { toast.success(t("entryDeleted")); setConfirming(null); refresh(); },
    onError: (e) => { toast.error(e instanceof Error ? e.message : t("deleteFailed")); setConfirming(null); },
  });
  const columns = useMemo<ColumnDef<CreditEntry, unknown>[]>(() => {
    const cols: ColumnDef<CreditEntry, unknown>[] = [
    { accessorKey: "effective_at", header: t("colDate"),
      cell: (x) => <span style={{ fontFamily: mono }}>{formatDateTime(x.row.original.effective_at)}</span> },
    { accessorKey: "kind", header: t("colType"),
      cell: (x) => <span>{x.row.original.kind === "opening_balance" ? t("kindOpening") : t("kindTopup")}</span> },
    { accessorKey: "amount_usd", header: t("colAmount"), meta: { align: "right" },
      cell: (x) => <span style={{ fontFamily: mono, color: K.ink }}>{usd(x.row.original.amount_usd, 2)}</span> },
    { accessorKey: "note", header: t("colNote"), enableSorting: false,
      cell: (x) => <span style={{ color: K.muted }}>{x.row.original.note || "—"}</span> },
    { id: "added", header: t("colAddedBy"), accessorFn: (r) => r.created_at ?? "",
      cell: (x) => <span style={small}>{x.row.original.created_by || "—"}</span> },
    ];
    if (canEdit) {
      cols.push({ id: "actions", header: "", enableSorting: false, enableHiding: false, meta: { align: "right" },
        cell: (x) => (
          <IconButton size="sm" variant="plain" tone="danger" label={t("deleteTitle")} disabled={del.isPending}
            onClick={() => setConfirming(x.row.original)}>
            <Trash2 size={13} />
          </IconButton>
        ) });
    }
    return cols;
  }, [t, formatDateTime, del.isPending, canEdit]);
  if (c.entries.length === 0) {
    return <EmptyState icon={<Wallet size={30} />} title={t("noEntriesTitle")} description={t("noEntriesBody")} />;
  }
  return (
    <>
      <DataTable<CreditEntry>
        tableId={`ai-gateway-credit-${c.connection_key}`} data={c.entries} columns={columns}
        getRowId={(r) => String(r.id)} searchable={false} paginate={false} stickyHeader={false} tableMinWidth={560}
        emptyTitle={t("noEntriesTitle")}
      />
      <ConfirmModal open={confirming !== null} onClose={() => setConfirming(null)}
        onConfirm={() => confirming && del.mutate(confirming.id)} title={t("deleteTitle")} confirmLabel={t("delete")}
        destructive busy={del.isPending}
        body={confirming ? t("deleteBody", usd(confirming.amount_usd, 2), formatDateTime(confirming.effective_at)) : null}
      />
    </>
  );
}

function ConnectionCredit({ c, canEdit, canThreshold }: {
  c: CreditConnection; canEdit: boolean; canThreshold: boolean;
}) {
  const t = useT(CREDIT);
  const { formatDateTime } = useFormat();
  return (
    <Section title={`${c.label}${c.enabled ? "" : ` (${t("off")})`}`}>
      <div data-testid={`credit-connection-${c.connection_key}`}
        style={{ display: "grid", gridTemplateColumns: "minmax(0, 1fr)", gap: 14 }}>
        <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fit, minmax(min(200px, 100%), 1fr))", gap: 12 }}>
          <KpiTile label={t("recorded")} value={usd(c.recorded_usd, 2)}
            hint={c.since ? t("since", formatDateTime(c.since)) : undefined} />
          <KpiTile label={t("spent")} value={usd(c.spent_usd)} hint={t("spentHint")} />
          <KpiTile label={t("estimatedLeft")} value={usd(c.estimated_left_usd, 2)} hint={t("estimate")}
            valueTone={c.below_threshold ? "danger" : undefined} />
        </div>
        {c.below_threshold && (
          <span role="status" style={{ fontSize: 12, color: K.amber }}>
            {t("belowThreshold", usd(c.low_balance_usd, 2))}
            {c.last_low_alert_at ? t("lastAlert", formatDateTime(c.last_low_alert_at)) : ""}
          </span>
        )}
        {canThreshold
          ? <ThresholdForm c={c} />
          : <span style={small} data-testid={`credit-threshold-view-${c.connection_key}`}>
            {c.low_balance_usd == null ? t("thresholdNone") : t("thresholdView", usd(c.low_balance_usd, 2))}</span>}
        {canEdit && (
          <>
            <span style={{ ...small, fontWeight: 600, color: K.ink }}>{t("addEntry")}</span>
            <AddEntryForm c={c} />
          </>
        )}
        <EntriesTable c={c} canEdit={canEdit} />
      </div>
    </Section>
  );
}

function CheckNowButton() {
  const t = useT(CREDIT);
  const toast = useToast();
  const refresh = useRefresh();
  const mut = useMutation({
    mutationFn: () => apiSend<{ results: { alerted?: boolean }[] }>(`${CREDIT_API}/check`,
      { method: "POST", body: {}, admin: true }),
    onSuccess: (r) => {
      const results = r?.results ?? [];
      toast.success(t("checked", results.length, results.filter((x) => x.alerted).length));
      refresh();
    },
    onError: (e) => toast.error(e instanceof Error ? e.message : t("saveFailed")),
  });
  return (
    <div style={{ display: "flex", alignItems: "center", gap: 10, flexWrap: "wrap" }}>
      <Button size="sm" variant="ghost" disabled={mut.isPending} onClick={() => mut.mutate()} testId="credit-check-now">
        {mut.isPending ? <Spinner size={12} /> : <BellRing size={13} />}
        {t("checkNow")}
      </Button>
      <span style={small}>{t("checkHint")}</span>
    </div>
  );
}

/** The Credit view: one section per vendor API connection whose balance comes from this ledger. */
export function ProviderCreditPanel() {
  const t = useT(CREDIT);
  const access = usePageAccess();
  const canEdit = access.can("credit");
  const canThreshold = access.can("connections");
  const { data, isLoading, isError, error, refetch } = useQuery({
    queryKey: [...CREDIT_KEY],
    queryFn: () => apiGet<CreditResp>(CREDIT_API, { admin: true }),
    retry: false,
  });
  const notDeployed = isError && error instanceof ApiError && [404, 501, 503].includes(error.status);
  if (isLoading) return <div style={{ padding: 40, textAlign: "center" }}><Spinner /></div>;
  if (isError && !notDeployed) {
    return <ErrorState message={error instanceof Error ? error.message : t("loadError")} onRetry={() => refetch()} />;
  }
  const connections = data?.connections ?? [];
  return (
    <div data-testid="ai-gateway-credit" style={{ display: "grid", gridTemplateColumns: "minmax(0, 1fr)", gap: 16 }}>
      <p style={{ ...small, margin: 0 }}>{t("intro")}</p>
      {notDeployed && <span role="status" style={{ fontSize: 12, color: K.muted2 }}>{t("notDeployed")}</span>}
      {access.readOnly && (
        <div role="status" style={small} data-testid="ai-gateway-credit-read-only">{t("readOnly")}</div>
      )}
      {canEdit && !notDeployed && connections.length > 0 && <CheckNowButton />}
      {!notDeployed && connections.length === 0 && (
        <EmptyState icon={<Wallet size={30} />} title={t("noConnections")} description={t("noConnectionsBody")} />
      )}
      {connections.map((c) => (
        <ConnectionCredit key={c.connection_key} c={c} canEdit={canEdit} canThreshold={canThreshold} />
      ))}
    </div>
  );
}
