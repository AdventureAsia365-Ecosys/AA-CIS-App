# AA-824 — application_name per pool (CIS)

## Decisions
- Pools carry `server_settings={"application_name": ...}`: `cis-api` (main pool), `cis-tenant` (aa_app_user canary pool), `cis-worker` (job runner), `cis-job` (per-job pools in grounding, export, Jev gates).
- One-off `asyncpg.connect()` calls (admin_pipeline, jobs_repo, ingestion) are left unnamed: short-lived, and the role already identifies CIS.

## Should know
- Data standards C2.2 / AA-824 Done-when: `pg_stat_activity` shows one application_name per pool. Booking (`booking-api`) and TripPlanner (Lambda function name) set theirs in their own repos.
