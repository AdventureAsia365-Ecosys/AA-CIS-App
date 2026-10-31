// UI kit core (root ADR 0016). Canonical: AA-CIS-App frontend/app/_kit/core. Edit only there; copies are checked byte for byte (kit.lock.json).
"use client";
// app/_kit/core/primitives.tsx
// AA-662 — shared UI-kit primitives (brand-neutral, inline-style, token-driven).
//
// These are the canonical implementations. The admin (adminUi.tsx) and portal (ui.tsx) modules
// keep their own exported names/signatures and delegate to these where it helps, so the ~26
// existing import sites do not change (see adminUi.tsx shim notes). New UI-v2 pages import from
// here directly.

import {
  AlertTriangle,
  ArrowLeftRight,
  ClipboardList,
  Info,
  Loader2,
  RefreshCw,
  X,
} from "lucide-react";
import { useCallback, useEffect, useId, useLayoutEffect, useRef, useState } from "react";
import { K, RADIUS, sans } from "./tokens";
import { ButtonStyles, BUTTON_CLASS, rovingTarget } from "./Buttons";
import { KIT_MESSAGES } from "./kit.messages";
import { useT } from "./useI18n";

// ── Breadcrumb (AA-831) ───────────────────────────────────────────────────────
// One-line "<root> / <title>" crumb for a shell top bar. The root and separator never shrink; the
// title is the only flex item that does, and it ends in an ellipsis instead of being hard-clipped
// (S239 UI verify: "Connect your Facebook Pag" at 390px). `minWidth: 0` on both the wrapper and the
// title is what lets a flex item shrink below its text width. The full title stays in `title=`.
export function Breadcrumb({
  root,
  title,
  fontSize = 13,
  testId = "topbar-breadcrumb",
}: {
  root: string;
  title: string;
  fontSize?: number;
  testId?: string;
}) {
  return (
    <div
      data-testid={testId}
      style={{
        display: "flex", alignItems: "center", gap: 6, minWidth: 0, flexShrink: 1,
        overflow: "hidden", whiteSpace: "nowrap", fontSize, fontFamily: sans,
      }}
    >
      <span style={{ color: K.muted2, flexShrink: 0 }}>{root}</span>
      <span aria-hidden style={{ color: K.line, flexShrink: 0 }}>/</span>
      <span
        data-testid={`${testId}-title`}
        title={title || undefined}
        style={{
          color: K.body, fontWeight: 500, minWidth: 0,
          overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap",
        }}
      >
        {title}
      </span>
    </div>
  );
}

// ── Buttons ───────────────────────────────────────────────────────────────────────────────────────────────────
// The kit buttons live in ./Buttons (AA-994); re-exported here so every barrel that does
// `export * from "./core/primitives"` keeps exporting them. Named, not `export *`: Next.js refuses `export *` in a
// "use client" module. A new export of ./Buttons must be added here too (lib/kitButtons.test.mjs checks it).
export {
  Button, LinkButton, IconButton, ChipButton, Switch, SegmentedControl, ButtonGroup, ListButton, Pressable,
  ButtonStyles, BUTTON_CLASS, rovingTarget,
} from "./Buttons";
export type {
  BaseButtonProps, ButtonProps, ButtonVariant, ButtonSize, LinkButtonProps, LinkButtonTone, IconButtonProps,
  IconButtonVariant, IconButtonSize, ChipButtonProps, SwitchProps, SegmentedOption, ListButtonProps, PressableProps,
} from "./Buttons";

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
  neutral: { bg: K.neutralBg, fg: K.neutralFg },
  accent: { bg: K.accentTint, fg: K.accentDeep },
  success: { bg: K.successSoft, fg: K.success },
  warning: { bg: K.amberSoft, fg: K.amber },
  danger: { bg: K.dangerSoft, fg: K.danger },
  info: { bg: K.infoSoft, fg: K.info },
  purple: { bg: K.purpleSoft, fg: K.purple },
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

