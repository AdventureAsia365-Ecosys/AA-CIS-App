# AA-1048 W2 — claim-label import path and held-out size (lane l3-w2-import, S273)

Follows `AA-1048-ground-truth.md` (S272: CIS 388, `services/ground_truth/`, `scripts/aa1048/`). The first panel round
(150 claims x 3 lenses = 450 labels, 83 agreed) could not reach the Dev DB, and the CL-1 export had 24 held-out
claims of 1,602.

## Decisions

- **Import through a staff API route, not the CLI on ECS and not a DB Migrate run-script.**
  `POST /admin/writing-memory/claim-labels/import` runs the same `panel.parse_judgements` / `panel.consolidate` /
  `store.insert_rows` as `cli import`. Why:
  - `scripts/` is dev-only by rule (`tests/unit/test_aa725_dockerfile_copy.py` `_NOT_IN_IMAGE`), so baking the CLI
    into the image breaks a repo convention for one tool.
  - Streaming data into an ECS exec is what the pilot PII guard refuses (a data bundle on S3, decoded text on an
    exec command line). Working around it would weaken a privacy guard.
  - A DB Migrate run-script logs in public, and its input lives in the public Infra repo. Real claim text cannot go
    there.
  - Existing pattern: runbooks already call staff routes with the admin secret over HTTPS
    (`docs/ops/runbooks/embedding-re-embed.md`, `llm-anthropic-api-key.md` in the root repo). The API pool is
    `cis_app`, which is the role the CLI was written for.
- **The rest of the DB steps are routes too**: report (counts, agreement, contradictions, manifest), enqueue,
  pull-resolutions, finals (claim text; a Writing Lab text read, logged for ai_ops). Without them the import alone
  would leave W2 blocked one step later.
- **Idempotent import**: the key is (claim_hash, labeller_kind, lens), the DB key of CIS 388 with the set version.
  - A judgement already stored with the same class and label is `unchanged`.
  - One stored with a different class or label is a 409: a relabel is a new set version (append-only, as S272).
  - New panel rows in a version that already has panel rows are refused, as `store.insert_rows` does.
  - Tech-lead rows can be added later, at most one per claim.
  - Each applied run writes one `acp_shared.audit_log` row (`ground_truth.claim_labels_import`, counts only). The
    staff request audit middleware logs the call as well.
- **A slim payload, checked again by the server**: the CLI `payload` command keeps only the keys the consolidation
  reads (`importer.EXPORT_KEYS`). There is no source text, field text, context or evidence, so 150 claims is 324 KB.
  The server recomputes every claim hash from its content sha, field and claim text. A line edited after the
  export, or a hand-typed hash, is a 422 and nothing is written. Limits are 5,000 claims and 15,000 judgements per
  call.
- **A masked piece id is dropped, not imported**: the AA-729 fixture scrub had turned part of one T9 piece id into
  "[phone removed]", and the DB column is a uuid. `importer.clean_refs` drops a non-uuid piece / version id when the
  sample id still names the object. The split reads tour and sample first, so the line stays on the same side. The
  payload reports `refs_dropped` (4 lines of round 1).
- **Held-out size: choose what to label, keep the split.** The split is frozen per tour, and changing the seed or the
  share would move labelled claims between sides. Instead:
  - One set per writer surface (review fix, S273): AA-1048 Scope names three sets, `CL-1` (T9), `L2` (S1) and
    `t2@2` (T2), and CIS 388 uses those ids. `claims.set_writers` maps the families (`CL-<n>` / `L<n>` /
    `t2@<n>`) to their writers. `export-fixture` without `--writers` exports the set's own surface. A writer of
    another surface is refused by `export-fixture`, `cli import` and the payload consolidation (so the API too).
    A set id outside the families may hold any one surface, never two. An earlier draft exported every writer into
    `CL-1` and cited "1,289 of 6,173 held-out" as the fix; 1,092 of those were a1 and 173 t2, so T9 still had 24.
    The T9 gap closes only with more held-out T9 objects (the split-plan picks or new pieces).
  - `export-fixture --split held_out` keeps the held-out lines only; `split-plan` takes `--export` once per set.
  - `split_plan` lists the existing objects whose tour falls on the held-out side, so the next labelling round
    targets them.
