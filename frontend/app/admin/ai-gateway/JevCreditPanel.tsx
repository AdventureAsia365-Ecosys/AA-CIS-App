"use client";
// app/admin/ai-gateway/JevCreditPanel.tsx
// AA-756 — the Jev credit ledger: TypeSafe (Jev) has no balance API, so admins record each manual top-up here; the
// panel shows total topped up, Jev spend since the first top-up, and an *estimated* balance. Alerts: the exhausted
// alert (AA-720, on a 402) and, since AA-845, a low-credit alert (bell + operator e-mail) when the estimate drops
// below low_threshold_usd. AA-804 UI part 2 moved it from Settings › Jev Credit to AI providers › Providers › Jev
// credit (the old link is sent on here) and rebuilt it on the kit (KpiTile, Field/Input/DateField, DataTable).
//
// A backend without the GET endpoint or the shared.jev_credit_topup table (migration 209) answers 404/501: that
// renders a clean empty state, not an error screen.
// AA-994: text in the UI language (jevCredit.messages.ts); notes and API errors stay as sent.

import { useMemo, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { BellRing, Plus, Trash2, Wallet } from "lucide-react";
import {
  ApiError, Button, ConfirmModal, DataTable, DateField, EmptyState, ErrorState, Field, IconButton, Input, K, KpiTile,
  Section, Spinner, apiGet, apiSend, mono, useFormat, useT, useToast, type ColumnDef,
} from "../../_kit";
import { JEV_CREDIT } from "./jevCredit.messages";

interface Topup {
  id: number;
  topped_up_on: string;
  amount_usd: number;
  note: string | null;
  created_by: string | null;
  created_at: string | null;
}

interface JevCreditResp {
  topups: Topup[];
  total_topped_up_usd: number | null;
  first_topup_on: string | null;
  spent_since_first_topup_usd: number | null;
  estimated_left_usd: number | null;
  last_exhausted_alert_at: string | null;
  // AA-845 — optional so an older backend (without the low-credit alert) still renders.
  low_threshold_usd?: number | null;
  below_low_threshold?: boolean;
  last_low_alert_at?: string | null;
}

export const JEV_CREDIT_KEY = ["jev-credit"] as const;

const usd = (v: number | null | undefined) =>
  v == null ? "—" : `$${v.toLocaleString("en-US", { minimumFractionDigits: 2, maximumFractionDigits: 2 })}`;

const small = { fontSize: 12, color: K.muted2, lineHeight: 1.5 } as const;

// ── AA-845: send the two Jev credit alerts as marked tests ─────────────────────────────────────────────────────────
// POST /admin/decisions/jev-credit/check with a test threshold: one "[TEST]" low-credit bell + e-mail and one
// "[TEST]" exhausted bell + e-mail, through the same code as the real alerts. Test alerts never throttle a real
// one; the e-mails are capped at one per alert type per UTC day.
interface CheckResp {
  estimated_left_usd: number | null;
  alerted: boolean;
  email_job_id: string | null;
  exhausted_test_alerted: boolean;
}

function TestAlertsButton() {
  const t = useT(JEV_CREDIT);
  const toast = useToast();
  const qc = useQueryClient();
  const mut = useMutation({
    mutationFn: () => apiSend<CheckResp>("/api/admin/decisions/jev-credit/check", {
      method: "POST", body: { threshold_usd: 1000, test_exhausted_alert: true }, admin: true,
    }),
    onSuccess: (r) => {
      // email_job_id is the low-credit test e-mail only; the exhausted test e-mail is queued by the same path.
      const mail = r.email_job_id ? t("lowMailQueued") : t("lowMailSkipped");
      toast.success(t("testSent", r.alerted, r.exhausted_test_alerted, mail));
      qc.invalidateQueries({ queryKey: JEV_CREDIT_KEY });
    },
    onError: (e) => toast.error(e instanceof Error ? e.message : t("testFailed")),
  });
  return (
    <div style={{ display: "flex", alignItems: "center", gap: 10, flexWrap: "wrap" }}>
      <Button size="sm" variant="ghost" disabled={mut.isPending} onClick={() => mut.mutate()}>
        {mut.isPending ? <Spinner size={12} /> : <BellRing size={13} />}
        {t("sendTest")}
      </Button>
      <span style={small}>{t("sendTestHint")}</span>
    </div>
  );
}

// ── Add-top-up form ────────────────────────────────────────────────────────────────────────────────────────────────
function AddTopupForm() {
  const t = useT(JEV_CREDIT);
  const toast = useToast();
  const qc = useQueryClient();
  const [date, setDate] = useState(() => new Date().toISOString().slice(0, 10));
  const [amount, setAmount] = useState("");
  const [note, setNote] = useState("");
  const [error, setError] = useState("");

  const mut = useMutation({
    mutationFn: (body: { topped_up_on: string; amount_usd: number; note?: string }) =>
      apiSend<Topup>("/api/admin/decisions/jev-credit/topups", { method: "POST", body, admin: true }),
    onSuccess: () => {
      toast.success(t("topupRecorded"));
      setAmount("");
      setNote("");
      setError("");
      qc.invalidateQueries({ queryKey: JEV_CREDIT_KEY });
    },
    onError: (e) => setError(e instanceof Error ? e.message : t("saveFailed")),
  });

  function submit() {
    const amt = Number(amount);
    if (!amount || !Number.isFinite(amt) || amt <= 0) {
      setError(t("amountPositive"));
      return;
    }
    setError("");
    mut.mutate({ topped_up_on: date, amount_usd: amt, note: note.trim() || undefined });
  }

  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 10 }}>
      <div style={{ display: "flex", gap: 12, flexWrap: "wrap", alignItems: "flex-end" }}>
        <Field label={t("date")} style={{ flex: "0 1 170px" }}>
          <DateField value={date} onChange={setDate} disabled={mut.isPending} aria-label={t("date")} />
        </Field>
        <Field label={t("amountUsd")} style={{ flex: "0 1 140px" }}>
          <Input type="number" min={0} step="0.01" value={amount} placeholder="10.00" inputMode="decimal"
            onChange={(e) => setAmount(e.target.value)} disabled={mut.isPending} style={{ fontFamily: mono }} />
        </Field>
        <Field label={t("noteOptional")} style={{ flex: "1 1 200px" }}>
          <Input value={note} onChange={(e) => setNote(e.target.value)} disabled={mut.isPending}
            placeholder={t("notePlaceholder")} />
        </Field>
        <Button variant="primary" disabled={mut.isPending} onClick={submit}>
          {mut.isPending ? <Spinner size={12} /> : <Plus size={13} />}
          {mut.isPending ? t("saving") : t("addTopup")}
        </Button>
      </div>
      {error && <span role="alert" style={{ fontSize: 12, color: K.danger }}>{error}</span>}
    </div>
  );
}

