// lib/issueIds.ts — keep internal issue IDs out of text a page shows (AA-994).
//
// Our own copy never carries one (guard: tests/unit/test_aa994_no_issue_ids_in_ui.py). Text the page shows from
// elsewhere does: the API reference (/admin/api-docs) renders the OpenAPI document, whose descriptions are the
// route docstrings, and those keep their issue references as code documentation. stripIssueIds() takes them out at
// display time, so the reader sees the sentence without the reference.

// "AA-123 — ", "AA-123 C3 — ", "AA-916 WM-06 — ", "AA-646/650: " at the start of a text or a line.
const LEAD = /(^|\n)[ \t]*AA-[0-9]+(?:\/[0-9]+)?(?:[ \t]+(?:[A-Z][A-Za-z0-9.-]*|\([^()\n]*\)))*[ \t]*(?:—|–|:)[ \t]*/g;
// "(AA-123)", "(AA-123, AA-456)", "(AA-123 C2 / AA-456)": a parenthesis that holds only references.
const REF = String.raw`AA-[0-9]+(?:\/[0-9]+)?(?:[ \t]+[A-Z][A-Za-z0-9.]*)*`;
// One reference, then (separator, reference) pairs: the separator is required between two references, so the
// pattern cannot split a run of references in more than one way (no exponential backtracking, CodeQL js/redos).
const PAREN = new RegExp(String.raw`[ \t]*\([ \t]*${REF}(?:[ \t]*(?:[,/;]|and)[ \t]*${REF})*[ \t]*\)`, "g");
// ", AA-123" or "; AA-123 C2" at the end of a parenthesis or a clause.
const TAIL = /[ \t]*[,;][ \t]*AA-[0-9]+(?:\/[0-9]+)?(?:[ \t]+[A-Z][A-Za-z0-9.]*)*(?=[)\].,;\n]|$)/g;
// Anything left: the bare reference.
const BARE = /(?<![A-Za-z0-9])AA-[0-9]+(?:\/[0-9]+)?/g;

export function stripIssueIds(text: string): string {
  if (!text || !text.includes("AA-")) return text;
  return text
    .replace(LEAD, "$1")
    .replace(PAREN, "")
    .replace(TAIL, "")
    .replace(BARE, "")
    .replace(/\([ \t]*\)/g, "")
    .replace(/[ \t]{2,}/g, " ")
    .replace(/[ \t]+([,.;:)])/g, "$1")
    .trim();
}

const TEXT_KEYS = new Set(["description", "summary", "title"]);

/** A copy of a JSON document with stripIssueIds() applied to every description / summary / title string. */
export function stripIssueIdsDeep<T>(value: T): T {
  if (Array.isArray(value)) return value.map((v) => stripIssueIdsDeep(v)) as T;
  if (value && typeof value === "object") {
    return Object.fromEntries(Object.entries(value as Record<string, unknown>).map(([k, v]) => [
      k,
      TEXT_KEYS.has(k) && typeof v === "string" ? stripIssueIds(v) : stripIssueIdsDeep(v),
    ])) as T;
  }
  return value;
}
