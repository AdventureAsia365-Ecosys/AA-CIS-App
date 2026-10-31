// UI kit core (root ADR 0016). Canonical: AA-CIS-App frontend/app/_kit/core. Edit only there; copies are checked byte for byte (kit.lock.json).
"use client";
// app/_kit/core/TraceTimeline.tsx
// AA-804 (UI part 3) — a vertical time line of what happened in one run: one row per event, in time order, with a
// tone dot on a rail, the time since the first event ("+1 m 05 s"), a short label (the source), the title, a line of
// detail, an amount (a cost) and a duration (a latency). First used by the CIS "AI providers" trace (every paid call
// of a job or a tour); any app can show a run's steps with it (a booking's payment attempts, a CRM job's steps).
//
// The rows are an ordered list (<ol>), each an <li> whose accessible text is the time, label, title and detail. The
// list scrolls inside its own box when `maxHeight` is set, so a long run never stretches the page. `onSelect` makes
// each row a button (open a detail). Pure helpers `sinceStart` and `formatDuration` are exported for tests and pages.

import type { CSSProperties, ReactNode } from "react";
import { TONE, type Tone } from "./primitives";
import { K, RADIUS, mono, sans } from "./tokens";
import { KIT_MESSAGES } from "./kit.messages";
import { useT } from "./useI18n";

export interface TimelineEvent {
  /** Unique within the list (the React key). */
  id: string;
  /** ISO timestamp (or a Date) of the event. */
  at: string | Date;
  /** A short label in front of the title, e.g. the source ("LLM", "DataForSEO"). */
  label?: ReactNode;
  title: ReactNode;
  /** One line under the title (model, account, error text…). */
  detail?: ReactNode;
  /** Colour of the dot; default neutral. */
  tone?: Tone;
  /** Right-aligned figure, e.g. a cost. */
  amount?: ReactNode;
  /** Milliseconds the event took; shown as "420 ms" / "1.2 s". */
  durationMs?: number | null;
}

/** "0 ms", "420 ms", "1.2 s", "1 m 05 s", "2 h 03 m"; null / negative / NaN → "—". */
export function formatDuration(ms: number | null | undefined): string {
  if (ms == null || !Number.isFinite(ms) || ms < 0) return "—";
  if (ms < 1000) return `${Math.round(ms)} ms`;
  if (ms < 60_000) return `${(ms / 1000).toFixed(ms < 10_000 ? 1 : 0)} s`;
  const s = Math.round(ms / 1000);
  if (s < 3600) return `${Math.floor(s / 60)} m ${String(s % 60).padStart(2, "0")} s`;
  const m = Math.round(s / 60);
  return `${Math.floor(m / 60)} h ${String(m % 60).padStart(2, "0")} m`;
}

/** Milliseconds from `start` to `at` (0 when either is unreadable or `at` is earlier). */
export function sinceStart(at: string | Date, start: string | Date): number {
  const a = (at instanceof Date ? at : new Date(at)).getTime();
  const s = (start instanceof Date ? start : new Date(start)).getTime();
  return Number.isNaN(a) || Number.isNaN(s) ? 0 : Math.max(0, a - s);
}

function clock(at: string | Date): string {
  const d = at instanceof Date ? at : new Date(at);
  if (Number.isNaN(d.getTime())) return "—";
  return [d.getHours(), d.getMinutes(), d.getSeconds()].map((n) => String(n).padStart(2, "0")).join(":");
}

