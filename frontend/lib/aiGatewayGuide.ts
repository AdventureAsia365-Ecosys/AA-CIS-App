// lib/aiGatewayGuide.ts — AA-1042 (S270): the Guide tab of AI gateway (/admin/ai-gateway?tab=guide).
//
// Backend GET /admin/ai-gateway/guide (api/services/ai_gateway_guide.py, spend.read) answers {doc, doc_reason, live}:
// the gateway's one prose page (root docs/architecture/ai-gateway.md, catalog id ARCH-01-ai-gateway, and its
// Vietnamese copy) from the Docs sync bundle, and the live tables (routes with the deployment that serves each stage
// first, the model x connection matrix, the connection register with secret NAMES only, the connection caps, the stage
// coverage). Pure helpers here (node --test loads this file directly): which language to show, how a link in the page
// renders, and the rows of each table. The tab: app/admin/ai-gateway/GuideTab.tsx.

import { imageTarget, resolveLink } from "./docsReader.ts";

export const GUIDE_API = "/api/admin/ai-gateway/guide";
/** AA-1064: the BFF route of an image the guide page links (backend GET /admin/ai-gateway/guide/asset, spend.read). */
export const GUIDE_ASSET_API = "/api/admin/ai-gateway/guide/asset";
export const GUIDE_QUERY_KEY = ["ai-gateway", "guide"] as const;

export type GuideTranslation = { lang: string; path: string; title: string | null; markdown: string };

export type GuideDoc = {
  missing: false;
  id: string; repo: string; path: string; title: string; lang: string;
  status?: string | null; last_reviewed?: string | null;
  markdown: string;
  translations: GuideTranslation[];
  source: { repo: string | null; sha: string | null };
  generated_at?: string | null;
  /** reader: this caller may open /admin/docs (backend POLICY of GET /admin/docs/page); published: the staff pages
   *  of the bundle, sent only to a reader. */
  links: { reader: boolean; published: string[] };
  /** AA-1064: the staff images of the bundle the page (or a translation) links; absent from an older API answer. */
  assets?: string[];
};
export type GuideDocMissing = { missing: true; reason: "not_published" | "unavailable" | string };

export type RouteStage = {
  stage: string; role: string | null; is_active: boolean; product?: string | null; chain: string[];
  pii_class?: string | null;
  first: string | null; first_model: string | null; first_connection: string | null;
  called_first?: string | null;
  skipped: { model: string | null; connection: string | null; reason: string | null }[];
  error?: string | null;
};
export type ProviderOrder = {
  model_key: string; label?: string;
  deployments: { connection_key: string; priority: number; enabled: boolean; connection_enabled?: boolean;
    access_state?: string | null }[];
};
export type GuideRoutes = {
  version: number; order_mode: string; updated_at?: string | null; updated_by?: string | null;
  max_attempts?: number | null; stages: RouteStage[]; provider_order: ProviderOrder[];
};
export type MatrixCell = {
  model_key: string; connection_key: string; enabled: boolean; priority: number | null;
  access_state: string | null; last_probe_at: string | null;
};
export type GuideMatrix = {
  models: { model_key: string; label: string; family: string | null; enabled: boolean | null }[];
  connections: { connection_key: string; label: string; kind: string | null; enabled: boolean | null }[];
  cells: MatrixCell[];
};
export type RegisterRow = {
  connection_key: string; kind: string; label: string | null; enabled: boolean; data_scope: string | null;
  access_state: string | null; last_probe_at: string | null; secret_name: string | null;
  credential_env: string | null; credential_source: string | null; failure_domain: string | null;
  balance_source: string | null; served_roles: string[] | null; deployments: number; deployments_enabled: number;
};
export type CapRow = {
  connection_key: string; kind: string | null; label: string | null; enabled: boolean;
  monthly_budget_usd: number | null; month_spent_usd: number; month_calls: number; used_pct: number | null;
  alert_pct: number | null; hard_stop: boolean | null; state: string;
};
export type GuideCoverage = {
  ready: boolean; ok?: boolean; checked_at?: string | null; stages_checked?: number;
  uncovered: string[]; disabled: string[]; proof_max_age_hours?: number;
  stages: { stage: string; role: string | null; status: string; live_model: string | null;
    live_connection: string | null; unproven: number }[];
};
export type GuideLive = {
  routes: GuideRoutes | null; matrix: GuideMatrix | null; register: RegisterRow[] | null; caps: CapRow[] | null;
  coverage: GuideCoverage | null; errors: { source: string; error: string }[];
};
export type GuideResp = {
  generated_at: string; page_id: string;
  doc: GuideDoc | GuideDocMissing | null;
  doc_reason: string | null;
  live: GuideLive;
};

