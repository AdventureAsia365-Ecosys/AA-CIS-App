"use client";
// app/admin/docs/page.tsx — AA-898: Platform › Developer › Docs, a READER of the ecosystem docs catalog.
//
// Data: GET /api/admin/docs/manifest and GET /api/admin/docs/page?repo=&path= (BFF → backend api/routers/admin_docs.py).
// The root repo workflow "Docs sync" builds the bundle from a CI checkout (publication rule v1: catalog pages and ADRs
// only; journals, audits and security pages never; account and resource IDs redacted) and uploads it to S3; the API
// caches it 5 minutes. Admin only (lib/rbac.ts PAGE_RULES + API_READ_RULES "docs"; the backend re-checks, ops.read).
// Owner-only pages (privacy / legal, infrastructure maps) appear only for a platform owner signed in with Cognito.
//
// What the page does: a list grouped by folder with search, type and audience filters (audience is navigation, not
// access); the selected page (?path=…, so a link can be shared) is rendered as Markdown with a table of contents, its
// catalog facts and the commit it came from. Links to other published pages open in the reader; anything not
// published is plain text. No editing here: docs change by PR in git. This repo is public: no page content lives here.
// Languages: English by default; a page with a published Vietnamese copy (<page>.vi.md, lang: vi, translation_of) gets
// an EN / VI switch, remembered in this browser (localStorage "aa_docs_lang"); until the viewer picks one there, the
// page language follows the admin UI language (AA-994: the topbar EN | VI). The list shows originals only; a page
// without the chosen language shows English with a note. ```mermaid blocks are drawn as diagrams (MarkdownView).
// AA-697: `![alt](x.svg)` shows an SVG of the bundle (GET /api/admin/docs/asset, checked server-side) through <img>.

import { useQuery, useMutation, useQueryClient } from "@tanstack/react-query";
import { ArrowLeft, BookMarked, Lock, RefreshCw, Search, X } from "lucide-react";
import { usePathname, useRouter, useSearchParams } from "next/navigation";
import { Suspense, useCallback, useEffect, useMemo, useState, useSyncExternalStore } from "react";
import {
  PageFrame,
  ApiError,
  Badge,
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
  useT,
  type Tone,
} from "../../_kit";
import { M } from "./messages";
import { DOC_AUDIENCE_MESSAGES, DOC_LANG_MESSAGES, DOC_TYPE_MESSAGES } from "@/lib/docsReader.messages";
import { MarkdownView, scrollToHeading, type MarkdownLink } from "../../_kit/MarkdownView";
import {
  type LangChoice,
  ALL,
  DOC_LANG_LABEL,
  DOC_LANG_STORAGE_KEY,
  DOC_LANGS,
  DEFAULT_DOC_LANG,
  audienceOptions,
  groupBySection,
  listedPages,
  matchesFilter,
  originalOf,
  parseDocLang,
  pickLanguage,
  outline,
  resolveImage,
  resolveLink,
  shortSha,
  typeOptions,
  type DocEntry,
  type DocLang,
  type DocPageResp,
  type DocsManifestResp,
} from "@/lib/docsReader";

const MANIFEST_PATH = "/api/admin/docs/manifest";
const MANIFEST_KEY = ["admin", "docs", "manifest"] as const;
const DEFAULT_REPO = "AA-Ecosys";

const STATUS_TONE: Record<string, Tone> = {
  active: "success",
  accepted: "success",
  draft: "warning",
  proposed: "warning",
  deprecated: "neutral",
  superseded: "neutral",
};

// ── small pieces ────────────────────────────────────────────────────────────────────────────────

function Card({ children, style }: { children: React.ReactNode; style?: React.CSSProperties }) {
  return (
    <section style={{ background: K.card, border: `1px solid ${K.line}`, borderRadius: RADIUS.lg, minWidth: 0, ...style }}>
      {children}
    </section>
  );
}

function Select({
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
    <select
      value={value}
      onChange={(e) => onChange(e.target.value)}
      aria-label={label}
      style={{
        flex: "1 1 120px",
        padding: "8px 10px",
        background: K.card,
        color: K.ink,
        border: `1px solid ${K.line}`,
        borderRadius: RADIUS.md,
        fontSize: 13,
        fontFamily: sans,
        maxWidth: "100%",
        minWidth: 0,
      }}
    >
      {options.map((o) => (
        <option key={o.value} value={o.value}>
          {o.label}
        </option>
      ))}
    </select>
  );
}

