"use client";
// app/admin/docs/page.tsx — AA-898: Platform › Developer › Docs, a READER of the ecosystem docs catalog.
//
// Data: GET /api/admin/docs/manifest and GET /api/admin/docs/page?repo=&path= (BFF → backend api/routers/admin_docs.py).
// The root repo workflow "Docs sync" builds the bundle from a CI checkout (publication rule v1: catalog pages and ADRs
// only; journals, audits and security pages never; account and resource IDs redacted) and uploads it to S3; the API
// caches it 5 minutes. Admin only (lib/rbac.ts PAGE_RULES + API_READ_RULES "docs"; the backend re-checks, ops.read).
// Owner-only pages (privacy / legal, infrastructure maps) appear only for a platform owner signed in with Cognito.
//
// What the page does: a list grouped by folder (AA-1104: numbered 1, 1.1…, groups fold, DocsList.tsx) with search, type
// and audience filters (audience is navigation, not access); the selected page (?path=…, so a link can be shared) is
// rendered as Markdown with a table of contents, its catalog facts and the commit it came from. Links to other
// published pages open in the reader; anything not published is plain text. No editing here: docs change by PR in
// git. This repo is public: no page content lives here.
// Languages: English by default; a page with a published Vietnamese copy (<page>.vi.md, lang: vi, translation_of) gets
// an EN / VI switch, remembered in this browser (localStorage "aa_docs_lang"); until the viewer picks one there, the
// page language follows the admin UI language (AA-994: the topbar EN | VI). The list shows originals only; a page
// without the chosen language shows English with a note. ```mermaid blocks are drawn as diagrams (MarkdownView).
// AA-697: `![alt](x.svg)` shows an SVG of the bundle (GET /api/admin/docs/asset, checked server-side) through <img>.
// AA-1064: a PNG / WebP screenshot a page links loads the same way (lib/docsAsset.ts decodes it in the BFF).
// AA-1133 (reader v2, tech lead S283 decisions): every staff role (lib/rbac.ts; backend docs.read). The list is the
// topic sidebar (bundle `nav`); the search box runs the server full-text search (FTS5) and lists hits with snippets,
// falling back to the title filter when the index is not there. A page opens with a header (DocHeader: breadcrumb,
// title, lead, facts, GitHub Source / History for all, Edit for admins, known-issue banner), code blocks with a
// language label and Copy (MarkdownView), the table of contents as a sticky scroll-spy column when the reader itself
// is wide enough (DocToc; lib/docsReader.ts tocColumnFits, measured on the reader, not the viewport), and "Was this page helpful?" at the end (DocFeedback; not for the read-only viewer). Admins open the
// answers with the Feedback button (?view=feedback).
// AA-1133 follow-up (S284 live check): the feedback question shows only when the manifest says this session may
// answer (`can_feedback`: not for the viewer nor the read-only Dev E2E session); opening a page at a heading (a search
// hit, a link) puts #doc-<slug> in the address, so the place can be shared.

