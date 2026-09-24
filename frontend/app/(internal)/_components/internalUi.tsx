// app/(internal)/_components/internalUi.tsx
import { Loader2 } from "lucide-react";
import { BRAND, BTN_RADIUS, FONT_DISPLAY, FONT_MONO, FONT_SANS } from "../../_brand/tokens";

// AA-605: colours + fonts come from app/_brand/tokens.ts (same source as admin `A` and portal `T`);
// key names stay as-is for the existing call sites.
export const A = {
  gold:      BRAND.accent,
  goldSoft:  BRAND.accentSoft,
  goldTint:  BRAND.accentTint,
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
  red:       BRAND.danger,
  redSoft:   BRAND.dangerSoft,
  amber:     "#F59E0B",
  amberSoft: "#FEF3C7",
} as const;

export const serif = FONT_DISPLAY;
export const mono  = FONT_MONO;
export const sans  = FONT_SANS;

export function Card({ children, style = {} }: { children: React.ReactNode; style?: React.CSSProperties }) {
  return (
    <div style={{ background: A.card, border: `1px solid ${A.line}`, borderRadius: 12, padding: "20px 22px", ...style }}>
      {children}
    </div>
  );
}

export function SLabel({ children }: { children: React.ReactNode }) {
  return <div style={{ fontSize: 11, fontWeight: 600, textTransform: "uppercase", letterSpacing: "0.14em", color: A.muted, marginBottom: 12 }}>{children}</div>;
}

export function Btn({ children, onClick, variant = "secondary", size = "md", disabled = false, style = {} }: {
  children: React.ReactNode; onClick?: () => void;
  variant?: "primary" | "secondary" | "danger" | "ghost";
  size?: "sm" | "md" | "lg"; disabled?: boolean; style?: React.CSSProperties;
}) {
  const pad = { sm: "5px 12px", md: "8px 16px", lg: "10px 22px" }[size];
  const fz  = { sm: 11, md: 13, lg: 14 }[size];
  const base: Record<string, React.CSSProperties> = {
    primary:   { background: A.gold,    color: A.ink, border: `1px solid ${A.gold}` },
    secondary: { background: A.card,    color: A.ink,  border: `1px solid ${A.line}` },
    danger:    { background: A.redSoft, color: A.red,  border: `1px solid ${BRAND.dangerBorder}` },
    ghost:     { background: "transparent", color: A.muted, border: `1px solid ${A.line}` },
  };
  return (
    <button onClick={onClick} disabled={disabled} style={{ display: "inline-flex", alignItems: "center", justifyContent: "center", gap: 6, padding: pad, borderRadius: BTN_RADIUS, fontSize: fz, fontWeight: 600, cursor: disabled ? "not-allowed" : "pointer", opacity: disabled ? 0.5 : 1, transition: "opacity .15s", fontFamily: sans, ...base[variant], ...style }}>
      {children}
    </button>
  );
}

export function Spinner({ size = 16 }: { size?: number }) {
  return <Loader2 size={size} style={{ animation: "spin 1s linear infinite", color: A.gold }} />;
}

export function LoadingScreen({ msg = "Loading..." }: { msg?: string }) {
  return (
    <div style={{ display: "flex", flexDirection: "column", alignItems: "center", justifyContent: "center", minHeight: 300, gap: 10 }}>
      <Spinner size={22} /><span style={{ fontSize: 13, color: A.muted }}>{msg}</span>
    </div>
  );
}

export const TH: React.CSSProperties = {
  padding: "10px 16px", fontSize: 11, fontWeight: 600,
  textTransform: "uppercase", letterSpacing: "0.1em", color: A.muted,
  textAlign: "left", background: A.bg, borderBottom: `1px solid ${A.line}`,
};
export const TD: React.CSSProperties = {
  padding: "13px 16px", fontSize: 13, color: A.body, borderBottom: `1px solid ${A.line2}`,
};

export function TopBar({ breadcrumb }: { breadcrumb: string[] }) {
  return (
    <header style={{ height: 56, background: "#fff", borderBottom: `1px solid ${A.line}`, display: "flex", alignItems: "center", padding: "0 32px", gap: 8, position: "sticky", top: 0, zIndex: 10, flexShrink: 0 }}>
      {breadcrumb.map((crumb, i) => (
        <span key={i} style={{ color: i === breadcrumb.length - 1 ? A.body : A.muted2, fontSize: 12, fontWeight: i === breadcrumb.length - 1 ? 500 : 400 }}>
          {crumb}{i < breadcrumb.length - 1 && <span style={{ marginLeft: 6, color: A.line }}>/</span>}
        </span>
      ))}
    </header>
  );
}
