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
  // Translations (backend docs_reader "Translations"). Optional so an older API answer still type-checks.
  lang?: string;                          // "en" unless the page is a translation
  translation_of?: string | null;         // the English page's path when this page is a translation
  translations?: DocTranslation[];        // on an English page: its copies this caller may read, by lang
  // AA-1133 (reader v2, root build_docs_reader.py rule 11). Optional: an older API answer has none of them.
  nav?: DocNav | null;                    // sidebar group (front-matter `nav`, else derived from the folder)
  nav_order?: number | null;              // position in the group, lower first
  nav_derived?: boolean | null;           // the group came from the folder
  known_issue?: string | null;            // banner: the page has known errors and is being fixed
  last_changed?: { sha: string; date: string } | null;   // the file's last commit
};

export type DocNav = "start" | "guides" | "operations" | "architecture" | "reference" | "legal-security";
/** AA-1133: the sidebar groups in display order (root frontmatter.schema.json `nav`). */
export const DOC_NAV_GROUPS: DocNav[] = ["start", "guides", "operations", "architecture", "reference", "legal-security"];

export type DocTranslation = { lang: string; path: string; title: string | null };

// AA-697: an SVG image of the bundle (root build_docs_reader.py rule 9), e.g. an architecture diagram; AA-1064: also a
// PNG / WebP screenshot a published page links (rule 10).
export type DocAsset = { repo: string; path: string };

export type DocSource = { repo: string | null; sha: string | null; ref: string | null; github?: string | null };

export type DocsManifestResp = {
  generated_at: string | null;
  rule: string | null;
  source: DocSource;
  search?: { available: boolean };   // AA-1133: the bundle carries a search index
  pages: DocEntry[];
  assets?: DocAsset[];    // AA-697 / AA-1064: images (SVG, PNG, WebP) this caller may load (absent from an older API)
  counts: { shown: number; owner_only_shown: number; hidden_owner_only: number };
  is_owner: boolean;
  owner_read?: boolean;   // owner-only pages are listed for this caller (an owner, or the Dev-only E2E admin)
  loaded_at: string;
  cache_ttl_s: number;
};

