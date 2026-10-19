// app/admin/overview/_components/opsUi.tsx
// AA-858 — small building blocks shared by the four ops-overview cards (kit tokens only, inline styles).
import { CloudOff, ExternalLink } from "lucide-react";
import { Badge, K, ScrollX, sans, useIsPhone } from "../../../_kit";
import { SourceHint } from "./SourceHint";
import { APP_STATUS_LABEL, type AppStatus } from "../../../../lib/platform";
import type { HealthState, Unavailable } from "./opsTypes";

export const fmtInt = (n: number | null | undefined) => (n == null ? "—" : Number(n).toLocaleString("en-US"));

export const fmtUsd = (n: number | null | undefined) =>
  n == null
    ? "—"
    : `$${Number(n).toLocaleString("en-US", { minimumFractionDigits: 2, maximumFractionDigits: 2 })}`;

export function fmtMs(ms: number | null | undefined): string {
  if (ms == null) return "—";
  return ms >= 1000 ? `${(ms / 1000).toFixed(1)} s` : `${Math.round(ms)} ms`;
}

/** "2026-10-19" → "Oct 19" (UTC date, no time zone shift). */
export function fmtDay(iso: string): string {
  const d = new Date(`${iso.slice(0, 10)}T00:00:00Z`);
  return d.toLocaleDateString("en-US", { month: "short", day: "numeric", timeZone: "UTC" });
}

/** Traffic state -> app status, the backend's app_health.TRAFFIC_TO_STATUS (only used when `overall` is missing). */
const TRAFFIC_TO_STATUS: Record<HealthState, AppStatus> = { ok: "ok", warn: "degraded", alarm: "degraded", unknown: "unknown" };

/** S243 — an app's status pill, with the same words as /admin/platform/apps (lib/platform APP_STATUS_LABEL). */
export function AppStatusPill({ status, state }: { status?: AppStatus; state: HealthState }) {
  const s = APP_STATUS_LABEL[status ?? TRAFFIC_TO_STATUS[state] ?? "unknown"] ?? APP_STATUS_LABEL.unknown;
  return (
    <Badge tone={s.tone} dot>
      {s.label}
    </Badge>
  );
}

/** One ops-overview card: title, optional source hint, optional link-out, body. */
export function OpsCard({
  title,
  subtitle,
  source,
  link,
  testId,
  children,
}: {
  title: string;
  subtitle?: string;
  source?: string;
  link?: { href: string; label: string; external?: boolean; hint?: string };
  testId: string;
  children: React.ReactNode;
}) {
  const phone = useIsPhone();
  return (
    <section
      data-testid={testId}
      style={{
        background: K.card,
        border: `1px solid ${K.line}`,
        borderRadius: 12,
        padding: phone ? "14px 12px" : "18px 20px",
        minWidth: 0,
      }}
    >
      <div
        style={{
          display: "flex",
          alignItems: "flex-start",
          justifyContent: "space-between",
          gap: 12,
          flexWrap: "wrap",
          marginBottom: 14,
        }}
      >
        <div style={{ minWidth: 0 }}>
          <h2
            style={{
              margin: 0,
              fontSize: 15,
              fontWeight: 600,
              color: K.ink,
              fontFamily: sans,
            }}
          >
            {title}
            {source && <SourceHint source={source} />}
          </h2>
          {subtitle && <div style={{ fontSize: 12, color: K.muted, marginTop: 3 }}>{subtitle}</div>}
        </div>
        {link && (
          <a
            href={link.href}
            title={link.hint}
            target={link.external ? "_blank" : undefined}
            rel={link.external ? "noopener noreferrer" : undefined}
            style={{
              fontSize: 12,
              color: K.accentDeep,
              textDecoration: "none",
              fontWeight: 500,
              display: "inline-flex",
              alignItems: "center",
              gap: 4,
              whiteSpace: "nowrap",
            }}
          >
            {link.label}
            {link.external ? <ExternalLink size={12} aria-hidden /> : " →"}
          </a>
        )}
      </div>
      {children}
    </section>
  );
}

