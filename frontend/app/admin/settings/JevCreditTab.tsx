"use client";
// app/admin/settings/JevCreditTab.tsx
// AA-756 — the "Jev Credit" tab of admin Settings. TypeSafe (Jev) has no balance API, so admins
// record each manual top-up here; the card shows total topped up, Jev spend since the first top-up,
// and an *estimated* balance. Alerts: the exhausted alert (AA-720, on a 402) and, since AA-845, a
// low-credit alert (bell + operator e-mail) when the estimate drops below low_threshold_usd.
//
// The Dev preview calls the Dev backend, which may not have the GET endpoint or the
// shared.jev_credit_topup table until Claude deploys + applies migration 209. A 404 must render a
// clean empty state (no crash, no console error), not an error screen.
// AA-994: text in the UI language (messages.ts); notes and API errors stay as sent.

import { useMemo, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { BellRing, Plus, Trash2, Wallet } from "lucide-react";
import {
  Button,
  ConfirmModal,
  EmptyState,
  ErrorState,
  Spinner,
  ToastProvider,
  apiGet,
  apiSend,
  ApiError,
  useFormat,
  useT,
  useToast,
  K,
  Section,
  alpha,
  mono,
  sans,
  serif,
} from "../../_kit";
import { SETTINGS } from "./messages";

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

const usd = (v: number | null | undefined) =>
  v == null ? "—" : `$${v.toLocaleString("en-US", { minimumFractionDigits: 2, maximumFractionDigits: 2 })}`;

// ── Summary stat card ──────────────────────────────────────────────────────────
function Stat({ label, value, hint, tone }: {
  label: string; value: string; hint?: string; tone?: "estimate";
}) {
  const t = useT(SETTINGS);
  return (
    <div style={{ minWidth: 0 }}>
      <div style={{
        fontSize: 11, color: K.muted, marginBottom: 4, textTransform: "uppercase",
        letterSpacing: "0.1em", display: "flex", alignItems: "center", gap: 6,
      }}>
        {label}
        {tone === "estimate" && (
          <span style={{
            fontSize: 9.5, fontWeight: 600, letterSpacing: "0.04em", padding: "1px 6px",
            borderRadius: 999, background: alpha(K.accent, 10), color: K.accentDeep,
          }}>
            {t("estimate")}
          </span>
        )}
      </div>
      <div style={{
        fontFamily: serif, fontSize: 28, fontWeight: 500, color: K.ink,
        letterSpacing: "-0.02em", fontVariantNumeric: "tabular-nums",
      }}>
        {value}
      </div>
      {hint && <div style={{ fontSize: 10.5, color: K.muted2, marginTop: 2 }}>{hint}</div>}
    </div>
  );
}

// ── AA-845: send the two Jev credit alerts as marked tests ─────────────────────────
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
  const t = useT(SETTINGS);
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
      qc.invalidateQueries({ queryKey: ["jev-credit"] });
    },
    onError: (e) => toast.error(e instanceof Error ? e.message : t("testFailed")),
  });
  return (
    <div style={{ display: "flex", alignItems: "center", gap: 10, flexWrap: "wrap", marginTop: 12 }}>
      <Button size="sm" variant="ghost" disabled={mut.isPending} onClick={() => mut.mutate()}>
        {mut.isPending ? <Spinner size={12} /> : <BellRing size={13} />}
        {t("sendTest")}
      </Button>
      <span style={{ fontSize: 11, color: K.muted2 }}>
        {t("sendTestHint")}
      </span>
    </div>
  );
}

