"use client";
// app/admin/platform/agencies/consoleUi.tsx
// AA-941 (S-01) — small building blocks shared by the agency list and the agency drawer: form field + label, drawer
// section, and WriteModal, the one confirm dialog every L1 write goes through (it asks for the ticket reference,
// the CRM refuses a write without one, and shows the CRM's refusal inline, code included).

import { AlertTriangle } from "lucide-react";
import { useState } from "react";
import { Button, K, Modal, RADIUS, sans } from "../../../_kit";
import { isTicket, type WriteResult } from "@/lib/crmConsole";

export const fieldStyle: React.CSSProperties = {
  fontSize: 13.5, padding: "8px 10px", border: `1px solid ${K.line}`, borderRadius: RADIUS.sm, background: K.card,
  color: K.ink, width: "100%", boxSizing: "border-box", fontFamily: sans, minHeight: 36,
};

export const noteStyle: React.CSSProperties = { fontSize: 12, color: K.muted, lineHeight: 1.5, fontFamily: sans };

export function Field({ label, htmlFor, hint, children }: {
  label: string; htmlFor: string; hint?: React.ReactNode; children: React.ReactNode;
}) {
  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 5, minWidth: 0 }}>
      <label htmlFor={htmlFor} style={{ fontSize: 12.5, fontWeight: 600, color: K.ink3 }}>{label}</label>
      {children}
      {hint && <span style={{ fontSize: 12, color: K.muted }}>{hint}</span>}
    </div>
  );
}

export function Section({ title, aside, children, testId }: {
  title: string; aside?: React.ReactNode; children: React.ReactNode; testId?: string;
}) {
  return (
    <section data-testid={testId} style={{
      border: `1px solid ${K.line}`, borderRadius: RADIUS.lg, padding: "12px 14px", display: "flex",
      flexDirection: "column", gap: 10, minWidth: 0,
    }}>
      <div style={{ display: "flex", alignItems: "center", justifyContent: "space-between", gap: 8, flexWrap: "wrap" }}>
        <h3 style={{ margin: 0, fontSize: 14, fontWeight: 700, color: K.ink, fontFamily: sans }}>{title}</h3>
        {aside}
      </div>
      {children}
    </section>
  );
}

export function InlineError({ message }: { message: string }) {
  return (
    <div role="alert" style={{
      display: "flex", gap: 8, alignItems: "flex-start", padding: "8px 10px", borderRadius: RADIUS.md,
      background: K.dangerSoft, color: K.danger, fontSize: 12.5, lineHeight: 1.45, fontFamily: sans,
    }}>
      <AlertTriangle size={14} style={{ flexShrink: 0, marginTop: 2 }} />
      <span style={{ color: K.ink, overflowWrap: "anywhere" }}>{message}</span>
    </div>
  );
}

/** The confirm dialog of every L1 write: what will happen, the action's own fields, the ticket reference. */
export function WriteModal<T>({
  open, onClose, title, children, confirmLabel, destructive = false, canSubmit = true, ticketLabel = "Ticket reference",
  ticketHint = "The support or Jira ticket this action belongs to, for example AA-941. It goes into the agency's access log.",
  submit, onDone,
}: {
  open: boolean;
  onClose: () => void;
  title: string;
  children?: React.ReactNode;
  confirmLabel: string;
  destructive?: boolean;
  canSubmit?: boolean;
  ticketLabel?: string;
  ticketHint?: string;
  submit: (ticketRef: string) => Promise<WriteResult<T>>;
  onDone: (result: Extract<WriteResult<T>, { ok: true }>) => void;
}) {
  const [ticket, setTicket] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const ticketOk = isTicket(ticket);

  const close = () => {
    if (busy) return;
    setTicket("");
    setError(null);
    onClose();
  };

  const go = async () => {
    if (!ticketOk || !canSubmit || busy) return;
    setBusy(true);
    setError(null);
    const result = await submit(ticket.trim());
    setBusy(false);
    if (!result.ok) {
      setError(result.message);
      return;
    }
    setTicket("");
    onDone(result as Extract<WriteResult<T>, { ok: true }>);
  };

  return (
    <Modal open={open} onClose={close} width={500}
      title={destructive ? (
        <span style={{ display: "inline-flex", alignItems: "center", gap: 8 }}>
          <AlertTriangle size={17} style={{ color: K.danger }} /> {title}
        </span>
      ) : title}
      footer={
        <>
          <Button variant="ghost" onClick={close} disabled={busy}>Cancel</Button>
          <Button variant={destructive ? "danger" : "primary"} onClick={() => void go()}
            disabled={!ticketOk || !canSubmit || busy}>
            {busy ? "Working…" : confirmLabel}
          </Button>
        </>
      }>
      <form onSubmit={(e) => { e.preventDefault(); void go(); }}
        style={{ display: "flex", flexDirection: "column", gap: 14 }}>
        {children}
        <Field label={ticketLabel} htmlFor="crm-write-ticket"
          hint={ticket && !ticketOk ? "Use a project key, a dash and a number, for example AA-941." : ticketHint}>
          <input id="crm-write-ticket" data-testid="crm-write-ticket" type="text" autoComplete="off" value={ticket}
            maxLength={27} placeholder="AA-941" onChange={(e) => setTicket(e.target.value)} style={fieldStyle}
            aria-invalid={!!ticket && !ticketOk} />
        </Field>
        {error && <InlineError message={error} />}
      </form>
    </Modal>
  );
}