import { useQuery, useMutation, useQueryClient } from "@tanstack/react-query";
import { ArrowLeft, BookMarked, RefreshCw, Search, X } from "lucide-react";
import { usePathname, useRouter, useSearchParams } from "next/navigation";
import { Suspense, useCallback, useEffect, useMemo, useState, useSyncExternalStore } from "react";
import {
  PageFrame,
  ApiError,
  Button,
  EmptyState,
  ErrorState,
  K,
  PageHeader,
  RADIUS,
  SkeletonStyle,
  Spinner,
  TableSkeleton,
  apiGet,
  mono,
  sans,
  useFormat,
  useIsPhone,
  useLang,
  useMediaQuery,
  useT,
  useAdminIdentity,
  IconButton, Input, Select,
} from "../../_kit";
import { feedbackAllowed } from "@/lib/docsFeedback";
import { DocHeader } from "./DocHeader";
import { DocToc } from "./DocToc";
import { DocFeedback, FeedbackToggle, FeedbackView } from "./DocFeedback";
import { SearchFallbackNote, useDocsSearchPanel } from "./DocSearchResults";
import { M } from "./messages";
import { DocsList } from "./DocsList";
import { DOC_AUDIENCE_MESSAGES, DOC_LANG_MESSAGES, DOC_TYPE_MESSAGES } from "@/lib/docsReader.messages";
import { HEADING_ID_PREFIX, MarkdownView, scrollToHeading, type MarkdownLink } from "../../_kit/MarkdownView";
import { addressHeading, headingHash } from "@/lib/docsToc";
import {
  type LangChoice,
  ALL,
  DOC_LANG_LABEL,
  DOC_LANG_STORAGE_KEY,
  DOC_LANGS,
  DEFAULT_DOC_LANG,
  DOC_TOC_COLUMN,
  DOC_TOC_GAP,
  audienceOptions,
  githubLinks,
  splitLeadingH1,
  listedPages,
  originalOf,
  parseDocLang,
  pickLanguage,
  outline,
  resolveImage,
  resolveLink,
  shortSha,
  tocColumnFits,
  typeOptions,
  type DocEntry,
  type DocLang,
  type DocPageResp,
  type DocsManifestResp,
  type Heading,
} from "@/lib/docsReader";

// AA-1133: from this viewport width the reader card gets roomier padding. Whether the sticky table of contents sits
// beside the text is decided by the reader's own width (ReaderColumns), never by this query.
const WIDE_QUERY = "(min-width: 1280px)";

const MANIFEST_PATH = "/api/admin/docs/manifest";
const MANIFEST_KEY = ["admin", "docs", "manifest"] as const;
const DEFAULT_REPO = "AA-Ecosys";

// ── small pieces ────────────────────────────────────────────────────────────────────────────────

function Card({ children, style }: { children: React.ReactNode; style?: React.CSSProperties }) {
  return (
    <section style={{ background: K.card, border: `1px solid ${K.line}`, borderRadius: RADIUS.lg, minWidth: 0, ...style }}>
      {children}
    </section>
  );
}

function FilterSelect({
  value,
  onChange,
  label,
  options,
}: {
  value: string;
  onChange: (v: string) => void;
  label: string;
  options: { value: string; label: string }[];
}) {
  return (
    <Select
      value={value}
      onChange={(e) => onChange(e.target.value)}
      aria-label={label}
      options={options}
      style={{ flex: "1 1 120px" }}
    />
  );
}

// ── reader ──────────────────────────────────────────────────────────────────────────────────────

function LangSwitch({ choice, onLang }: { choice: LangChoice; onLang: (l: DocLang) => void }) {
  const t = useT(M);
  const langName = useT(DOC_LANG_MESSAGES);
  return (
    <div role="group" aria-label={t("pageLanguage")} style={{ display: "inline-flex", gap: 4 }} data-testid="docs-lang-switch">
      {DOC_LANGS.map((l) => {
        const has = choice.available.includes(l);
        const active = choice.lang === l;
        return (
          <Button
            key={l}
            size="sm"
            variant={active ? "primary" : "secondary"}
            disabled={!has && !active}
            onClick={() => onLang(l)}
            ariaLabel={`${langName(l)}${active ? t("shown") : ""}`}
            title={has ? langName(l) : t("noVersion", langName(l))}
          >
            {DOC_LANG_LABEL[l].short}
          </Button>
        );
      })}
    </div>
  );
}

