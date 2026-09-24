// app/(tenant)/portal/_components/ui.tsx
// Design system — Adventure Asia brand tokens shared with the admin UI (app/_brand/tokens.ts,
// AA-605): Fahkwang display + Poppins UI + JetBrains Mono, gold accent, pill buttons.

import { Loader2 } from "lucide-react";
import { BRAND, BTN_PRIMARY_TEXT, BTN_RADIUS, FONT_DISPLAY, FONT_MONO, FONT_SANS } from "../../../_brand/tokens";

// ─── Tokens ───────────────────────────────────────────────────────────────────
// Key names unchanged (hundreds of call sites); values come from BRAND. red = danger only
// (AA-605: #B14A3B -> brand #C20101, same value as admin's A.red).
export const T = {
  gold: BRAND.accent, goldDeep: BRAND.accentDeep, goldSoft: BRAND.accentSoft, goldTint: BRAND.accentTint,
  ink: BRAND.ink, ink2: BRAND.ink2, ink3: BRAND.ink3,
  body: BRAND.body, muted: BRAND.muted, muted2: BRAND.muted2,
  bg: BRAND.bg, card: BRAND.card, line: BRAND.line, line2: BRAND.line2,
  green: BRAND.success, greenSoft: BRAND.successSoft,
  red: BRAND.danger, redSoft: BRAND.dangerSoft, redBorder: BRAND.dangerBorder,
  amber: "#B5791F", amberSoft: "#FBEFD6",
} as const;

// `serif` keeps its name; it is now the brand display face (titles only — numbers use `sans`).
export const serif = FONT_DISPLAY;
export const mono  = FONT_MONO;
export const sans  = FONT_SANS;

// ─── Helpers ──────────────────────────────────────────────────────────────────
export function parseJSON(raw: unknown): unknown {
  if (raw == null) return null;
  try {
    let v = typeof raw === "string" ? JSON.parse(raw) : raw;
    if (typeof v === "string") v = JSON.parse(v);
    return v;
  } catch { return null; }
}

export function parseHighlights(raw: unknown): string[] {
  const v = parseJSON(raw);
  return Array.isArray(v) ? (v as string[]) : [];
}

export function parseContent(raw: unknown): Record<string, unknown> | null {
  const v = parseJSON(raw);
  if (v && typeof v === "object" && !Array.isArray(v)) return v as Record<string, unknown>;
  return null;
}

// AA-566 Phần A — GET /v1/tours/my-versions returns one row per VERSION
// (gold_aa_internal.tenant_tour_versions), not one per tour. AA-565 made CatalogTab.tsx's own
// heading count unique tours (1 row per tour, latest version only) but layout.tsx's Sidebar
// badge still read the flat `pagination.total` from the same endpoint — the exact bug this
// fixes: both must count the same thing (unique tours), from the same client-side dedup logic,
// not two different numbers from two different readings of one endpoint.
export function countUniqueTours(list: { published_tour_id?: string | null }[]): number {
  return new Set(list.filter(v => v.published_tour_id).map(v => v.published_tour_id)).size;
}

export function fmtDate(iso: string | null | undefined): string {
  if (!iso) return "—";
  const d = new Date(iso);
  if (isNaN(d.getTime())) return "—";
  return d.toLocaleDateString("en-GB", { day: "2-digit", month: "short", year: "numeric" });
}

export function fmtDateTime(iso: string | null | undefined): string {
  if (!iso) return "—";
  const d = new Date(iso);
  if (isNaN(d.getTime())) return "—";
  return d.toLocaleString("en-GB", { day: "2-digit", month: "short", hour: "2-digit", minute: "2-digit" });
}

// ─── Status helpers ───────────────────────────────────────────────────────────
export type BadgeVariant = "success" | "warning" | "error" | "info" | "gold" | "default";

const BADGE_MAP: Record<BadgeVariant, { bg: string; color: string; dot: string }> = {
  success: { bg: T.greenSoft, color: T.green, dot: T.green },
  warning: { bg: T.amberSoft, color: T.amber, dot: T.gold },
  error:   { bg: T.redSoft, color: T.red, dot: T.red },
  info:    { bg: "#E8EEF4", color: "#3A4F66", dot: "#5C7895" },
  gold:    { bg: T.goldTint, color: T.goldDeep, dot: T.gold },
  default: { bg: "#F0EBE0", color: "#6B7380", dot: "#9099A6" },
};

