// UI kit core (root ADR 0016). Canonical: AA-CIS-App frontend/app/_kit/core. Edit only there; copies are checked byte for byte (kit.lock.json).
"use client";
// app/_kit/core/Drawer.tsx
// AA-662 — right-side slide-over for detail / edit views.
//
// Controlled by `open`. Backdrop click + Escape close it. Slides in from the right; on narrow
// viewports it takes the full width.
//
// AA-1131 (S283 UI audit, batch K1) — a modal dialog a keyboard and a screen reader can use (WAI-ARIA APG dialog):
//   - name: the title (aria-labelledby on the title's id); without a title, `ariaLabel`, else the kit word "Dialog";
//   - initial focus: `initialFocusRef` when given, else the first focusable element of the panel (the Close button
//     unless the title holds a link), else the panel itself;
//   - focus trap: Tab and Shift+Tab cycle inside the panel (trapTarget); focus that lands outside it (a click on the
//     page behind is caught by the backdrop, but a script or the browser can still move it) is pulled back, except
//     into another dialog (a ConfirmModal opened from the drawer);
//   - return: on close, focus goes back to the element that had it when the drawer opened (the row or button), when
//     that element is still in the page.

import { useEffect, useId, useRef } from "react";
import { X } from "lucide-react";
import { K, sans, serif } from "./tokens";
import { KIT_MESSAGES } from "./kit.messages";
import { useT } from "./useI18n";

/** What can take keyboard focus inside the panel (visible or not is checked by `focusables`). */
export const FOCUSABLE =
  'a[href], area[href], button:not([disabled]), input:not([disabled]):not([type="hidden"]), select:not([disabled]), ' +
  'textarea:not([disabled]), iframe, summary, [contenteditable="true"], [tabindex]:not([tabindex="-1"])';

/** The focusable elements of `root` in tab order (DOM order; an explicit positive tabindex is not used by the kit),
 *  without the ones that are hidden (no layout box) or inside a nested dialog. */
export function focusables(root: HTMLElement): HTMLElement[] {
  return Array.from(root.querySelectorAll<HTMLElement>(FOCUSABLE)).filter(
    (el) => el.getClientRects().length > 0 && el.closest('[role="dialog"], [role="alertdialog"]') === root,
  );
}

/**
 * AA-1131 — the focus trap's move for a Tab key: the index of the element to focus instead of the browser's own move,
 * or null to let the browser move (the focus is inside the list and not at the end it would leave from). `active` is
 * the index of the focused element in `count` focusables, -1 when focus is not on one of them (the panel itself, or
 * outside). With no focusable element the panel keeps the focus (-1: focus the panel).
 */
export function trapTarget(count: number, active: number, shift: boolean): number | null {
  if (count === 0) return -1;
  if (active < 0) return shift ? count - 1 : 0;
  if (shift && active === 0) return count - 1;
  if (!shift && active === count - 1) return 0;
  return null;
}

export function Drawer({
  open,
  onClose,
  title,
  children,
  footer,
  width = 480,
  ariaLabel,
  initialFocusRef,
  testId,
}: {
  open: boolean;
  onClose: () => void;
  title?: React.ReactNode;
  children: React.ReactNode;
  footer?: React.ReactNode;
  width?: number;
  /** The dialog's accessible name when it has no title (a title names it already). */
  ariaLabel?: string;
  /** The element to focus when the drawer opens (a form's first field); default: the first focusable element. */
  initialFocusRef?: React.RefObject<HTMLElement | null>;
  /** On the dialog panel. */
  testId?: string;
}) {
  const t = useT(KIT_MESSAGES);
  const titleId = useId();
  const panelRef = useRef<HTMLDivElement>(null);
  // The latest onClose, so the focus effect below runs once per opening (a parent that passes a new arrow function on
  // every render must not re-run it: that would move the focus back to the first element on each render).
  const closeRef = useRef(onClose);
  useEffect(() => {
    closeRef.current = onClose;
  }, [onClose]);

  useEffect(() => {
    if (!open) return;
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") closeRef.current();
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [open]);

  // Initial focus, the trap's outside catch, and the focus return on close.
  useEffect(() => {
    if (!open) return;
    const panel = panelRef.current;
    if (!panel) return;
    const opener = document.activeElement instanceof HTMLElement ? document.activeElement : null;
    const first = initialFocusRef?.current ?? focusables(panel)[0] ?? panel;
    first.focus({ preventScroll: true });
    const onFocusIn = (e: FocusEvent) => {
      const target = e.target as Node | null;
      if (!target || panel.contains(target)) return;
      // Another dialog (a confirm opened from the drawer, portalled or not) keeps its own focus.
      if (target instanceof Element && target.closest('[role="dialog"], [role="alertdialog"]')) return;
      (focusables(panel)[0] ?? panel).focus({ preventScroll: true });
    };
    document.addEventListener("focusin", onFocusIn);
    return () => {
      document.removeEventListener("focusin", onFocusIn);
      if (opener && opener.isConnected && opener !== document.body) opener.focus({ preventScroll: true });
    };
    // initialFocusRef is a ref object (stable), so the effect still runs once per opening.
  }, [open, initialFocusRef]);

  const onPanelKeyDown = (e: React.KeyboardEvent<HTMLDivElement>) => {
    const panel = panelRef.current;
    if (e.key !== "Tab" || !panel) return;
    // A key inside a nested dialog (a ConfirmModal rendered in the drawer) is that dialog's.
    const target = e.target as Element;
    if (target.closest('[role="dialog"], [role="alertdialog"]') !== panel) return;
    const list = focusables(panel);
    const active = list.indexOf(document.activeElement as HTMLElement);
    const next = trapTarget(list.length, active, e.shiftKey);
    if (next === null) return;
    e.preventDefault();
    (next < 0 ? panel : list[next]).focus();
  };

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
        ref={panelRef}
        onClick={(e) => e.stopPropagation()}
        onKeyDown={onPanelKeyDown}
        role="dialog"
        aria-modal="true"
        aria-labelledby={title != null ? titleId : undefined}
        aria-label={title != null ? undefined : (ariaLabel ?? t("dialog"))}
        tabIndex={-1}
        data-testid={testId}
        data-kit-drawer=""
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
          outline: "none",
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
            id={titleId}
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
            aria-label={t("close")}
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
