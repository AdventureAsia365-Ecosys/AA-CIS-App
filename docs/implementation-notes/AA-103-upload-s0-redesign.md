# AA-103: Upload S0 Redesign — Implementation Notes

## Decisions

**dry_run semantics changed:**
- Old dry_run=true called `process_file()` which wrote to DB (wrong)
- New dry_run=true: parse-only (download S3 → ExcelParser → dedup check), no DB write
- New dry_run=false: calls process_file() which writes raw_sources + raw_tours, returns count WITHOUT triggering rewrite pipeline

This fixes the bug where the "preview" step was silently writing to DB and the "commit" step returned "duplicate" on the second call.

**Pipeline NOT triggered on dry_run=false:**
Removed the background_tasks / Step Functions trigger from ingest-s3 non-dry-run path. Tours stay at pipeline_status='ingested' until user manually triggers from S1 Rewrite. This was the intended design but was not implemented before.

**Multi-file upload — per-file state:**
Each file tracks its own `FileState` with status enum: pending → uploading → parsing → parsed/blocked-file → committing → done/error. Parallel upload and parse (Promise.all), then parallel commit after user confirmation.

**allDone logic:**
Files with parsed status but 0 ready tours are atomically pre-set to "done" (skipped) when doCommit starts. This ensures allDone becomes true after all commits complete, not stuck on "parsed" files.

## Changed

- `api/routers/admin_pipeline.py`:
  - Removed BackgroundTasks and Depends from imports (unused after changes)
  - `POST /admin/ingest-s3`: dry_run=true no longer calls process_file; instead downloads + parses without DB write. dry_run=false calls process_file but does NOT trigger background tasks or Step Functions.
  - Added `GET /admin/upload-history`: last 20 raw_sources rows, parse_error_count computed server-side
  - Added `GET /admin/tours-ready`: raw_tours WHERE pipeline_status='ingested', LEFT JOIN raw_sources for filename
  - Added `_count_parse_errors()` helper for jsonb parse_errors field

- `frontend/app/admin/upload/page.tsx`:
  - Full redesign of TourContentTab: 3-step single-file → 4-step multi-file
  - Step 3 uses dry_run=true (parse preview), Step 4 uses dry_run=false (commit)
  - Added UploadHistorySection and ToursReadySection components (always visible below wizard)
  - `stripUuidPrefix()`: removes `{uuid}_` prefix from filenames for display
  - `relativeTime()`: human-readable timestamps (no library dependency)
  - Toast component: green/red fixed-position overlay
  - RefreshKey pattern: both sections auto-refresh after commit

## Tradeoffs

**Inline parse in dry_run=true vs extract function:**
Kept the parse logic inline in ingest-s3 rather than extracting a `parse_only()` function. The endpoint is already complex; extracting would require more refactoring and the logic is self-contained.

**No XHR upload progress percentage:**
Using fetch instead of XMLHttpRequest — can't easily track upload progress % per file. Shows per-file status badges (uploading/parsing/done) instead. Acceptable UX tradeoff given upload sizes are typically small XLSX files.

**ExcelParser import style:**
Using `from services.ingestion.excel_parser import ExcelParser as _ExcelParser` inside the function scope (lazy import) to avoid circular import risks and keep the import scoped to dry_run path only.

## Should know

- `raw_tours.source_id` FK references `raw_sources.id` (UUID). The LEFT JOIN in tours-ready handles tours that might have null source_id (edge case from old ingestion before source tracking).
- `raw_sources.parse_errors` is jsonb in DB but asyncpg returns it as Python dict/list. `_count_parse_errors()` handles all shapes.
- The `_run_tour_safe` function and `_pipeline_semaphore` remain in admin_pipeline.py (used by POST /admin/run-tour for individual tour rewrite, not affected by S0 changes).
- process_file still tries to trigger Step Functions if STEP_FUNCTIONS_ARN is set — in dev this env var is unset so it's a no-op. The removal of background tasks means rewrite is no longer auto-triggered at all after S0 commit.