export function statusVariant(s: string): BadgeVariant {
  if (s === "approved") return "success";
  if (s === "rejected") return "error";
  if (s === "pending" || s === "needs_review") return "warning";
  return "default";
}

// ─── UI Components ────────────────────────────────────────────────────────────

export function Card({ children, style = {}, dark = false }: {
  children: React.ReactNode; style?: React.CSSProperties; dark?: boolean;
}) {
  return (
    <div style={{
      background: dark ? `linear-gradient(160deg,${T.ink} 0%,${T.ink2} 100%)` : T.card,
      border: `1px solid ${dark ? T.ink2 : T.line}`,
      borderRadius: 12, padding: "22px 22px 20px", position: "relative",
      ...style,
    }}>{children}</div>
  );
}

// AA-524 — shared sticky-header wrapper. `<main>` in layout.tsx is the scroll container (the
// outer breadcrumb bar above it is already its own sticky element, zIndex 10); this sticks AT
// the top of that same scrollport, one zIndex band below so the two never compete.
//
// `top: 0` on ANY descendant of `<main>` resolves to `<main>`'s own padding edge — 28px down
// (layout.tsx's `<main>` has `padding: "28px 36px 56px"`), REGARDLESS of how deep the sticky
// element is nested. That 28px band (viewport y 56-84) is inside `<main>`'s padding box, so
// `overflow-y: auto` does NOT clip it — anything that scrolls up INTO that band while this bar
// is already stuck renders ABOVE the bar, uncovered (confirmed live: a form input scrolled
// there, in front of a page title). `bleedTop` (default 28, matching that padding) extends this
// bar's own opaque background up to cover it — every usage needs this, not just page-level ones,
// since the 28px offset is constant no matter the nesting depth. Only skip it (bleedTop=0) for a
// bar that is deliberately NOT the first sticky point `<main>` will hit while scrolling.
export function StickyBar({ children, background = T.bg, style = {}, bleedTop = 28 }: {
  children: React.ReactNode; background?: string; style?: React.CSSProperties; bleedTop?: number;
}) {
  const { marginTop, paddingTop, ...rest } = style;
  const mt = (typeof marginTop === "number" ? marginTop : 0) - bleedTop;
  const pt = (typeof paddingTop === "number" ? paddingTop : 0) + bleedTop;
  return (
    <div style={{
      position: "sticky", top: 0, zIndex: 5, background,
      marginTop: mt, paddingTop: pt,
      ...rest,
    }}>{children}</div>
  );
}

export function CardHead({ title, action, light = false }: {
  title: string; action?: React.ReactNode; light?: boolean;
}) {
  return (
    <div style={{ display: "flex", alignItems: "center", justifyContent: "space-between", marginBottom: 16 }}>
      <span style={{ fontSize: 11, fontWeight: 600, textTransform: "uppercase", letterSpacing: "0.14em", color: light ? "rgba(255,255,255,0.5)" : T.muted }}>{title}</span>
      {action && <div style={{ fontSize: 12, color: T.ink3 }}>{action}</div>}
    </div>
  );
}

export function Badge({ children, variant = "default" }: {
  children: React.ReactNode; variant?: BadgeVariant;
}) {
  const s = BADGE_MAP[variant];
  return (
    <span style={{ display: "inline-flex", alignItems: "center", gap: 5, fontSize: 11, fontWeight: 600, padding: "3px 9px", borderRadius: 999, letterSpacing: "0.04em", textTransform: "uppercase", background: s.bg, color: s.color }}>
      <span style={{ width: 5, height: 5, borderRadius: "50%", background: s.dot, display: "block", flexShrink: 0 }} />
      {children}
    </span>
  );
}

export function ScoreBadge({ score }: { score: number | null }) {
  if (score == null) return <span style={{ fontSize: 11, color: T.muted2, fontFamily: mono }}>—</span>;
  const color = score >= 9 ? T.green : score >= 7 ? T.amber : T.red;
  const bg    = score >= 9 ? "#E4F1E9" : score >= 7 ? "#FBEFD6" : "#FBE7E1";
  return <span style={{ fontFamily: mono, fontSize: 11.5, fontWeight: 600, padding: "2px 8px", borderRadius: 6, background: bg, color }}>★ {score.toFixed(1)}</span>;
}