// ── Add-top-up form ──────────────────────────────────────────────────────────────
function AddTopupForm() {
  const t = useT(SETTINGS);
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
      qc.invalidateQueries({ queryKey: ["jev-credit"] });
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

  const inp: React.CSSProperties = {
    padding: "7px 10px", borderRadius: 8, border: `1px solid ${K.line}`,
    fontSize: 13, fontFamily: sans, color: K.body, background: K.card, outline: "none",
    minWidth: 0, maxWidth: "100%", boxSizing: "border-box",
  };
  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 8 }}>
      <div style={{ display: "flex", gap: 8, flexWrap: "wrap", alignItems: "flex-end" }}>
        <label style={{ fontSize: 11, color: K.muted, display: "flex", flexDirection: "column", gap: 4 }}>
          {t("date")}
          <input type="date" value={date} onChange={(e) => setDate(e.target.value)}
                 disabled={mut.isPending} style={{ ...inp, width: 150 }} />
        </label>
        <label style={{ fontSize: 11, color: K.muted, display: "flex", flexDirection: "column", gap: 4 }}>
          {t("amountUsd")}
          <input type="number" min={0} step="0.01" value={amount} placeholder="10.00"
                 onChange={(e) => setAmount(e.target.value)} disabled={mut.isPending}
                 style={{ ...inp, width: 120, fontFamily: mono }} />
        </label>
        <label style={{
          fontSize: 11, color: K.muted, display: "flex", flexDirection: "column", gap: 4,
          flex: 1, minWidth: 160,
        }}>
          {t("noteOptional")}
          <input value={note} onChange={(e) => setNote(e.target.value)} disabled={mut.isPending}
                 placeholder={t("notePlaceholder")} style={inp} />
        </label>
        <Button variant="primary" size="sm" disabled={mut.isPending} onClick={submit}>
          {mut.isPending ? <Spinner size={12} /> : <Plus size={13} />}
          {mut.isPending ? t("saving") : t("addTopup")}
        </Button>
      </div>
      {error && <span style={{ fontSize: 11.5, color: K.danger }}>{error}</span>}
    </div>
  );
}