/** What the doc part shows: the page in a language, a "not published yet" note, or a "not shown here" note. */
export type DocView =
  | { kind: "page"; lang: string; path: string; title: string; markdown: string; available: string[];
      fallback: boolean }
  | { kind: "missing"; reason: string }
  | { kind: "restricted"; reason: string };

/** The doc in the app language: the Vietnamese copy when the app is in Vietnamese and the bundle has it, else the
 *  English page (fallback = a language was asked for that the bundle does not hold). */
export function docView(resp: Pick<GuideResp, "doc" | "doc_reason"> | undefined, lang: string): DocView {
  const doc = resp?.doc;
  if (!doc) return { kind: "restricted", reason: resp?.doc_reason ?? "not_shown" };
  if (doc.missing) return { kind: "missing", reason: doc.reason };
  const available = ["en", ...doc.translations.map((t) => t.lang)];
  const t = lang !== "en" ? doc.translations.find((x) => x.lang === lang) : undefined;
  if (t) {
    return { kind: "page", lang: t.lang, path: t.path, title: t.title ?? doc.title, markdown: t.markdown,
             available, fallback: false };
  }
  return { kind: "page", lang: "en", path: doc.path, title: doc.title, markdown: doc.markdown, available,
           fallback: lang !== "en" };
}

// An issue reference: "AA-1042", "AA-646/650", with its slice words ("AA-1025 H1a", "AA-1002 S4").
const ISSUE = String.raw`AA-[0-9]+(?:\/[0-9]+)?(?:[ \t]+[A-Z][A-Za-z0-9.]*(?![A-Za-z0-9.]*[a-z]{3}))*`;
const ISSUE_PAREN = new RegExp(String.raw`[ \t]*\([ \t]*${ISSUE}[ \t]*\)`, "g");
const ISSUE_LIST_HEAD = new RegExp(String.raw`${ISSUE}[ \t]*,[ \t]*`, "g");
const ISSUE_LIST_TAIL = new RegExp(String.raw`[ \t]*,[ \t]*${ISSUE}`, "g");
const ISSUE_BARE = new RegExp(String.raw`(?<![A-Za-z0-9-])${ISSUE}`, "g");

function stripProse(text: string): string {
  if (!text.includes("AA-")) return text;
  return text
    .replace(ISSUE_PAREN, "")
    .replace(ISSUE_LIST_HEAD, "")
    .replace(ISSUE_LIST_TAIL, "")
    .replace(ISSUE_BARE, "")
    .replace(/\([ \t]*[,/;][ \t]*/g, "(")
    .replace(/[ \t]*[,/;][ \t]*\)/g, ")")
    .replace(/\([ \t]*\)/g, "")
    .replace(/(\S)[ \t]{2,}/g, "$1 ")
    .replace(/[ \t]+([,.;:)])/g, "$1");
}

/** The page text without internal issue references (no issue IDs in what a page shows; the docs keep them for
 *  engineers). Line by line so the Markdown structure stays: indentation, list markers and tables are kept, a fenced
 *  block and an inline code span are left as written. */
export function stripIssueIdsMarkdown(markdown: string): string {
  let fence: string | null = null;
  return markdown.split("\n").map((line) => {
    const f = /^\s*(```+|~~~+)/.exec(line);
    if (f) {
      if (fence === null) fence = f[1][0];
      else if (f[1][0] === fence) fence = null;
      return line;
    }
    if (fence !== null || !line.includes("AA-")) return line;
    // odd parts are inline code spans: kept as written
    return line.split(/(`[^`]*`)/).map((part, i) => (i % 2 ? part : stripProse(part))).join("");
  }).join("\n");
}

/** How one link of the page renders (MarkdownView's MarkdownLink without the click handler): another published page
 *  opens the docs reader only when this viewer may read /admin/docs (backend flag AND the page rule of the viewer's
 *  role); an anchor stays an anchor; https / mailto open a new tab; anything else is plain text (no broken link). */
export type GuideLink =
  | { kind: "internal"; href: string; path: string; hash: string }
  | { kind: "anchor"; id: string }
  | { kind: "external"; href: string }
  | { kind: "text" };

export function guideLink(href: string | undefined, fromPath: string, links: GuideDoc["links"] | undefined,
                          canOpenDocs: boolean): GuideLink {
  const reader = Boolean(links?.reader) && canOpenDocs;
  const r = resolveLink(href, fromPath, new Set(reader ? links?.published ?? [] : []));
  if (r.kind === "page" && reader) {
    const hash = r.hash ? `#doc-${encodeURIComponent(r.hash)}` : "";
    return { kind: "internal", href: `/admin/docs?path=${encodeURIComponent(r.path)}${hash}`, path: r.path,
             hash: r.hash };
  }
  if (r.kind === "anchor") return { kind: "anchor", id: r.hash };
  if (r.kind === "external") return { kind: "external", href: r.href };
  return { kind: "text" };
}

