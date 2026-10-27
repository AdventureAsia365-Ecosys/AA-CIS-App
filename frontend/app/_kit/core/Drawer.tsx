// UI kit core (root ADR 0016). Canonical: AA-CIS-App frontend/app/_kit/core. Edit only there; copies are checked byte for byte (kit.lock.json).
"use client";
// app/_kit/core/Drawer.tsx
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
          {/* S241 UI verify — at 390 a long title + badge pushed the close X off-screen: the title
              box had no min-width, so it kept its content width and the row overflowed. It now
              takes the free space (flex 1, min-width 0) and the X never shrinks. Titles WRAP (long
              words break anywhere) so the whole text stays readable on a phone, where a tooltip
              cannot be opened — no ellipsis here. A caller that wants one line passes a node with
              its own min-width 0 + ellipsis span (JobDrawer does). `break-word`, not `anywhere`:
              `anywhere` also lowers the min-content width of child nodes, so title pills ("BLOG",
              "APPROVED") were squeezed letter by letter (S241 re-verify of #789). */}
          <div
            style={{
              flex: "1 1 auto",
              minWidth: 0,
              overflowWrap: "break-word",
              fontFamily: serif,
              fontSize: 17,
              fontWeight: 600,
              color: K.ink,
            }}
          >
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
              flexShrink: 0,
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