function Reader({
  repo,
  original,
  fallbackPath,
  lang,
  onLang,
  published,
  assets,
  onOpen,
  pendingHash,
  canFeedback,
}: {
  repo: string;
  original: DocEntry | undefined;
  fallbackPath: string;
  lang: DocLang;
  onLang: (l: DocLang) => void;
  published: Set<string>;
  assets: Set<string>;
  onOpen: (path: string, hash: string) => void;
  pendingHash: string;
  /** May this session answer "Was this page helpful?" (lib/docsFeedback.ts feedbackAllowed). */
  canFeedback: boolean;
}) {
  const t = useT(M);
  const langName = useT(DOC_LANG_MESSAGES);
  const role = useAdminIdentity().role;
  const { formatDateTime } = useFormat();
  const uiLang = useLang();
  const choice: LangChoice = original
    ? pickLanguage(original, lang)
    : { path: fallbackPath, lang: DEFAULT_DOC_LANG, available: [DEFAULT_DOC_LANG], missing: false };
  const path = choice.path;
  const { data, error, isLoading, refetch } = useQuery({
    queryKey: ["admin", "docs", "page", repo, path],
    queryFn: () =>
      apiGet<DocPageResp>(`/api/admin/docs/page?repo=${encodeURIComponent(repo)}&path=${encodeURIComponent(path)}`, {
        admin: true,
      }),
    staleTime: 5 * 60_000,
    retry: (count, err) => !(err instanceof ApiError && (err.status === 404 || err.status === 403)) && count < 2,
  });

  const markdown = data?.markdown ?? "";
  // AA-1133: the header shows the title, so the body's own H1 is taken out (its slug still counts: outline seed).
  const split = useMemo(() => splitLeadingH1(markdown), [markdown]);
  const body = split.body;
  const headings = useMemo(() => outline(body, split.h1 ? [split.h1] : []), [body, split.h1]);
  const byLine = useMemo(() => new Map(headings.map((h) => [h.line, h.slug])), [headings]);
  const toc = useMemo(() => headings.filter((h) => h.level === 2 || h.level === 3), [headings]);
  const slugForLine = useCallback((line: number | undefined) => (line ? byLine.get(line) : undefined), [byLine]);
  const resolveHref = useCallback(
    (href: string | undefined): MarkdownLink => {
      const r = resolveLink(href, path, published);
      if (r.kind === "page") {
        const hash = r.hash ? `#doc-${encodeURIComponent(r.hash)}` : "";
        return {
          kind: "internal",
          href: `/admin/docs?path=${encodeURIComponent(r.path)}${hash}`,
          onNavigate: () => onOpen(r.path, r.hash),
        };
      }
      if (r.kind === "anchor") return { kind: "anchor", id: r.hash };
      if (r.kind === "external") return { kind: "external", href: r.href };
      return { kind: "text" };
    },
    [path, published, onOpen],
  );
  const resolveImg = useCallback(
    (src: string | undefined) => resolveImage(src, path, repo, assets),
    [path, repo, assets],
  );

  if (isLoading) return <TableSkeleton rows={10} cols={1} />;
  if (error instanceof ApiError && error.status === 404) {
    return (
      <EmptyState
        icon={<BookMarked size={30} />}
        title={t("pageNotFound")}
        description={t("pageNotFoundBody")}
      />
    );
  }
  if (error || !data) {
    return <ErrorState message={t("couldNotLoadPage", (error as Error | null)?.message ?? t("noData"))} onRetry={() => refetch()} />;
  }

  const p = data.page;
  const pagePath = original?.path ?? p.path;
  return (
    <article style={{ minWidth: 0 }} lang={choice.lang}>
      {(choice.available.length > 1 || choice.missing) && (
        <div style={{ display: "flex", flexWrap: "wrap", alignItems: "center", justifyContent: "space-between", gap: 8,
                      marginBottom: 10 }} lang={uiLang}>
          <LangSwitch choice={choice} onLang={onLang} />
          {choice.missing && (
            <span style={{ fontSize: 12, color: K.muted }} data-testid="docs-lang-missing">
              {t("missingLang", langName(lang), langName(choice.lang))}
            </span>
          )}
        </div>
      )}
      <DocHeader
        page={p}
        title={split.h1 ?? p.title}
        markdown={body}
        links={githubLinks(data.source, pagePath, role === "admin")}
        hasVietnamese={choice.available.includes("vi") && choice.lang !== "vi"}
        github={data.source.github}
      />
      <ReaderColumns toc={toc} scrollTo={pendingHash}>
          <MarkdownView
            markdown={body}
            resolveHref={resolveHref}
            slugForLine={slugForLine}
            resolveImage={resolveImg}
          />
          <footer
            style={{
              marginTop: 28,
              paddingTop: 14,
              borderTop: `1px solid ${K.line}`,
              display: "grid",
              gap: 12,
            }}
          >
            <DocFeedback key={`${repo}/${pagePath}`} repo={repo} path={pagePath} lang={choice.lang}
                         show={canFeedback} />
            <div style={{ fontSize: 12, color: K.muted, overflowWrap: "anywhere" }}>
              {t("source")} {data.source.repo ?? p.repo}@<span style={{ fontFamily: mono }}>{shortSha(data.source.sha) || t("unknown")}</span>
              {data.generated_at ? t("publishedAt", formatDateTime(data.generated_at)) : ""}{t("readOnlyFooter")}
            </div>
          </footer>
      </ReaderColumns>
    </article>
  );
}

