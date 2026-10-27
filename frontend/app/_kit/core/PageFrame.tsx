// UI kit core (root ADR 0016). Canonical: AA-CIS-App frontend/app/_kit/core. Edit only there; copies are checked byte for byte (kit.lock.json).
"use client";
// app/_kit/core/PageFrame.tsx
// PageFrame — the one content frame every page renders inside its app shell (S252 as `AdminMain`; renamed in AA-994
// S1a, root ADR 0016 decision 1). `AdminMain`, `AdminPaneHeader` and `AdminPaneBody` stay as aliases, so pages that
// use the old names do not change. In CIS every page under app/admin renders it exactly once (guarded by
// lib/adminFrame.test.mjs); no page hand-writes <main className="aa-admin-main" style={...}>.
//
// variant "page" (default): the standard padding (28px 36px 56px on desktop; the `.aa-admin-main` phone rule in
// globals.css makes it 16px with room for the BottomNav), its own vertical scroll and `minWidth: 0` so wide
// tables scroll inside their card, never the page.
//
// variant "split": for pages with a fixed/sticky header band over a body (Social Content, Content Trace, Master
// Content, S1 Rewrite, Jev Decisions, A4 Quality). The frame itself has no side padding; the page puts
// PageFrameHeader (the band) and PageFrameBody (the content) inside it, which carry the same 36px desktop inset
// and the same 16px phone inset, so the header band can run edge to edge with its border while the text lines
// up with every other page. The split frame keeps the 84px phone gap above the BottomNav.
//
// `data-kit-frame` marks the frame for the automated visual job (root ADR 0016 decision 9: a page without it fails).
import type { CSSProperties, ReactNode } from "react";
import { K } from "./tokens";

type FrameProps = { children: ReactNode; style?: CSSProperties; testId?: string; className?: string };

export function PageFrame({ children, style, testId, variant = "page" }: FrameProps & { variant?: "page" | "split" }) {
  if (variant === "split") {
    return (
      <main className="aa-admin-split" data-testid={testId} data-kit-frame="split"
        style={{ flex: 1, minWidth: 0, minHeight: 0, display: "flex", flexDirection: "column", overflowY: "auto", ...style }}>
        {children}
      </main>
    );
  }
  return (
    <main className="aa-admin-main" data-testid={testId} data-kit-frame="page"
      style={{ flex: 1, minWidth: 0, minHeight: 0, overflowY: "auto", padding: "28px 36px 56px", ...style }}>
      {children}
    </main>
  );
}

/** The header band of a split page: page background, bottom rule, 28px 36px 16px (phone: 16px sides). Pass
 *  `sticky` when the frame itself scrolls and the band should stay on top. */
export function PageFrameHeader({ children, style, testId, className, sticky }: FrameProps & { sticky?: boolean }) {
  return (
    <div className={`aa-admin-pane aa-admin-pane-head${className ? ` ${className}` : ""}`} data-testid={testId}
      style={{
        flexShrink: 0, minWidth: 0, background: K.bg, borderBottom: `1px solid ${K.line}`,
        padding: "28px 36px 16px",
        ...(sticky ? { position: "sticky" as const, top: 0, zIndex: 5 } : {}),
        ...style,
      }}>
      {children}
    </div>
  );
}

/** The body of a split page: 20px 36px 56px (phone: 16px sides). Pass `scroll` when the body, not the frame, is
 *  the scroll region (the header band then never moves). */
export function PageFrameBody({ children, style, testId, className, scroll }: FrameProps & { scroll?: boolean }) {
  return (
    <div className={`aa-admin-pane aa-admin-pane-body${className ? ` ${className}` : ""}`} data-testid={testId}
      style={{
        minWidth: 0, padding: "20px 36px 56px",
        ...(scroll ? { flex: 1, minHeight: 0, overflowY: "auto" as const } : {}),
        ...style,
      }}>
      {children}
    </div>
  );
}

/** The S252 names, kept so existing pages do not change (CIS admin renders `<AdminMain>`). */
export const AdminMain = PageFrame;
export const AdminPaneHeader = PageFrameHeader;
export const AdminPaneBody = PageFrameBody;
