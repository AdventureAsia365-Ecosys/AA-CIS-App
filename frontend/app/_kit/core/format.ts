// UI kit core (root ADR 0016). Canonical: AA-CIS-App frontend/app/_kit/core. Edit only there; copies are checked byte for byte (kit.lock.json).
// app/_kit/core/format.ts
// AA-994 S4a — money, calendar-day and relative-time helpers of the kit, upstreamed from AA-Booking
// (`ui/booking/Money.tsx`, `ui/booking/dates.ts`, `ui/booking/DateText.tsx`). Money.tsx, DateText.tsx and
// DateField.tsx render with them.
//
// Pure and import-free on purpose: unit tests (CIS lib/kitFormat.test.mjs) load this file with Node's type stripping,
// which resolves only relative files with their extension. The `lang` argument is the kit UI language ("en" | "vi",
// core/i18n.ts) written out, so this file needs no import; components pass `useLang()`.
//
// Formats (one per UI language, never the runtime locale, so server and client render the same text):
//   formatMoney("1845", "USD")        -> "1,845.00 USD"   (vi: "1.845,00 USD")   always the currency's minor units
//   formatDay("2026-10-09")           -> "9 Oct 2026"     (vi: "9 thg 10, 2026") a calendar day, never shifted by zone
//   parseDayInput("28/10/2026")       -> "2026-10-28"     day first, never US order
//   relativeParts(t, now)             -> { unit: "h", n: 3 }  for "3 h ago"; null when older than 7 days

type UiLang = "en" | "vi";

const NUMBER_LOCALE: Readonly<Record<UiLang, string>> = { en: "en-GB", vi: "vi-VN" };

// ── money ───────────────────────────────────────────────────────────────────────────────────────────────────────

const minorUnitsCache = new Map<string, number>();

/** The currency's minor units (USD 2, VND 0, KWD 3), so amounts always show them; 2 for an unknown or bad code. */
export function minorUnits(code: string): number {
  if (!/^[A-Z]{3}$/.test(code)) return 2;
  let digits = minorUnitsCache.get(code);
  if (digits === undefined) {
    try {
      digits = new Intl.NumberFormat("en", { style: "currency", currency: code }).resolvedOptions()
        .maximumFractionDigits ?? 2;
    } catch {
      digits = 2;
    }
    minorUnitsCache.set(code, digits);
  }
  return digits;
}

/**
 * An amount plus its ISO code, never a bare "$": "1,845.00 USD". Amounts arrive from APIs as decimal strings; they are
 * formatted, never re-computed. Empty or unreadable: "—". No code: the number alone, with 2 decimals.
 */
export function formatMoney(
  amount: string | number | null | undefined,
  currency: string | null | undefined,
  lang: UiLang = "en",
): string {
  if (amount === null || amount === undefined || amount === "") return "—";
  const n = typeof amount === "number" ? amount : Number(amount);
  if (!Number.isFinite(n)) return "—";
  const code = (currency || "").toUpperCase().trim();
  const digits = code ? minorUnits(code) : 2;
  const formatted = new Intl.NumberFormat(NUMBER_LOCALE[lang] ?? "en-GB", {
    minimumFractionDigits: digits,
    maximumFractionDigits: digits,
  }).format(n);
  return code ? `${formatted} ${code}` : formatted;
}

// ── calendar days ───────────────────────────────────────────────────────────────────────────────────────────────
// A date-only value ("2026-10-09": a due date, a trip start) is a calendar day, not an instant: `new Date()` reads it
// as UTC midnight and shows the day before west of Greenwich, so days are always formatted in UTC here.

const ISO_DAY_RE = /^(\d{4})-(\d{2})-(\d{2})$/;

const MONTHS: Readonly<Record<string, number>> = {
  jan: 1, january: 1, feb: 2, february: 2, mar: 3, march: 3, apr: 4, april: 4, may: 5, jun: 6, june: 6,
  jul: 7, july: 7, aug: 8, august: 8, sep: 9, sept: 9, september: 9, oct: 10, october: 10, nov: 11, november: 11,
  dec: 12, december: 12,
};

function pad2(n: number): string {
  return n < 10 ? `0${n}` : String(n);
}