- **Target = the R1 bar on one writer surface**: r1.measure takes precision over every fire on a writer surface.
  At round 1's pooled violation rate (9 of 150 = 0.06) and an observed precision of 0.92, that needs 47 held-out
  fires, so 784 held-out claims per writer (`split_plan.target_held_out_claims`). Both numbers are query / CLI
  parameters.
  - The bar is per class (n_pos >= 20 of that class's violations), so a class whose own violation rate is under the
    pooled one needs more. The plan says what the pooled rate allows and nothing more.

## Changed

- `services/ground_truth/importer.py` (new): payload, line checks, consolidation of a payload, idempotency plan,
  `import_labels`, aggregate `counts`, `finals_rows` (the CLI's finals shape, now one function for both).
- `services/ground_truth/split_plan.py` (new): export stats, target, plan (round robin over held-out tours, cap 6
  per tour, already-exported and tourless objects never picked), cost of the missing objects, the
  `select_samples --ids` list, and the live candidate queries (the `_T9_SQL` / `_T2_SQL` filters of
  `scripts/aa729/select_samples.py`, ids and tour only).
- `services/ground_truth/store.py`: `existing_judgements` (keys and labels, no text).
- `api/routers/admin_claim_labels.py` (new) + `api/main.py` + POLICY in `api/core/staff_authz.py`: writes need
  ai.admin; report and split-plan need lab.read (no text); finals is `_LAB_TEXT_READ` + `LAB_TEXT_LOGGED`, under
  `/admin/writing-memory/` as `test_aa1035_s2_lab_read` requires.
- `scripts/aa1048/cli.py`: `payload`, `split-plan` (repeatable `--export`), `export-fixture --split` and the set's
  own surface by default; `_finals_to_jsonl` calls `importer.finals_rows`.
- `services/ground_truth/claims.py`: `SURFACES`, `set_surface`, `set_writers`, `set_writer_problems` (one surface per
  label set).
- Snapshots: `docs/api/openapi.json` (FastAPI 0.161.1 / Pydantic 2.14.1, the pins),
  `tests/unit/snapshots/aa1035_role_route_matrix.json`, GD3 samples.
- `docs/calibration/aa1048-claim-panel-brief.md`: orchestrator steps through the API.

## Measured ($0, local, fixture 1.3.0 + round 1)

| Writer | Objects | Tours | Held-out tours | Held-out claims | Target | Gap |
|---|---|---|---|---|---|---|
| a1 | 50 | 50 | 16 | 1,092 | 784 | 0 |
| t2 | 20 | 13 | 5 | 173 | 784 | 611 (about 11 versions) |
| t9_blog | 20 | 3 | 0 | 0 | 784 | 784 (about 14 pieces) |
| t9_social | 34 | 7 | 2 | 24 | 784 | 760 (about 64 posts) |

Why only 24: the 54 T9 pieces of the fixture sit on 9 tours, and the 20 blogs on only 3. A 30% split of 9 groups put
2 tours (3 social posts) on the held-out side. The fixture picked T9 pieces spread over tenants and destinations, not
tours.

Round 1 payload, DRY (local consolidation): 450 rows, by label ok 408 / violation 27 / unsure 15, states agreed 66 /
audit 17 / disputed 67, split held_out 72 rows (24 claims).

## Tradeoffs

- One API call per round carries claim text over HTTPS to the API. The DB already stores that text, and the call is
  admin-only, audit-logged and never in a log line. The alternative paths either weaken a guard or publish the text.
- If nothing more exists on Dev, the worst case cost of the gap is 14 blogs x 0.11 + 64 posts x 0.11 + 11 T2
  versions x 0.19 = about 10.67 USD. The split plan prices the real gap from live candidates first. Existing held-out
  pieces cost $0 to freeze.
- The live plan reads only the T9 and T2 candidates. S1 already has 1,092 held-out claims from the fixture.
- 784 held-out claims per writer x 4 writers is about 3,100 claims for the panel ($0). At round 1's dispute rate
  (67 of 150) that is about 1,400 disputes, far over D11's 2-3 hours of tech-lead time. Order the writers: T9 first
  (D7, the instrument's first surface), and only claims of held-out objects.

## Should know

- The route needs no migration (CIS 388 is live). It ships as a plain code PR.
- Before the first write, call `curl .../claim-labels/import` with the dry payload. The answer has the same counts as
  `cli payload` (above). Then send the `--apply` payload once. A second send answers `unchanged`.
- `select_samples --ids` (an addendum freeze) still runs inside the container the old way (scripts shipped as text
  files: every `scripts/aa729/*.py` passes `pilot_pii_guard.py --check`). The split plan only says which ids to
  freeze.
- The fixture scrub bug: `scripts/aa729/common.scrub` masks a uuid fragment that looks like a phone number. This is
  a follow-up for the next fixture version.