// ── Top-ups table ──────────────────────────────────────────────────────────────
function TopupsTable({ topups }: { topups: Topup[] }) {
  const t = useT(SETTINGS);
  const { formatDateTime } = useFormat();
  const toast = useToast();
  const qc = useQueryClient();
  const [confirming, setConfirming] = useState<Topup | null>(null);

  const del = useMutation({
    mutationFn: (id: number) =>
      apiSend<{ deleted: boolean }>(`/api/admin/decisions/jev-credit/topups/${id}`,
        { method: "DELETE", admin: true }),
    onSuccess: () => {
      toast.success(t("topupDeleted"));
      setConfirming(null);
      qc.invalidateQueries({ queryKey: ["jev-credit"] });
    },
    onError: (e) => {
      toast.error(e instanceof Error ? e.message : t("deleteFailed"));
      setConfirming(null);
    },
  });

  if (topups.length === 0) {
    return (
      <EmptyState
        icon={<Wallet size={30} />}
        title={t("noTopupsTitle")}
        description={t("noTopupsBody")}
      />
    );
  }

  return (
    <>
      <div style={{ overflowX: "auto" }}>
        <table style={{ width: "100%", borderCollapse: "collapse", fontSize: 12.5, minWidth: 520 }}>
          <thead>
            <tr style={{ textAlign: "left", color: K.muted, borderBottom: `1px solid ${K.line}` }}>
              <th style={{ padding: "6px 8px", fontWeight: 600 }}>{t("date")}</th>
              <th style={{ padding: "6px 8px", fontWeight: 600, textAlign: "right" }}>{t("colAmount")}</th>
              <th style={{ padding: "6px 8px", fontWeight: 600 }}>{t("colNote")}</th>
              <th style={{ padding: "6px 8px", fontWeight: 600 }}>{t("colAddedBy")}</th>
              <th style={{ padding: "6px 8px", fontWeight: 600 }} aria-label={t("colActions")} />
            </tr>
          </thead>
          <tbody>
            {topups.map((row) => (
              <tr key={row.id} style={{ borderBottom: `1px solid ${K.line2}` }}>
                <td style={{ padding: "7px 8px", fontFamily: mono, color: K.body }}>{row.topped_up_on}</td>
                <td style={{ padding: "7px 8px", fontFamily: mono, textAlign: "right", color: K.ink }}>
                  {usd(row.amount_usd)}
                </td>
                <td style={{ padding: "7px 8px", color: K.muted }}>{row.note || "—"}</td>
                <td style={{ padding: "7px 8px", color: K.muted2, fontSize: 11.5 }}>
                  <div>{row.created_by || "—"}</div>
                  {row.created_at && (
                    <div style={{ fontSize: 10.5 }}>{formatDateTime(row.created_at)}</div>
                  )}
                </td>
                <td style={{ padding: "7px 8px", textAlign: "right" }}>
                  <Button size="sm" variant="ghost" disabled={del.isPending}
                          onClick={() => setConfirming(row)}>
                    <Trash2 size={13} />
                  </Button>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>

      <ConfirmModal
        open={confirming !== null}
        onClose={() => setConfirming(null)}
        onConfirm={() => confirming && del.mutate(confirming.id)}
        title={t("deleteTitle")}
        confirmLabel={t("delete")}
        destructive
        busy={del.isPending}
        body={
          confirming ? (
            <p style={{ fontSize: 12.5, color: K.muted, margin: 0 }}>
              {t("deleteBody", usd(confirming.amount_usd), confirming.topped_up_on)}
            </p>
          ) : null
        }
      />
    </>
  );
}

// ── Tab body ───────────────────────────────────────────────────────────────────
function JevCreditTabInner() {
  const t = useT(SETTINGS);
  const { formatDateTime } = useFormat();
  const { data, isLoading, isError, error, refetch } = useQuery({
    queryKey: ["jev-credit"],
    queryFn: () => apiGet<JevCreditResp>("/api/admin/decisions/jev-credit", { admin: true }),
    retry: false,
  });

  // Until Claude deploys the endpoint + applies migration 209, the Dev backend returns 404/501 for
  // this route. Treat a missing endpoint/table as "no data yet" (clean empty state), not an error.
  const notDeployed = isError && error instanceof ApiError && (error.status === 404 || error.status === 501);
  const empty = useMemo<JevCreditResp>(() => ({
    topups: [], total_topped_up_usd: null, first_topup_on: null,
    spent_since_first_topup_usd: null, estimated_left_usd: null, last_exhausted_alert_at: null,
  }), []);
  const view = data ?? (notDeployed ? empty : null);

  if (isLoading) {
    return (
      <div style={{ padding: 40, textAlign: "center" }}><Spinner /></div>
    );
  }
  if (isError && !notDeployed) {
    return (
      <Section>
        <ErrorState
          message={error instanceof Error ? error.message : t("loadJevError")}
          onRetry={() => refetch()}
        />
      </Section>
    );
  }
  if (!view) return null;

  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 16 }}>
      <p style={{ fontSize: 12, color: K.muted2, margin: 0 }}>
        {t("introA")} <b>{t("introB")}</b>
        {t("introC", view.low_threshold_usd != null ? ` (${usd(view.low_threshold_usd)})` : "")}
      </p>

      <Section>
        <div style={{
          display: "grid", gridTemplateColumns: "repeat(auto-fit, minmax(130px, 1fr))", gap: 16,
        }}>
          <Stat label={t("totalToppedUp")} value={usd(view.total_topped_up_usd)}
                hint={view.first_topup_on ? t("since", view.first_topup_on) : undefined} />
          <Stat label={t("spentJev")} value={usd(view.spent_since_first_topup_usd)}
                hint="shared.llm_call_log · provider typesafe" />
          <Stat label={t("estimatedLeft")} value={usd(view.estimated_left_usd)} tone="estimate" />
        </div>
        {view.below_low_threshold && (
          <div style={{ fontSize: 11.5, color: K.amber, marginTop: 12 }}>
            {t("belowThreshold", usd(view.low_threshold_usd))}
            {view.last_low_alert_at ? t("lastLowAlert", formatDateTime(view.last_low_alert_at)) : ""}
          </div>
        )}
        {view.last_exhausted_alert_at && (
          <div style={{ fontSize: 11.5, color: K.amber, marginTop: 12 }}>
            {t("lastExhausted", formatDateTime(view.last_exhausted_alert_at))}
          </div>
        )}
        {notDeployed && (
          <div style={{ fontSize: 11.5, color: K.muted2, marginTop: 12 }}>
            {t("notDeployed")}
          </div>
        )}
        {!notDeployed && view.low_threshold_usd != null && <TestAlertsButton />}
      </Section>

      <Section>
        <AddTopupForm />
      </Section>

      <Section>
        <TopupsTable topups={view.topups} />
      </Section>
    </div>
  );
}

export function JevCreditTab() {
  return (
    <ToastProvider>
      <JevCreditTabInner />
    </ToastProvider>
  );
}
