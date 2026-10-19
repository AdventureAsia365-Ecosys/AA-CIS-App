# AA-103 S1 Rewrite Page + Country Resolution Fix

## Decisions

1. **country_resolver lives in `shared/`** — not `api/shared/` (which doesn't exist). The `shared/` package at root is already imported by ingestion services and the API layer. No new package needed.

2. **`resolve_country()` replaces both raw normalization AND filename fallback** — one call handles both layers. Old `detect_country_from_filename()` kept in place but no longer called by the parser (preserved for any callers outside this path).

3. **S1 page at `/admin/s1-rewrite/`** — new file, separate from existing `/admin/pipeline/s1/` (which uses ACP `/acp/s1/*` endpoints). The existing page is kept intact; sidebar updated to point to new path.

4. **POST /admin/run-tour requires batch_id** — tours from `/admin/tours-ready` include `batch_id`. Hardcode `tenant_id = "00000000-0000-0000-0000-000000000001"` in the frontend call.

5. **Max 3 concurrent rewrites** — implemented with chunk(3) + Promise.all per chunk, not a semaphore queue. Simpler and achieves the same constraint.

6. **No PATCH /admin/tours/{tour_id}/country route conflict** — safe: `/tours-ready` is a GET, new PATCH is `/tours/{tour_id}/country`. Different HTTP verb and no path collision.

## Changed

- `services/ingestion/excel_parser.py`: country now goes through `resolve_country()` always, not just when Excel cell is empty. Raw values like "JAPAN", "SRI LANKA", "KOR" now normalize to canonical names.
- `frontend/app/admin/_components/AdminSidebar.tsx`: S1 Rewrite href changed from `/admin/pipeline/s1` to `/admin/s1-rewrite`.
- `api/routers/admin_pipeline.py`: added `PATCH /admin/tours/{tour_id}/country` for manual correction.

## Tradeoffs

- Keeping old `detect_country_from_filename()` in excel_parser even though it's no longer called — avoids risk of breaking any test that imports it directly.
- S1 page does NOT reuse the existing `/admin/pipeline/s1` page. That page uses ACP multi-run SSE flow which is more complex than needed for admin direct rewrites. Clean separation.

## Should Know

- The `/admin/run-tour` endpoint auto-upgrades to Sonnet if Haiku score < `AUTO_UPGRADE_THRESHOLD` env var (default 8.5). The frontend shows this transparently as "Done" — no separate UX needed.
- Tours with `pipeline_status='ingested'` are shown. After a successful rewrite, `pipeline_status` does NOT auto-update in raw_tours (it stays 'ingested'). The "Done" badge is purely frontend state after the POST returns 200.
- Filter dropdowns are populated from the fetched tour data (unique countries, unique filenames), not from separate API calls.
