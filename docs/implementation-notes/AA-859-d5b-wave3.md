# AA-859 — D5b and wave 3 (S247)

Plan: root `docs/audits/2026-10-16-S240-aa859/aa859_plan.md` (PR-D5 "D5b", "Follow-up waves / Wave 3"). Wave 4 is
out of scope. Two commits on `feat/aa859-d5b-wave3`: D5b (no migration), then wave 3 (migration 262 + routes).

## D5b — the T2 detail reads (commit 1)

Flag: `T2_VERSION_DETAIL_READS_FLAG` = `aa859:t2_version_detail_reads_pool`. No migration.

### Decisions
- **Own flag, not `T2_VERSION_READS_FLAG`.** The lane brief said "same flag as W5a reads, so they switch at
  deploy time". PR-D5a's review decided the opposite and pinned it with
  `test_t2_version_reads_flag_is_used_only_by_the_three_d5a_handlers`: W5a's flag is verified and ON, so a route
  added under it moves with no flip of its own, and reverting D5b would revert W5a too. Under S241 D-12 a new
  key that is missing in Redis means ON, so the D5b routes still switch at deploy time with their own flag —
  the brief's intent holds, and the rollback stays per route group.
- **One log event for the three routes**, separate from W5a's: `aa859_t2_version_detail_read route=full|tour|
  export_docx used_tenant_pool=... found=...` (after the block, also on a 404). A separate event name keeps the
  W5a and D5b Logs Insights checks apart.
- **`/full` staff branch reads no flag.** With `X-Admin-Secret` the handler goes straight to the admin pool
  (plan D2, staff are outside the tenant boundary): no Redis read, no tenant-pool acquire, no log line. Only the
  tenant-JWT branch is on the tenant pool. The two branches are now separate code paths; the staff SQL text is
  unchanged.
- **export-docx audit on MAIN after the block** (`# AA-859 MAIN exception`), best-effort as before.
- SQL lifted to module constants (`_TOUR_VIA_VERSION_SQL` + `_TOUR_VIA_VERSION_TENANT_COND`, `_TOUR_RAW_SQL`,
  `_TOUR_SEO_SQL`, `_EXPORT_VERSION_SQL` + `_EXPORT_VERSION_TENANT_COND`) so tests can forget the tenant
  condition; a unit test pins the flag-off text to the pre-D5b inline SQL (whitespace-normalised). `/{id}` and
  the `/full` tenant branch ran the same SQL text before; they share one constant now.
- The local `safe()` helper of `/full` became the module function `_safe_row()` (same body).

### Changed
- `api/core/aa544_tenant_pool.py`: new constant + `ALL_TENANT_POOL_FLAGS`; header list.
- `api/routers/v1_tours.py`: the three handlers (above).
- `tests/unit/test_s241_tenant_flag_keys.py` (12 keys), `tests/unit/test_aa817_tenant_pool_fail_closed.py`
  (inventory size 12).
- New: `tests/unit/test_aa859_t2_version_detail_reads.py` (fail-closed matrix x 3 routes, 404 path, staff branch,
  audit ordering, flag-off SQL, flag ownership), `tests/unit/test_aa859_reentrancy_d5b.py`,
  `tests/integration/test_aa859_t2_version_detail_reads_pool.py` (tenant pool = admin pool for A, forgotten-WHERE
  for B on `/{id}`, `/full`, export; it imports D5a's stand-in DDL and fakes instead of copying them).
- `CONTEXT.md` route table: D5b row; W5a's "still on MAIN" note removed.

### Tradeoffs
- Two branches in `/full` duplicate the raw/seo reads (one per pool) instead of one shared block with an
  `if is_admin` inside: the staff path must not read the flag at all, and a shared block would.

### Should know
- **Same known divergence as W5a** (239 policies): on the tenant pool a master tour shows `raw` / `seo` only when
  those rows carry the AA master tenant id. Pre-flip probe (read-only, as cis_app through ECS exec), both must be
  0, else do not keep the flag ON:
  `SELECT count(*) FROM gold_aa_internal.published_tours pt JOIN silver_aa_internal.raw_tours rt ON rt.tour_id =
  pt.tour_id WHERE pt.tenant_id = '00000000-0000-0000-0000-000000000001'::uuid AND rt.tenant_id IS DISTINCT FROM
  pt.tenant_id;` and the same with `silver_aa_internal.seo_context sc ON sc.tour_id = pt.tour_id`.
- Ships ON (missing key). To ship it dark, `SET aa859:t2_version_detail_reads_pool false` before the deploy (the
  flag tool's ALLOWED set needs the key: `.tmp-session/aa859_wave3_flag.py` carries it).
- Flip-verify: `.tmp-session/aa859_wave3_flip_verify.py t2_version_detail_reads` (not run by this lane).
