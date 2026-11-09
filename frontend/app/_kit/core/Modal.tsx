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
//
// AA-1008 (S6f) — `pinFooter`: the long-form dialog of AA-Partner's stage engine (S262, AA-957 live at 390 and 1440 px)
// moved into the kit. The title and the footer (a confirm step and the buttons) stay in view and only the body between
// them scrolls, with a "more below" cue (`moreLabel`) while there is more under the fold (one scroll area, never one in
// another). Under 480 px of window height (a phone held sideways) the whole box scrolls instead and the footer is
// compact, so the body is never squeezed to a sliver. The layout needs a fallback declaration (90vh before dvh) and a
// media query, so it is CSS: the kit's own style element (MODAL_CSS, hoisted once like ShellStyles), keyed on
// `data-kit-modal` (box / body / footer). `initialFocus` focuses the first control of the body (else the footer) on open;
// `bodyTestId` names the scrolling body.

import { useEffect, useRef, useState, type CSSProperties } from "react";
import { AlertTriangle, Info, X } from "lucide-react";
import { Button } from "./primitives";
import { Field, Textarea } from "./FormControls";
import { K, RADIUS, sans, serif } from "./tokens";
import { KIT_MESSAGES } from "./kit.messages";
import { useT } from "./useI18n";

export const MODAL_CSS = `[data-kit-modal="box"][data-pin-footer] { display: flex; flex-direction: column; overflow: hidden; max-height: 90vh; max-height: min(90vh, calc(100dvh - 32px)); }
[data-kit-modal="box"][data-pin-footer] > [data-kit-modal="body"] { flex: 1 1 auto; min-height: 0; overflow-y: auto; }
[data-kit-modal="box"][data-pin-footer] > [data-kit-modal="footer"] { flex: none; display: grid; gap: 10px; padding: 12px 20px 16px; }
@media (max-height: 479px) {
  [data-kit-modal="box"][data-pin-footer] { overflow-y: auto; }
  [data-kit-modal="box"][data-pin-footer] > [data-kit-modal="body"] { flex: none; overflow-y: visible; }
  [data-kit-modal="box"][data-pin-footer] > [data-kit-modal="footer"] { gap: 6px; padding: 8px 20px 10px; }
}`;

export function ModalStyles() {
  return <style href="aa-kit-modal" precedence="default">{MODAL_CSS}</style>;
}

/** More content below the fold of `node` (a 4 px tolerance). Pure. */
export function moreBelow(node: { scrollHeight: number; scrollTop: number; clientHeight: number } | null): boolean {
  return !!node && node.scrollHeight - node.scrollTop - node.clientHeight > 4;
}

/** The "more below" cue of a pinned-footer Modal: follows the body (and, on a short window, the box) as it scrolls or
 *  changes size. The observer reports once on observe, so nothing is set during the effect itself. */
function useMoreBelow(enabled: boolean, box: React.RefObject<HTMLDivElement | null>, body: React.RefObject<HTMLDivElement | null>) {
  const [more, setMore] = useState(false);
  useEffect(() => {
    const el = body.current;
    const frame = box.current;
    if (!enabled || !el || typeof ResizeObserver === "undefined") return;
    const measure = () => setMore(moreBelow(el) || moreBelow(frame));
    const observer = new ResizeObserver(measure);
    observer.observe(el);
    if (frame) observer.observe(frame);
    if (el.firstElementChild) observer.observe(el.firstElementChild);
    el.addEventListener("scroll", measure, { passive: true });
    frame?.addEventListener("scroll", measure, { passive: true });
    return () => {
      observer.disconnect();
      el.removeEventListener("scroll", measure);
      frame?.removeEventListener("scroll", measure);
    };
  }, [enabled, box, body]);
  return more;
}

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
  pinFooter = false,
  moreLabel,
  initialFocus = false,
  bodyTestId,
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
  /** AA-1008: the title and footer stay in view, only the body scrolls (the whole box under 480 px of height). */
  pinFooter?: boolean;
  /** With pinFooter: the "more below" cue's words; no cue without them. */
  moreLabel?: string;
  /** Focus the first control of the body (else of the footer) when the dialog opens. */
  initialFocus?: boolean;
  /** On the body (the scrolling region with pinFooter). */
  bodyTestId?: string;
}) {
  const t = useT(KIT_MESSAGES);
  const boxRef = useRef<HTMLDivElement>(null);
  const bodyRef = useRef<HTMLDivElement>(null);
  const footerRef = useRef<HTMLDivElement>(null);
  const more = useMoreBelow(open && pinFooter && !!moreLabel, boxRef, bodyRef);
  useEffect(() => {
    if (!open) return;
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") onClose();
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [open, onClose]);
  useEffect(() => {
    if (!open || !initialFocus) return;
    const control = "select, input, textarea, button";
    (bodyRef.current?.querySelector<HTMLElement>(control) ?? footerRef.current?.querySelector<HTMLElement>(control))?.focus();
  }, [open, initialFocus]);

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
      {pinFooter && <ModalStyles />}
      <div
        ref={boxRef}
        onClick={(e) => e.stopPropagation()}
        role="dialog"
        aria-modal="true"
        aria-label={ariaLabel ?? (typeof title === "string" ? title : undefined)}
        data-testid={testId}
        data-kit-modal="box"
        data-pin-footer={pinFooter ? "" : undefined}
        style={{
          width,
          maxWidth: "100%",
          // pinFooter: the box's height and scrolling are MODAL_CSS's (a 90vh fallback before dvh, a media query).
          ...(pinFooter ? {} : { maxHeight: "90vh", overflow: "auto" }),
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
        <div
          ref={bodyRef}
          data-kit-modal="body"
          data-testid={bodyTestId}
          style={{ padding: "18px 20px", fontSize: 14, color: K.body, lineHeight: 1.5, position: pinFooter ? "relative" : undefined }}
        >
          {pinFooter ? <div>{children}</div> : children}
          {pinFooter && more && moreLabel && (
            <div
              aria-hidden="true"
              data-testid="kit-modal-more"
              style={{
                position: "sticky",
                bottom: -18,
                marginTop: 4,
                padding: "18px 0 4px",
                textAlign: "center",
                fontSize: 12,
                color: K.muted,
                background: `linear-gradient(to bottom, transparent, ${K.card} 60%)`,
                pointerEvents: "none",
              }}
            >
              ↓ {moreLabel}
            </div>
          )}
        </div>
        {footer && (
          <div
            ref={footerRef}
            data-kit-modal="footer"
            style={
              pinFooter
                ? { borderTop: `1px solid ${K.line}`, ...footerStyle }
                : {
                    display: "flex",
                    justifyContent: "flex-end",
                    gap: 8,
                    padding: "14px 20px",
                    borderTop: `1px solid ${K.line}`,
                    ...footerStyle,
                  }
            }
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
