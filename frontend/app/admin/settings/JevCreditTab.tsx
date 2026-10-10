"use client";
// app/admin/settings/JevCreditTab.tsx
// AA-756 — the "Jev Credit" tab of admin Settings. TypeSafe (Jev) has no balance API, so admins
// record each manual top-up here; the card shows total topped up, Jev spend since the first top-up,
// and an *estimated* balance. The only alert is the exhausted alert (AA-720) — there is no
// "running low" alert (Nghiệp, 10/10).
//
// The Dev preview calls the Dev backend, which may not have the GET endpoint or the
// shared.jev_credit_topup table until Claude deploys + applies migration 209. A 404 must render a
// clean empty state (no crash, no console error), not an error screen.

import { useMemo, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Plus, Trash2, Wallet } from "lucide-react";
import { A, alpha, serif, sans, mono, Card } from "../_components/adminUi";
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
  formatDateTime,
  useToast,
} from "../../_kit";

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
}

const usd = (v: number | null | undefined) =>
  v == null ? "—" : `$${v.toLocaleString("en-US", { minimumFractionDigits: 2, maximumFractionDigits: 2 })}`;

// ── Summary stat card ──────────────────────────────────────────────────────────
function Stat({ label, value, hint, tone }: {
  label: string; value: string; hint?: string; tone?: "estimate";
}) {
  return (
    <div style={{ minWidth: 0 }}>
      <div style={{
        fontSize: 11, color: A.muted, marginBottom: 4, textTransform: "uppercase",
        letterSpacing: "0.1em", display: "flex", alignItems: "center", gap: 6,
      }}>
        {label}
        {tone === "estimate" && (
          <span style={{
            fontSize: 9.5, fontWeight: 600, letterSpacing: "0.04em", padding: "1px 6px",
            borderRadius: 999, background: alpha(A.accent, 10), color: A.accentDeep,
          }}>
            estimate
          </span>
        )}
      </div>
      <div style={{
        fontFamily: serif, fontSize: 28, fontWeight: 500, color: A.ink,
        letterSpacing: "-0.02em", fontVariantNumeric: "tabular-nums",
      }}>
        {value}
      </div>
      {hint && <div style={{ fontSize: 10.5, color: A.muted2, marginTop: 2 }}>{hint}</div>}
    </div>
  );
}

// ── Add-top-up form ──────────────────────────────────────────────────────────────
function AddTopupForm() {
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
      toast.success("Top-up recorded");
      setAmount("");
      setNote("");
      setError("");
      qc.invalidateQueries({ queryKey: ["jev-credit"] });
    },
    onError: (e) => setError(e instanceof Error ? e.message : "Save failed"),
  });

  function submit() {
    const amt = Number(amount);
    if (!amount || !Number.isFinite(amt) || amt <= 0) {
      setError("Amount must be a positive number");
      return;
    }
    setError("");
    mut.mutate({ topped_up_on: date, amount_usd: amt, note: note.trim() || undefined });
  }

  const inp: React.CSSProperties = {
    padding: "7px 10px", borderRadius: 8, border: `1px solid ${A.line}`,
    fontSize: 13, fontFamily: sans, color: A.body, background: A.card, outline: "none",
    minWidth: 0, maxWidth: "100%", boxSizing: "border-box",
  };
  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 8 }}>
      <div style={{ display: "flex", gap: 8, flexWrap: "wrap", alignItems: "flex-end" }}>
        <label style={{ fontSize: 11, color: A.muted, display: "flex", flexDirection: "column", gap: 4 }}>
          Date
          <input type="date" value={date} onChange={(e) => setDate(e.target.value)}
                 disabled={mut.isPending} style={{ ...inp, width: 150 }} />
        </label>
        <label style={{ fontSize: 11, color: A.muted, display: "flex", flexDirection: "column", gap: 4 }}>
          Amount (USD)
          <input type="number" min={0} step="0.01" value={amount} placeholder="10.00"
                 onChange={(e) => setAmount(e.target.value)} disabled={mut.isPending}
                 style={{ ...inp, width: 120, fontFamily: mono }} />
        </label>
        <label style={{
          fontSize: 11, color: A.muted, display: "flex", flexDirection: "column", gap: 4,
          flex: 1, minWidth: 160,
        }}>
          Note (optional)
          <input value={note} onChange={(e) => setNote(e.target.value)} disabled={mut.isPending}
                 placeholder="e.g. topped up via Ms. Thư" style={inp} />
        </label>
        <Button variant="primary" size="sm" disabled={mut.isPending} onClick={submit}>
          {mut.isPending ? <Spinner size={12} /> : <Plus size={13} />}
          {mut.isPending ? "Saving…" : "Add top-up"}
        </Button>
      </div>
      {error && <span style={{ fontSize: 11.5, color: A.red }}>{error}</span>}
    </div>
  );
}