export type DocPageResp = {
  page: DocEntry;
  markdown: string;
  source: DocSource;
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

// ── sidebar (AA-1133) ───────────────────────────────────────────────────────────────────────────

/** The sidebar group of a page: its `nav`, or "" when the API sent none (an older bundle: grouped by folder then). */
export function navOf(p: DocEntry): DocNav | "" {
  return p.nav && (DOC_NAV_GROUPS as string[]).includes(p.nav) ? p.nav : "";
}

/** Pages grouped by sidebar group in DOC_NAV_GROUPS order; inside a group by nav_order (pages without one after),
 *  then folder, then title. Null when no page carries a group (an older bundle: the caller groups by folder). */
export function groupByNav(pages: DocEntry[]): { section: string; pages: DocEntry[] }[] | null {
  if (!pages.some((p) => navOf(p))) return null;
  const order = (p: DocEntry) => (typeof p.nav_order === "number" ? p.nav_order : 1000);
  const groups = new Map<string, DocEntry[]>();
  for (const p of pages) {
    const g = navOf(p) || "reference";
    const list = groups.get(g);
    if (list) list.push(p);
    else groups.set(g, [p]);
  }
  return DOC_NAV_GROUPS.filter((g) => groups.has(g)).map((g) => ({
    section: g,
    pages: [...(groups.get(g) ?? [])].sort(
      (a, b) => order(a) - order(b) || sectionOf(a.path).localeCompare(sectionOf(b.path)) || a.title.localeCompare(b.title),
    ),
  }));
}

// ── GitHub links (AA-1133; tech lead S283: Source and History for every reader, Edit for admins) ───────────────

export type DocGithubLinks = { source: string; history: string; edit: string | null };

/** The page's GitHub links, or null when the bundle names no public repo (the repo went private, or an older bundle).
 *  Source is the file at the published commit; History and Edit follow the branch the bundle was built from. */
export function githubLinks(source: DocSource, path: string, canEdit: boolean): DocGithubLinks | null {
  const repo = source.github;
  if (!repo || !/^[A-Za-z0-9-]+\/[A-Za-z0-9._-]+$/.test(repo) || !path) return null;
  const file = path.split("/").map(encodeURIComponent).join("/");
  const ref = source.ref && /^[A-Za-z0-9._/-]+$/.test(source.ref) ? source.ref : "master";
  const sha = source.sha && /^[0-9a-f]{7,40}$/.test(source.sha) ? source.sha : ref;
  const base = `https://github.com/${repo}`;
  return {
    source: `${base}/blob/${sha}/${file}`,
    history: `${base}/commits/${ref}/${file}`,
    edit: canEdit ? `${base}/edit/${ref}/${file}` : null,
  };
}

// ── code blocks (AA-1133) ───────────────────────────────────────────────────────────────────────

const CODE_LANG_LABEL: Record<string, string> = {
  bash: "Bash", sh: "Shell", shell: "Shell", zsh: "Shell", console: "Console", python: "Python", py: "Python",
  sql: "SQL", hcl: "HCL", terraform: "Terraform", yaml: "YAML", yml: "YAML", json: "JSON", ts: "TypeScript",
  typescript: "TypeScript", tsx: "TSX", js: "JavaScript", javascript: "JavaScript", diff: "Diff", text: "Text",
  markdown: "Markdown", md: "Markdown", http: "HTTP", toml: "TOML", ini: "INI", dockerfile: "Dockerfile",
};

/** The language label of a fenced block from its code element's class list ("language-bash" -> "Bash"); "" when the
 *  block names no language. An unknown language shows as typed (letters, digits, + and - only, at most 20). */
export function codeLanguage(className: unknown): string {
  const classes = Array.isArray(className) ? className.map(String) : typeof className === "string" ? className.split(/\s+/) : [];
  const tag = classes.find((c) => c.startsWith("language-"));
  if (!tag) return "";
  const lang = tag.slice("language-".length).toLowerCase();
  if (!/^[a-z0-9+#-]{1,20}$/.test(lang)) return "";
  return CODE_LANG_LABEL[lang] ?? lang;
}

// ── search (AA-1133; backend GET /admin/docs/search) ─────────────────────────────────────────────

export type DocSnippetSegment = { text: string; hit: boolean };
export type DocSearchSection = { heading: string; slug: string; snippet: DocSnippetSegment[] };
export type DocSearchResult = {
  repo: string; path: string; lang: string; id: string; title: string; type: string; nav: DocNav | null;
  access: DocAccess; sections: DocSearchSection[];
};
export type DocSearchResp = {
  query: string; terms: string[]; results: DocSearchResult[]; total_pages: number; source_sha: string | null;
};

/** Server search runs from this many typed characters (the API refuses a shorter query). */
export const DOC_SEARCH_MIN_CHARS = 2;

export function searchUrl(query: string, limit = 20): string {
  return `/api/admin/docs/search?q=${encodeURIComponent(query.trim())}&limit=${limit}`;
}

/** The reader URL a search hit opens: the page, at the section's heading when it has one. */
export function hitHref(r: { path: string }, slug: string): string {
  return `/admin/docs?path=${encodeURIComponent(r.path)}${slug ? `#doc-${encodeURIComponent(slug)}` : ""}`;
}

// ── page facts (AA-1133) ────────────────────────────────────────────────────────────────────────

/** "tech-lead" -> "Tech lead": a role slug as a label (company docs name roles, not people). */
export function ownerLabel(owner: string | null | undefined): string {
  if (!owner) return "";
  const words = owner.replace(/[-_]+/g, " ").trim();
  return words ? words[0].toUpperCase() + words.slice(1) : "";
}

/** Minutes to read a Markdown body (about 220 words a minute, at least 1). */
export function readingMinutes(markdown: string): number {
  const words = markdown.split(/\s+/).filter(Boolean).length;
  return Math.max(1, Math.round(words / 220));
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

/** Image types the docs bundle publishes: SVG diagrams (AA-697) and PNG / WebP screenshots (AA-1064). */
export const DOC_IMAGE_SUFFIXES = [".svg", ".png", ".webp"] as const;

/** AA-697 / AA-1064: the BFF URL of an image found on page `from` (`![alt](src)`), or null when it is not a published
 *  image (SVG, PNG or WebP) of the bundle. Only a relative path that resolves to a listed asset qualifies: remote (http,
 *  https, protocol-relative), data: and other scheme URLs never load (a remote image would leak the viewer's IP), so
 *  the alt text shows instead. */
export function resolveImage(src: string | undefined, from: string, repo: string, assets: Set<string>): string | null {
  const target = imageTarget(src, from, assets);
  if (!target) return null;
  return `/api/admin/docs/asset?repo=${encodeURIComponent(repo)}&path=${encodeURIComponent(target)}`;
}

/** The repo-relative path of a published image (SVG, PNG or WebP) that `![alt](src)` on page `from` points at, or
 *  null (see resolveImage). Shared with the AI gateway Guide tab (lib/aiGatewayGuide.ts guideImage), which loads the
 *  same images through its own route. */
export function imageTarget(src: string | undefined, from: string, assets: Set<string>): string | null {
  if (!src) return null;
  const h = src.trim();
  if (!h || h.startsWith("#") || h.startsWith("//") || /^[a-z][a-z0-9+.-]*:/i.test(h)) return null;
  let decoded = h.split("#")[0].split("?")[0];
  try {
    decoded = decodeURIComponent(decoded);
  } catch {
    return null;
  }
  const base = from.includes("/") ? from.slice(0, from.lastIndexOf("/")) : "";
  const joined = decoded.startsWith("/") ? decoded.slice(1) : base ? `${base}/${decoded}` : decoded;
  const target = normalizePath(joined);
  if (!target || !assets.has(target)) return null;
  const lower = target.toLowerCase();
  if (!DOC_IMAGE_SUFFIXES.some((suffix) => lower.endsWith(suffix))) return null;
  return target;
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
export function outline(markdown: string, seed: string[] = []): Heading[] {
  const slugger = new Slugger();
  // AA-1133: headings removed from the body (the H1 the page header shows) still take their slug first, so the ids
  // stay the ones the search index computed on the whole page (root tools/docs/docs_search_index.py).
  for (const text of seed) slugger.slug(text);
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

/** AA-1133: the body without its first heading when that is an H1 (the page header shows the title instead), and the
 *  H1's text; the body unchanged and null otherwise. Front-matter is already stripped by the builder. */
export function splitLeadingH1(markdown: string): { body: string; h1: string | null } {
  const lines = markdown.split("\n");
  const i = lines.findIndex((l) => l.trim() !== "");
  const m = i >= 0 ? lines[i].match(/^ {0,3}#\s+(.+?)\s*#*\s*$/) : null;
  if (!m) return { body: markdown, h1: null };
  return { body: [...lines.slice(0, i), ...lines.slice(i + 1)].join("\n"), h1: headingText(m[1]) };
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

// ── languages ───────────────────────────────────────────────────────────────────────────────────

export type DocLang = "en" | "vi";
export const DOC_LANGS: DocLang[] = ["en", "vi"];
export const DEFAULT_DOC_LANG: DocLang = "en";
export const DOC_LANG_LABEL: Record<DocLang, { short: string; name: string }> = {
  en: { short: "EN", name: "English" },
  vi: { short: "VI", name: "Vietnamese" },
};
export const DOC_LANG_STORAGE_KEY = "aa_docs_lang";

export function parseDocLang(v: unknown): DocLang {
  return v === "vi" ? "vi" : DEFAULT_DOC_LANG;
}

/** The pages the list shows: English pages only (a translation is reached through its page's language switch). An
 * older API answer without `lang` lists everything. */
export function listedPages(pages: DocEntry[]): DocEntry[] {
  return pages.filter((p) => (p.lang ?? DEFAULT_DOC_LANG) === DEFAULT_DOC_LANG);
}

/** The original a path belongs to: itself, or the page it translates when that original is listed. */
export function originalOf(path: string, byPath: Map<string, DocEntry>): DocEntry | undefined {
  const e = byPath.get(path);
  if (!e) return undefined;
  if (e.translation_of && byPath.has(e.translation_of)) return byPath.get(e.translation_of);
  return e;
}

export type LangChoice = { path: string; lang: DocLang; available: DocLang[]; missing: boolean };

/** Which file to read for an original in the wanted language. `missing` = the wanted language has no copy (English is
 * shown instead). */
export function pickLanguage(original: DocEntry, wanted: DocLang): LangChoice {
  const byLang = new Map((original.translations ?? []).map((t) => [t.lang, t.path] as const));
  const own = parseDocLang(original.lang);
  const available = DOC_LANGS.filter((l) => l === own || byLang.has(l));
  if (wanted === own) return { path: original.path, lang: own, available, missing: false };
  const path = byLang.get(wanted);
  if (path) return { path, lang: wanted, available, missing: false };
  return { path: original.path, lang: own, available, missing: true };
}
