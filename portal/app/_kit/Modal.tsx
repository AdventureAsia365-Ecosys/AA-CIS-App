"use client";
// app/_kit/Modal.tsx
// AA-662 — centered modal for confirm / destructive actions.
//
// Controlled: parent owns `open`. Renders nothing when closed. Clicking the backdrop or pressing
// Escape calls onClose. `ConfirmModal` is the common confirm/destructive case built on Modal.

import { useEffect } from "react";
import { AlertTriangle, X } from "lucide-react";
import { K, RADIUS, sans, serif } from "./tokens";

export function Modal({
  open,
  onClose,
  title,
  children,
  footer,
  width = 460,
}: {
  open: boolean;
  onClose: () => void;
  title?: React.ReactNode;
  children: React.ReactNode;
  footer?: React.ReactNode;
  width?: number;
}) {
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
              aria-label="Close"
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
            }}
          >
            {footer}
          </div>
        )}
      </div>
    </div>
  );
}

export function ConfirmModal({
  open,
  onClose,
  onConfirm,
  title,
  body,
  confirmLabel = "Confirm",
  cancelLabel = "Cancel",
  destructive = false,
  busy = false,
}: {
  open: boolean;
  onClose: () => void;
  onConfirm: () => void;
  title: string;
  body: React.ReactNode;
  confirmLabel?: string;
  cancelLabel?: string;
  destructive?: boolean;
  busy?: boolean;
}) {
  const confirmColor = destructive ? K.danger : K.accent;
  return (
    <Modal
      open={open}
      onClose={onClose}
      title={
        destructive ? (
          <span style={{ display: "inline-flex", alignItems: "center", gap: 8 }}>
            <AlertTriangle size={17} style={{ color: K.danger }} /> {title}
          </span>
        ) : (
          title
        )
      }
      footer={
        <>
          <button
            onClick={onClose}
            disabled={busy}
            style={{
              padding: "8px 16px",
              borderRadius: RADIUS.pill,
              border: `1px solid ${K.line}`,
              background: K.card,
              color: K.ink3,
              fontSize: 13,
              fontWeight: 600,
              cursor: busy ? "not-allowed" : "pointer",
              fontFamily: sans,
            }}
          >
            {cancelLabel}
          </button>
          <button
            onClick={onConfirm}
            disabled={busy}
            style={{
              padding: "8px 16px",
              borderRadius: RADIUS.pill,
              border: `1px solid ${confirmColor}`,
              background: destructive ? K.dangerSoft : confirmColor,
              color: destructive ? K.danger : "var(--aa-on-solid)",
              fontSize: 13,
              fontWeight: 600,
              cursor: busy ? "not-allowed" : "pointer",
              opacity: busy ? 0.6 : 1,
              fontFamily: sans,
            }}
          >
            {confirmLabel}
          </button>
        </>
      }
    >
      {body}
    </Modal>
  );
}
