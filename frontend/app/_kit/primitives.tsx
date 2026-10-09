"use client";
// app/_kit/primitives.tsx
// AA-662 — shared UI-kit primitives (brand-neutral, inline-style, token-driven).
//
// These are the canonical implementations. The admin (adminUi.tsx) and portal (ui.tsx) modules
// keep their own exported names/signatures and delegate to these where it helps, so the ~26
// existing import sites do not change (see adminUi.tsx shim notes). New UI-v2 pages import from
// here directly.

import {
  AlertTriangle,
  ClipboardList,
  Loader2,
  RefreshCw,
  X,
} from "lucide-react";
import { K, RADIUS, sans } from "./tokens";

// ── Tone system ───────────────────────────────────────────────────────────────
// One tone vocabulary the whole kit shares. Both the admin Badge `color` union and the portal
// Badge `variant` union map onto these.
export type Tone =
  | "neutral"
  | "accent"
  | "success"
  | "warning"
  | "danger"
  | "info"
  | "purple";

export const TONE: Record<Tone, { bg: string; fg: string }> = {
  neutral: { bg: "#F3F4F6", fg: "#4B5563" },
  accent: { bg: K.accentTint, fg: K.accentDeep },
  success: { bg: K.successSoft, fg: K.success },
  warning: { bg: K.amberSoft, fg: K.amber },
  danger: { bg: K.dangerSoft, fg: K.danger },
  info: { bg: K.infoSoft, fg: K.info },
  purple: { bg: "#EDE9FE", fg: "#5B21B6" },
};

// ── Badge ───────────────────────────────────────────────────────────────────
export function Badge({
  children,
  tone = "neutral",
  dot = true,
  style,
}: {
  children: React.ReactNode;
  tone?: Tone;
  dot?: boolean;
  style?: React.CSSProperties;
}) {
  const t = TONE[tone];
  return (
    <span
      style={{
        display: "inline-flex",
        alignItems: "center",
        gap: 5,
        fontSize: 11,
        fontWeight: 600,
        padding: "3px 9px",
        borderRadius: RADIUS.pill,
        letterSpacing: "0.04em",
        textTransform: "uppercase",
        background: t.bg,
        color: t.fg,
        fontFamily: sans,
        ...style,
      }}
    >
      {dot && (
        <span
          style={{
            width: 5,
            height: 5,
            borderRadius: "50%",
            background: t.fg,
            display: "block",
            flexShrink: 0,
          }}
        />
      )}
      {children}
    </span>
  );
}

// ── StatusBadge ───────────────────────────────────────────────────────────────
// Driven by a job/row status string (AA-662: "driven by job status, see the job runner issue").
// Maps the known job-runner states to a tone + label; unknown values fall back to neutral.
const STATUS_TONE: Record<string, { tone: Tone; label: string }> = {
  queued: { tone: "neutral", label: "Queued" },
  running: { tone: "info", label: "Running" },
  succeeded: { tone: "success", label: "Succeeded" },
  completed: { tone: "success", label: "Completed" },
  done: { tone: "success", label: "Done" },
  failed: { tone: "danger", label: "Failed" },
  cancelled: { tone: "neutral", label: "Cancelled" },
  canceled: { tone: "neutral", label: "Cancelled" },
  retrying: { tone: "warning", label: "Retrying" },
  pending: { tone: "warning", label: "Pending" },
  held: { tone: "warning", label: "Held" },
  stalled: { tone: "warning", label: "Stalled" },
  ingested: { tone: "neutral", label: "Ingested" },
  approved: { tone: "success", label: "Approved" },
  rejected: { tone: "danger", label: "Rejected" },
  dismissed: { tone: "neutral", label: "Dismissed" },
  regenerating: { tone: "info", label: "Regenerating" },
};

