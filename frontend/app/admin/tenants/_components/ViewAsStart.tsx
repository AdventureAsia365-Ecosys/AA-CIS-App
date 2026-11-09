"use client";
// app/admin/tenants/_components/ViewAsStart.tsx — AA-1035 R2 slice 2 (root ADR 0019 d5): Tenant 360 › "View as
// partner". A kit Modal asks a reason (3-500 characters), an optional ticket and the length (15 / 30 / 60 minutes),
// then POST /api/view-as (the BFF keeps the read-only token in an httpOnly cookie) and opens /admin/tenants/view-as.
// Shown only to a role with tenant.view_as (admin, customer_success, support); the backend re-checks it and the scope.

import { useState } from "react";
import { useRouter } from "next/navigation";
import { useQueryClient } from "@tanstack/react-query";
import { Eye } from "lucide-react";
import { Button, Field, Input, K, Modal, Select, Textarea, useT } from "../../../_kit";
import {
  DEFAULT_MINUTES, MINUTE_CHOICES, REASON_MAX, TICKET_MAX, VIEW_AS_API, VIEW_AS_PAGE, VIEW_AS_QUERY_KEY, startProblem,
} from "@/lib/viewAs";
import { staffErrorMessage } from "@/lib/rbac";
import { VIEW_AS } from "../view-as/messages";

export function ViewAsStart({ tenantId, tenantName }: { tenantId: string; tenantName: string }) {
  const t = useT(VIEW_AS);
  const router = useRouter();
  const qc = useQueryClient();
  const [open, setOpen] = useState(false);
  const [reason, setReason] = useState("");
  const [ticket, setTicket] = useState("");
  const [minutes, setMinutes] = useState<number>(DEFAULT_MINUTES);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [touched, setTouched] = useState(false);
  const problem = startProblem(reason, ticket, minutes);

  const start = async () => {
    setTouched(true);
    if (problem) return;
    setBusy(true);
    setError(null);
    try {
      const r = await fetch(VIEW_AS_API, {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ tenant_id: tenantId, reason: reason.trim(), ticket_ref: ticket.trim() || null, minutes }),
      });
      const body = await r.json().catch(() => ({}));
      if (!r.ok) {
        const detail = (body as { detail?: unknown }).detail;
        setError(staffErrorMessage(r.status, Array.isArray(detail) ? t("err_reason") : detail, t("startFailed")));
        return;
      }
      await qc.invalidateQueries({ queryKey: VIEW_AS_QUERY_KEY });
      setOpen(false);
      router.push(VIEW_AS_PAGE);
    } catch {
      setError(t("startFailed"));
    } finally {
      setBusy(false);
    }
  };

  return (
    <>
      <span data-testid="view-as-start">
        <Button variant="secondary" size="sm" onClick={() => setOpen(true)}>
          <Eye size={13} /> {t("startButton")}
        </Button>
      </span>
      <Modal open={open} onClose={() => !busy && setOpen(false)} title={t("startTitle", tenantName)} width={520}
        testId="view-as-start-modal"
        footer={
          <>
            <Button variant="ghost" onClick={() => setOpen(false)} disabled={busy}>{t("cancel")}</Button>
            <Button variant="primary" onClick={start} disabled={busy || (touched && !!problem)}>
              {busy ? t("starting") : t("start")}
            </Button>
          </>
        }>
        <div style={{ display: "flex", flexDirection: "column", gap: 12 }}>
          <p style={{ margin: 0, fontSize: 13 }}>{t("startIntro")}</p>
          <Field label={t("reasonLabel")} required hint={t("reasonHint")}
            error={touched && problem === "reason" ? t("err_reason") : undefined}>
            <Textarea value={reason} maxLength={REASON_MAX} rows={3} onChange={(e) => setReason(e.target.value)} />
          </Field>
          <Field label={t("ticketLabel")} hint={t("ticketHint")}
            error={touched && problem === "ticket" ? t("err_ticket") : undefined}>
            <Input value={ticket} maxLength={TICKET_MAX} onChange={(e) => setTicket(e.target.value)} />
          </Field>
          <Field label={t("minutesLabel")} error={touched && problem === "minutes" ? t("err_minutes") : undefined}>
            <Select value={String(minutes)} onChange={(e) => setMinutes(Number(e.target.value))}
              options={MINUTE_CHOICES.map((n) => ({ value: String(n), label: t("minutesOption", n) }))} />
          </Field>
          {error && <p role="alert" style={{ margin: 0, fontSize: 13, color: K.danger }}>{error}</p>}
        </div>
      </Modal>
    </>
  );
}
