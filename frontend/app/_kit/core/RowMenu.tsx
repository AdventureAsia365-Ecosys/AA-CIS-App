// UI kit core (root ADR 0016). Canonical: AA-CIS-App frontend/app/_kit/core. Edit only there; copies are checked byte for byte (kit.lock.json).
"use client";
// app/_kit/core/RowMenu.tsx
// AA-994 S4a — the "⋯" action menu of a list row, upstreamed from AA-Booking `ui/booking/RowMenu.tsx` (inline styles
// instead of Tailwind). The panel is `position: fixed` at the trigger's rect, so a DataTable's scrolling wrapper never
// clips it. Escape, an outside press, scrolling, a resize or picking an item closes it; clicks never bubble to the row
// (DataTable onRowClick). A trigger that is only partly visible is scrolled into view first; scrolls within
// OPEN_GRACE_MS of opening move the panel with the trigger instead of closing it.

import { useEffect, useRef, useState, type ReactNode } from "react";
import { MoreHorizontal } from "lucide-react";
import { K, RADIUS, sans } from "./tokens";
import { KIT_MESSAGES } from "./kit.messages";
import { useT } from "./useI18n";

export interface RowMenuItem {
  label: string;
  icon?: ReactNode;
  onSelect: () => void;
  danger?: boolean;
  disabled?: boolean;
  /** Draw a divider above this item. */
  divider?: boolean;
}

const PANEL_WIDTH = 208;
const OPEN_GRACE_MS = 400;

// Colours live in the stylesheet, not inline styles, so the :hover rules can win.
const MENU_CSS =
  `.aa-kit-rowmenu-btn{color:${K.muted};border-color:${K.line}}` +
  `.aa-kit-rowmenu-btn:hover,.aa-kit-rowmenu-btn[aria-expanded="true"]{color:${K.ink};border-color:${K.accentBorder}}` +
  `.aa-kit-rowmenu-item{color:${K.body};background:transparent;outline:none}` +
  // Keyboard focus: the tint alone is about 1.1:1 on card, so an inset accent-deep ring as well (same as ListButton).
  `.aa-kit-rowmenu-item:focus-visible{outline:2px solid ${K.accentDeep};outline-offset:-2px}` +
  `.aa-kit-rowmenu-item:not(:disabled):hover,.aa-kit-rowmenu-item:not(:disabled):focus-visible{background:${K.accentTint}}` +
  `.aa-kit-rowmenu-item[data-danger]{color:${K.danger}}` +
  `.aa-kit-rowmenu-item[data-danger]:not(:disabled):hover,` +
  `.aa-kit-rowmenu-item[data-danger]:not(:disabled):focus-visible{background:${K.dangerSoft}}`;

/** Where the panel goes for a trigger rect: right-aligned under it, above it when there is no room below. */
function placeFor(r: DOMRect, itemCount: number): { top: number; left: number } {
  const left = Math.max(8, Math.min(r.right - PANEL_WIDTH, window.innerWidth - PANEL_WIDTH - 8));
  const below = r.bottom + 4;
  const estimated = itemCount * 38 + 12;
  const top = below + estimated > window.innerHeight ? Math.max(8, r.top - estimated - 4) : below;
  return { top, left };
}

export function RowMenu({ items, label, testId }: { items: RowMenuItem[]; label?: string; testId?: string }) {
  const t = useT(KIT_MESSAGES);
  const [pos, setPos] = useState<{ top: number; left: number } | null>(null);
  const btn = useRef<HTMLButtonElement>(null);
  const panel = useRef<HTMLDivElement>(null);
  const openedAt = useRef(0);
  const open = pos !== null;
  const itemCount = items.length;

  useEffect(() => {
    if (!open) return;
    const close = () => setPos(null);
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") {
        close();
        btn.current?.focus();
      }
    };
    const onDown = (e: MouseEvent) => {
      const target = e.target as Node;
      if (!panel.current?.contains(target) && !btn.current?.contains(target)) close();
    };
    const onScroll = () => {
      const r = btn.current?.getBoundingClientRect();
      if (r && performance.now() - openedAt.current < OPEN_GRACE_MS) setPos(placeFor(r, itemCount));
      else close();
    };
    window.addEventListener("keydown", onKey);
    window.addEventListener("mousedown", onDown);
    window.addEventListener("scroll", onScroll, true);
    window.addEventListener("resize", close);
    panel.current?.querySelector<HTMLButtonElement>("button:not(:disabled)")?.focus();
    return () => {
      window.removeEventListener("keydown", onKey);
      window.removeEventListener("mousedown", onDown);
      window.removeEventListener("scroll", onScroll, true);
      window.removeEventListener("resize", close);
    };
  }, [open, itemCount]);

  function toggle(e: React.MouseEvent) {
    e.stopPropagation();
    if (open) return setPos(null);
    const b = btn.current;
    if (!b) return;
    // Bring a clipped trigger fully into its scroll box first, then place the panel at its final rect.
    b.scrollIntoView({ block: "nearest", inline: "nearest" });
    openedAt.current = performance.now();
    setPos(placeFor(b.getBoundingClientRect(), items.length));
  }

  if (items.length === 0) return null;
  return (
    <span data-testid={testId} onClick={(e) => e.stopPropagation()} style={{ display: "inline-flex" }}>
      <style href="aa-kit-rowmenu" precedence="default">{MENU_CSS}</style>
      <button
        ref={btn}
        type="button"
        aria-label={label ?? t("rowActions")}
        aria-haspopup="menu"
        aria-expanded={open}
        onClick={toggle}
        className="aa-kit-rowmenu-btn"
        style={{
          display: "grid", placeItems: "center", width: 32, height: 32, padding: 0, cursor: "pointer",
          borderRadius: RADIUS.md, borderWidth: 1, borderStyle: "solid", background: K.card,
        }}
      >
        <MoreHorizontal size={16} />
      </button>
      {open && (
        <div
          ref={panel}
          role="menu"
          style={{
            position: "fixed", zIndex: 9000, top: pos.top, left: pos.left, width: PANEL_WIDTH,
            display: "flex", flexDirection: "column", padding: "6px 0", borderRadius: RADIUS.lg,
            border: `1px solid ${K.line}`, background: K.card, fontFamily: sans,
            boxShadow: "0 10px 30px rgba(0, 0, 0, 0.16)",
          }}
        >
          {items.map((it) => (
            <div key={it.label}>
              {it.divider && <div aria-hidden="true" style={{ margin: "4px 0", borderTop: `1px solid ${K.line2}` }} />}
              <button
                type="button"
                role="menuitem"
                disabled={it.disabled}
                data-danger={it.danger ? "" : undefined}
                onClick={() => {
                  setPos(null);
                  it.onSelect();
                }}
                className="aa-kit-rowmenu-item"
                style={{
                  display: "flex", width: "100%", alignItems: "center", gap: 10, padding: "8px 14px",
                  textAlign: "left", fontSize: 13, fontFamily: sans, border: "none",
                  cursor: it.disabled ? "not-allowed" : "pointer", opacity: it.disabled ? 0.5 : 1,
                }}
              >
                {it.icon && <span style={{ flexShrink: 0, opacity: 0.8, display: "inline-flex" }}>{it.icon}</span>}
                {it.label}
              </button>
            </div>
          ))}
        </div>
      )}
    </span>
  );
}
