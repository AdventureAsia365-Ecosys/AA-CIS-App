// app/(admin)/_components/adminUi.tsx
// Design system for admin — Adventure Asia brand tokens (app/_brand/tokens.ts, AA-605).

import { Loader2 } from "lucide-react";
import { BRAND, BTN_PRIMARY_TEXT, BTN_RADIUS, FONT_DISPLAY, FONT_MONO, FONT_SANS } from "../../_brand/tokens";

export const A = {
  // Accent — brand gold (AA-605: was red #EF4444, which also meant "error")
  accent:       BRAND.accent,
  accentDeep:   BRAND.accentDeep,
  accentSoft:   BRAND.accentSoft,
  accentTint:   BRAND.accentTint,
  accentBorder: BRAND.accentBorder,
  slate:        BRAND.slate,
  // `red*` now means danger/error only (failed, blocked, delete, low score).
  red:       BRAND.danger,
  redSoft:   BRAND.dangerSoft,
  redTint:   BRAND.dangerTint,
  redBorder: BRAND.dangerBorder,
  // Base (shared with portal)
  ink:       BRAND.ink,
  ink2:      BRAND.ink2,
  ink3:      BRAND.ink3,
  body:      BRAND.body,
  muted:     BRAND.muted,
  muted2:    BRAND.muted2,
  bg:        BRAND.bg,
  card:      BRAND.card,
  line:      BRAND.line,
  line2:     BRAND.line2,
  green:     BRAND.success,
  greenSoft: BRAND.successSoft,
  amber:     "#F59E0B",
  amberSoft: "#FEF3C7",
  gold:      BRAND.accent,
  goldTint:  BRAND.accentTint,
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
export const SIDEBAR_WIDTH = 220;

// ── Card ─────────────────────────────────────────────────────────────────────
export function Card({ children, style = {}, dark = false }: {
  children: React.ReactNode; style?: React.CSSProperties; dark?: boolean;
}) {
  return (
    <div style={{
      background: dark ? `linear-gradient(160deg,${A.ink} 0%,${A.ink2} 100%)` : A.card,
      border: `1px solid ${dark ? A.ink2 : A.line}`,
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
          <div style={{ padding: 8, borderRadius: 8, background: `${accent}15`, color: accent }}>
            {icon}
          </div>
        )}
        <span style={{ fontSize: 12, color: A.muted }}>{label}</span>
      </div>
      <div style={{ fontFamily: serif, fontSize: 28, fontWeight: 500, color: A.ink, letterSpacing: "-0.02em" }}>
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
    green:  { bg: "#D1FAE5", c: "#065F46" },
    amber:  { bg: "#FEF3C7", c: "#92400E" },
    gray:   { bg: "#F3F4F6", c: "#4B5563" },
    gold:   { bg: A.accentTint, c: A.accentDeep },
    blue:   { bg: "#DBEAFE", c: "#1E40AF" },
    purple: { bg: "#EDE9FE", c: "#5B21B6" },
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
export function Btn({ children, onClick, variant = "secondary", size = "md", disabled = false, style = {} }: {
  children: React.ReactNode; onClick?: () => void;
  variant?: "primary" | "secondary" | "danger" | "ghost";
  size?: "sm" | "md" | "lg"; disabled?: boolean; style?: React.CSSProperties;
}) {
  const pad = { sm: "5px 12px", md: "8px 16px", lg: "10px 22px" }[size];
  const fz  = { sm: 11, md: 13, lg: 14 }[size];
  const base: Record<string, React.CSSProperties> = {
    primary:   { background: A.accent,  color: "#fff",  border: `1px solid ${A.accent}`, ...BTN_PRIMARY_TEXT },
    secondary: { background: A.card,    color: A.ink3,  border: `1px solid ${A.line}` },
    danger:    { background: A.redSoft, color: A.red,   border: `1px solid ${A.redBorder}` },
    ghost:     { background: "transparent", color: A.muted, border: `1px solid ${A.line}` },
  };
  return (
    <button onClick={onClick} disabled={disabled} style={{
      display: "inline-flex", alignItems: "center", justifyContent: "center", gap: 6,
      padding: pad, borderRadius: BTN_RADIUS, fontSize: fz, fontWeight: 600,
      cursor: disabled ? "not-allowed" : "pointer", opacity: disabled ? 0.5 : 1,
      transition: "opacity .15s", fontFamily: sans,
      ...base[variant], ...style,
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
export const CHART_TOOLTIP = {
  contentStyle: {
    background: A.ink, border: `1px solid ${A.ink2}`,
    borderRadius: 8, fontSize: 12, color: "#F8F6F2",
  },
  labelStyle: { color: "#F8F6F2" },
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
