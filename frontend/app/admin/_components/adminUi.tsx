// app/(admin)/_components/adminUi.tsx
// Design system for admin — Adventure Asia brand tokens (app/_brand/tokens.ts, AA-605).
// AA-601 part B: `A` returns the `var(--aa-…)` CSS variables from app/globals.css (light = the
// previous hex, dark = the dark palette), so <html data-theme="dark"> recolours every admin page.
// `BRAND` stays hex for the login pages / portal `T`. Use `alpha(color, pct)` (from _kit/tokens)
// for translucent fills — the old hex-alpha-suffix trick breaks with var().

import { Loader2 } from "lucide-react";
import { BTN_PRIMARY_TEXT, BTN_RADIUS, FONT_DISPLAY, FONT_MONO, FONT_SANS } from "../../_brand/tokens";
import { alpha } from "../../_kit/tokens";

export { alpha };

export const A = {
  // Accent — brand gold (AA-605: was a red that also meant "error")
  accent:       "var(--aa-accent)",
  accentDeep:   "var(--aa-accent-deep)",
  accentSoft:   "var(--aa-accent-soft)",
  accentTint:   "var(--aa-accent-tint)",
  accentBorder: "var(--aa-accent-border)",
  slate:        "var(--aa-slate)",
  // `red*` now means danger/error only (failed, blocked, delete, low score).
  red:       "var(--aa-red)",
  redSoft:   "var(--aa-red-soft)",
  redTint:   "var(--aa-red-tint)",
  redBorder: "var(--aa-red-border)",
  // Base (shared with portal)
  ink:       "var(--aa-ink)",
  ink2:      "var(--aa-ink2)",
  ink3:      "var(--aa-ink3)",
  body:      "var(--aa-body)",
  muted:     "var(--aa-muted)",
  muted2:    "var(--aa-muted2)",
  bg:        "var(--aa-bg)",
  card:      "var(--aa-card)",
  line:      "var(--aa-line)",
  line2:     "var(--aa-line2)",
  green:     "var(--aa-green)",
  greenSoft: "var(--aa-green-soft)",
  amber:     "var(--aa-amber)",
  amberSoft: "var(--aa-amber-soft)",
  gold:      "var(--aa-gold)",
  goldTint:  "var(--aa-gold-tint)",
} as const;

// `serif` keeps its name (used for titles and big numbers everywhere) but is now the brand
// display face, Fahkwang.
export const serif = FONT_DISPLAY;
export const mono  = FONT_MONO;
export const sans  = FONT_SANS;

// AA-412 follow-up (layout fixes round) — AdminSidebar's own width was a bare `220` literal with
// no shared constant, so a viewport-covering element (e.g. a `position: fixed` modal) had no way
// to know how much space to leave for it without either a portal-free DOM approach or duplicating
// the number. Single source of truth now — AdminSidebar imports this instead of hardcoding it.
// AA-605: 220 -> 236 — Poppins is wider than IBM Plex Sans; with the "ADMIN" nav tag, labels
// like "External Spend" were truncated at 220.
export const SIDEBAR_WIDTH = 236;

// ── Card ─────────────────────────────────────────────────────────────────────
export function Card({ children, style = {}, dark = false }: {
  children: React.ReactNode; style?: React.CSSProperties; dark?: boolean;
}) {
  return (
    <div style={{
      // The `dark` variant is a deliberately-dark surface in BOTH themes (used for hero/stat
      // panels), so it reads from the fixed-dark sidebar tokens, not A.ink (which is light in
      // the dark theme).
      background: dark ? "linear-gradient(160deg,var(--aa-side-bg) 0%,var(--aa-side-panel) 100%)" : A.card,
      border: `1px solid ${dark ? "var(--aa-side-panel)" : A.line}`,
      borderRadius: 12, padding: "20px 22px", position: "relative",
      ...style,
    }}>{children}</div>
  );
}

// ── Section label ─────────────────────────────────────────────────────────────
export function SLabel({ children, light = false, style }: { children: React.ReactNode; light?: boolean; style?: React.CSSProperties }) {
  return (
    <div style={{
      fontSize: 11, fontWeight: 600, textTransform: "uppercase",
      letterSpacing: "0.14em", color: light ? "rgba(255,255,255,0.5)" : A.muted,
      marginBottom: 14, ...style,
    }}>{children}</div>
  );
}

// ── Stat card ─────────────────────────────────────────────────────────────────
export function StatCard({ label, value, sub, accent = A.accent, icon }: {
  label: string; value: string; sub?: string; accent?: string; icon?: React.ReactNode;
}) {
  return (
    <Card>
      <div style={{ display: "flex", alignItems: "center", gap: 10, marginBottom: 12 }}>
        {icon && (
          <div style={{ padding: 8, borderRadius: 8, background: alpha(accent, 8), color: accent }}>
            {icon}
          </div>
        )}
        <span style={{ fontSize: 12, color: A.muted }}>{label}</span>
      </div>
      <div style={{ fontFamily: sans, fontVariantNumeric: "tabular-nums", fontSize: 28, fontWeight: 600, color: A.ink, letterSpacing: "-0.02em" }}>
        {value}
      </div>
      {sub && <div style={{ fontSize: 11, color: A.muted2, marginTop: 4 }}>{sub}</div>}
    </Card>
  );
}