// ── Top-ups table ──────────────────────────────────────────────────────────────
function TopupsTable({ topups }: { topups: Topup[] }) {
  const toast = useToast();
  const qc = useQueryClient();
  const [confirming, setConfirming] = useState<Topup | null>(null);

  const del = useMutation({
    mutationFn: (id: number) =>
      apiSend<{ deleted: boolean }>(`/api/admin/decisions/jev-credit/topups/${id}`,
        { method: "DELETE", admin: true }),
    onSuccess: () => {
      toast.success("Top-up deleted");
      setConfirming(null);
      qc.invalidateQueries({ queryKey: ["jev-credit"] });
    },
    onError: (e) => {
      toast.error(e instanceof Error ? e.message : "Delete failed");
      setConfirming(null);
    },
  });

  if (topups.length === 0) {
    return (
      <EmptyState
        icon={<Wallet size={30} />}
        title="No top-ups recorded yet"
        description="Add a top-up above to track Jev (TypeSafe) credit and see an estimated balance."
      />
    );
  }

  return (
    <>
      <div style={{ overflowX: "auto" }}>
        <table style={{ width: "100%", borderCollapse: "collapse", fontSize: 12.5, minWidth: 520 }}>
          <thead>
            <tr style={{ textAlign: "left", color: A.muted, borderBottom: `1px solid ${A.line}` }}>
              <th style={{ padding: "6px 8px", fontWeight: 600 }}>Date</th>
              <th style={{ padding: "6px 8px", fontWeight: 600, textAlign: "right" }}>Amount</th>
              <th style={{ padding: "6px 8px", fontWeight: 600 }}>Note</th>
              <th style={{ padding: "6px 8px", fontWeight: 600 }}>Added by</th>
              <th style={{ padding: "6px 8px", fontWeight: 600 }} aria-label="actions" />
            </tr>
          </thead>
          <tbody>
            {topups.map((t) => (
              <tr key={t.id} style={{ borderBottom: `1px solid ${A.line2}` }}>
                <td style={{ padding: "7px 8px", fontFamily: mono, color: A.body }}>{t.topped_up_on}</td>
                <td style={{ padding: "7px 8px", fontFamily: mono, textAlign: "right", color: A.ink }}>
                  {usd(t.amount_usd)}
                </td>
                <td style={{ padding: "7px 8px", color: A.muted }}>{t.note || "—"}</td>
                <td style={{ padding: "7px 8px", color: A.muted2, fontSize: 11.5 }}>
                  <div>{t.created_by || "—"}</div>
                  {t.created_at && (
                    <div style={{ fontSize: 10.5 }}>{formatDateTime(t.created_at)}</div>
                  )}
                </td>
                <td style={{ padding: "7px 8px", textAlign: "right" }}>
                  <Button size="sm" variant="ghost" disabled={del.isPending}
                          onClick={() => setConfirming(t)}>
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
        title="Delete this top-up?"
        confirmLabel="Delete"
        destructive
        busy={del.isPending}
        body={
          confirming ? (
            <p style={{ fontSize: 12.5, color: A.muted, margin: 0 }}>
              Remove the {usd(confirming.amount_usd)} top-up from {confirming.topped_up_on}? This only
              changes the recorded balance estimate; it does not touch the TypeSafe account.
            </p>
          ) : null
        }
      />
    </>
  );
}

// ── Tab body ───────────────────────────────────────────────────────────────────
function JevCreditTabInner() {
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
      <Card>
        <ErrorState
          message={error instanceof Error ? error.message : "Failed to load Jev credit"}
          onRetry={() => refetch()}
        />
      </Card>
    );
  }
  if (!view) return null;

  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 16 }}>
      <p style={{ fontSize: 12, color: A.muted2, margin: 0 }}>
        Jev (TypeSafe) has no balance API, so top-ups are recorded here by hand. The balance below is
        an <b>estimate</b>: total topped up minus Jev spend since the first top-up. The only automatic
        alert is when credit is exhausted — there is no &quot;running low&quot; warning.
      </p>

      <Card>
        <div style={{
          display: "grid", gridTemplateColumns: "repeat(auto-fit, minmax(130px, 1fr))", gap: 16,
        }}>
          <Stat label="Total topped up" value={usd(view.total_topped_up_usd)}
                hint={view.first_topup_on ? `since ${view.first_topup_on}` : undefined} />
          <Stat label="Spent (Jev)" value={usd(view.spent_since_first_topup_usd)}
                hint="shared.llm_call_log · provider typesafe" />
          <Stat label="Estimated left" value={usd(view.estimated_left_usd)} tone="estimate" />
        </div>
        {view.last_exhausted_alert_at && (
          <div style={{ fontSize: 11.5, color: A.amber, marginTop: 12 }}>
            Last exhausted alert: {formatDateTime(view.last_exhausted_alert_at)}
          </div>
        )}
        {notDeployed && (
          <div style={{ fontSize: 11.5, color: A.muted2, marginTop: 12 }}>
            The Jev credit endpoint is not deployed on this environment yet.
          </div>
        )}
      </Card>

      <Card>
        <AddTopupForm />
      </Card>

      <Card>
        <TopupsTable topups={view.topups} />
      </Card>
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
