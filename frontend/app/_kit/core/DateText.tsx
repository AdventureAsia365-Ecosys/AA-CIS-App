// UI kit core (root ADR 0016). Canonical: AA-CIS-App frontend/app/_kit/core. Edit only there; copies are checked byte for byte (kit.lock.json).
"use client";
// app/_kit/core/DateText.tsx
// AA-994 S4a — one date on screen, upstreamed from AA-Booking `ui/booking/DateText.tsx`: the kit formats ("9 Oct
// 2026", "9 Oct 2026, 14:05"; core/tokens.ts formatDate / formatDateTime) in the UI language, inside a <time> element.
// `relative` shows "3 h ago" (list "Updated" columns; older than 7 days falls back to the date) with the full date
// and time in the tooltip. A date-only value ("2026-10-09": a due date, a trip start) is a calendar day and is shown
// as that day in every time zone, with no time and no "x d ago" (core/format.ts formatDay). An instant is shown in the
// viewer's time zone, which the server cannot know, so its <time> carries suppressHydrationWarning (React's advice for
// timestamps): the client text replaces the server text without a hydration error.

import type { CSSProperties } from "react";
import { formatDay, isIsoDay, relativeParts } from "./format";
import { translator, type Lang } from "./i18n";
import { formatDate, formatDateTime } from "./tokens";
import { KIT_MESSAGES } from "./kit.messages";
import { useLang } from "./useI18n";

/** "just now", "5 min ago", "3 h ago", "2 d ago" in a UI language; older than 7 days: the absolute date. */
export function relativeTime(value: string | number | Date, now: number = Date.now(), lang: Lang = "en"): string {
  const t = value instanceof Date ? value.getTime() : new Date(value).getTime();
  if (Number.isNaN(t)) return "—";
  const p = relativeParts(t, now);
  if (p === null) return formatDate(t, lang);
  const tr = translator(KIT_MESSAGES, lang);
  if (p.unit === "now") return tr("justNow");
  if (p.unit === "min") return tr("minutesAgo", p.n);
  if (p.unit === "h") return tr("hoursAgo", p.n);
  return tr("daysAgo", p.n);
}

export function DateText({
  value,
  withTime = false,
  relative = false,
  style,
  className,
}: {
  value: string | Date | null | undefined;
  withTime?: boolean;
  relative?: boolean;
  style?: CSSProperties;
  /** For Tailwind apps (Booking); inline-style apps leave it out. */
  className?: string;
}) {
  const lang = useLang();
  if (value == null || value === "") return <span className={className} style={style}>—</span>;
  if (typeof value === "string" && isIsoDay(value)) {
    return (
      <time dateTime={value} className={className} style={style}>
        {formatDay(value, lang)}
      </time>
    );
  }
  const d = value instanceof Date ? value : new Date(value);
  if (Number.isNaN(d.getTime())) return <span className={className} style={style}>—</span>;
  const absolute = withTime ? formatDateTime(d, lang) : formatDate(d, lang);
  return (
    <time
      suppressHydrationWarning
      dateTime={d.toISOString()}
      title={relative ? formatDateTime(d, lang) : undefined}
      className={className}
      style={style}
    >
      {relative ? relativeTime(d, undefined, lang) : absolute}
    </time>
  );
}