/** AA-1064: the BFF URL of an image of the guide page (`![alt](src)` on page `fromPath`), or null (alt text shows) when
 *  it is not one of the bundle images the backend listed for this page (`doc.assets`). Served by the tab's own
 *  route (spend.read), so a viewer who may not open /admin/docs still sees the page's pictures. */
export function guideImage(src: string | undefined, fromPath: string, assets: readonly string[] | undefined):
  string | null {
  const target = imageTarget(src, fromPath, new Set(assets ?? []));
  return target ? `${GUIDE_ASSET_API}?path=${encodeURIComponent(target)}` : null;
}

// ── table rows ─────────────────────────────────────────────────────────────────────────────────────────────────

export type RouteRow = {
  stage: string; role: string | null; active: boolean; chain: string[]; first: string | null;
  /** the serving model is not the chain's first key (a refused, switched-off or skipped primary) */
  servedByFallback: boolean;
  skipped: string[];
};

/** One row per stage: active stages first (by stage), then the switched-off ones. */
export function routeRows(routes: GuideRoutes | null | undefined): RouteRow[] {
  const rows = (routes?.stages ?? []).map((s) => ({
    stage: s.stage, role: s.role, active: s.is_active, chain: s.chain ?? [],
    first: s.is_active ? s.first : null,
    servedByFallback: Boolean(s.is_active && s.first_model && s.chain?.length && s.first_model !== s.chain[0]),
    skipped: (s.skipped ?? []).map((x) => `${x.model ?? "?"}${x.connection ? `@${x.connection}` : ""}: ${x.reason ?? ""}`
      .replace(/: $/, "")),
  }));
  return rows.sort((a, b) => Number(b.active) - Number(a.active) || a.stage.localeCompare(b.stage));
}

export type MatrixRow = {
  model_key: string; label: string; enabled: boolean | null;
  /** connection key -> the deployment there (absent = the model has no deployment on that connection) */
  cells: Record<string, MatrixCell>;
  /** connection keys in the order the gateway tries them (priority, then key) */
  order: string[];
};

export function matrixRows(matrix: GuideMatrix | null | undefined): { connections: string[]; rows: MatrixRow[] } {
  if (!matrix) return { connections: [], rows: [] };
  const byModel = new Map<string, MatrixCell[]>();
  for (const c of matrix.cells) byModel.set(c.model_key, [...(byModel.get(c.model_key) ?? []), c]);
  const rows = matrix.models.map((m) => {
    const cells = byModel.get(m.model_key) ?? [];
    const order = [...cells].sort((a, b) => (a.priority ?? 0) - (b.priority ?? 0)
      || a.connection_key.localeCompare(b.connection_key)).map((c) => c.connection_key);
    return { model_key: m.model_key, label: m.label, enabled: m.enabled,
             cells: Object.fromEntries(cells.map((c) => [c.connection_key, c])), order };
  });
  return { connections: matrix.connections.map((c) => c.connection_key), rows };
}

/** A matrix cell's tone: off (switched off), ok (the probe got an answer), refused, or unknown. */
export function cellTone(cell: MatrixCell | undefined): "none" | "off" | "ok" | "refused" | "unknown" {
  if (!cell) return "none";
  if (!cell.enabled) return "off";
  if (cell.access_state === "ok") return "ok";
  if (cell.access_state && ["access_denied", "quota_exhausted", "unconfigured", "error"].includes(cell.access_state)) {
    return "refused";
  }
  return "unknown";
}

export type CoverageRow = { stage: string; role: string | null; status: string; live: string | null;
  unproven: number };

/** The coverage table: every active stage (no live model first), then each switched-off stage as status "disabled". */
export function coverageRows(cov: GuideCoverage | null | undefined): CoverageRow[] {
  if (!cov || !cov.ready) return [];
  const active = cov.stages.map((s) => ({
    stage: s.stage, role: s.role, status: s.status, unproven: s.unproven,
    live: s.live_model ? `${s.live_model}${s.live_connection ? `@${s.live_connection}` : ""}` : null,
  })).sort((a, b) => Number(a.status === "live") - Number(b.status === "live") || a.stage.localeCompare(b.stage));
  const off = (cov.disabled ?? []).map((stage) => ({ stage, role: null, status: "disabled", live: null,
                                                     unproven: 0 }));
  return [...active, ...off];
}

