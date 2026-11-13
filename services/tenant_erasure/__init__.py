"""AA-1093 lane L2 — the tenant offboarding erasure saga (CIS orchestrates CIS, AA-Booking, TripPlanner and the CRM).

saga.py   pure rules: the per-app schedule, the plan hashes and the combined fingerprint, the phase checks
policy.py the knobs (windows, approval validity, the default-off apply switch)
store.py  shared.tenant_erasure_request / tenant_erasure_step (CIS 408) and the feed event (CIS 409)
reports.py the Booking / TripPlanner report views (their own migrations), read as JSON rows
The job kind is services/jobs/tenant_erasure_saga_job.py; the routes are api/routers/admin_tenant_erasure.py (staff)
and api/routers/tenant_erasure.py (the CRM, M2M).
"""