function DocRow({ doc, selected, onSelect }: { doc: DocEntry; selected: boolean; onSelect: () => void }) {
  const t = useT(M);
  const typeLabel = useT(DOC_TYPE_MESSAGES);
  // A link, not a button: the row has a real URL (?path=…), so a middle click or "open in new tab" works too.
  return (
    <a
      href={`/admin/docs?path=${encodeURIComponent(doc.path)}`}
      onClick={(e) => {
        if (e.metaKey || e.ctrlKey || e.shiftKey || e.button !== 0) return;
        e.preventDefault();
        onSelect();
      }}
      aria-current={selected ? "page" : undefined}
      style={{
        display: "block",
        padding: "8px 14px 8px 11px",
        background: selected ? K.accentTint : "transparent",
        borderLeft: `3px solid ${selected ? K.accent : "transparent"}`,
        fontFamily: sans,
        color: K.ink,
        textDecoration: "none",
      }}
    >
      <span style={{ display: "flex", alignItems: "center", gap: 6, fontSize: 13, fontWeight: 600, minWidth: 0 }}>
        {doc.access === "owner" && <Lock size={12} style={{ color: K.muted, flexShrink: 0 }} aria-label={t("ownerOnly")} />}
        <span style={{ overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>{doc.title}</span>
      </span>
      <span style={{ display: "block", fontSize: 11, color: K.muted, fontFamily: mono, marginTop: 2, overflowWrap: "anywhere" }}>
        {doc.id} · {typeLabel(doc.type)}
      </span>
    </a>
  );
}

function SectionLabel({ children }: { children: React.ReactNode }) {
  return (
    <div
      style={{
        fontSize: 11,
        fontWeight: 700,
        letterSpacing: "0.06em",
        textTransform: "uppercase",
        color: K.muted,
        padding: "12px 14px 4px",
      }}
    >
      {children}
    </div>
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
}) {
  const t = useT(M);
  const typeLabel = useT(DOC_TYPE_MESSAGES);
  const audienceLabel = useT(DOC_AUDIENCE_MESSAGES);
  const langName = useT(DOC_LANG_MESSAGES);
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
  const headings = useMemo(() => outline(markdown), [markdown]);
  const byLine = useMemo(() => new Map(headings.map((h) => [h.line, h.slug])), [headings]);
  const toc = headings.filter((h) => h.level === 2 || h.level === 3);
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

  // After the page renders, jump to the heading a link asked for (no state change, only a scroll).
  useEffect(() => {
    if (data && pendingHash) scrollToHeading(pendingHash);
  }, [data, pendingHash]);

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
      <div style={{ display: "flex", flexWrap: "wrap", alignItems: "center", gap: 6, marginBottom: 6 }}>
        <Badge tone="info">{typeLabel(p.type)}</Badge>
        {p.status && <Badge tone={STATUS_TONE[p.status] ?? "neutral"}>{p.status}</Badge>}
        {p.access === "owner" && (
          <Badge tone="warning">
            <Lock size={11} /> {t("ownerOnly")}
          </Badge>
        )}
        {p.generated && <Badge tone="neutral">{t("generated")}</Badge>}
      </div>
      <div style={{ fontSize: 12, color: K.muted, fontFamily: mono, marginBottom: 14, overflowWrap: "anywhere" }}>
        {p.id} · {p.path}
        {p.last_reviewed ? t("lastReviewed", p.last_reviewed) : ""}
        {p.audience.length ? t("forAudience", p.audience.map((a) => audienceLabel(a)).join(", ")) : ""}
      </div>

      {toc.length >= 3 && (
        <details style={{ marginBottom: 16, border: `1px solid ${K.line}`, borderRadius: RADIUS.md, padding: "8px 12px" }}>
          <summary style={{ cursor: "pointer", fontSize: 13, fontWeight: 600, color: K.ink }}>
            {t("onThisPageCount", toc.length)}
          </summary>
          <nav aria-label={t("onThisPage")} style={{ marginTop: 8, display: "grid", gap: 4 }}>
            {toc.map((h) => (
              <a
                key={h.slug}
                href={`#doc-${h.slug}`}
                onClick={(e) => {
                  e.preventDefault();
                  scrollToHeading(h.slug);
                }}
                style={{ fontSize: 13, color: K.accentDeep, paddingLeft: h.level === 3 ? 14 : 0, textDecoration: "none" }}
              >
                {h.text}
              </a>
            ))}
          </nav>
        </details>
      )}

      <MarkdownView
        markdown={markdown}
        resolveHref={resolveHref}
        slugForLine={slugForLine}
        resolveImage={resolveImg}
      />

      <footer
        style={{
          marginTop: 24,
          paddingTop: 12,
          borderTop: `1px solid ${K.line}`,
          fontSize: 12,
          color: K.muted,
          overflowWrap: "anywhere",
        }}
      >
        {t("source")} {data.source.repo ?? p.repo}@<span style={{ fontFamily: mono }}>{shortSha(data.source.sha) || t("unknown")}</span>
        {data.generated_at ? t("publishedAt", formatDateTime(data.generated_at)) : ""}{t("readOnlyFooter")}
      </footer>
    </article>
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

function DocsReaderPage() {
  const t = useT(M);
  const typeLabel = useT(DOC_TYPE_MESSAGES);
  const audienceLabel = useT(DOC_AUDIENCE_MESSAGES);
  const { formatDateTime } = useFormat();
  const uiLang = useLang();
  const isPhone = useIsPhone();
  const qc = useQueryClient();
  const router = useRouter();
  const pathname = usePathname();
  const params = useSearchParams();
  const selectedPath = params.get("path");
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
  const [pendingHash, setPendingHash] = useState("");
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
  // AA-697: SVG images of the bundle (architecture diagrams) the pages may show.
  const assets = useMemo(
    () => new Set((data?.assets ?? []).filter((a) => a.repo === selectedRepo).map((a) => a.path)),
    [data, selectedRepo],
  );
  const selected = selectedPath ? originalOf(selectedPath, byPath) : undefined;
  const shown = useMemo(() => pages.filter((p) => matchesFilter(p, { query, type, audience })), [pages, query, type, audience]);
  const groups = useMemo(() => groupBySection(shown), [shown]);

  const open = useCallback(
    (path: string, hash = "") => {
      setPendingHash(hash);
      const qs = new URLSearchParams();
      qs.set("path", path);
      router.push(`${pathname}?${qs.toString()}`, { scroll: false });
    },
    [router, pathname, setPendingHash],
  );
  const back = () => router.push(pathname, { scroll: false });

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
      <label
        style={{
          display: "flex",
          alignItems: "center",
          gap: 8,
          flex: "1 1 100%",
          minWidth: 0,
          padding: "8px 12px",
          background: K.card,
          border: `1px solid ${K.line}`,
          borderRadius: RADIUS.md,
        }}
      >
        <Search size={14} style={{ color: K.muted2, flexShrink: 0 }} />
        <input
          type="text"
          value={query}
          onChange={(e) => setQuery(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === "Escape") setQuery("");
          }}
          placeholder={t("searchPlaceholder")}
          aria-label={t("searchAria")}
          style={{
            flex: 1,
            minWidth: 0,
            border: "none",
            outline: "none",
            background: "transparent",
            color: K.ink,
            fontSize: 13,
            fontFamily: sans,
          }}
        />
        {query && (
          <Button variant="ghost" size="sm" onClick={() => setQuery("")} ariaLabel={t("clearSearch")} style={{ padding: "2px 6px" }}>
            <X size={12} />
          </Button>
        )}
      </label>
      <Select
        value={type}
        onChange={setType}
        label={t("type")}
        options={[{ value: ALL, label: t("allTypes") }, ...typeOptions(pages).map((ty) => ({ value: ty, label: typeLabel(ty) }))]}
      />
      <Select
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
      {filters}
      <div style={{ padding: "8px 14px 0", fontSize: 12, color: K.muted }}>
        {t("countOf", shown.length, pages.length)}
      </div>
      <div style={{ maxHeight: isPhone ? "none" : "calc(100vh - 290px)", overflowY: "auto", paddingBottom: 8 }}>
        {groups.length ? (
          groups.map((g) => (
            <div key={g.section}>
              <SectionLabel>{g.section}</SectionLabel>
              {g.pages.map((d) => (
                <DocRow
                  key={`${d.repo}/${d.path}`}
                  doc={d}
                  selected={d.path === (selected?.path ?? selectedPath) && d.repo === selectedRepo}
                  onSelect={() => open(d.path)}
                />
              ))}
            </div>
          ))
        ) : (
          <EmptyState title={t("noMatch")} description={t("noMatchBody")} />
        )}
      </div>
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
    />
  ) : (
    <EmptyState
      icon={<BookMarked size={30} />}
      title={t("selectPage")}
      description={t("selectPageBody")}
    />
  );

  return (
    <PageFrame style={{ fontFamily: sans }}>
      <SkeletonStyle />
      <PageHeader
        title={t("title")}
        description={description}
        actions={
          <Button variant="ghost" size="sm" onClick={() => reload.mutate()} disabled={busy} ariaLabel={t("reloadAria")}>
            {busy ? <Spinner size={13} /> : <RefreshCw size={13} />} {t("reload")}
          </Button>
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
        selectedPath ? (
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
          <Card style={{ padding: "18px 22px", minWidth: 0 }}>{reader}</Card>
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
