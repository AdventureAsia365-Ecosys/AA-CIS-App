// frontend/lib/docsReader.ts — AA-898: pure helpers of the docs reader page /admin/docs.
//
// The page lists the docs bundle the root repo "Docs sync" workflow publishes (backend GET /admin/docs/manifest) and
// renders one page's Markdown (GET /admin/docs/page). These helpers hold the parts worth testing: filters, the
// resolution of links between pages (a relative link to another published page opens it in the reader; anything
// else is either an in-page anchor, an external https link, or plain text), heading slugs and the table of contents.
// No page content lives in this repo (it is public): the tests use invented paths.

export type DocAccess = "staff" | "owner";

export type DocEntry = {
  repo: string;
  path: string;
  id: string;
  title: string;
  description: string | null;
  type: string;
  status: string | null;
  audience: string[];
  owner: string | null;
  last_reviewed: string | null;
  generated: boolean;
  access: DocAccess;
  bytes: number | null;
};

export type DocsManifestResp = {
  generated_at: string | null;
  rule: string | null;
  source: { repo: string | null; sha: string | null; ref: string | null };
  pages: DocEntry[];
  counts: { shown: number; owner_only_shown: number; hidden_owner_only: number };
  is_owner: boolean;
  loaded_at: string;
  cache_ttl_s: number;
};

export type DocPageResp = {
  page: DocEntry;
  markdown: string;
  source: { repo: string | null; sha: string | null; ref: string | null };
  generated_at: string | null;
};

export const ALL = "__all__";

// Display order and labels of the Diátaxis types (plus adr / report / template) the catalog uses.
export const TYPE_LABEL: Record<string, string> = {
  tutorial: "Tutorial",
  "how-to": "How-to",
  reference: "Reference",
  explanation: "Explanation",
  adr: "ADR",
  report: "Report",
  template: "Template",
};

export const AUDIENCE_LABEL: Record<string, string> = {
  owner: "Owner (CEO)",
  staff: "Staff",
  engineer: "Engineer",
  devops: "DevOps",
  cto: "CTO",
  cio: "CIO",
  pm: "PM",
  agent: "Agent",
};

export function typeLabel(t: string): string {
  return TYPE_LABEL[t] ?? t;
}

export function audienceLabel(a: string): string {
  return AUDIENCE_LABEL[a] ?? a;
}

/** The distinct values of a field across the pages, in label order for types, alphabetical otherwise. */
export function typeOptions(pages: DocEntry[]): string[] {
  const seen = new Set(pages.map((p) => p.type));
  const known = Object.keys(TYPE_LABEL).filter((t) => seen.has(t));
  const rest = [...seen].filter((t) => !(t in TYPE_LABEL)).sort();
  return [...known, ...rest];
}

export function audienceOptions(pages: DocEntry[]): string[] {
  const seen = new Set(pages.flatMap((p) => p.audience));
  const known = Object.keys(AUDIENCE_LABEL).filter((a) => seen.has(a));
  const rest = [...seen].filter((a) => !(a in AUDIENCE_LABEL)).sort();
  return [...known, ...rest];
}

export type DocFilter = { query: string; type: string; audience: string };

export function matchesFilter(p: DocEntry, f: DocFilter): boolean {
  if (f.type !== ALL && p.type !== f.type) return false;
  if (f.audience !== ALL && !p.audience.includes(f.audience)) return false;
  const q = f.query.trim().toLowerCase();
  if (!q) return true;
  return [p.title, p.id, p.path, p.description ?? ""].some((s) => s.toLowerCase().includes(q));
}

/** The folder a page lives in, for grouping the list: docs/ops/runbooks/x.md -> "ops/runbooks"; docs/x.md -> "docs". */
export function sectionOf(path: string): string {
  const parts = path.split("/");
  if (parts.length <= 2) return parts[0] || "docs";
  return parts.slice(1, -1).join("/");
}

export function groupBySection(pages: DocEntry[]): { section: string; pages: DocEntry[] }[] {
  const map = new Map<string, DocEntry[]>();
  for (const p of [...pages].sort((a, b) => a.path.localeCompare(b.path))) {
    const s = sectionOf(p.path);
    const list = map.get(s);
    if (list) list.push(p);
    else map.set(s, [p]);
  }
  return [...map.entries()]
    .sort(([a], [b]) => (a === "docs" ? -1 : b === "docs" ? 1 : a.localeCompare(b)))
    .map(([section, list]) => ({ section, pages: list }));
}

// ── links ───────────────────────────────────────────────────────────────────────────────────────

export type ResolvedLink =
  | { kind: "page"; path: string; hash: string }   // another published page: open it in the reader
  | { kind: "anchor"; hash: string }               // a heading on this page
  | { kind: "external"; href: string }             // https / mailto: open in a new tab
  | { kind: "none" };                              // a repo file that is not published: shown as text

