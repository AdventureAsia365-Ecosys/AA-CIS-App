# AA-1132 (reader lane) — page history and "needs review" in the admin docs reader

Done-when item 3 of the docs sync engine: the reader (`/admin/docs`) shows each page's change history and a
"needs review" flag when a mapped code dependency changed. The data comes from the root docs bundle (root
`tools/docs/build_docs_reader.py` rule 13 `history`, rule 14 the generated "What changed in docs" page; the
`needs_review` / `needs_review_reason` fields come from a parallel root lane).

## Decisions

- **The API had to change.** `api/services/docs_reader.py` copies only the fields in `_ENTRY_FIELDS`, so `history`,
  `needs_review` and `needs_review_reason` were stripped. They are now listed and shape-checked in a new module,
  `api/services/docs_page_history.py` (docs_reader.py is at 781 lines, under the 800-line file ratchet; a separate
  module keeps it there and keeps `_clean_v2` under the complexity limit).
- **Shape rules (API).** A history item needs an ISO date and a string subject; `sha` (7-40 hex) and `pr` (1-999999)
  are optional and become null otherwise, because a redacted line ("A change to N pages with restricted access") has
  neither. At most 10 items (the builder's HISTORY_MAX). Subjects are whitespace-collapsed, cut at 160 characters
  (the builder's SUBJECT_MAX) and 12-digit runs are masked again, like `known_issue`. `needs_review` passes only as a
  real boolean; the reason only on a flagged page (cut at 300, masked). Absent fields read as `[]` / null.
- **Unknown manifest fields still do not pass through.** The reader keeps its allow-list (a page entry carries no
  surprise data to the browser); new bundle fields are added on purpose, as here.
- **Links.** Commit and PR links point at the bundle's public repo (`source.github`), the same rule as the existing
  Source / History links: no links while the bundle names no public repo (private repo or an older bundle). The
  short sha still shows as text then.
- **"Last changed".** The facts line now reads "Last changed <date>" (was "Updated"), from `last_changed`, else the
  newest history item. Same test id (`docs-updated`).
- **"What changed in docs".** A link above the topic groups opens the generated page when the manifest lists it (found
  by path `docs/generated/docs-changes.md`, else by id `CAT-00-docs-changes`); the page also stays in its own group.

## Changed

- `api/services/docs_page_history.py` (new), `api/services/docs_reader.py` (fields + wiring + docstring).
- `frontend/lib/docsReader.ts`: types `DocHistoryItem`, entry fields, helpers `pageHistory`, `lastChangedDate`,
  `historyLinks`, `needsReview`, `docsChangesPage`.
- `frontend/app/admin/docs/DocHistory.tsx` (new: `DocHistory`, `NeedsReviewBadge`), `DocHeader.tsx`, `DocsList.tsx`,
  `page.tsx` (passes `source.github`), `messages.ts` (EN + VI).
- Tests: `tests/unit/test_aa1132_docs_page_history.py`, `frontend/lib/docsHistory.test.mjs`.

## Tradeoffs

- The 10 / 160 / 300 caps are shape checks that mirror the builder's output sizes (marked `technical-constant`), not
  policy a person tunes: they are not admin settings. If they ever need to change without a PR, the root builder has
  no settings store either (the root lane notes the same follow-up).
- The API does not strip issue keys from subjects again (the builder does); it only re-masks account IDs.
- The history list is collapsed by default to keep the header compact; a page without history shows nothing.

## Should know

- The Badge kit component takes no `data-*` props, so the test id and the hint (`title`) sit on a wrapper span.
- No migration, no LLM call, no new route: the manifest and page routes return the new fields as part of each entry.
- Before the root bundle carries `needs_review`, no badge shows; before Docs sync runs with full history, the history
  list stays hidden and "Last changed" falls back to nothing.