export function TraceTimeline({
  events,
  start,
  emptyText,
  maxHeight,
  onSelect,
  ariaLabel,
  testId,
  style,
  className,
}: {
  /** In time order (the component does not sort). */
  events: readonly TimelineEvent[];
  /** The time the offsets count from; default the first event's. */
  start?: string | Date | null;
  emptyText?: ReactNode;
  /** Scroll inside the box past this height (px). */
  maxHeight?: number;
  /** Each row becomes a button that calls this with the event's id. */
  onSelect?: (id: string) => void;
  ariaLabel?: string;
  testId?: string;
  style?: CSSProperties;
  /** For Tailwind apps (Booking); inline-style apps leave it out. */
  className?: string;
}) {
  const t = useT(KIT_MESSAGES);
  const origin = start ?? events[0]?.at ?? null;
  if (events.length === 0) {
    return (
      <div data-testid={testId} className={className}
        style={{ fontFamily: sans, fontSize: 13, color: K.muted, padding: "12px 0", ...style }}>
        {emptyText ?? t("timelineEmpty")}
      </div>
    );
  }
  return (
    <ol
      data-testid={testId}
      data-kit-timeline=""
      aria-label={ariaLabel ?? t("timeline")}
      className={className}
      style={{
        listStyle: "none", margin: 0, padding: 0, fontFamily: sans, minWidth: 0,
        ...(maxHeight ? { maxHeight, overflowY: "auto" } : {}),
        ...style,
      }}
    >
      {events.map((e, i) => {
        const tone = TONE[e.tone ?? "neutral"];
        const last = i === events.length - 1;
        const body = (
          <>
            <span aria-hidden style={{ position: "relative", width: 14, flexShrink: 0, alignSelf: "stretch" }}>
              {!last && (
                <span style={{ position: "absolute", left: 6, top: 14, bottom: -6, width: 2, background: K.line }} />
              )}
              <span style={{
                position: "absolute", left: 2, top: 4, width: 10, height: 10, borderRadius: "50%",
                background: tone.fg, boxShadow: `0 0 0 3px ${tone.bg}`,
              }} />
            </span>
            <span style={{ display: "flex", flexDirection: "column", gap: 2, minWidth: 0, flex: 1 }}>
              <span style={{ display: "flex", gap: 8, alignItems: "baseline", flexWrap: "wrap", minWidth: 0 }}>
                <span style={{ fontFamily: mono, fontSize: 11.5, color: K.muted, whiteSpace: "nowrap" }}
                  title={clock(e.at)}>
                  +{formatDuration(origin ? sinceStart(e.at, origin) : 0)}
                </span>
                {e.label != null && (
                  <span style={{
                    fontSize: 11, fontWeight: 600, color: tone.fg, background: tone.bg, borderRadius: RADIUS.sm,
                    padding: "1px 6px", whiteSpace: "nowrap",
                  }}>{e.label}</span>
                )}
                <span style={{ fontSize: 13, color: K.ink, minWidth: 0, overflowWrap: "anywhere" }}>{e.title}</span>
              </span>
              {e.detail != null && (
                <span style={{ fontSize: 12, color: K.muted, minWidth: 0, overflowWrap: "anywhere" }}>{e.detail}</span>
              )}
            </span>
            <span style={{ display: "flex", flexDirection: "column", alignItems: "flex-end", gap: 2, flexShrink: 0,
              fontFamily: mono, fontSize: 12, fontVariantNumeric: "tabular-nums" }}>
              {e.amount != null && <span style={{ color: K.ink }}>{e.amount}</span>}
              {e.durationMs != null && <span style={{ color: K.muted }}>{formatDuration(e.durationMs)}</span>}
            </span>
          </>
        );
        const rowStyle: CSSProperties = {
          display: "flex", gap: 10, alignItems: "flex-start", padding: "6px 4px", width: "100%", minWidth: 0,
          textAlign: "left",
        };
        return (
          <li key={e.id} data-kit-timeline-row="" style={{ minWidth: 0 }}>
            {onSelect ? (
              <button type="button" onClick={() => onSelect(e.id)}
                style={{ ...rowStyle, background: "none", border: "none", cursor: "pointer", font: "inherit",
                  color: "inherit", borderRadius: RADIUS.sm }}>
                {body}
              </button>
            ) : (
              <div style={rowStyle}>{body}</div>
            )}
          </li>
        );
      })}
    </ol>
  );
}
