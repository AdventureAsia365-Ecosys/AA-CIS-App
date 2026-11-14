// lib/platformInfra.ts — Platform › Infra limits (app/admin/platform/infra): the response types of
// GET /api/admin/platform/infra (backend api/services/platform_infra.py) and the pure helpers of the page.
// The limits that stay in Terraform on purpose (WAF, API Gateway throttles, Lambda concurrency, AWS budgets, alarm
// thresholds) are read-only here; every label and explanation comes from the API in English and Vietnamese.

export type InfraLang = "en" | "vi";
export type Bilingual = { en: string; vi: string };

export type InfraUnit =
  | "per_5min_ip" | "per_5min" | "rps" | "requests" | "executions" | "usd_month" | "usd" | "percent" | "seconds"
  | "bytes" | "connections" | "count" | "switch" | "countries" | "alerts" | "text";

export interface InfraItem {
  key: string;
  value: string;
  unit: InfraUnit;
  source: string | null;
  label: Bilingual;
  known: boolean;
}

export interface InfraGroup {
  group: string;
  title: Bilingual;
  what: Bilingual;
  why: Bilingual;
  change: Bilingual;
  editable: Bilingual | null;
  editable_href: string | null;
  items: InfraItem[];
}

export interface InfraDoc {
  missing: boolean;
  reason: "not_published" | "unavailable" | "owner_tier" | null;
  id?: string;
  path?: string;
  title?: string;
  generated_at?: string | null;
  source?: { repo: string | null; sha: string | null };
  terraform_sha?: string | null;
}

export interface InfraResp {
  doc: InfraDoc;
  groups: InfraGroup[];
  missing_keys: string[];
}

/** The BFF path of the page's one read (admin only: lib/rbac.ts API_READ_RULES "platform"). */
export const INFRA_PATH = "/api/admin/platform/infra";

/** The docs reader link of the page the values come from (OPS-07, the generated alarm catalog). */
export function docsLink(doc: InfraDoc): string | null {
  return doc.path ? `/admin/docs?path=${encodeURIComponent(doc.path)}` : null;
}

export function pick(text: Bilingual | null | undefined, lang: InfraLang): string {
  if (!text) return "";
  return (lang === "vi" ? text.vi : text.en) || text.en || "";
}

/** The first 7 characters of a commit, or "" when unknown. */
export function shortSha(sha: string | null | undefined): string {
  return sha && /^[0-9a-f]{7,40}$/.test(sha) ? sha.slice(0, 7) : "";
}

const GIB = 1024 ** 3;

function plainNumber(value: string): number | null {
  return /^-?\d+(\.\d+)?$/.test(value.trim()) ? Number(value) : null;
}

function gib(n: number): string {
  const g = n / GIB;
  return `${Number.isInteger(g) ? g : g.toFixed(1)} GiB`;
}

function wholeHours(n: number): boolean {
  return Number.isInteger(n) && n >= 3600 && n % 3600 === 0;
}

/** A readable form of a value for its unit, or null when the raw value already reads well. Pure.
 *  bytes -> "10 GiB"; a whole number of seconds that is a whole number of hours -> "4 h"; large integers get
 *  thousands separators. "on" / "off", lists and text stay as the generator wrote them. */
export function prettyValue(item: Pick<InfraItem, "unit" | "value">, lang: InfraLang): string | null {
  const n = plainNumber(item.value);
  if (n === null) return null;
  if (item.unit === "bytes" && n >= GIB) return gib(n);
  if (item.unit === "seconds" && wholeHours(n)) return `${n / 3600} h`;
  return Math.abs(n) >= 10000 ? n.toLocaleString(lang === "vi" ? "vi-VN" : "en-US") : null;
}

/** Every item of every group whose label, key, value or source matches `query` (case-insensitive). Pure. */
export function filterGroups(groups: InfraGroup[], query: string, lang: InfraLang): InfraGroup[] {
  const q = query.trim().toLowerCase();
  if (!q) return groups;
  return groups
    .map((g) => ({
      ...g,
      items: g.items.filter((i) =>
        [pick(i.label, lang), i.key, i.value, i.source ?? "", pick(g.title, lang)]
          .some((s) => s.toLowerCase().includes(q))),
    }))
    .filter((g) => g.items.length > 0);
}
