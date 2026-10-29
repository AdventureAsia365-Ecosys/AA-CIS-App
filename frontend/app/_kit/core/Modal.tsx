// UI kit core (root ADR 0016). Canonical: AA-CIS-App frontend/app/_kit/core. Edit only there; copies are checked byte for byte (kit.lock.json).
"use client";
// app/_kit/core/Modal.tsx
// AA-662 — centered modal for confirm / destructive actions.
//
// Controlled: parent owns `open`. Renders nothing when closed. Clicking the backdrop or pressing
// Escape calls onClose. `ConfirmModal` is the common confirm/destructive case built on Modal.
//
// AA-994 S4a: ConfirmModal absorbed AA-Booking's `ConfirmDialog` (one confirm dialog in every app): the Figma layout
// (a round icon at the left, the title and the message beside it, a footer band with Cancel + the action, no header
// bar), an optional reason field (`reason`, `required` blocks the action until one is typed; onConfirm receives the
// trimmed text), an inline `error`, "Working…" while `busy`, and no closing while busy. Booking's prop names are
// accepted as aliases (`message` = `body`, `danger` = `destructive`), so `ConfirmDialog` becomes a plain alias.

import { useEffect, useState, type CSSProperties } from "react";
import { AlertTriangle, Info, X } from "lucide-react";
import { Button } from "./primitives";
import { Field, Textarea } from "./FormControls";
import { K, RADIUS, sans, serif } from "./tokens";
import { KIT_MESSAGES } from "./kit.messages";
import { useT } from "./useI18n";