/** The content-box width of an element, kept current by a ResizeObserver; 0 until it is measured. */
function useElementWidth(): [(el: HTMLDivElement | null) => void, number] {
  const [el, setEl] = useState<HTMLDivElement | null>(null);
  const [width, setWidth] = useState(0);
  useEffect(() => {
    if (!el || typeof ResizeObserver === "undefined") return;
    const ro = new ResizeObserver((entries) => setWidth(entries[0]?.contentRect.width ?? 0));
    ro.observe(el);
    return () => ro.disconnect();
  }, [el]);
  return [setEl, width];
}

/**
 * AA-1133: the text and, when the reader itself has room (tocColumnFits on its measured width), a sticky table of
 * contents beside it; the disclosure above the text otherwise. One grid in both cases, so switching never remounts
 * the text (diagrams are not drawn again on a resize).
 *
 * `scrollTo` is the heading a link, a search hit or the page address asked for. The jump waits until the reader is
 * measured: the first render lays the table of contents out above the text, and a jump made then would land too low
 * once it moves to the side (AA-1133 follow-up). Only a scroll, no state change.
 */
function ReaderColumns({ toc, scrollTo, children }: { toc: Heading[]; scrollTo: string; children: React.ReactNode }) {
  const [ref, width] = useElementWidth();
  const side = tocColumnFits(width);
  const measured = width > 0 || typeof ResizeObserver === "undefined";
  useEffect(() => {
    if (measured && scrollTo) scrollToHeading(scrollTo);
  }, [measured, scrollTo]);
  return (
    <div
      ref={ref}
      data-testid="docs-reader-columns"
      data-toc={side ? "side" : "top"}
      style={{
        display: "grid",
        gridTemplateColumns: side ? `minmax(0, 1fr) ${DOC_TOC_COLUMN}px` : "minmax(0, 1fr)",
        gap: side ? DOC_TOC_GAP : 0,
        alignItems: "start",
      }}
    >
      {!side && <DocToc items={toc} sticky={false} />}
      <div style={{ minWidth: 0, maxWidth: side ? 820 : undefined }}>{children}</div>
      {side && <DocToc items={toc} sticky />}
    </div>
  );
}

// ── page language (this browser) ────────────────────────────────────────────────────────────────

// localStorage when it works (it can throw or be empty in a private window); the in-memory value otherwise. null =
// the viewer has not picked a page language: the page then follows the admin UI language (AA-994).
let memoryDocLang: DocLang | null = null;
const docLangListeners = new Set<() => void>();

