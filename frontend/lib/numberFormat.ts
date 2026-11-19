// frontend/lib/numberFormat.ts
// AA-1127 C3 (S283 UI audit, Jobs and Jev decisions) — one way to print counts and small USD amounts on the admin
// pages: thousands separators in the UI language (en-GB "744,707", vi-VN "744.707"), and USD with 2 decimals from a
// cent up (a 4-decimal "$139.4329" or "$0.0000" was noise), more decimals only below a cent so a sub-cent LLM cost does
// not show as $0.00. Pure, tested in lib/aa1127C3.test.mjs.

export type NumLang = "en" | "vi";

const LOCALE: Readonly<Record<NumLang, string>> = { en: "en-GB", vi: "vi-VN" };

/** A count with thousands separators in the UI language; "—" when missing. */
export function fmtCount(n: number | null | undefined, lang: NumLang = "en"): string {
  if (n == null || !Number.isFinite(n)) return "—";
  return new Intl.NumberFormat(LOCALE[lang] ?? "en-GB", { maximumFractionDigits: 0 }).format(n);
}

/** USD: "$0.00" for zero, 2 decimals from one cent up, up to 4 significant decimals below a cent ("<$0.0001" under
 *  that). Thousands separators in the UI language. */
export function fmtUsd(v: number | null | undefined, lang: NumLang = "en"): string {
  const n = v ?? 0;
  if (!Number.isFinite(n)) return "—";
  const locale = LOCALE[lang] ?? "en-GB";
  const abs = Math.abs(n);
  const sign = n < 0 ? "-" : "";
  if (abs === 0) return "$" + new Intl.NumberFormat(locale, { minimumFractionDigits: 2 }).format(0);
  if (abs < 0.0001) return `<${sign}$` + new Intl.NumberFormat(locale, { minimumFractionDigits: 4 }).format(0.0001);
  const digits = abs >= 0.01 ? 2 : 4;
  return sign + "$" + new Intl.NumberFormat(locale, {
    minimumFractionDigits: digits, maximumFractionDigits: digits,
  }).format(abs);
}
