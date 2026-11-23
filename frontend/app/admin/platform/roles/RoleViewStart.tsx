"use client";
// app/admin/platform/roles/RoleViewStart.tsx — AA-1157: Roles & permissions › "View as" on a CIS role, owners only.
// A kit Modal asks a reason (prefilled, 3-500 characters) and the length (15 / 30 / 60 minutes), then POST
// /api/view-as/roles (the BFF keeps the signed token in an httpOnly cookie) and opens the role's first page with a full
// reload, so the shell, the nav and every poller start again as that role. The API re-checks owner and downward.

import { useState } from "react";
import { Eye } from "lucide-react";
import { Button, Field, K, Modal, Select, Textarea, useT } from "../../../_kit";
import { STAFF_VIEW_AS } from "../../../_kit/staffViewAs.messages";
import { DEFAULT_MINUTES, MINUTE_CHOICES, REASON_MAX, REASON_MIN } from "@/lib/viewAs";
import { DEFAULT_ROLE_VIEW_REASON, STAFF_VIEW_AS_API } from "@/lib/staffViewAs";

export function RoleViewStart({ role, label }: { role: string; label: string }) {
  const t = useT(STAFF_VIEW_AS);
  const [open, setOpen] = useState(false);
  const [reason, setReason] = useState(DEFAULT_ROLE_VIEW_REASON);
  const [minutes, setMinutes] = useState<number>(DEFAULT_MINUTES);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const reasonOk = reason.trim().length >= REASON_MIN && reason.trim().length <= REASON_MAX;

  const start = async () => {
    if (!reasonOk) { setError(t("errReason")); return; }
    setBusy(true);
    setError(null);
    try {
      const r = await fetch(STAFF_VIEW_AS_API, {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ role, reason: reason.trim(), minutes }),
      });
      const body = (await r.json().catch(() => ({}))) as { detail?: unknown; landing?: unknown };
      if (!r.ok) {
        const detail = body.detail;
        setError(detail === "view_as_not_downward" ? t("errNotDownward")
          : detail === "view_as_unknown_role" ? t("errUnknown")
            : r.status === 403 ? t("errOwner") : t("startFailed"));
        return;
      }
      window.location.assign(typeof body.landing === "string" && body.landing.startsWith("/admin") ? body.landing
        : "/admin");
    } catch {
      setError(t("startFailed"));
    } finally {
      setBusy(false);
    }
  };

  return (
    <>
      <span data-testid={`role-view-as-${role}`}>
        <Button variant="secondary" size="sm" onClick={() => setOpen(true)}>
          <Eye size={13} /> {t("startButton")}
        </Button>
      </span>
      <Modal open={open} onClose={() => !busy && setOpen(false)} title={t("startTitle", label)} width={520}
        testId="role-view-as-modal"
        footer={
          <>
            <Button variant="ghost" onClick={() => setOpen(false)} disabled={busy}>{t("cancel")}</Button>
            <Button variant="primary" onClick={start} disabled={busy}>{busy ? t("starting") : t("start")}</Button>
          </>
        }>
        <div style={{ display: "flex", flexDirection: "column", gap: 12 }}>
          <p style={{ margin: 0, fontSize: 13 }}>{t("startIntro")}</p>
          <Field label={t("reasonLabel")} required hint={t("reasonHint")}>
            <Textarea value={reason} maxLength={REASON_MAX} rows={2} onChange={(e) => setReason(e.target.value)} />
          </Field>
          <Field label={t("minutesLabel")}>
            <Select value={String(minutes)} onChange={(e) => setMinutes(Number(e.target.value))}
              options={MINUTE_CHOICES.map((n) => ({ value: String(n), label: t("minutesOption", n) }))} />
          </Field>
          {error && <p role="alert" style={{ margin: 0, fontSize: 13, color: K.danger }}>{error}</p>}
        </div>
      </Modal>
    </>
  );
}
