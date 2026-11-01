# AA-1026 (slice D5 + T-15, lane gw-embed) — embedding space registry, fallback embedding model, re-embed

Plan: root `docs/audits/2026-11-01-S263-gateway-gap-audit/plan.md` §1.D D5, §4 OPS-13 (corrected: the
`cohere-embed-v4-acc3` fallback is the same space; the weakness is one model and one vendor). Tech lead S263: T-15
approved (registry $0, a ready fallback of another vendor, a runbook; one re-embed test < 1 USD run by the
orchestrator, not this lane).

## Decisions
- **Space = model + dimension (+ version).** `cohere-embed-v4/1536/v1` (active) and `titan-embed-text-v2/1024/v1`
  (standby). The input types (Cohere `search_document` / `search_query`) are attributes of the space, not separate
  spaces: Cohere's query and document types are one space by design.
- **Swap groups.** Columns compared with each other or written by one stage always share a space and swap together:
  `f10_embed` = atom + question + content piece vectors (one writer function, `compute_embeddings`; questions are
  compared with atoms), `tour_place` (TripPlanner searches it), `published_tours` (no writer since 041).
- **Fallback model: Amazon Titan Text Embeddings V2 on acc2, us-west-2.** `aws bedrock list-foundation-models` on
  acc2 (01/11/2026): us-west-1 offers only Cohere Embed v4; us-west-2 offers Titan V2 on demand; the organization's
  region SCP allows us-west-2; the CIS task role allows bedrock:InvokeModel on any resource (migration 238 note).
  Titan answers at most 1024 dimensions, so it is its own space (never mixed into a vector(1536) column). Price from
  the AWS Price List API (USW2 TitanEmbeddingV2 0.00002 USD / 1K tokens). New connection `acc2-usw2` (task role,
  region us-west-2, failure domain `bedrock:acc2:aws_only` = the same account: it adds a vendor, not an account),
  off; catalog row enabled (a priced row; nothing routes to it).
- **Off through the connection, not the deployment (deviation from the lane brief "deployment disabled").** cis_app
  may switch a connection (`PATCH /admin/llm-connections/{key}`, audited) but has no UPDATE on
  `shared.llm_model_deployment`; a disabled deployment would need an owner run-script (Infra PR + DB Migrate) in the
  middle of an incident. `deployment_plan` keeps only enabled deployments on enabled connections, so an enabled
  deployment on a disabled connection is never called; the daily probe skips disabled connections. Same safety,
  one switch.
- **Space-bound embed.** `embed(..., space=)` filters the stage route to the space's models, or uses the space's own
  models when the route names none (a swapped column needs no route change). So switching is data: registry row +
  re-embed + swap.
- **Swap by rename, views rebuilt.** A view binds a column by number, so renaming would keep
  `catalog.v_component_embedding` on the old vectors. `shared.embedding_swap` saves every dependent view (recursive,
  with options, grants, comments, owner), drops them deepest first, renames `col -> col__prev`,
  `col__shadow -> col` (NOT NULL and single-column indexes follow), recreates the views as their owners
  (`SET LOCAL ROLE`, the X1 S22 recipe: cis_owner is not a member of catalog_owner), updates the registry in the
  same transaction.
- **Checkpoint = the data.** The fill reads only rows whose shadow is NULL (and that have a vector in the serving
  column), in key order with a keyset, so a retry, a release on deploy or a re-queue resumes; blank texts are skipped
  without looping.