export function StatusBadge({
  status,
  label,
}: {
  status: string;
  label?: string;
}) {
  const key = (status || "").toLowerCase();
  const m = STATUS_TONE[key] ?? { tone: "neutral" as Tone, label: status };
  const spin = key === "running" || key === "regenerating" || key === "retrying";
  return (
    <Badge tone={m.tone} dot={!spin}>
      {spin && (
        <Loader2
          size={10}
          style={{ animation: "spin 1s linear infinite", flexShrink: 0 }}
        />
      )}
      {label ?? m.label}
    </Badge>
  );
}

// ── Spinner ───────────────────────────────────────────────────────────────────
export function Spinner({ size = 16, color = K.accent }: { size?: number; color?: string }) {
  return (
    <Loader2
      size={size}
      style={{ animation: "spin 1s linear infinite", color }}
    />
  );
}

export function LoadingScreen({ message = "Loading..." }: { message?: string }) {
  return (
    <div
      style={{
        display: "flex",
        flexDirection: "column",
        alignItems: "center",
        justifyContent: "center",
        minHeight: 300,
        gap: 10,
      }}
    >
      <Spinner size={22} />
      <span style={{ fontSize: 13, color: K.muted, fontFamily: sans }}>{message}</span>
    </div>
  );
}

// ── Skeleton ────────────────────────────────────────────────────────────────
// Shimmer keyframes are injected once via <SkeletonStyle/>; mount it near the top of any page
// that uses skeletons (idempotent — a page may mount more than one).
export function SkeletonStyle() {
  return (
    <style>{`
      @keyframes kit-shimmer { 0% { background-position: -200px 0; } 100% { background-position: calc(200px + 100%) 0; } }
      @keyframes spin { to { transform: rotate(360deg); } }
    `}</style>
  );
}

export function SkeletonBar({
  w = "100%",
  h = 12,
  r = 6,
  style = {},
}: {
  w?: number | string;
  h?: number;
  r?: number;
  style?: React.CSSProperties;
}) {
  return (
    <div
      style={{
        width: w,
        height: h,
        borderRadius: r,
        background: `linear-gradient(90deg, ${K.line2} 25%, ${K.line} 37%, ${K.line2} 63%)`,
        backgroundSize: "400px 100%",
        animation: "kit-shimmer 1.4s ease infinite",
        ...style,
      }}
    />
  );
}

export function TableSkeleton({ rows = 6, cols = 4 }: { rows?: number; cols?: number }) {
  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 10 }}>
      <SkeletonStyle />
      {Array.from({ length: rows }).map((_, r) => (
        <div key={r} style={{ display: "flex", gap: 12 }}>
          {Array.from({ length: cols }).map((_, c) => (
            <SkeletonBar key={c} w={c === 0 ? "40%" : "20%"} h={14} />
          ))}
        </div>
      ))}
    </div>
  );
}

// ── EmptyState ──────────────────────────────────────────────────────────────
// Superset of both legacy signatures: admin was { title, body }, portal was { icon, title, sub,
// action }. The kit accepts icon?/title/description?/action? and the shims adapt the old names.
export function EmptyState({
  icon,
  title,
  description,
  action,
}: {
  icon?: React.ReactNode;
  title: string;
  description?: string;
  action?: React.ReactNode;
}) {
  return (
    <div
      data-testid="kit-empty-state"
      style={{
        display: "flex",
        flexDirection: "column",
        alignItems: "center",
        justifyContent: "center",
        textAlign: "center",
        padding: "48px 24px",
        gap: 10,
        color: K.muted,
        fontFamily: sans,
      }}
    >
      <div style={{ color: K.muted2 }}>{icon ?? <ClipboardList size={30} />}</div>
      <div style={{ fontSize: 15, fontWeight: 600, color: K.ink }}>{title}</div>
      {description && (
        <div style={{ fontSize: 13, color: K.muted, maxWidth: 420 }}>{description}</div>
      )}
      {action && <div style={{ marginTop: 6 }}>{action}</div>}
    </div>
  );
}