/** A section the backend could not fill: a calm note with its plain-English reason, never raw JSON. */
export function UnavailableNote({ section }: { section: Unavailable }) {
  return (
    <div
      data-testid="ops-unavailable"
      style={{
        display: "flex",
        alignItems: "center",
        gap: 10,
        padding: "14px 16px",
        background: K.bg,
        border: `1px dashed ${K.line}`,
        borderRadius: 10,
        color: K.muted,
        fontSize: 13,
      }}
    >
      <CloudOff size={18} style={{ color: K.muted2, flexShrink: 0 }} aria-hidden />
      <span>{section.message || "Not available right now."}</span>
    </div>
  );
}

/** A KPI tile: label (+ source hint), big value, optional small line under it. `data-kpi` marks it for the e2e
 * check that every number tile names its source. */
export function Kpi({
  label,
  value,
  source,
  sub,
  subTone,
  color = K.ink,
  testId,
  compact,
}: {
  label: string;
  value: React.ReactNode;
  source: string;
  sub?: React.ReactNode;
  subTone?: "good" | "bad" | "muted";
  color?: string;
  testId?: string;
  /** Smaller value font for tiles with three KPIs side by side (phone width). */
  compact?: boolean;
}) {
  const subColor = subTone === "good" ? K.success : subTone === "bad" ? K.danger : K.muted;
  return (
    <div
      data-testid={testId}
      data-kpi=""
      style={{
        background: K.bg,
        border: `1px solid ${K.line}`,
        borderRadius: 10,
        padding: compact ? "8px 8px" : "10px 12px",
        minWidth: 0,
      }}
    >
      <div
        style={{
          fontSize: 11,
          fontWeight: 600,
          color: K.muted,
          minWidth: 0,
        }}
      >
        {label}
        <SourceHint source={source} />
      </div>
      <div
        style={{
          fontSize: compact ? 18 : 22,
          fontWeight: 600,
          color,
          fontVariantNumeric: "tabular-nums",
          letterSpacing: "-0.02em",
          marginTop: 2,
          whiteSpace: "nowrap",
        }}
      >
        {value}
      </div>
      {sub != null && <div style={{ fontSize: 11, color: subColor, marginTop: 2 }}>{sub}</div>}
    </div>
  );
}

/** "+12% vs previous 7 days" / "−5% …" / "new" — growth is good for traffic metrics. */
export function ChangeLine({ pct, prev }: { pct: number | null | undefined; prev: number | null | undefined }) {
  if (prev == null) return null;
  if (pct == null) return <span>{prev === 0 ? "none the 7 days before" : "—"}</span>;
  const sign = pct > 0 ? "+" : pct < 0 ? "−" : "±";
  return (
    <span>
      {sign}
      {Math.abs(pct)}% vs the 7 days before
    </span>
  );
}

export const TH: React.CSSProperties = {
  textAlign: "left",
  fontSize: 11,
  fontWeight: 600,
  color: K.muted,
  padding: "6px 10px",
  borderBottom: `1px solid ${K.line}`,
  whiteSpace: "nowrap",
};

/** Tighter cells for phone width (< 768 px); tables wider than that scroll in their TableWrap (with a hint). */
export const PHONE_CELL: React.CSSProperties = { padding: "6px 4px", fontSize: 12 };

export const TD: React.CSSProperties = {
  fontSize: 13,
  color: K.body,
  padding: "7px 10px",
  borderBottom: `1px solid ${K.line2}`,
  fontVariantNumeric: "tabular-nums",
};

/** Horizontal-scroll wrapper for a table (kit ScrollX): every column stays, the table scrolls inside its own box
 * with a visible hint when it is wider than the screen, and never widens the page (lessons S221/S239, S243). */
export function TableWrap({ children, testId }: { children: React.ReactNode; testId?: string }) {
  return <ScrollX testId={testId}>{children}</ScrollX>;
}
