// UI kit core (root ADR 0016). Canonical: AA-CIS-App frontend/app/_kit/core. Edit only there; copies are checked byte for byte (kit.lock.json).
"use client";
// app/_kit/core/LatencyStrip.tsx
// AA-804 (UI part 3) — how long a set of calls took, at a glance: one thin bar per call in order (height = its
// latency against the slowest), coloured by its tone (a failed call red), above one line of figures: the median
// (p50), the 95th percentile (p95), the slowest and how many calls had a time. First used by the CIS "AI providers"
// trace and Calls tab; any list of timed calls can use it.
//
// The strip is an image to assistive technology (role="img") whose label is that same line of figures, so a screen
// reader hears the summary, not one value per bar. Calls without a time (null) are left out of the figures and drawn
// as an empty slot. Pure helper `latencyStats` is exported for tests and pages.

import type { CSSProperties } from "react";
import { TONE, type Tone } from "./primitives";
import { K, mono, sans } from "./tokens";
import { formatDuration } from "./TraceTimeline";
import { KIT_MESSAGES } from "./kit.messages";
import { useT } from "./useI18n";

export interface LatencyPoint {
  ms: number | null | undefined;
  tone?: Tone;
}

export interface LatencyStats {
  count: number;
  p50: number | null;
  p95: number | null;
  max: number | null;
}

/** Nearest-rank percentiles of the timed values (null / negative / NaN left out). */
export function latencyStats(values: readonly (number | null | undefined)[]): LatencyStats {
  const v = values.filter((x): x is number => x != null && Number.isFinite(x) && x >= 0).sort((a, b) => a - b);
  if (v.length === 0) return { count: 0, p50: null, p95: null, max: null };
  const rank = (p: number) => v[Math.min(v.length - 1, Math.max(0, Math.ceil((p / 100) * v.length) - 1))];
  return { count: v.length, p50: rank(50), p95: rank(95), max: v[v.length - 1] };
}

export function LatencyStrip({
  points,
  height = 28,
  maxBars = 120,
  testId,
  style,
  className,
}: {
  /** In call order. */
  points: readonly LatencyPoint[];
  /** Height of the bars (px). */
  height?: number;
  /** Past this many calls only the last `maxBars` are drawn (the figures still count every call). */
  maxBars?: number;
  testId?: string;
  style?: CSSProperties;
  /** For Tailwind apps (Booking); inline-style apps leave it out. */
  className?: string;
}) {
  const t = useT(KIT_MESSAGES);
  const stats = latencyStats(points.map((p) => p.ms));
  const summary = stats.count === 0
    ? t("latencyNone")
    : t("latencySummary", formatDuration(stats.p50), formatDuration(stats.p95), formatDuration(stats.max),
      stats.count);
  const shown = points.slice(-maxBars);
  const top = stats.max && stats.max > 0 ? stats.max : 1;
  return (
    <div data-testid={testId} data-kit-latency="" className={className}
      style={{ display: "flex", flexDirection: "column", gap: 4, minWidth: 0, fontFamily: sans, ...style }}>
      <div role="img" aria-label={summary}
        style={{ display: "flex", alignItems: "flex-end", gap: 2, height, minWidth: 0, overflow: "hidden",
          borderBottom: `1px solid ${K.line}` }}>
        {shown.map((p, i) => {
          const timed = p.ms != null && Number.isFinite(p.ms) && p.ms >= 0;
          const h = timed ? Math.max(2, Math.round((height * (p.ms as number)) / top)) : 0;
          return (
            <span key={i} title={timed ? formatDuration(p.ms) : undefined}
              style={{ flex: "1 1 0", minWidth: 2, maxWidth: 10, height: timed ? h : height,
                background: timed ? TONE[p.tone ?? "info"].fg : "transparent",
                border: timed ? undefined : `1px dashed ${K.line}`, borderRadius: 1, opacity: timed ? 0.85 : 1 }} />
          );
        })}
      </div>
      <div aria-hidden style={{ fontFamily: mono, fontSize: 11.5, color: K.muted, fontVariantNumeric: "tabular-nums" }}>
        {summary}
      </div>
    </div>
  );
}
