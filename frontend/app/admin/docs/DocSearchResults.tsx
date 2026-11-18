"use client";
// app/admin/docs/DocSearchResults.tsx — AA-1133: full-text search hits in the docs list (tech lead S283
// c-s283-docs-search: server-side FTS5).
//
// GET /api/admin/docs/search?q= (backend admin_docs.py → api/services/docs_search.py): the pages this caller may read
// that hold every typed word (prefix match, Vietnamese marks optional), best first, each with up to three sections and
// a snippet whose matched words come marked (segments, never HTML). A hit opens the page at its section heading.
// When the index is not published or not reachable (404 / 503), the list falls back to its title / ID / path filter
// and says so (DocsList), so search never leaves the reader empty.

import { useQuery } from "@tanstack/react-query";
import { ApiError, Badge, EmptyState, K, Spinner, apiGet, useT } from "../../_kit";
import { M } from "./messages";
import { DOC_NAV_MESSAGES } from "@/lib/docsReader.messages";
import {
  DOC_SEARCH_MIN_CHARS, hitHref, searchUrl, type DocSearchResp, type DocSearchResult,
} from "@/lib/docsReader";

/** The server search for `query` (enabled from DOC_SEARCH_MIN_CHARS characters while the bundle has an index). */
export function useDocSearch(query: string, enabled: boolean) {
  const q = query.trim();
  return useQuery({
    queryKey: ["admin", "docs", "search", q],
    queryFn: () => apiGet<DocSearchResp>(searchUrl(q), { admin: true }),
    enabled: enabled && q.length >= DOC_SEARCH_MIN_CHARS,
    staleTime: 5 * 60_000,
    retry: (count, err) => !(err instanceof ApiError && [403, 404, 422, 503].includes(err.status)) && count < 1,
  });
}

function Snippet({ segments }: { segments: DocSearchResult["sections"][number]["snippet"] }) {
  return (
    <span style={{ display: "block", fontSize: 12, color: K.ink2, lineHeight: 1.45, overflowWrap: "anywhere" }}>
      {segments.map((s, i) =>
        s.hit ? (
          <mark key={i} style={{ background: K.accentTint, color: K.ink, fontWeight: 600, padding: "0 1px" }}>{s.text}</mark>
        ) : (
          <span key={i}>{s.text}</span>
        ),
      )}
    </span>
  );
}

export function DocSearchResults({
  data, isLoading, onOpen,
}: {
  data: DocSearchResp | undefined;
  isLoading: boolean;
  onOpen: (path: string, slug: string) => void;
}) {
  const t = useT(M);
  const navLabel = useT(DOC_NAV_MESSAGES);
  if (isLoading) {
    return (
      <div role="status" style={{ display: "flex", alignItems: "center", gap: 8, padding: "12px 14px", fontSize: 12,
                                  color: K.muted }}>
        <Spinner size={12} /> {t("searching")}
      </div>
    );
  }
  if (!data || !data.results.length) {
    return <EmptyState title={t("searchNone")} description={t("searchNoneBody")} />;
  }
  const go = (e: React.MouseEvent, path: string, slug: string) => {
    if (e.metaKey || e.ctrlKey || e.shiftKey || e.button !== 0) return;
    e.preventDefault();
    onOpen(path, slug);
  };
  return (
    <div aria-label={t("searchResultsAria")} role="region" data-testid="docs-search-results">
      <div style={{ padding: "6px 14px", fontSize: 12, color: K.muted }} role="status">
        {t("searchCount", data.results.length, data.total_pages)}
      </div>
      {data.results.map((r) => (
        <div key={`${r.repo}/${r.path}`} style={{ padding: "8px 14px", borderTop: `1px solid ${K.line}` }}
             data-testid="docs-search-hit">
          <a href={hitHref(r, "")} onClick={(e) => go(e, r.path, "")}
             style={{ display: "flex", alignItems: "center", gap: 6, fontSize: 13, fontWeight: 600, color: K.ink,
                      textDecoration: "none" }}>
            <span style={{ minWidth: 0, overflowWrap: "anywhere" }}>{r.title}</span>
            {r.lang !== "en" && <Badge tone="neutral" dot={false}>{t("langBadge", r.lang)}</Badge>}
          </a>
          {r.nav && <div style={{ fontSize: 11, color: K.muted, marginTop: 1 }}>{navLabel(r.nav)}</div>}
          {r.sections.map((s, i) => (
            <a key={`${s.slug}-${i}`} href={hitHref(r, s.slug)} onClick={(e) => go(e, r.path, s.slug)}
               style={{ display: "block", marginTop: 5, textDecoration: "none" }}>
              <span style={{ display: "block", fontSize: 12, fontWeight: 600, color: K.accentDeep }}>
                {s.slug ? s.heading : t("topOfPage")}
              </span>
              <Snippet segments={s.snippet} />
            </a>
          ))}
        </div>
      ))}
    </div>
  );
}