export function Modal({
  open,
  onClose,
  title,
  children,
  footer,
  width = 460,
  ariaLabel,
  testId,
  footerStyle,
}: {
  open: boolean;
  onClose: () => void;
  title?: React.ReactNode;
  children: React.ReactNode;
  footer?: React.ReactNode;
  width?: number;
  /** Accessible name of the dialog when it has no string `title`. */
  ariaLabel?: string;
  /** On the dialog box (body and footer). */
  testId?: string;
  footerStyle?: CSSProperties;
}) {
  const t = useT(KIT_MESSAGES);
  useEffect(() => {
    if (!open) return;
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") onClose();
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [open, onClose]);

  if (!open) return null;

  return (
    <div
      onClick={onClose}
      style={{
        position: "fixed",
        inset: 0,
        zIndex: 10000,
        background: "rgba(31,41,51,0.45)",
        display: "flex",
        alignItems: "center",
        justifyContent: "center",
        padding: 24,
        fontFamily: sans,
      }}
    >
      <div
        onClick={(e) => e.stopPropagation()}
        role="dialog"
        aria-modal="true"
        aria-label={ariaLabel ?? (typeof title === "string" ? title : undefined)}
        data-testid={testId}
        style={{
          width,
          maxWidth: "100%",
          maxHeight: "90vh",
          overflow: "auto",
          background: K.card,
          borderRadius: RADIUS.lg,
          border: `1px solid ${K.line}`,
          boxShadow: "0 20px 60px rgba(0,0,0,0.25)",
        }}
      >
        {title != null && (
          <div
            style={{
              display: "flex",
              alignItems: "center",
              justifyContent: "space-between",
              gap: 12,
              padding: "16px 20px",
              borderBottom: `1px solid ${K.line}`,
            }}
          >
            <div style={{ fontFamily: serif, fontSize: 17, fontWeight: 600, color: K.ink }}>
              {title}
            </div>
            <button
              onClick={onClose}
              aria-label={t("close")}
              style={{
                border: "none",
                background: "transparent",
                color: K.muted2,
                cursor: "pointer",
                padding: 0,
                lineHeight: 0,
              }}
            >
              <X size={18} />
            </button>
          </div>
        )}
        <div style={{ padding: "18px 20px", fontSize: 14, color: K.body, lineHeight: 1.5 }}>
          {children}
        </div>
        {footer && (
          <div
            style={{
              display: "flex",
              justifyContent: "flex-end",
              gap: 8,
              padding: "14px 20px",
              borderTop: `1px solid ${K.line}`,
              ...footerStyle,
            }}
          >
            {footer}
          </div>
        )}
      </div>
    </div>
  );
}

type ConfirmReason = { label: string; required?: boolean; placeholder?: string; maxLength?: number };

const NO_CLOSE = () => {};

export function ConfirmModal({
  open,
  onClose,
  onConfirm,
  title,
  body,
  message,
  confirmLabel,
  cancelLabel,
  destructive,
  danger,
  reason,
  busy = false,
  error,
  testId = "confirm-dialog",
}: {
  open: boolean;
  onClose: () => void;
  /** Receives the trimmed reason text ("" without a `reason` field). */
  onConfirm: (reason: string) => void;
  title: string;
  /** What happens; name the entity ("Retire tour Angkor at Dawn"). `message` is the same (Booking name). */
  body?: React.ReactNode;
  message?: React.ReactNode;
  confirmLabel?: string;
  cancelLabel?: string;
  /** Danger styling (red icon and action). `danger` is the same (Booking name). */
  destructive?: boolean;
  danger?: boolean;
  /** Ask for a reason; `required` blocks the action until one is typed; `maxLength` defaults to 2000. */
  reason?: ConfirmReason;
  busy?: boolean;
  error?: string | null;
  testId?: string;
}) {
  const t = useT(KIT_MESSAGES);
  const isDanger = !!(destructive ?? danger);
  // The reason starts empty each time the dialog opens (state adjusted during render, no effect).
  const [text, setText] = useState("");
  const [wasOpen, setWasOpen] = useState(open);
  if (wasOpen !== open) {
    setWasOpen(open);
    if (open) setText("");
  }
  const blocked = busy || (!!reason?.required && !text.trim());
  return (
    <Modal
      open={open}
      onClose={busy ? NO_CLOSE : onClose}
      width={520}
      ariaLabel={title}
      testId={testId}
      footerStyle={{ flexWrap: "wrap", background: K.panel }}
      footer={
        <>
          <Button variant="ghost" onClick={onClose} disabled={busy}>
            {cancelLabel ?? t("cancel")}
          </Button>
          <Button variant={isDanger ? "danger" : "primary"} onClick={() => onConfirm(text.trim())} disabled={blocked}>
            {busy ? t("working") : (confirmLabel ?? t("confirm"))}
          </Button>
        </>
      }
    >
      <div style={{ display: "flex", gap: 16, fontFamily: sans }}>
        <span
          aria-hidden="true"
          style={{
            display: "grid", placeItems: "center", width: 44, height: 44, flexShrink: 0, borderRadius: "50%",
            background: isDanger ? K.dangerSoft : K.accentTint, color: isDanger ? K.danger : K.accentDeep,
          }}
        >
          {isDanger ? <AlertTriangle size={20} /> : <Info size={20} />}
        </span>
        <div style={{ display: "flex", flexDirection: "column", gap: 12, minWidth: 0, flex: 1 }}>
          <div>
            <h2 style={{ margin: 0, fontSize: 17, fontWeight: 600, color: K.ink, fontFamily: sans }}>{title}</h2>
            <div style={{ marginTop: 6, fontSize: 14, color: K.body, overflowWrap: "anywhere" }}>{body ?? message}</div>
          </div>
          {reason && (
            // AA-994 S6a: the kit Field + Textarea (focus / disabled states, aria-required).
            <Field label={reason.label} required={reason.required}>
              <Textarea
                value={text}
                onChange={(e) => setText(e.target.value)}
                rows={3}
                maxLength={reason.maxLength ?? 2000}
                placeholder={reason.placeholder}
                style={{ width: "100%" }}
              />
            </Field>
          )}
          {error && <p role="alert" style={{ margin: 0, fontSize: 13, color: K.danger }}>{error}</p>}
        </div>
      </div>
    </Modal>
  );
}
