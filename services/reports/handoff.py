"""AA-1003 R2 (TP-03) — inquiry handoff reconciliation, TripPlanner to Booking: RETIRED by the R0 hotfix.

TP-03 checked that every TripPlanner inquiry reached the Booking inbox (hourly check, daily report, three daily KPIs,
an admin bell and an ops alert e-mail). On 2026-11-03 the ecosystem moved to the B2B-only model (root ADR 0019 d8,
d11): send-to-advisor answers 410 b2b_only and bk_inquiry_pull is off, so the hand-off carries nothing and the check
could only raise false gaps (rows sent before the switch-off that Booking never pulled). The catalog marks TP-03
`retired` (no generator); its past report runs and KPI rows stay readable. Tenant leads travel on lead.submitted.v1
into the tenant's CRM; a report on that path is new work (services/reports/b2b_only.py).

What is left here:
- CHECK_KIND: the job kind name stays registered as a no-op (services/jobs/report_ops_jobs.py), so a check queued by
  an api task that started before this release finishes instead of waiting in the queue forever;
- CONTRACT_ERRORS and _json, shared by the readers of the other apps' contract views (money_sources, reliability,
  digest).
"""
from __future__ import annotations

import json

import asyncpg

CHECK_KIND = "inquiry_handoff_check"
# A contract view CIS may no longer read as written: grant revoked, view dropped, a column renamed by its owner.
CONTRACT_ERRORS: tuple[type[BaseException], ...] = (asyncpg.InsufficientPrivilegeError, asyncpg.UndefinedTableError,
                                                    asyncpg.UndefinedColumnError)


def _json(v):
    return json.loads(v) if isinstance(v, str) else (v or {})
