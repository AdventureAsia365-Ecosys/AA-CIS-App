# AA-1040 — machine-readable AI mark of Content studio pieces (CIS backend)

Lane l6-ai-marking, S266. Kit part (notice variants, badge): `AA-1040.md` (kit PR). EU AI Act Art. 50(2): AI output
carries a mark software can read. Deadline 02/12/2026 (root ADR 0019 d19).

## Decisions

- **No migration.** Every `acp_shared.content_piece` row is written by the T9 writer, so "AI-generated" is true by
  construction. The extra facts (writer model, when, whether a person edited it) go in a JSON key of the existing
  jsonb `writer_input`: `writer_input -> 'ai_provenance'`. A new column would be a contract file (ALTER of an
  existing table, ADR 0017 / MIG-4: two PRs and a Migration Apply) for data that fits an existing column.
  Tenant reads select only that key (`writer_input -> 'ai_provenance' AS ai_provenance`), never the admin record.
- **Model = the ledger's writer row**, read at finalize: the newest `t9_write` row of the request since the piece row
  was created, status `ok`/`truncated`, not shadow (`ai_marking.WRITER_MODEL_SQL`, through
  `idx_llm_call_log_stage_created`). Plumbing `resp.model_used` out of `write_content()` would have changed a
  4-tuple returned to ~20 call sites and tests. The ledger row is written synchronously before finalize (the writer
  runs in `asyncio.to_thread`, so `record_call_sync` takes its `asyncio.run` path). Best-effort: an unreadable
  ledger gives `model: null`, never a failed finalize. Only `approved`/`held` pieces are stamped (a failed piece has
  no content).
- **Human edit:** `_PIECE_EDIT_SQL` sets `ai_provenance.human_edited_at = now()` (jsonb_set, keeps every other key).
  A piece without `writer_input` (older than AA-769) keeps NULL so the admin "has writer input" flag stays true to
  its meaning. The mark is still `ai_generated: true` after an edit (the text is AI-assisted); `human_edited` says a
  person changed it.
- **HTML:** the fragment (WordPress publish, tenant embed) is `<article data-ai-generated="true" data-ai-generator
  data-ai-model data-ai-generated-at data-ai-human-edited>` + the `<meta name="ai-*">` tags on ONE line AFTER
  `</article>`. Why after and on one line: WordPress runs `wpautop` on classic content; `<meta>` is not a block tag,
  so tags inside the text or separated by blank lines would be wrapped in `<p>` and could add visible gaps; after the
  article they form at most one empty-looking paragraph at the very end. `data-*` attributes survive `wp_kses` for
  every role (WP 5.0+); `<meta>` survives only for users with `unfiltered_html` (administrators, editors on a single
  site), so the attributes are the guaranteed mark and the meta tags the best effort. The downloaded document puts
  the meta tags in its `<head>`.
- **Meta names:** `ai-generated`, `ai-generator`, `ai-model`, `ai-generated-at`, `ai-human-edited`, plus
  `digital-source-type` = the IPTC NewsCodes URI `trainedAlgorithmicMedia`. No single web standard marks AI text
  yet; the names are documented in the root AI Use Register.
- **Downloads** also send `X-AI-Generated` / `X-AI-Generator` / `X-AI-Model` headers (a text export has no markup).
- **Facebook:** the Graph API Page feed has no AI label for a text post (Meta's "AI info" label is detected from
  image/video metadata or set in the app). The post text is not changed; the mark stays in the piece and its publish
  record. Per-platform notes in the register.
- Default of `render_content_text_to_fragment/document` without `ai` stays byte for byte (AA-613 tests); every
  caller with a real piece passes it (export route, publish dispatch).

## Changed

- New `services/acp_content_writing/ai_marking.py`.
- `service.py`: `_finalize_piece` stamps provenance; `_tenant_safe_piece` adds `ai`; `_FETCH_PIECE_QUERY`,
  `_LATEST_PIECE_FOR_OPTION_QUERY`, `_PIECE_EDIT_SQL` select the key; the edit stamps `human_edited_at`.
- `export.py`: `ai=` on fragment / document. `dispatch.py`: `piece_ai_mark`, the blog post carries the mark.
- `v1_publish._PUBLISH_PIECE_SQL`: `cp.created_at`, the provenance key. `v1_content_writing.export_piece`: marked
  HTML and `X-AI-*` headers.
- Tests: `tests/unit/test_aa1040_ai_marking.py`, `tests/integration/test_aa1040_ai_marking_sql.py`;
  `test_aa916_capture_edits.TENANT_SAFE_KEYS` gains `ai`.

## Tradeoffs

- Ledger lookup vs plumbing the model through the writer: one indexed query per finished piece against a
  ~20-call-site signature change. If the final text was reverted to an earlier attempt (AA-528 guard), the model is
  the newest attempt's; attempts of one piece use the same route, so this only differs after a fallback hop.
- Meta tags in the post body are not valid HTML5 placement (`name` metas belong in `<head>`); browsers and parsers
  keep them, and getting them into the WordPress `<head>` would need a plugin on the tenant's site.

## Should know

- No migration, no new grant (`cis_app` already reads `shared.llm_call_log`; the tenant pool never reads it).
- Live check after deploy: one publish to the WordPress test site, then fetch the post URL and look for
  `data-ai-generated="true"` (always) and `<meta name="ai-generated"` (when the test site user is an administrator).
  Pieces finalized before this deploy have no stored model: their mark has `ai_generated` and `generated_at` only.
