// lib/erasureView.ts — AA-1190 (S296 live check): the Data erasure page's view in the URL, both ways: the tab
// (`?tab=offboarding`, the My work tile "Data erasure to approve") and the selected DRY RUN / apply jobs
// (`?job=…&apply=…`, so a reload between the DRY RUN and the apply keeps the page). The page reads it through
// useLinkFilters (useSearchParams), so a client-side tile click opens the Tenant offboarding tab, not only a direct
// load. Pure and dependency-free, so node --test checks it (lib/erasureView.test.mjs).

export type ErasureTab = "subjects" | "offboarding";

export interface ErasureView { job: string | null; apply: string | null }

const JOB_ID = /^[0-9a-f-]{36}$/i;

/** The tab a query string asks for: "offboarding" only for `?tab=offboarding`, else "subjects". */
export function erasureTabFromSearch(search: string): ErasureTab {
  return new URLSearchParams(search).get("tab") === "offboarding" ? "offboarding" : "subjects";
}

/** The selected jobs a query string carries (a value that is not a job id is ignored). */
export function erasureViewFromSearch(search: string): ErasureView {
  const p = new URLSearchParams(search);
  const id = (k: string) => {
    const v = p.get(k);
    return v && JOB_ID.test(v) ? v.toLowerCase() : null;
  };
  return { job: id("job"), apply: id("apply") };
}

/** The query string (without "?") after a change of tab or jobs; every other key of `search` is kept. */
export function erasureSearch(search: string, change: Partial<ErasureView> & { tab?: ErasureTab }): string {
  const p = new URLSearchParams(search);
  if (change.tab !== undefined) {
    if (change.tab === "offboarding") p.set("tab", "offboarding");
    else p.delete("tab");
  }
  for (const k of ["job", "apply"] as const) {
    if (!(k in change)) continue;
    const v = change[k];
    if (v) p.set(k, v);
    else p.delete(k);
  }
  return p.toString();
}