/** Normalize "a/b/../c/./d.md" -> "a/c/d.md"; null when it climbs above the repo root. */
export function normalizePath(path: string): string | null {
  const out: string[] = [];
  for (const seg of path.split("/")) {
    if (!seg || seg === ".") continue;
    if (seg === "..") {
      if (!out.length) return null;
      out.pop();
      continue;
    }
    out.push(seg);
  }
  return out.join("/");
}

/** Resolve a Markdown link found on page `from` against the set of published paths. */
export function resolveLink(href: string | undefined, from: string, published: Set<string>): ResolvedLink {
  if (!href) return { kind: "none" };
  const h = href.trim();
  if (h.startsWith("#")) return { kind: "anchor", hash: h.slice(1) };
  if (/^(https?:|mailto:)/i.test(h)) return /^(https:|mailto:)/i.test(h) ? { kind: "external", href: h } : { kind: "none" };
  if (/^[a-z][a-z0-9+.-]*:/i.test(h) || h.startsWith("//")) return { kind: "none" };   // javascript:, data:, ...
  const [rawPath, ...rest] = h.split("#");
  const hash = rest.join("#");
  let decoded = rawPath;
  try {
    decoded = decodeURIComponent(rawPath.split("?")[0]);
  } catch {
    return { kind: "none" };
  }
  const base = from.includes("/") ? from.slice(0, from.lastIndexOf("/")) : "";
  const joined = decoded.startsWith("/") ? decoded.slice(1) : base ? `${base}/${decoded}` : decoded;
  const target = normalizePath(joined);
  if (target === null) return { kind: "none" };
  if (target === from) return hash ? { kind: "anchor", hash } : { kind: "page", path: target, hash: "" };
  if (published.has(target)) return { kind: "page", path: target, hash };
  // A folder link (docs/ops/) or a README-less folder: try its README.md.
  const readme = normalizePath(`${target}/README.md`);
  if (readme && published.has(readme)) return { kind: "page", path: readme, hash };
  return { kind: "none" };
}

// ── headings ────────────────────────────────────────────────────────────────────────────────────

/** GitHub-like heading slug: lower case, spaces to "-", punctuation dropped (letters with accents kept). */
export function slugify(text: string): string {
  return text
    .trim()
    .toLowerCase()
    .replace(/[^\p{L}\p{N}\s_-]/gu, "")
    .replace(/\s/g, "-");
}

/** Gives repeated slugs a -1, -2 suffix like GitHub, so in-page anchors stay unique. */
export class Slugger {
  private seen = new Map<string, number>();

  slug(text: string): string {
    const base = slugify(text) || "section";
    const n = this.seen.get(base);
    this.seen.set(base, (n ?? -1) + 1);
    return n === undefined ? base : `${base}-${n + 1}`;
  }
}

/** Plain text of an inline Markdown heading: drops links' targets, emphasis and code ticks. */
export function headingText(md: string): string {
  return md
    .replace(/!\[([^\]]*)\]\([^)]*\)/g, "$1")
    .replace(/\[([^\]]*)\]\([^)]*\)/g, "$1")
    .replace(/[`*_~]/g, "")
    .trim();
}

export type Heading = { line: number; level: number; text: string; slug: string };
export type TocItem = { level: 2 | 3; text: string; slug: string };

/** Every ATX heading of a Markdown body (fenced code blocks skipped) with its 1-based line and a unique slug. The
 * renderer looks the slug up by the heading node's start line, so the ids and the table of contents always agree. */
export function outline(markdown: string): Heading[] {
  const slugger = new Slugger();
  const out: Heading[] = [];
  let fence: string | null = null;
  markdown.split("\n").forEach((line, i) => {
    const f = line.match(/^\s*(```+|~~~+)/);
    if (f) {
      if (fence === null) fence = f[1][0];
      else if (f[1][0] === fence) fence = null;
      return;
    }
    if (fence !== null) return;
    const m = line.match(/^ {0,3}(#{1,6})\s+(.+?)\s*#*\s*$/);
    if (!m) return;
    const text = headingText(m[2]);
    out.push({ line: i + 1, level: m[1].length, text, slug: slugger.slug(text) });
  });
  return out;
}

/** The h2 / h3 outline for the page's table of contents. */
export function tableOfContents(markdown: string): TocItem[] {
  return outline(markdown)
    .filter((h) => h.level === 2 || h.level === 3)
    .map((h) => ({ level: h.level as 2 | 3, text: h.text, slug: h.slug }));
}

/** "a1b2c3d4e5f6…" -> first 12 characters, "" when unknown. */
export function shortSha(sha: string | null | undefined): string {
  return sha ? sha.slice(0, 12) : "";
}
