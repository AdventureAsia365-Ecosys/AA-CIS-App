// UI kit core (root ADR 0016). Canonical: AA-CIS-App frontend/app/_kit/core. Edit only there; copies are checked byte for byte (kit.lock.json).
"use client";
// app/_kit/core/ShellFrame.tsx
// ShellFrame — the one app shell frame of every staff and agency app (AA-1008 S6e, root ADR 0016 decision 1 "a
// slot-based ShellFrame"): CIS admin, Booking admin and the AA-Partner agency app (CRM and Content studio). It owns
// what every shell had copied: the layout [sidebar | (topbar over the page)], the phone drawer (off-canvas below a
// breakpoint, a backdrop, Esc closes it, a closed drawer is out of the tab order), the ⌘K / Ctrl K palette state, a
// phone bottom bar and a footer. What differs per app is a slot: the sidebar, the topbar, the bottom bar, the palette
// and the footer are the app's own components, given the shell controls (open / close the drawer and the palette).
// The nav, identity, sign-out and brand label live in those slots, so core imports no router, no RBAC and no app
// session (decision 1: the core is portable).
//
// Two scroll models:
//   scroll "main" (default; CIS, Booking, the studio): the column is one screen high and the page scrolls inside it,
//     in the PageFrame the page renders (`main="page"`) or in the shell's own <main> (`main="shell"`);
//   scroll "document" (the CRM): the document scrolls; the sidebar stays in view (sticky) and the topbar is the
//     slot's own (it may be sticky).
// `main="shell"` wraps the children in <main data-kit-shell="main"> (pages then render no <main> of their own);
// `main="page"` renders the children as they are (each page renders the kit PageFrame, which is the <main>).
//
// Layout hooks are data attributes (no class names, the kit lint rule), and the CSS that needs a media query is the
// kit's own style element (ShellStyles, hoisted once like ButtonStyles): `data-kit-shell` on each part, the
// breakpoint on the frame (`data-kit-shell-bp`, 768 or 900), `data-drawer="open"` while the drawer is open. Apps keep
// their own look (sidebar width, topbar padding) in their stylesheet, keyed on their own hooks.
import { createContext, useCallback, useContext, useEffect, useMemo, useState } from "react";
import type { CSSProperties, ReactNode } from "react";
import { K } from "./tokens";

/** Below this width (px) the sidebar is an off-canvas drawer. CIS / Booking: 768; the agency app: 900. */
export type ShellBreakpoint = 768 | 900;

export interface ShellControls {
  drawerOpen: boolean;
  openDrawer: () => void;
  closeDrawer: () => void;
  paletteOpen: boolean;
  openPalette: () => void;
  closePalette: () => void;
  togglePalette: () => void;
  /** The id of the sidebar wrapper, for `aria-controls` on the menu button. */
  sidebarId: string;
}

type Slot = ReactNode | ((c: ShellControls) => ReactNode);

export interface ShellFrameProps {
  /** The app's sidebar (nav, identity, brand). Rendered in the drawer wrapper. */
  sidebar: Slot;
  /** The app's topbar (menu button, search, language, theme, account, sign-out). */
  topbar?: Slot;
  /** A phone bottom bar (fixed; the app's CSS shows it below its breakpoint). */
  bottomNav?: Slot;
  /** The command palette. Given one, ⌘K / Ctrl K toggles it. */
  palette?: Slot;
  /** A line under the page (inside the column, after the main). */
  footer?: Slot;
  /** Something between the topbar and the page in the column (a banner every page shows). */
  banner?: Slot;
  children: ReactNode;
  drawerBelow?: ShellBreakpoint;
  scroll?: "main" | "document";
  /** "shell": the shell renders the <main>; "page": the page renders its own PageFrame. */
  main?: "shell" | "page";
  /** Style of the shell's <main> (main="shell"). */
  mainStyle?: CSSProperties;
  /** The id of the shell's <main> (a skip link target). Default "main". */
  mainId?: string;
  /** The sidebar wrapper's id (default "kit-shell-sidebar"). */
  sidebarId?: string;
  /** A shell inside a box (the kit gallery demo): the frame fills its parent's height instead of the screen's. */
  embedded?: boolean;
  /** Merged into the frame. */
  style?: CSSProperties;
  testId?: string;
  /** Extra data-* hooks on the frame (an app's existing CSS or tests). */
  frameProps?: Record<`data-${string}`, string | undefined>;
  /** Extra data-* hooks on the shell's <main> (main="shell"). */
  mainProps?: Record<`data-${string}`, string | undefined>;
}

const ShellContext = createContext<ShellControls | null>(null);

/** The controls of the ShellFrame around this component (null outside a shell). */
export function useShell(): ShellControls | null {
  return useContext(ShellContext);
}

function renderSlot(slot: Slot | undefined, c: ShellControls): ReactNode {
  if (slot === undefined || slot === null) return null;
  return typeof slot === "function" ? (slot as (c: ShellControls) => ReactNode)(c) : slot;
}

/** The modifier shortcut that toggles the palette (⌘K on a Mac, Ctrl K elsewhere). */
export function isPaletteShortcut(e: { key: string; metaKey: boolean; ctrlKey: boolean }): boolean {
  return (e.metaKey || e.ctrlKey) && e.key.toLowerCase() === "k";
}