// ── ErrorState ──────────────────────────────────────────────────────────────
export function ErrorState({
  message,
  onRetry,
}: {
  message: string;
  onRetry?: () => void;
}) {
  return (
    <div
      style={{
        display: "flex",
        flexDirection: "column",
        alignItems: "center",
        justifyContent: "center",
        textAlign: "center",
        padding: "40px 24px",
        gap: 12,
        fontFamily: sans,
      }}
    >
      <AlertTriangle size={28} style={{ color: K.danger }} />
      <div style={{ fontSize: 13, color: K.body, maxWidth: 440 }}>{message}</div>
      {onRetry && (
        <button
          onClick={onRetry}
          style={{
            display: "inline-flex",
            alignItems: "center",
            gap: 6,
            padding: "7px 14px",
            borderRadius: RADIUS.pill,
            border: `1px solid ${K.line}`,
            background: K.card,
            color: K.ink3,
            fontSize: 12,
            fontWeight: 600,
            cursor: "pointer",
            fontFamily: sans,
          }}
        >
          <RefreshCw size={13} /> Retry
        </button>
      )}
    </div>
  );
}

// ── PageHeader ────────────────────────────────────────────────────────────────
export function PageHeader({
  title,
  breadcrumbs,
  description,
  actions,
}: {
  title: React.ReactNode;
  breadcrumbs?: { label: string; href?: string }[];
  description?: React.ReactNode;
  actions?: React.ReactNode;
}) {
  return (
    <div
      style={{
        display: "flex",
        alignItems: "flex-start",
        justifyContent: "space-between",
        gap: 16,
        marginBottom: 20,
        fontFamily: sans,
      }}
    >
      <div style={{ minWidth: 0 }}>
        {breadcrumbs && breadcrumbs.length > 0 && (
          <div
            style={{
              display: "flex",
              gap: 6,
              fontSize: 12,
              color: K.muted2,
              marginBottom: 6,
            }}
          >
            {breadcrumbs.map((b, i) => (
              <span key={i} style={{ display: "inline-flex", gap: 6 }}>
                {b.href ? (
                  <a href={b.href} style={{ color: K.muted, textDecoration: "none" }}>
                    {b.label}
                  </a>
                ) : (
                  <span>{b.label}</span>
                )}
                {i < breadcrumbs.length - 1 && <span>/</span>}
              </span>
            ))}
          </div>
        )}
        <h1
          style={{
            margin: 0,
            fontFamily: "'Fahkwang', 'Poppins', Georgia, serif",
            fontSize: 24,
            fontWeight: 600,
            color: K.ink,
            letterSpacing: "-0.01em",
          }}
        >
          {title}
        </h1>
        {description && (
          <div style={{ fontSize: 13, color: K.muted, marginTop: 6 }}>{description}</div>
        )}
      </div>
      {actions && (
        <div style={{ display: "flex", gap: 8, flexShrink: 0, alignItems: "center" }}>
          {actions}
        </div>
      )}
    </div>
  );
}

// ── Tabs ──────────────────────────────────────────────────────────────────────
export function Tabs({
  tabs,
  active,
  onChange,
}: {
  tabs: { key: string; label: string }[];
  active: string;
  onChange: (k: string) => void;
}) {
  return (
    <div
      style={{
        display: "flex",
        gap: 4,
        padding: 4,
        background: K.line2,
        borderRadius: RADIUS.md,
        width: "fit-content",
        fontFamily: sans,
      }}
    >
      {tabs.map((t) => {
        const on = active === t.key;
        return (
          <button
            key={t.key}
            onClick={() => onChange(t.key)}
            style={{
              padding: "7px 16px",
              borderRadius: 7,
              border: "none",
              background: on ? K.card : "transparent",
              color: on ? K.ink : K.muted,
              fontSize: 13,
              fontWeight: on ? 600 : 400,
              cursor: "pointer",
              fontFamily: sans,
              boxShadow: on ? "0 1px 3px rgba(0,0,0,0.08)" : "none",
              transition: "all .15s",
            }}
          >
            {t.label}
          </button>
        );
      })}
    </div>
  );
}

// Re-export the dismiss icon so pages can build their own close buttons consistently.
export { X as CloseIcon };
