// UI kit core (root ADR 0016). Canonical: AA-CIS-App frontend/app/_kit/core. Edit only there; copies are checked byte for byte (kit.lock.json).
// app/_kit/core/Section.tsx
// AA-994 S4a — the card region pages are built from, upstreamed from AA-Booking `ui/booking/Section.tsx` (design
// system "Cards": card background, 1px line, radius 12, no double frame). An optional header band carries the title
// (h2), a description and actions at the right, over a rule; the body is padded 20px unless `padded={false}` (a
// DataTable or a list that draws its own rows edge to edge).
//
// Replaces the page-local `Section` / `Panel` / `Card` helpers of the apps (CIS had seven). Stack sections in a
// PageFrame column (`width` or `title` set: 20px gap) or give them a margin through `style`.
//
// No hooks, no "use client": a server component may render it.

import type { CSSProperties, ReactNode } from "react";
import { K, RADIUS, sans } from "./tokens";

export function Section({
  title,
  description,
  actions,
  children,
  padded = true,
  testId,
  style,
  bodyStyle,
  className,
}: {
  title?: ReactNode;
  description?: ReactNode;
  /** Buttons or links at the right of the header band. */
  actions?: ReactNode;
  children?: ReactNode;
  /** false: the body has no padding (a table or list that runs edge to edge). */
  padded?: boolean;
  testId?: string;
  /** Outer box (e.g. a margin when the section is not in a PageFrame column). */
  style?: CSSProperties;
  /** Body box (e.g. a flex column with a gap). */
  bodyStyle?: CSSProperties;
  /** For Tailwind apps (Booking); inline-style apps leave it out. */
  className?: string;
}) {
  const header = title != null || description != null || actions != null;
  return (
    <section
      data-testid={testId}
      className={className}
      style={{
        minWidth: 0,
        borderRadius: RADIUS.lg,
        border: `1px solid ${K.line}`,
        background: K.card,
        fontFamily: sans,
        ...style,
      }}
    >
      {header && (
        <header
          style={{
            display: "flex",
            flexWrap: "wrap",
            alignItems: "flex-start",
            justifyContent: "space-between",
            gap: 12,
            padding: "16px 20px",
            borderBottom: `1px solid ${K.line2}`,
          }}
        >
          <div style={{ minWidth: 0 }}>
            {title != null && (
              <h2 style={{ margin: 0, fontSize: 15, fontWeight: 600, color: K.ink, fontFamily: sans }}>{title}</h2>
            )}
            {description != null && (
              <div style={{ marginTop: title != null ? 2 : 0, fontSize: 13, color: K.muted }}>{description}</div>
            )}
          </div>
          {actions != null && (
            <div style={{ display: "flex", flexWrap: "wrap", alignItems: "center", gap: 8 }}>{actions}</div>
          )}
        </header>
      )}
      <div style={{ minWidth: 0, ...(padded ? { padding: 20 } : {}), ...bodyStyle }}>{children}</div>
    </section>
  );
}