// ── Top-ups table ──────────────────────────────────────────────────────────────────────────────────────────────────
function TopupsTable({ topups }: { topups: Topup[] }) {
  const t = useT(JEV_CREDIT);
  const { formatDateTime } = useFormat();
  const toast = useToast();
  const qc = useQueryClient();
  const [confirming, setConfirming] = useState<Topup | null>(null);

  const del = useMutation({
    mutationFn: (id: number) =>
      apiSend<{ deleted: boolean }>(`/api/admin/decisions/jev-credit/topups/${id}`, { method: "DELETE", admin: true }),
    onSuccess: () => {
      toast.success(t("topupDeleted"));
      setConfirming(null);
      qc.invalidateQueries({ queryKey: JEV_CREDIT_KEY });
    },
    onError: (e) => {
      toast.error(e instanceof Error ? e.message : t("deleteFailed"));
      setConfirming(null);
    },
  });

  const columns = useMemo<ColumnDef<Topup, unknown>[]>(() => [
    { accessorKey: "topped_up_on", header: t("date"),
      cell: (c) => <span style={{ fontFamily: mono }}>{c.row.original.topped_up_on}</span> },
    { accessorKey: "amount_usd", header: t("colAmount"), meta: { align: "right" },
      cell: (c) => <span style={{ fontFamily: mono, color: K.ink }}>{usd(c.row.original.amount_usd)}</span> },
    { accessorKey: "note", header: t("colNote"), enableSorting: false,
      cell: (c) => <span style={{ color: K.muted }}>{c.row.original.note || "—"}</span> },
    { id: "added", header: t("colAddedBy"), accessorFn: (r) => r.created_at ?? "",
      cell: (c) => (
        <div style={{ fontSize: 12, color: K.muted2 }}>
          <div>{c.row.original.created_by || "—"}</div>
          {c.row.original.created_at && <div>{formatDateTime(c.row.original.created_at)}</div>}
        </div>
      ) },
    { id: "actions", header: "", enableSorting: false, enableHiding: false, meta: { align: "right" },
      cell: (c) => (
        <IconButton size="sm" variant="plain" tone="danger" label={t("deleteTitle")} disabled={del.isPending}
          onClick={() => setConfirming(c.row.original)}>
          <Trash2 size={13} />
        </IconButton>
      ) },
  ], [t, formatDateTime, del.isPending]);

  if (topups.length === 0) {
    return <EmptyState icon={<Wallet size={30} />} title={t("noTopupsTitle")} description={t("noTopupsBody")} />;
  }

  return (
    <>
      <DataTable<Topup>
        tableId="ai-gateway-jev-topups" data={topups} columns={columns} getRowId={(r) => String(r.id)}
        searchable={false} paginate={false} stickyHeader={false} tableMinWidth={560}
        emptyTitle={t("noTopupsTitle")}
      />
      <ConfirmModal
        open={confirming !== null}
        onClose={() => setConfirming(null)}
        onConfirm={() => confirming && del.mutate(confirming.id)}
        title={t("deleteTitle")}
        confirmLabel={t("delete")}
        destructive
        busy={del.isPending}
        body={confirming ? t("deleteBody", usd(confirming.amount_usd), confirming.topped_up_on) : null}
      />
    </>
  );
}