// ── Eyebrow ─────────────────────────────────────────────────────────────────
// The small uppercase label over a group of fields, a list or a block of figures ("Retry policy", "Last 30 days"):
// 11px, 600, uppercase, 0.14em tracking, muted, 14px above what it labels. AA-994 S6b: the kit twin of the legacy admin
// `SLabel`, so pages stop restyling the same label by hand. For a card's own title use Section `title`; for the page
// title PageHeader. `tone="onDark"` is for a deliberately dark surface (a hero or the sidebar).
export function Eyebrow({
  children,
  as: Tag = "div",
  tone = "muted",
  testId,
  style,
}: {
  children: React.ReactNode;
  /** The element (a <div> by default; "h3" when it heads a group in the outline, "span" inside a line). */
  as?: "div" | "span" | "h3" | "h4" | "p" | "legend";
  tone?: "muted" | "onDark";
  testId?: string;
  /** Spacing (the default bottom margin is 14px; `marginBottom: 0` inside a flex row). */
  style?: React.CSSProperties;
}) {
  return (
    <Tag
      data-testid={testId}
      style={{
        margin: 0,
        marginBottom: 14,
        fontFamily: sans,
        fontSize: 11,
        fontWeight: 600,
        textTransform: "uppercase",
        letterSpacing: "0.14em",
        color: tone === "onDark" ? "rgba(255,255,255,0.5)" : K.muted,
        ...style,
      }}
    >
      {children}
    </Tag>
  );
}

// ── StatusBadge ───────────────────────────────────────────────────────────────
// Driven by a job/row status string (AA-662: "driven by job status, see the job runner issue").
// Maps the known job-runner states to a tone + label; unknown values fall back to neutral.
// AA-994: the label is a key of KIT_MESSAGES (kit.messages.ts), shown in the UI language.
type StatusLabelKey = keyof typeof KIT_MESSAGES.en & `st${string}`;
const STATUS_TONE: Record<string, { tone: Tone; label: StatusLabelKey }> = {
  queued: { tone: "neutral", label: "stQueued" },
  running: { tone: "info", label: "stRunning" },
  succeeded: { tone: "success", label: "stSucceeded" },
  completed: { tone: "success", label: "stCompleted" },
  done: { tone: "success", label: "stDone" },
  failed: { tone: "danger", label: "stFailed" },
  stopped_budget: { tone: "warning", label: "stStoppedBudget" },
  cancelled: { tone: "neutral", label: "stCancelled" },
  canceled: { tone: "neutral", label: "stCancelled" },
  retrying: { tone: "warning", label: "stRetrying" },
  pending: { tone: "warning", label: "stPending" },
  held: { tone: "warning", label: "stHeld" },
  stalled: { tone: "warning", label: "stStalled" },
  ingested: { tone: "neutral", label: "stIngested" },
  approved: { tone: "success", label: "stApproved" },
  rejected: { tone: "danger", label: "stRejected" },
  dismissed: { tone: "neutral", label: "stDismissed" },
  regenerating: { tone: "info", label: "stRegenerating" },
};

