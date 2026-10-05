"use client";
// app/_kit/Drawer.tsx
// AA-662 — right-side slide-over for detail / edit views.
//
// Controlled by `open`. Backdrop click + Escape close it. Slides in from the right; on narrow
// viewports it takes the full width.

import { useEffect } from "react";
import { X } from "lucide-react";
import { K, sans, serif } from "./tokens";

export function Drawer({
  open,
  onClose,
  title,
  children,
  footer,
  width = 480,
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
        justifyContent: "flex-end",
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
          height: "100%",
          background: K.card,
          borderLeft: `1px solid ${K.line}`,
          boxShadow: "-12px 0 40px rgba(0,0,0,0.18)",
          display: "flex",
          flexDirection: "column",
          animation: "kit-drawer-in .18s ease",
        }}
      >
        <style>{`@keyframes kit-drawer-in { from { transform: translateX(24px); opacity: 0.6; } to { transform: translateX(0); opacity: 1; } }`}</style>
        <div
          style={{
            display: "flex",
            alignItems: "center",
            justifyContent: "space-between",
            gap: 12,
            padding: "16px 20px",
            borderBottom: `1px solid ${K.line}`,
            flexShrink: 0,
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
        <div style={{ flex: 1, overflow: "auto", padding: "18px 20px", fontSize: 14, color: K.body }}>
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
              flexShrink: 0,
            }}
          >
            {footer}
          </div>
        )}
      </div>
    </div>
  );
}