- **Review fix pass (S263).** Three reviewer findings, all accepted:
  - *Stale shadows.* A writer that rewrites a serving vector after the fill (T9 `UPDATE ... content_embedding`, an
    upsert) left the old shadow, and the swap would promote it. `embedding_prepare_shadow` now installs a trigger
    `<column>__shadow_reset` (BEFORE UPDATE OF the serving column, WHEN it changes and the statement did not set the
    shadow) that empties the row's shadow; the fill and the swap then treat it like a new row. The swap and
    drop_shadow remove it before the renames (a trigger names columns by number), the rollback puts it back. One
    generic trigger function (`jsonb_populate_record(NEW, {shadow: null})`), no per-table function. It watches the
    serving vector only, not the source text: the shadow reproduces what the serving column holds, and a text change
    without a new serving vector leaves the serving vector equally stale (outside a re-embed's scope).
  - *Cost cap per attempt.* The cap now covers the job: the job passes `ctx.cost_usd` (`shared.job.cost_usd`, kept
    across retries and releases) as `already_spent`; result and progress show `spent_usd` (this attempt) and
    `job_spent_usd`. An answer outside the target space raises `ReembedWrongSpace` (a `ReembedRefused`, so
    `NonRetryable`): a retry would only pay for the same page.
  - *complete vs swap.* `complete` is now `remaining_rows == 0`, the swap's own rule; the result lists `blank_keys`
    (up to 50). The runbook's new step 7b purges blank-text rows of the two caches with the Infra script
    (`purge_blank`, refused unless the count equals `EXPECT_BLANK` = the fill's `remaining_blank_rows`); content
    pieces and places have no purge (fix the text, or a tech lead `ALLOW_MISSING` swap).
- **Ledger.** Every embed row of a writer, the re-embed fill and the probe carries `quality_signal.embedding_space`
  (no ledger table change: tech lead T-14, no migration to change tables for this).
- **Migrations split.** 328 is contract after the S263 preflight (the expand classify replay found it changing an existing object; applied by hand). 329 is contract (functions with
  REVOKE, DO guards, routing rows, the catalog recipe view): manual DB Migrate apply after review.

## Changed
- `api/migrations/328_embedding_space_registry.sql`, `329_embedding_reembed_tools_titan_fallback.sql`.
- `shared/llm_client/embedding_space.py` (new), `shared/llm_client/embed.py` (`space=`, Titan request shape,
  `EmbedResponse.space_key`).
- `services/acp_shared/content_embedding.py`, `services/places/llm.py` (writers pass their column's space).
- `services/embeddings/{sources,reembed}.py`, `services/jobs/embedding_reembed_job.py` (kind `embedding_reembed`).
- `api/routers/admin_job_runner.py`: `GET /admin/job-runner/embedding-spaces`, `POST
  /admin/job-runner/embedding-reembed/run` (ai.admin, DRY RUN by default); openapi + route policy snapshot.
- Tests: `tests/unit/test_aa1026_embedding_space.py`, `tests/integration/test_aa1026_migration_329.py`; conftest
  skips 329 in the replay (catalog recipe), like 323.
- TripPlanner (separate PR): search embeds only in the space of `geo.tour_place.embedding`
  (`catalog.v_embedding_space_v1`).

## Tradeoffs
- Rename swap + view rebuild instead of `UPDATE col = shadow`: the update cannot change the dimension and rewrites
  every row; the rename is instant and reversible (`embedding_rollback` while `__prev` is kept).
- The registry is cached 60 s in each process. After a swap, a writer may embed in the old space for up to 60 s;
  the runbook swaps with the job tables idle and redeploys api + worker (statement caches, `SELECT *` sites).
- A rollback after writers added rows is refused (those rows have no old vector) unless `allow_missing`, which leaves
  a NOT NULL column nullable; for the f10 caches the runbook deletes those rows instead (re-embedded on demand).
- Titan is one request per text (cost per token is the same; throughput lower): batch 16, paced 1 s.
- The probe uses stage `adhoc_conn_test` for the ledger row (as the daily probe does).

## Should know
- The reset trigger compares vectors with `IS DISTINCT FROM` (pgvector's `=` operator); CI has no pgvector, so the
  integration test runs it on `real[]`. First live check: the T-15 prepare on Dev, then `drop_shadow`.
- 329 must be reviewed (RISKY: DDL tools, catalog grant, routing rows). Migration numbers 328/329 are placeholders
  above the S263 lanes (325-327): renumber at merge to main max + 1 (`git grep '328_'`, `git grep '329_'`, the
  conftest skip, tests, CONTEXT, root runbook).
- cis_app's default privileges in `shared` grant DML on new tables and EXECUTE on new functions; 329 revokes both
  (SELECT stays).
- Titan is not usable until the runbook switches connection `acc2-usw2` on (one audited admin PATCH) — the T-15
  test does that, then the `probe` mode, then switches it off again.
- `published_tours.content_embedding` has no writer and no text source: it cannot be re-embedded; a swap of its
  group needs `allow_missing` and leaves it empty.
- Lane overlap: lane L1 (embed ledger rows) also edits `shared/llm_client/embed.py`; this lane's hunks are small
  (signature + chain filter + `space_key` + `_titan_call` + `EMBED_VENDORS`), whoever merges second rebases. L2
  (#1218, merged) added the daily embedding probe: its `_embeddable` now reads `embed.EMBED_VENDORS`, so the Titan
  deployment is probed daily while its connection is on (a disabled connection is skipped).
- Rebased onto main after L1 (#1221 A9 embed ledger rows, #1223 A1 stage kill switch): `embed()` keeps both. The
  stage kill switch runs first (refuses before any attempt, its refused row names the route's primary model), then
  the space filter narrows the chain; every attempt still carries the A9 facts and `space_key`; a failed or skipped
  model of the space writes the A9 gateway row (no `embedding_space` in that row — the success row of the caller
  carries it through `ledger_signal`). The space tests capture the background ledger writes (no DB) and the fake
  Titan connection carries `auth_mode` (A9's `_provider` reads it).