/** The ISO day for year / month / day, or null when that day does not exist (31 Feb). */
function toIsoDay(y: number, m: number, d: number): string | null {
  if (!Number.isInteger(y) || !Number.isInteger(m) || !Number.isInteger(d)) return null;
  if (y < 1900 || y > 2999 || m < 1 || m > 12 || d < 1) return null;
  const t = new Date(Date.UTC(y, m - 1, d));
  if (t.getUTCFullYear() !== y || t.getUTCMonth() !== m - 1 || t.getUTCDate() !== d) return null;
  return `${y}-${pad2(m)}-${pad2(d)}`;
}

/** True for a real calendar day written "YYYY-MM-DD". */
export function isIsoDay(value: string | null | undefined): value is string {
  if (!value) return false;
  const m = ISO_DAY_RE.exec(value);
  return !!m && toIsoDay(Number(m[1]), Number(m[2]), Number(m[3])) === value;
}

const DAY_FORMAT = { day: "numeric", month: "short", year: "numeric" } as const;

/** "2026-10-09" -> "9 Oct 2026" with no time-zone shift. A full timestamp is shown as the viewer's day; empty,
 *  unreadable or a day that does not exist ("2026-02-30"): "—". */
export function formatDay(value: string | null | undefined, lang: UiLang = "en"): string {
  if (!value) return "—";
  const locale = NUMBER_LOCALE[lang] ?? "en-GB";
  if (!isIsoDay(value)) {
    if (ISO_DAY_RE.test(value)) return "—";
    const d = new Date(value);
    return Number.isNaN(d.getTime()) ? "—" : d.toLocaleDateString(locale, DAY_FORMAT);
  }
  const [y, m, d] = value.split("-").map(Number);
  return new Date(Date.UTC(y, m - 1, d)).toLocaleDateString(locale, { ...DAY_FORMAT, timeZone: "UTC" });
}

/** What a person typed in a date field -> "YYYY-MM-DD", "" for an empty field, null when it is not a date.
 *  Accepts day-first numbers ("28/10/2026", "28-10-2026", "28.10.26"), English month names ("28 Oct 2026",
 *  "Oct 28 2026") and ISO ("2026-10-28"). Never month-first numbers: "10/11/2026" is 10 November. */
export function parseDayInput(text: string): string | null {
  const t = text.trim().toLowerCase().replace(/,/g, " ").replace(/\s+/g, " ");
  if (t === "") return "";
  let m = /^(\d{4})-(\d{1,2})-(\d{1,2})$/.exec(t);
  if (m) return toIsoDay(Number(m[1]), Number(m[2]), Number(m[3]));
  m = /^(\d{1,2})[/.\-\s](\d{1,2})[/.\-\s](\d{2}|\d{4})$/.exec(t);
  if (m) {
    const y = m[3].length === 2 ? 2000 + Number(m[3]) : Number(m[3]);
    return toIsoDay(y, Number(m[2]), Number(m[1]));
  }
  m = /^(\d{1,2})(?:st|nd|rd|th)? ([a-z]+)\.? (\d{4})$/.exec(t);
  if (m && MONTHS[m[2]]) return toIsoDay(Number(m[3]), MONTHS[m[2]], Number(m[1]));
  m = /^([a-z]+)\.? (\d{1,2})(?:st|nd|rd|th)? (\d{4})$/.exec(t);
  if (m && MONTHS[m[1]]) return toIsoDay(Number(m[3]), MONTHS[m[1]], Number(m[2]));
  return null;
}

// ── relative time ───────────────────────────────────────────────────────────────────────────────────────────────

const MINUTE = 60_000;
const HOUR = 60 * MINUTE;
const DAY = 24 * HOUR;

export type RelativeParts = { unit: "now" } | { unit: "min" | "h" | "d"; n: number };

/** How long ago an instant was, for list "Updated" columns: under a minute (or in the future) "now", then whole
 *  minutes, hours, days; null when older than 7 days (show the absolute date) or unreadable. DateText words it. */
export function relativeParts(value: string | number | Date, now: number = Date.now()): RelativeParts | null {
  const t = value instanceof Date ? value.getTime() : new Date(value).getTime();
  if (Number.isNaN(t)) return null;
  const diff = now - t;
  if (diff < MINUTE) return { unit: "now" };
  if (diff < HOUR) return { unit: "min", n: Math.floor(diff / MINUTE) };
  if (diff < DAY) return { unit: "h", n: Math.floor(diff / HOUR) };
  if (diff < 7 * DAY) return { unit: "d", n: Math.floor(diff / DAY) };
  return null;
}