function readDocLang(): DocLang | null {
  try {
    const stored = window.localStorage.getItem(DOC_LANG_STORAGE_KEY);
    return stored === null ? memoryDocLang : parseDocLang(stored);
  } catch {
    return memoryDocLang;
  }
}

function writeDocLang(l: DocLang): void {
  memoryDocLang = l;
  try {
    window.localStorage.setItem(DOC_LANG_STORAGE_KEY, l);
  } catch {
    /* storage blocked: the choice lasts for this visit */
  }
  docLangListeners.forEach((f) => f());
}

function subscribeDocLang(onChange: () => void): () => void {
  docLangListeners.add(onChange);
  window.addEventListener("storage", onChange);
  return () => {
    docLangListeners.delete(onChange);
    window.removeEventListener("storage", onChange);
  };
}

// ── page ────────────────────────────────────────────────────────────────────────────────────────

/** No page open: the admin feedback view (?view=feedback), else the "select a page" hint. */
function NoPageOpen({ feedback, onOpen }: { feedback: boolean; onOpen: (path: string) => void }) {
  const t = useT(M);
  if (feedback) return <FeedbackView onOpen={onOpen} />;
  return <EmptyState icon={<BookMarked size={30} />} title={t("selectPage")} description={t("selectPageBody")} />;
}

/** AA-1133: what the page shows from the URL: a page (?path=), or for an admin the feedback view (?view=feedback). */
function useDocsView() {
  const isAdmin = useAdminIdentity().role === "admin";
  const router = useRouter();
  const pathname = usePathname();
  const params = useSearchParams();
  const feedback = isAdmin && params.get("view") === "feedback";
  const path = feedback ? null : params.get("path");
  return {
    isAdmin,
    feedback,
    path,
    showReader: !!path || feedback,
    toggleFeedback: () => router.push(feedback ? pathname : `${pathname}?view=feedback`, { scroll: false }),
    cardPadding: (wide: boolean) => (wide ? "22px 28px" : "18px 22px"),
  };
}