// ── Tab bar ───────────────────────────────────────────────────────────────────
export function TabBar({ tabs, active, onChange }: {
  tabs: { key: string; label: string }[];
  active: string;
  onChange: (k: string) => void;
}) {
  return (
    <div style={{
      display: "flex", gap: 4, padding: "4px",
      background: A.line2, borderRadius: 10, width: "fit-content",
    }}>
      {tabs.map(t => (
        <button key={t.key} onClick={() => onChange(t.key)} style={{
          padding: "7px 16px", borderRadius: 7, border: "none",
          background: active === t.key ? A.card : "transparent",
          color: active === t.key ? A.ink : A.muted,
          fontSize: 13, fontWeight: active === t.key ? 600 : 400,
          cursor: "pointer", fontFamily: sans,
          boxShadow: active === t.key ? "0 1px 3px rgba(0,0,0,0.08)" : "none",
          transition: "all .15s",
        }}>{t.label}</button>
      ))}
    </div>
  );
}

// ── Badge ─────────────────────────────────────────────────────────────────────
export function Badge({ children, color = "gray" }: {
  children: React.ReactNode;
  color?: "red" | "green" | "amber" | "gray" | "gold" | "blue" | "purple";
}) {
  const s = {
    red:    { bg: A.redSoft, c: A.red },
    green:  { bg: "var(--aa-green-bg)", c: "var(--aa-green-darkest)" },
    amber:  { bg: "var(--aa-amber-bg)", c: "var(--aa-amber-deep)" },
    gray:   { bg: "var(--aa-neutral-bg)", c: "var(--aa-neutral-fg)" },
    gold:   { bg: A.accentTint, c: A.accentDeep },
    blue:   { bg: "var(--aa-blue-bg2)", c: "var(--aa-blue-darkest)" },
    purple: { bg: "var(--aa-purple-bg)", c: "var(--aa-purple-strong)" },
  }[color];
  return (
    <span style={{
      display: "inline-flex", alignItems: "center", gap: 5,
      fontSize: 11, fontWeight: 600, padding: "3px 9px",
      borderRadius: 999, letterSpacing: "0.04em", textTransform: "uppercase",
      background: s.bg, color: s.c,
    }}>
      <span style={{ width: 5, height: 5, borderRadius: "50%", background: s.c, display: "block", flexShrink: 0 }} />
      {children}
    </span>
  );
}

// ── Spinner ───────────────────────────────────────────────────────────────────
export function Spinner({ size = 16 }: { size?: number }) {
  return <Loader2 size={size} style={{ animation: "spin 1s linear infinite", color: A.accent }} />;
}

export function LoadingScreen({ msg = "Loading..." }: { msg?: string }) {
  return (
    <div style={{ display: "flex", flexDirection: "column", alignItems: "center", justifyContent: "center", minHeight: 300, gap: 10 }}>
      <Spinner size={22} /><span style={{ fontSize: 13, color: A.muted }}>{msg}</span>
    </div>
  );
}

// ── Button ────────────────────────────────────────────────────────────────────
export function Btn({ children, onClick, variant = "secondary", size = "md", disabled = false, style = {}, title, ariaLabel }: {
  children: React.ReactNode; onClick?: () => void; title?: string; ariaLabel?: string;
  variant?: "primary" | "secondary" | "danger" | "ghost";
  size?: "sm" | "md" | "lg"; disabled?: boolean; style?: React.CSSProperties;
}) {
  const pad = { sm: "5px 12px", md: "8px 16px", lg: "10px 22px" }[size];
  const fz  = { sm: 11, md: 13, lg: 14 }[size];
  const base: Record<string, React.CSSProperties> = {
    primary:   { background: A.accent,  color: "var(--aa-on-accent)",  border: `1px solid ${A.accent}` },
    secondary: { background: A.card,    color: A.ink3,  border: `1px solid ${A.line}` },
    danger:    { background: A.redSoft, color: A.red,   border: `1px solid ${A.redBorder}` },
    ghost:     { background: "transparent", color: A.muted, border: `1px solid ${A.line}` },
  };
  return (
    <button onClick={onClick} disabled={disabled} title={title} aria-label={ariaLabel} style={{
      display: "inline-flex", alignItems: "center", justifyContent: "center", gap: 6,
      padding: pad, borderRadius: BTN_RADIUS, fontSize: fz, fontWeight: 600,
      cursor: disabled ? "not-allowed" : "pointer", opacity: disabled ? 0.5 : 1,
      transition: "opacity .15s", fontFamily: sans,
      // AA-605: brand uppercase only on md/lg primaries; "sm" sits in dense table rows where the
      // wider uppercase label overflowed (Review Queue actions column).
      ...base[variant], ...(variant === "primary" && size !== "sm" ? BTN_PRIMARY_TEXT : {}), ...style,
    }}>{children}</button>
  );
}

// ── Table wrapper ─────────────────────────────────────────────────────────────
export const TH: React.CSSProperties = {
  padding: "10px 16px", fontSize: 11, fontWeight: 600,
  textTransform: "uppercase", letterSpacing: "0.1em",
  color: A.muted, textAlign: "left", background: A.bg,
  borderBottom: `1px solid ${A.line}`,
};

export const TD: React.CSSProperties = {
  padding: "13px 16px", fontSize: 13, color: A.body,
  borderBottom: `1px solid ${A.line2}`,
};

// ── TOOLTIP (recharts) ────────────────────────────────────────────────────────
// Fixed-dark panel in both themes (a tooltip floats over the chart); on-dark text.
export const CHART_TOOLTIP = {
  contentStyle: {
    background: "var(--aa-side-bg)", border: "1px solid var(--aa-side-panel)",
    borderRadius: 8, fontSize: 12, color: "var(--aa-on-dark)",
  },
  labelStyle: { color: "var(--aa-on-dark)" },
};

// ── Chart card ────────────────────────────────────────────────────────────────
export function ChartCard({ title, children }: { title: string; children: React.ReactNode }) {
  return (
    <Card>
      <SLabel>{title}</SLabel>
      {children}
    </Card>
  );
}
