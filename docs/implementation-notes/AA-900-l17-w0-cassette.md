# AA-900 — S271 lane l17: W0 cassette recorder (record-only)

Root ADR 0020 "Gateway amendments" d8 ("A cassette stores the raw reply, request and prompt shas for replay") and the
S271 writing-quality audit (`data/design-review.json` arch.roadmap[0] item "Cassette recorder", arch.shared_components
"Cassette recorder (gateway level)").

Two PRs, MIG-4 (the migration is contract):

1. Migration-only: CIS 385 `shared.llm_cassette` + its integration test, the X2 RLS allowlist entry and the erasure
   inventory entry (both CI guards fail on the replayed table without them).
2. Code: `shared/llm_client/cassette.py`, one call in `LLMClient._attempt`, the test switches, unit + integration
   tests, CONTEXT.md.

## Decisions

- **Hook point = `LLMClient._attempt`.** It is the one place every provider attempt passes (catalog Converse, the
  Anthropic API, OpenAI-format, the legacy tiers, shadows, the admin probe, the gateway's truncation retry), and the
  request it sees is the one SENT (the token floor already raised `max_tokens`, `token_floor.apply_floor`). Every role
  is covered, not only writers. Jev (`decide`) and embeddings do not pass there and are not chat replies: out of scope.
- **Only returned replies.** A failed attempt has no reply. A billed reply the gateway refuses as unusable (empty,
  invalid JSON) raises inside `call()`, so its text never reaches `_attempt`: not recorded in W0 (follow-up below).
- **Ledger link through (route_trace, attempt_no), no `call_log_id` column.** The ledger row is written later by the
  call site (or by the gateway for failures) and its insert returns no id; changing the ledger statements would touch
  the AA-729 eval relabel (`scripts/aa729/evalpatch.py` pins the column lists) and the shared core's ledger shape. The
  answered ledger row already carries `quality_signal.route_trace` + `attempt_no` (billed_fields), so
  `get_by_call_log_id` resolves the id with two index lookups (ledger PK, cassette `(route_trace, attempt_no)`). The
  truncation retry reports the cut reply's attempt number (S271 `retry_of`): two cassette rows share the key; the one
  whose provider request id, then whose `is_truncation_retry` mark, matches the ledger row wins.
- **Owner links.** Job and tenant come from call_log's bound context (`bind_job`, `bind_tenant`), the eval run from
  `bind_eval_stage`; content piece, tour and eval item from a new `cassette.bind_owner(...)` that callers may bind.
  No call site binds it yet (W0 is the substrate; the R2 runner and T2 / T9 jobs are being changed by parallel
  lanes); `get_by_call_log_id` also returns the ledger row's own `content_piece_id` / `tour_id` / `job_id`.
- **The eval relabel does not apply to the cassette.** The ledger moves eval rows to the eval stage with tenant NULL
  (cost attribution). The cassette is not a cost ledger: it keeps the real stage and the tenant (the erasure key) and
  sets `eval_run`.
- **Request sha** = sha256 of the canonical JSON (sorted keys, no spaces, UTF-8) of
  `{"v": 1, "messages": [system, *few_shots, user], "params": {model, max_tokens, temperature, seed, json_schema,
  effort}}`. The model key is in it (a replay is per model); `effort` is the one the adapter sent (resp.effort).
  `v` lets a later layout change never collide with old shas.
- **Prompt text never stored** (DM-6.4 / DM-10.4): `prompt_sha256` = the ledger's `prompt_hash` (system prompt),
  plus `prompt_version`. The user prompt (tour data) is in the request sha only.
- **Reply cap 256 KiB of UTF-8** (`octet_length` CHECK), cut on a character boundary, `reply_truncated`; sha and size
  are of the whole reply. A stage of data class `pii` stores no text (`reply_withheld = 'pii_stage'`), sha only.
- **PII class `pii:free_text`** on `reply_text` (DM-6.1): a reply may hold tenant content.
- **RLS off, like `llm_call_log`** (CIS-only gateway store, tenant_id = attribution + erasure key): X2 allowlist rule a
  entry, so the file is contract (the expand classify refuses a new tenant_id table without RLS). No FK anywhere.
- **Grants: cis_app SELECT, INSERT, DELETE, no UPDATE.** DELETE because the AA-839 tenant erasure (action `delete` in
  `services/erasure/inventory.py`) and a future TTL job run as the runtime role (plan.py refuses an apply without it).
  The file REVOKEs ALL from PUBLIC, cis_app, aa_app_user and catalog_owner before the GRANT (the 321 / 345 / 370
  pattern): Migration Apply creates the table as cis_owner, whose default privileges in `shared` (CIS 245) would
  otherwise give cis_app UPDATE too. CI creates it as the test superuser, so `test_385_narrows_the_dev_default_privileges`
  simulates those grants and re-runs the file (review fix, S271).
- **Best-effort write** on call_log's `_in_background` (a non-daemon thread with its own loop and a short-lived
  connection, the same path as the gateway's failure rows). Never raises; `cassette.stats()` counts recorded /
  written / write_failed / missing_table / build_failed. Before 385 is applied: UndefinedTable sets a 5-minute skip.
- **Switch `LLM_CASSETTE=record|off`, default record** (Dev is the only environment; no Terraform change). Unit tests
  default it off (conftest `_llm_cassette_off`, marker `real_cassette`), integration tests too (env default).

## Changed (vs the lane brief)

- No `call_log_id` column (see the link decision).
- `bind_owner` is provided but not bound at call sites yet.
- The 30-day prune is documented, not built: a new TTL job kind is a `TableSpec` plus a job-kind registration, a
  schedule and a first DRY RUN (DM-7.2), not trivial for W0.

## Tradeoffs

- One background thread + connection per recorded reply. LLM calls take seconds; the write is ~tens of ms off the
  call path. A queue with one long-lived writer would cost less but loses rows at process exit (scripts) unless
  flushed; the proven per-write thread keeps parity with the ledger's own gateway rows.
- Storage: about one row per paid chat call, a few KB each, TOAST-compressed; at Dev volume that is MBs per day.
  The 256 KiB cap bounds a pathological reply.

## Should know

- Number 385: CIS main max was 383 on 06/11 (384 is #1351; 385-386 held by parallel lanes). Renumber to main max + 1
  at merge: `git grep '385_'`, `git grep "'385'"`, `git grep 'CIS 385'`, `git grep '(385'`.
- The Infra copy needs `db/rls_catalogue_rules.py` byte-identical to the CIS file (Migration Copy Guard CODE_COPIES):
  the copy PR carries it next to `db/migrations/cis/385_llm_cassette.sql`.
- Root PR: data standards Part B section 7 retention row for `shared.llm_cassette` (30 days proposed, no rollup, no
  archive) and the DM-10.4 note that the cassette is the one store of completion text (ADR 0020 d8); merged before
  the Infra copy.
- Follow-ups: TTL kind `retention_llm_cassette` (archive off, DRY RUN first); record billed-unusable replies (carry
  the text on `BilledUnusableReply`); bind `bind_owner` in the T2 / T9 / S1 jobs and the R2 eval runner; a replay
  tier that serves `get_by_request_sha` to the eval ($0 replay ladder).