// The drawer rules for one breakpoint. Below it the sidebar wrapper leaves the row: fixed, slid out of view and
// hidden (a closed drawer is not focusable), slid in while the frame says data-drawer="open"; the backdrop covers the
// page behind it. At and above it the backdrop never shows (a drawer opened on a phone, then the window widened).
// The slot inside the drawer fills the drawer, not the screen: a slot sized 100vh in its own inline style (the CIS
// and Booking sidebars) is the large viewport on a phone browser, so its last rows (account, sign-out) would sit under
// the browser toolbar. !important because an inline height otherwise wins over a stylesheet rule.
function drawerCss(bp: ShellBreakpoint): string {
  const f = `[data-kit-shell="frame"][data-kit-shell-bp="${bp}"]`;
  return `@media (max-width: ${bp - 1}px) {
  ${f} > [data-kit-shell="sidebar"] {
    position: fixed; top: 0; left: 0; bottom: 0; height: 100dvh; z-index: 1000;
    transform: translateX(-100%); visibility: hidden;
    transition: transform .22s ease, visibility .22s ease;
    box-shadow: 0 0 40px rgba(0, 0, 0, 0.5);
  }
  ${f} > [data-kit-shell="sidebar"] > * { height: 100% !important; max-height: 100%; }
  ${f}[data-drawer="open"] > [data-kit-shell="sidebar"] { transform: translateX(0); visibility: visible; }
  ${f} > [data-kit-shell="backdrop"] { display: block; }
}`;
}

export const SHELL_CSS = `[data-kit-shell="sidebar"] { display: flex; flex: none; }
[data-kit-shell="frame"][data-kit-shell-scroll="document"] > [data-kit-shell="sidebar"] {
  position: sticky; top: 0; height: 100vh; align-self: flex-start;
}
[data-kit-shell="backdrop"] { display: none; position: fixed; inset: 0; z-index: 999; background: rgba(0, 0, 0, 0.45); }
${drawerCss(768)}
${drawerCss(900)}
@media (prefers-reduced-motion: reduce) {
  [data-kit-shell="sidebar"] { transition: none !important; }
}`;

export function ShellStyles() {
  return <style href="aa-kit-shell" precedence="default">{SHELL_CSS}</style>;
}

export function ShellFrame({
  sidebar,
  topbar,
  bottomNav,
  palette,
  footer,
  banner,
  children,
  drawerBelow = 768,
  scroll = "main",
  main = "shell",
  mainStyle,
  mainId = "main",
  sidebarId = "kit-shell-sidebar",
  embedded = false,
  style,
  testId,
  frameProps,
  mainProps,
}: ShellFrameProps) {
  const [drawerOpen, setDrawerOpen] = useState(false);
  const [paletteOpen, setPaletteOpen] = useState(false);
  const hasPalette = palette !== undefined && palette !== null;

  const openDrawer = useCallback(() => setDrawerOpen(true), []);
  const closeDrawer = useCallback(() => setDrawerOpen(false), []);
  const openPalette = useCallback(() => setPaletteOpen(true), []);
  const closePalette = useCallback(() => setPaletteOpen(false), []);
  const togglePalette = useCallback(() => setPaletteOpen((v) => !v), []);

  // ⌘K / Ctrl K toggles the palette (modifier-gated, so typing in a field never triggers it). Esc on the palette is
  // the palette's own.
  useEffect(() => {
    if (!hasPalette) return;
    const onKey = (e: KeyboardEvent) => {
      if (isPaletteShortcut(e)) {
        e.preventDefault();
        setPaletteOpen((v) => !v);
      }
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [hasPalette]);

  // Esc closes the drawer.
  useEffect(() => {
    if (!drawerOpen) return;
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") setDrawerOpen(false);
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [drawerOpen]);

  const controls = useMemo<ShellControls>(
    () => ({ drawerOpen, openDrawer, closeDrawer, paletteOpen, openPalette, closePalette, togglePalette, sidebarId }),
    [drawerOpen, openDrawer, closeDrawer, paletteOpen, openPalette, closePalette, togglePalette, sidebarId],
  );

  const docScroll = scroll === "document";
  const screen = embedded ? "100%" : "100vh";
  const column: CSSProperties = docScroll
    ? { flex: 1, minWidth: 0, display: "flex", flexDirection: "column", minHeight: screen }
    : { flex: 1, minWidth: 0, display: "flex", flexDirection: "column", height: screen };
  const mainBase: CSSProperties = docScroll
    ? { flex: 1, minWidth: 0 }
    : { flex: 1, minWidth: 0, minHeight: 0, overflowY: "auto", overflowX: "hidden" };

  return (
    <ShellContext.Provider value={controls}>
      <ShellStyles />
      <div
        {...frameProps}
        data-kit-shell="frame"
        data-kit-shell-bp={String(drawerBelow)}
        data-kit-shell-scroll={scroll}
        data-drawer={drawerOpen ? "open" : "closed"}
        data-testid={testId}
        style={{ display: "flex", ...(embedded ? { height: "100%" } : { minHeight: "100vh" }), background: K.bg, color: K.body, ...style }}
      >
        <div data-kit-shell="sidebar" id={sidebarId}>
          {renderSlot(sidebar, controls)}
        </div>
        {drawerOpen && <div data-kit-shell="backdrop" onClick={closeDrawer} aria-hidden="true" />}
        <div data-kit-shell="column" style={column}>
          {renderSlot(topbar, controls)}
          {renderSlot(banner, controls)}
          {main === "shell" ? (
            <main {...mainProps} data-kit-shell="main" id={mainId} style={{ ...mainBase, ...mainStyle }}>
              {children}
            </main>
          ) : (
            children
          )}
          {renderSlot(footer, controls)}
        </div>
        {renderSlot(bottomNav, controls)}
        {renderSlot(palette, controls)}
      </div>
    </ShellContext.Provider>
  );
}
