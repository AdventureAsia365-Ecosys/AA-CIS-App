"use client";
// AdminMain — the one content frame every admin page renders inside the shell (S252): the standard padding
// (28px 36px 56px on desktop; the `.aa-admin-main` phone rule in globals.css makes it 16px with room for the
// BottomNav), its own vertical scroll and `minWidth: 0` so wide tables scroll inside their card, never the page.
// New pages use it instead of a hand-written <main style={...}>.
import type { CSSProperties, ReactNode } from "react";

export function AdminMain({ children, style, testId }: { children: ReactNode; style?: CSSProperties; testId?: string }) {
  return (
    <main className="aa-admin-main" data-testid={testId}
      style={{ flex: 1, minWidth: 0, minHeight: 0, overflowY: "auto", padding: "28px 36px 56px", ...style }}>
      {children}
    </main>
  );
}