function DocsReaderPage() {
  const t = useT(M);
  const typeLabel = useT(DOC_TYPE_MESSAGES);
  const audienceLabel = useT(DOC_AUDIENCE_MESSAGES);
  const { formatDateTime } = useFormat();
  const uiLang = useLang();
  const isPhone = useIsPhone();
  const wide = useMediaQuery(WIDE_QUERY);
  const view = useDocsView();
  const isAdmin = view.isAdmin;
  const qc = useQueryClient();
  const router = useRouter();
  const pathname = usePathname();
  const params = useSearchParams();
  const feedbackView = view.feedback;
  const identityRole = useAdminIdentity().role;
  const selectedPath = view.path;
  const selectedRepo = params.get("repo") || DEFAULT_REPO;

  const { data, error, isLoading, isFetching, refetch } = useQuery({
    queryKey: MANIFEST_KEY,
    queryFn: () => apiGet<DocsManifestResp>(MANIFEST_PATH, { admin: true }),
    staleTime: 5 * 60_000,
    retry: (count, err) => !(err instanceof ApiError && (err.status === 404 || err.status === 403)) && count < 2,
  });
  const reload = useMutation({
    mutationFn: () => apiGet<DocsManifestResp>(`${MANIFEST_PATH}?refresh=1`, { admin: true }),
    onSuccess: (d) => {
      qc.setQueryData(MANIFEST_KEY, d);
      qc.invalidateQueries({ queryKey: ["admin", "docs", "page"] });
    },
  });

  const [query, setQuery] = useState("");
  const [type, setType] = useState(ALL);
  const [audience, setAudience] = useState(ALL);
  // The heading to open at: a search hit or a link sets it (open); a fresh load of a shared ?path=…#doc-<slug> address
  // starts from the address (AA-1133 follow-up: the browser's own fragment scroll runs before the page text exists).
  const [pendingHash, setPendingHash] = useState(() =>
    addressHeading(typeof window !== "undefined" ? window.location : undefined, HEADING_ID_PREFIX),
  );
  // Page language: the viewer's last choice remembered in this browser (best effort); without one, the admin UI
  // language (English by default).
  const lang = useSyncExternalStore(subscribeDocLang, readDocLang, () => null) ?? uiLang;
  const setLang = writeDocLang;

  const allPages = useMemo(() => data?.pages ?? [], [data]);
  const pages = useMemo(() => listedPages(allPages), [allPages]);
  const byPath = useMemo(
    () => new Map(allPages.filter((p) => p.repo === selectedRepo).map((p) => [p.path, p] as const)),
    [allPages, selectedRepo],
  );
  const published = useMemo(() => new Set(byPath.keys()), [byPath]);
  // AA-697 / AA-1064: images of the bundle (SVG diagrams, PNG / WebP screenshots) the pages may show.
  const assets = useMemo(
    () => new Set((data?.assets ?? []).filter((a) => a.repo === selectedRepo).map((a) => a.path)),
    [data, selectedRepo],
  );
  const selected = selectedPath ? originalOf(selectedPath, byPath) : undefined;

  const open = useCallback(
    (path: string, hash = "") => {
      setPendingHash(hash);
      const qs = new URLSearchParams();
      qs.set("path", path);
      router.push(`${pathname}?${qs.toString()}${headingHash(hash, HEADING_ID_PREFIX)}`, { scroll: false });
    },
    [router, pathname, setPendingHash],
  );
  const back = () => router.push(pathname, { scroll: false });
  // AA-1133: the server full-text search while the bundle has an index; the title filter otherwise (or on an error).
  // AA-1133 follow-up: hits in the reader's language list first (its page language, else the admin UI language).
  const search = useDocsSearchPanel(query, data, open, lang);

  // A link straight to a translation (?path=x.vi.md): show it through its original with that language chosen, so the
  // switch and the list highlight work and a shared link keeps one address per page.
  useEffect(() => {
    if (!selectedPath || !selected || selected.path === selectedPath) return;
    const target = byPath.get(selectedPath);
    if (target?.lang) writeDocLang(parseDocLang(target.lang));
    const qs = new URLSearchParams();
    qs.set("path", selected.path);
    if (selectedRepo !== DEFAULT_REPO) qs.set("repo", selectedRepo);
    router.replace(`${pathname}?${qs.toString()}${typeof window !== "undefined" ? window.location.hash : ""}`, {
      scroll: false,
    });
  }, [selectedPath, selected, byPath, selectedRepo, router, pathname]);

  const busy = isFetching || reload.isPending;
  const loadError = (reload.error as Error | null) ?? (error as Error | null);
  const notPublished = error instanceof ApiError && error.status === 404;
  const description = data
    ? t(
        "descLoaded",
        pages.length,
        `${data.source.repo ?? t("theRootRepo")}@${shortSha(data.source.sha)}`,
        data.generated_at ? t("descPublished", formatDateTime(data.generated_at)) : "",
        data.counts.hidden_owner_only ? t("descHidden", data.counts.hidden_owner_only) : "",
      )
    : t("descEmpty");

  const filters = (
    <div style={{ display: "flex", flexWrap: "wrap", gap: 8, padding: 12, borderBottom: `1px solid ${K.line}` }}>
      <div style={{ position: "relative", flex: "1 1 100%", minWidth: 0 }}>
        <Search
          size={14}
          style={{ position: "absolute", left: 12, top: "50%", transform: "translateY(-50%)", color: K.muted2, pointerEvents: "none" }}
        />
        <Input
          type="text"
          value={query}
          onChange={(e) => setQuery(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === "Escape") setQuery("");
          }}
          placeholder={t("searchPlaceholder")}
          aria-label={t("searchAria")}
          style={{ width: "100%", padding: "0 36px 0 34px" }}
        />
        <SearchFallbackNote show={search.fallback} />
        {query && (
          <IconButton
            variant="plain"
            size="sm"
            onClick={() => setQuery("")}
            label={t("clearSearch")}
            style={{ position: "absolute", right: 6, top: "50%", transform: "translateY(-50%)" }}
          >
            <X size={12} />
          </IconButton>
        )}
      </div>
      <FilterSelect
        value={type}
        onChange={setType}
        label={t("type")}
        options={[{ value: ALL, label: t("allTypes") }, ...typeOptions(pages).map((ty) => ({ value: ty, label: typeLabel(ty) }))]}
      />
      <FilterSelect
        value={audience}
        onChange={setAudience}
        label={t("audience")}
        options={[
          { value: ALL, label: t("allReaders") },
          ...audienceOptions(pages).map((a) => ({ value: a, label: audienceLabel(a) })),
        ]}
      />
    </div>
  );

  const list = (
    <Card style={{ overflow: "hidden" }}>
      <DocsList
        pages={pages}
        query={query}
        type={type}
        audience={audience}
        selected={selected}
        selectedPath={selectedPath}
        selectedRepo={selectedRepo}
        onOpen={open}
        filters={filters}
        isPhone={isPhone}
        searchPanel={search.panel}
      />
    </Card>
  );

  const reader = selectedPath ? (
    <Reader
      repo={selectedRepo}
      original={selected}
      fallbackPath={selectedPath}
      lang={lang}
      onLang={setLang}
      published={published}
      assets={assets}
      onOpen={open}
      pendingHash={pendingHash}
      canFeedback={feedbackAllowed(data, identityRole)}
    />
  ) : (
    <NoPageOpen feedback={feedbackView} onOpen={open} />
  );

  return (
    <PageFrame style={{ fontFamily: sans }}>
      <SkeletonStyle />
      <PageHeader
        title={t("title")}
        description={description}
        actions={
          <span style={{ display: "inline-flex", flexWrap: "wrap", gap: 8 }}>
            <FeedbackToggle show={isAdmin} active={feedbackView} onToggle={view.toggleFeedback} />
            <Button variant="ghost" size="sm" onClick={() => reload.mutate()} disabled={busy} ariaLabel={t("reloadAria")}>
              {busy ? <Spinner size={13} /> : <RefreshCw size={13} />} {t("reload")}
            </Button>
          </span>
        }
      />

      {isLoading ? (
        <Card style={{ padding: 16 }}>
          <TableSkeleton rows={8} cols={2} />
        </Card>
      ) : notPublished ? (
        <Card>
          <EmptyState
            icon={<BookMarked size={30} />}
            title={t("noDocs")}
            description={t("noDocsBody")}
          />
        </Card>
      ) : loadError && !data ? (
        <Card>
          <ErrorState message={t("couldNotLoadDocs", loadError.message)} onRetry={() => refetch()} />
        </Card>
      ) : isPhone ? (
        view.showReader ? (
          <Card style={{ padding: "14px 16px" }}>
            <div style={{ marginBottom: 10 }}>
              <Button variant="ghost" size="sm" onClick={back} ariaLabel={t("backToList")}>
                <ArrowLeft size={13} /> {t("allDocs")}
              </Button>
            </div>
            {reader}
          </Card>
        ) : (
          list
        )
      ) : (
        <div
          style={{
            display: "grid",
            gridTemplateColumns: "minmax(260px, 320px) minmax(0, 1fr)",
            gap: 14,
            alignItems: "start",
          }}
        >
          {list}
          <Card style={{ padding: view.cardPadding(wide), minWidth: 0 }}>{reader}</Card>
        </div>
      )}
    </PageFrame>
  );
}

export default function DocsPage() {
  // useSearchParams needs a Suspense boundary for `next build` (the page is prerendered as a client shell).
  return (
    <Suspense>
      <DocsReaderPage />
    </Suspense>
  );
}