export function StatusBadge({
  status,
  label,
}: {
  status: string;
  label?: string;
}) {
  const t = useT(KIT_MESSAGES);
  const key = (status || "").toLowerCase();
  const known = STATUS_TONE[key];
  const m = known ? { tone: known.tone, label: t(known.label) } : { tone: "neutral" as Tone, label: status };
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

export function LoadingScreen({ message }: { message?: string }) {
  const t = useT(KIT_MESSAGES);
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
      <span style={{ fontSize: 13, color: K.muted, fontFamily: sans }}>{message ?? t("loadingScreen")}</span>
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
  const t = useT(KIT_MESSAGES);
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
          <RefreshCw size={13} /> {t("retry")}
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
  // On a phone a header with a description puts its actions on their own row under the text, left
  // aligned (S241 UI verify: on /admin/platform/staff the description was squeezed into ~250px
  // beside Refresh). Without a description the actions still sit beside a short title. The switch
  // is the `aa-page-header--stack` rule in globals.css (< 768px), not a JS media query, so the
  // server HTML already has the phone layout (no shift on hydration). The text block's `flex` and
  // the actions' `justify-content` live in those classes because an inline style would win over them.
  const stack = !!description && !!actions;
  return (
    <div
      className={stack ? "aa-page-header aa-page-header--stack" : "aa-page-header"}
      style={{
        display: "flex",
        alignItems: "flex-start",
        justifyContent: "space-between",
        gap: 16,
        marginBottom: 20,
        fontFamily: sans,
        // Wrap on narrow screens so the actions drop below the title instead of overlapping it
        // (AA-752: at 390px the title and the action buttons were colliding).
        flexWrap: "wrap",
      }}
    >
      <div className="aa-page-header-text" style={{ minWidth: 0 }}>
        {breadcrumbs && breadcrumbs.length > 0 && (
          <div
            style={{
              display: "flex",
              gap: 6,
              fontSize: 12,
              color: K.muted2,
              marginBottom: 6,
              // S252 — a long last crumb (a tour title) wraps instead of widening the page.
              flexWrap: "wrap",
              minWidth: 0,
              overflowWrap: "anywhere",
            }}
          >
            {breadcrumbs.map((b, i) => (
              <span key={i} style={{ display: "inline-flex", gap: 6, minWidth: 0 }}>
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
        <div className="aa-page-header-actions" style={{ display: "flex", gap: 8, flexShrink: 0, alignItems: "center", flexWrap: "wrap" }}>
          {actions}
        </div>
      )}
    </div>
  );
}

// ── Tabs ──────────────────────────────────────────────────────────────────────
// variant "track" (default): the grey track with the active tab raised, as since AA-662 (its DOM is unchanged: plain
// buttons, no tab roles yet, so existing page tests keep finding them as buttons).
// variant "underline" (AA-994): the tab strip under a detail page header (Booking trip case, customer, supplier): a
// role="tablist" labelled by `ariaLabel`, each tab role="tab" with aria-selected, a 2px accent underline on the
// active one, an optional count after the label, disabled tabs with a `title` reason. Tab enters on the active tab;
// the arrow keys (and Home / End) move and select, skipping disabled tabs.
// Room the underline tablist keeps for the label focus ring (2px ring at a 2px offset).
const TAB_RING_ROOM = 4;

// S262 (AA-804 live finding F7) — a tab strip wider than its box scrolls sideways; on a phone nothing showed that, and
// the active tab could sit out of view (Budgets, the fifth tab). The strip now fades the edge that has more tabs
// behind it (a mask, so it works on any background) and scrolls the active tab into view when it changes. Pure DOM
// reads, no layout change: the markup and the test ids are the same as before.
const TAB_FADE_PX = 28;

/** The mask of a strip with more tabs to the left and / or right ("none" when everything fits). */
export function tabStripMask(left: boolean, right: boolean): string {
  if (!left && !right) return "none";
  const l = left ? `transparent 0, #000 ${TAB_FADE_PX}px` : "#000 0";
  const r = right ? `#000 calc(100% - ${TAB_FADE_PX}px), transparent 100%` : "#000 100%";
  return `linear-gradient(to right, ${l}, ${r})`;
}

/** The scrollLeft that brings [start, start + width) into a box of `clientWidth` scrolled to `scrollLeft` (with a
 *  margin so the fade does not cover it), or null when it is already in view. Pure. */
export function scrollToShow(scrollLeft: number, clientWidth: number, start: number, width: number,
  margin = TAB_FADE_PX): number | null {
  if (start - margin < scrollLeft) return Math.max(0, start - margin);
  if (start + width + margin > scrollLeft + clientWidth) return start + width + margin - clientWidth;
  return null;
}

function useTabStrip(activeKey: string) {
  const [el, setEl] = useState<HTMLDivElement | null>(null);
  const [edges, setEdges] = useState({ left: false, right: false });
  useEffect(() => {
    if (!el) return;
    const measure = () => {
      const left = el.scrollLeft > 1;
      const right = el.scrollLeft + el.clientWidth < el.scrollWidth - 1;
      setEdges((e) => (e.left === left && e.right === right ? e : { left, right }));
    };
    const ro = typeof ResizeObserver === "undefined" ? null : new ResizeObserver(measure);
    ro?.observe(el);
    el.addEventListener("scroll", measure, { passive: true });
    const frame = typeof requestAnimationFrame === "undefined" ? null : requestAnimationFrame(measure);
    return () => {
      ro?.disconnect();
      el.removeEventListener("scroll", measure);
      if (frame !== null) cancelAnimationFrame(frame);
    };
  }, [el]);
  useEffect(() => {
    if (!el) return;
    const btn = el.querySelector<HTMLElement>('[data-kit-tab-active="true"]');
    if (!btn) return;
    const next = scrollToShow(el.scrollLeft, el.clientWidth, btn.offsetLeft - el.offsetLeft, btn.offsetWidth);
    if (next !== null) el.scrollTo({ left: next });
  }, [el, activeKey]);
  const mask = tabStripMask(edges.left, edges.right);
  const fade: React.CSSProperties = mask === "none" ? {} : { maskImage: mask, WebkitMaskImage: mask };
  return [setEl, fade] as const;
}

export type TabItem = {
  key: string;
  label: React.ReactNode;
  /** underline only: a small count after the label ("Proposals 3"). */
  count?: React.ReactNode;
  /** underline only: shown but not selectable (give the reason as `title`). */
  disabled?: boolean;
  title?: string;
};

export function Tabs({
  tabs,
  active,
  onChange,
  variant = "track",
  ariaLabel,
  gap = 24,
  testId,
  style,
  className,
}: {
  tabs: TabItem[];
  active: string;
  onChange: (k: string) => void;
  variant?: "track" | "underline";
  /** underline: the tab list's name for screen readers ("Trip case sections"). */
  ariaLabel?: string;
  /** underline: space between tabs in px (default 24). */
  gap?: number;
  testId?: string;
  /** underline: merged into the tab list (a negative bottom margin to sit on a header border). The top, left and
   * right margins and paddings are the kit's (room for the focus ring): override them only on purpose. */
  style?: React.CSSProperties;
  /** underline: layout utilities for Tailwind apps (a bottom margin such as `-mb-px` is fine; inline top / left /
   * right margins win over a class). */
  className?: string;
}) {
  const refs = useRef<(HTMLButtonElement | null)[]>([]);
  const [attachStrip, stripFade] = useTabStrip(active);
  if (variant === "underline") {
    const current = tabs.findIndex((t) => t.key === active);
    const tabStop = current >= 0 && !tabs[current].disabled ? current : tabs.findIndex((t) => !t.disabled);
    // Arrow keys move and select (automatic activation); ArrowUp / ArrowDown are left to the page (a horizontal list).
    const onKey = (e: React.KeyboardEvent<HTMLButtonElement>, from: number) => {
      if (e.key === "ArrowUp" || e.key === "ArrowDown") return;
      const i = rovingTarget(tabs, from, e.key);
      if (i === null) return;
      e.preventDefault();
      if (i < 0) return;
      refs.current[i]?.focus();
      if (tabs[i].key !== active) onChange(tabs[i].key);
    };
    return (
      <>
        <ButtonStyles />
        <div
          ref={attachStrip}
          role="tablist"
          aria-label={ariaLabel}
          data-testid={testId}
          className={className}
          // The focus ring is drawn around each tab's label (Buttons.tsx, .aa-kit-tab__label) and reaches 4px past it;
          // the tablist scrolls sideways (overflow clips), so it keeps 4px of room on the top / left / right, taken
          // back with negative margins so the tabs stay where they were. The bottom is left alone (the label ring
          // fits in the tab's 10px bottom padding), so a bottom margin or baseline from `className` keeps working.
          style={{
            display: "flex",
            gap,
            maxWidth: `calc(100% + ${2 * TAB_RING_ROOM}px)`,
            overflowX: "auto",
            paddingTop: TAB_RING_ROOM,
            paddingLeft: TAB_RING_ROOM,
            paddingRight: TAB_RING_ROOM,
            marginTop: -TAB_RING_ROOM,
            marginLeft: -TAB_RING_ROOM,
            marginRight: -TAB_RING_ROOM,
            fontFamily: sans,
            ...stripFade,
            ...style,
          }}
        >
          {tabs.map((t, i) => {
            const on = i === current;
            return (
              <button
                key={t.key}
                ref={(el) => {
                  refs.current[i] = el;
                }}
                type="button"
                role="tab"
                aria-selected={on}
                data-kit-tab-active={on ? "true" : undefined}
                disabled={t.disabled}
                title={t.title}
                tabIndex={i === tabStop ? 0 : -1}
                onClick={() => onChange(t.key)}
                onKeyDown={(e) => onKey(e, i)}
                className={`${BUTTON_CLASS} aa-kit-tab`}
                style={{
                  margin: 0,
                  padding: "0 0 10px",
                  border: "none",
                  borderBottom: `2px solid ${on ? K.accent : "transparent"}`,
                  borderRadius: 0,
                  background: "transparent",
                  color: on ? K.accentDeep : K.muted,
                  fontSize: 13,
                  fontWeight: on ? 600 : 400,
                  fontFamily: sans,
                  whiteSpace: "nowrap",
                  flexShrink: 0,
                  cursor: t.disabled ? "not-allowed" : "pointer",
                  transition: "color .15s, border-color .15s",
                }}
              >
                <span className="aa-kit-tab__label">
                  {t.label}
                  {t.count != null && <span style={{ marginLeft: 4, fontSize: 12, color: K.muted2 }}>{t.count}</span>}
                </span>
              </button>
            );
          })}
        </div>
      </>
    );
  }
  return (
    <div
      ref={attachStrip}
      data-testid={testId}
      style={{
        display: "flex",
        gap: 4,
        padding: 4,
        background: K.line2,
        borderRadius: RADIUS.md,
        width: "fit-content",
        // S252 — more tabs than fit on a phone scroll inside the bar (like adminUi.TabBar since S224); the page never
        // scrolls sideways. S262: the edge with more tabs fades, and the active tab is scrolled into view.
        maxWidth: "100%",
        overflowX: "auto",
        fontFamily: sans,
        ...stripFade,
      }}
    >
      {tabs.map((t) => {
        const on = active === t.key;
        return (
          <button
            key={t.key}
            data-kit-tab-active={on ? "true" : undefined}
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
              whiteSpace: "nowrap",
              flexShrink: 0,
            }}
          >
            {t.label}
          </button>
        );
      })}
    </div>
  );
}

// ── ScrollX (S243) ────────────────────────────────────────────────────────────
// Horizontal-scroll wrapper for a plain <table> that is wider than a phone: the table keeps every
// column (give it a `minWidth`), the wrapper scrolls on its own so the page never scrolls sideways
// (lesson S221/S239), and a visible hint says so — but only while the content really overflows
// (measured with a ResizeObserver; a phone's overlay scrollbar is invisible until you touch it).
// The scroll region is keyboard-focusable while it overflows.
export function ScrollX({
  children,
  testId,
  hint: hintProp,
}: {
  children: React.ReactNode;
  testId?: string;
  hint?: string;
}) {
  const t = useT(KIT_MESSAGES);
  const hint = hintProp ?? t("scrollHint");
  const ref = useRef<HTMLDivElement>(null);
  const [overflows, setOverflows] = useState(false);
  useEffect(() => {
    const el = ref.current;
    if (!el || typeof ResizeObserver === "undefined") return;
    const measure = () => setOverflows(el.scrollWidth > el.clientWidth + 1);
    const ro = new ResizeObserver(measure);
    ro.observe(el);
    if (el.firstElementChild) ro.observe(el.firstElementChild);
    return () => ro.disconnect();
  }, []);
  return (
    <div style={{ minWidth: 0, maxWidth: "100%" }}>
      <div
        ref={ref}
        data-testid={testId}
        data-overflows={overflows ? "true" : "false"}
        data-kit-scroll=""
        tabIndex={overflows ? 0 : undefined}
        role={overflows ? "region" : undefined}
        aria-label={overflows ? hint : undefined}
        style={{ overflowX: "auto", minWidth: 0, maxWidth: "100%" }}
      >
        {children}
      </div>
      {overflows && (
        <div
          data-testid={testId ? `${testId}-scroll-hint` : undefined}
          style={{
            display: "flex",
            alignItems: "center",
            gap: 6,
            marginTop: 6,
            fontSize: 11,
            color: K.muted,
            fontFamily: sans,
          }}
        >
          <ArrowLeftRight size={12} aria-hidden /> {hint}
        </div>
      )}
    </div>
  );
}

// ── InfoTip (S243, AA-858) ────────────────────────────────────────────────────
// A small (i) button that explains something in a short tooltip — e.g. where a number comes from.
// One pattern for mouse, keyboard and touch:
//   - mouse: hover shows it (the pointer can move onto the bubble, WCAG 1.4.13), a click keeps it open, a second
//     click (or Escape / a click elsewhere) closes it; a press inside the bubble keeps it open (text can be selected);
//   - keyboard: Tab to the button shows it (focus-visible), Enter/Space keeps it open, Escape closes it;
//   - touch: a tap opens it, a second tap or a tap elsewhere closes it.
// The text is always in the DOM (hidden while closed) and tied to the button with aria-describedby, so a
// screen reader reads it with the button. The hit area is 24 px (WCAG 2.5.8) for a 12 px icon. The bubble is `position: fixed`, clamped to the viewport, so it is
// never clipped by a scrolling table and never widens the page (lesson S221/S239); it follows its button on scroll.
export function InfoTip({
  text,
  label: labelProp,
  size = 12,
  testId = "info-tip",
}: {
  text: string;
  /** Accessible name of the button (the text itself is its description). */
  label?: string;
  size?: number;
  testId?: string;
}) {
  const tk = useT(KIT_MESSAGES);
  const label = labelProp ?? tk("moreInfo");
  const tipId = useId();
  const btnRef = useRef<HTMLButtonElement>(null);
  const tipRef = useRef<HTMLSpanElement>(null);
  const [open, setOpen] = useState(false);
  const [pinned, setPinned] = useState(false);

  // A hover close waits a moment so the pointer can cross from the button onto the bubble.
  const hoverTimer = useRef<ReturnType<typeof setTimeout> | null>(null);
  // Set while the pointer is pressed inside the bubble: that press blurs the button but must not close the tip.
  const pressInTip = useRef(false);
  const cancelHoverClose = useCallback(() => {
    if (hoverTimer.current) clearTimeout(hoverTimer.current);
    hoverTimer.current = null;
  }, []);

  const close = useCallback(() => {
    cancelHoverClose();
    setOpen(false);
    setPinned(false);
  }, [cancelHoverClose]);

  const hoverLeave = (e: React.PointerEvent) => {
    if (e.pointerType !== "mouse" || pinned) return;
    cancelHoverClose();
    hoverTimer.current = setTimeout(() => setOpen(false), 150);
  };

  useEffect(() => cancelHoverClose, [cancelHoverClose]);

  // Place the bubble under the button (above it when there is no room), inside the viewport. Written straight to
  // the bubble's style (measured after it is shown, before paint), so no extra render; redone on scroll / resize
  // so it follows its button (a keyboard focus that scrolls the page must not close it).
  const place = useCallback(() => {
    const btn = btnRef.current;
    const tip = tipRef.current;
    if (!btn || !tip || tip.hidden) return;
    const r = btn.getBoundingClientRect();
    const vw = document.documentElement.clientWidth || window.innerWidth;
    const vh = window.innerHeight;
    const w = tip.offsetWidth;
    const h = tip.offsetHeight;
    const left = Math.min(Math.max(8, r.left + r.width / 2 - w / 2), Math.max(8, vw - w - 8));
    const below = r.bottom + 6;
    const top = below + h > vh - 8 && r.top - 6 - h >= 8 ? r.top - 6 - h : below;
    tip.style.left = `${Math.round(left)}px`;
    tip.style.top = `${Math.round(top)}px`;
    tip.style.visibility = "visible";
  }, []);

  useLayoutEffect(() => {
    if (open) place();
  }, [open, text, place]);

  // While open: Escape or a press outside closes it; a scroll or a resize moves it with its button.
  useEffect(() => {
    if (!open) return;
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") close();
    };
    const onDown = (e: PointerEvent) => {
      const t = e.target as Node | null;
      if (t && (btnRef.current?.contains(t) || tipRef.current?.contains(t))) return;
      close();
    };
    window.addEventListener("keydown", onKey);
    window.addEventListener("pointerdown", onDown, true);
    window.addEventListener("scroll", place, true);
    window.addEventListener("resize", place);
    return () => {
      window.removeEventListener("keydown", onKey);
      window.removeEventListener("pointerdown", onDown, true);
      window.removeEventListener("scroll", place, true);
      window.removeEventListener("resize", place);
    };
  }, [open, close, place]);

  return (
    <span
      data-testid={testId}
      style={{ display: "inline-flex", alignItems: "center", verticalAlign: "middle", flexShrink: 0 }}
    >
      <button
        ref={btnRef}
        type="button"
        aria-label={label}
        aria-describedby={tipId}
        data-testid={`${testId}-button`}
        onPointerEnter={(e) => {
          if (e.pointerType !== "mouse") return;
          cancelHoverClose();
          setOpen(true);
        }}
        onPointerLeave={hoverLeave}
        onFocus={(e) => {
          let keyboard = false;
          try {
            keyboard = e.currentTarget.matches(":focus-visible");
          } catch {
            keyboard = false;
          }
          if (keyboard) setOpen(true);
        }}
        onBlur={(e) => {
          const to = e.relatedTarget as Node | null;
          if (pressInTip.current || (to && tipRef.current?.contains(to))) {
            pressInTip.current = false;
            setPinned(true);
            return;
          }
          close();
        }}
        onClick={() => {
          if (pinned) {
            close();
          } else {
            setOpen(true);
            setPinned(true);
          }
        }}
        style={{
          display: "inline-flex",
          alignItems: "center",
          justifyContent: "center",
          // 24 px hit area, same footprint as the 12 px icon: the 4 px gap to the label is kept and the hit area
          // reaches only 2 px into the label, so a focus ring does not cover its last letter.
          padding: 6,
          margin: "-6px -6px -6px -2px",
          border: "none",
          borderRadius: 999,
          background: "transparent",
          color: open ? K.ink : K.muted2,
          cursor: "help",
          lineHeight: 0,
          font: "inherit",
        }}
      >
        <Info size={size} aria-hidden />
      </button>
      <span
        ref={tipRef}
        id={tipId}
        role="tooltip"
        data-testid={`${testId}-text`}
        hidden={!open}
        onPointerEnter={(e) => {
          if (e.pointerType === "mouse") cancelHoverClose();
        }}
        onPointerLeave={hoverLeave}
        onPointerDown={() => {
          pressInTip.current = true;
        }}
        onPointerUp={() => {
          pressInTip.current = false;
        }}
        style={{
          position: "fixed",
          top: 0,
          left: 0,
          visibility: "hidden",
          zIndex: 1000,
          width: "max-content",
          maxWidth: "min(300px, calc(100vw - 16px))",
          padding: "8px 10px",
          borderRadius: 8,
          background: K.ink,
          color: K.card,
          fontFamily: sans,
          fontSize: 12,
          fontWeight: 400,
          lineHeight: 1.45,
          letterSpacing: "normal",
          textTransform: "none",
          textAlign: "left",
          whiteSpace: "normal",
          overflowWrap: "anywhere",
          boxShadow: "0 6px 20px rgba(0, 0, 0, 0.18)",
          pointerEvents: "auto",
        }}
      >
        {text}
      </span>
    </span>
  );
}

// Re-export the dismiss icon so pages can build their own close buttons consistently.
export { X as CloseIcon };