export function ProgressBar({ pct, warn = false }: { pct: number; warn?: boolean }) {
  return (
    <div style={{ height: 7, background: T.line2, borderRadius: 999, overflow: "hidden" }}>
      <div style={{ height: "100%", width: `${Math.min(100, Math.max(0, pct))}%`, borderRadius: 999, transition: "width 0.5s ease", background: warn ? `linear-gradient(90deg,${T.amber},${T.red})` : `linear-gradient(90deg,${T.gold},#C9851F)` }} />
    </div>
  );
}

export function Spinner({ size = 16 }: { size?: number }) {
  return <Loader2 size={size} style={{ animation: "spin 1s linear infinite", color: T.gold }} />;
}

export function LoadingScreen({ message = "Loading..." }: { message?: string }) {
  return (
    <div style={{ display: "flex", flexDirection: "column", alignItems: "center", justifyContent: "center", minHeight: 400, gap: 12 }}>
      <Spinner size={22} /><span style={{ fontSize: 13, color: T.muted }}>{message}</span>
    </div>
  );
}

export function EmptyState({ icon, title, sub, action }: {
  icon: React.ReactNode; title: string; sub?: string; action?: React.ReactNode;
}) {
  return (
    <div style={{ textAlign: "center", padding: "60px 20px", display: "flex", flexDirection: "column", alignItems: "center", gap: 12 }}>
      <div style={{ fontSize: 36 }}>{icon}</div>
      <div style={{ fontSize: 15, fontWeight: 600, color: T.ink }}>{title}</div>
      {sub && <div style={{ fontSize: 13, color: T.muted, maxWidth: 320, lineHeight: 1.5 }}>{sub}</div>}
      {action}
    </div>
  );
}

type BtnVariant = "primary" | "secondary" | "ghost" | "danger";
type BtnSize = "sm" | "md" | "lg";

export function Btn({ children, onClick, variant = "secondary", size = "md", disabled = false, style = {} }: {
  children: React.ReactNode; onClick?: () => void; variant?: BtnVariant;
  size?: BtnSize; disabled?: boolean; style?: React.CSSProperties;
}) {
  const pad: Record<BtnSize, string> = { sm: "5px 12px", md: "8px 18px", lg: "11px 24px" };
  const fz:  Record<BtnSize, number> = { sm: 11, md: 13, lg: 14 };
  const base: Record<BtnVariant, React.CSSProperties> = {
    primary:   { background: T.gold,    color: "#fff", border: `1px solid ${T.gold}` },
    secondary: { background: T.card,    color: T.ink3, border: `1px solid ${T.line}` },
    ghost:     { background: "transparent", color: T.muted, border: `1px solid ${T.line}` },
    danger:    { background: T.redSoft, color: T.red,  border: `1px solid ${T.redBorder}` },
  };
  return (
    <button onClick={onClick} disabled={disabled} style={{ display: "inline-flex", alignItems: "center", justifyContent: "center", gap: 6, padding: pad[size], borderRadius: BTN_RADIUS, fontSize: fz[size], fontWeight: 600, cursor: disabled ? "not-allowed" : "pointer", opacity: disabled ? 0.5 : 1, transition: "all 0.15s", fontFamily: sans, ...base[variant], ...(variant === "primary" && size !== "sm" ? BTN_PRIMARY_TEXT : {}), ...style }}>{children}</button>
  );
}

export function Field({ label, value, onChange, rows = 3, placeholder = "", hint, hintColor }: {
  label: string; value: string; onChange: (v: string) => void;
  rows?: number; placeholder?: string; hint?: string; hintColor?: string;
}) {
  return (
    <div style={{ marginBottom: 16 }}>
      <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", marginBottom: 6 }}>
        <label style={{ fontSize: 11, fontWeight: 600, textTransform: "uppercase", letterSpacing: "0.1em", color: T.muted }}>{label}</label>
        {hint && <span style={{ fontSize: 11, color: hintColor ?? T.muted2 }}>{hint}</span>}
      </div>
      <textarea value={value} onChange={e => onChange(e.target.value)} rows={rows} placeholder={placeholder}
        style={{ width: "100%", padding: "10px 12px", background: T.bg, border: `1px solid ${T.line}`, borderRadius: 8, color: T.body, fontSize: 13, resize: "vertical", outline: "none", lineHeight: 1.6, fontFamily: sans, boxSizing: "border-box" }} />
    </div>
  );
}