// ── the panel ──────────────────────────────────────────────────────────────────────────────────────────────────────
export function JevCreditPanel() {
  const t = useT(JEV_CREDIT);
  const { formatDateTime } = useFormat();
  const { data, isLoading, isError, error, refetch } = useQuery({
    queryKey: JEV_CREDIT_KEY,
    queryFn: () => apiGet<JevCreditResp>("/api/admin/decisions/jev-credit", { admin: true }),
    retry: false,
  });

  // A backend without the endpoint / table answers 404 or 501: "no data yet" (clean empty state), not an error.
  const notDeployed = isError && error instanceof ApiError && (error.status === 404 || error.status === 501);
  const empty = useMemo<JevCreditResp>(() => ({
    topups: [], total_topped_up_usd: null, first_topup_on: null,
    spent_since_first_topup_usd: null, estimated_left_usd: null, last_exhausted_alert_at: null,
  }), []);
  const view = data ?? (notDeployed ? empty : null);

  if (isLoading) return <div style={{ padding: 40, textAlign: "center" }}><Spinner /></div>;
  if (isError && !notDeployed) {
    return (
      <ErrorState message={error instanceof Error ? error.message : t("loadJevError")} onRetry={() => refetch()} />
    );
  }
  if (!view) return null;

  return (
    <div data-testid="ai-gateway-jev-credit"
      style={{ display: "grid", gridTemplateColumns: "minmax(0, 1fr)", gap: 16 }}>
      <p style={{ ...small, margin: 0 }}>
        {t("introA")} <b>{t("introB")}</b>
        {t("introC", view.low_threshold_usd != null ? ` (${usd(view.low_threshold_usd)})` : "")}
      </p>
      <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fit, minmax(min(200px, 100%), 1fr))", gap: 12 }}>
        <KpiTile label={t("totalToppedUp")} value={usd(view.total_topped_up_usd)}
          hint={view.first_topup_on ? t("since", view.first_topup_on) : undefined} />
        <KpiTile label={t("spentJev")} value={usd(view.spent_since_first_topup_usd)}
          hint={t("spentJevHint")} />
        <KpiTile label={t("estimatedLeft")} value={usd(view.estimated_left_usd)} hint={t("estimate")}
          valueTone={view.below_low_threshold ? "danger" : undefined} />
      </div>
      {(view.below_low_threshold || view.last_exhausted_alert_at || notDeployed) && (
        <div role="status" style={{ display: "grid", gap: 6, fontSize: 12, color: notDeployed ? K.muted2 : K.amber }}>
          {view.below_low_threshold && (
            <span>
              {t("belowThreshold", usd(view.low_threshold_usd))}
              {view.last_low_alert_at ? t("lastLowAlert", formatDateTime(view.last_low_alert_at)) : ""}
            </span>
          )}
          {view.last_exhausted_alert_at && <span>{t("lastExhausted", formatDateTime(view.last_exhausted_alert_at))}</span>}
          {notDeployed && <span>{t("notDeployed")}</span>}
        </div>
      )}
      {!notDeployed && view.low_threshold_usd != null && <TestAlertsButton />}
      <Section title={t("addTopup")}>
        <AddTopupForm />
      </Section>
      <Section padded={false}>
        <TopupsTable topups={view.topups} />
      </Section>
    </div>
  );
}
