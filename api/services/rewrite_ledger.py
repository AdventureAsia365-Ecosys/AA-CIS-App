"""AA-1127 (S1, S283 UI audit) — ONE ledger for tour rewrites.

The month's rewrite count is ``shared.tenant_rewrite_usage`` for the UTC month key below, incremented only by
``api/routers/v1_tours._check_and_consume_rewrite_quota`` (called by ``trigger_rewrite``). ``GET /v1/quota`` reads it,
and since CIS 448 ``shared.v_tenant_monthly_usage.tours_rewritten`` (``GET /v1/billing``, the admin tenant list,
Tenant 360, the billed overage) reads the same row with the same month key. A tenant's own inline edit
(``edit_source 'tenant_edit'``) is not a rewrite: the dashboard's per-day rewrite line counts only the versions a
rewrite made (``REWRITE_EDIT_SOURCES``).
"""
from __future__ import annotations

import datetime as _dt

REWRITE_EDIT_SOURCES = ("ai_generated", "re_rewrite")
REWRITE_EDIT_SOURCES_SQL = "(" + ", ".join(f"'{s}'" for s in REWRITE_EDIT_SOURCES) + ")"


def current_year_month_and_reset() -> tuple:
    """(year_month 'YYYY-MM' string, first-of-next-month date) in UTC: the counter's key (CIS 448 reads
    to_char(now() AT TIME ZONE 'UTC', 'YYYY-MM'))."""
    now_utc = _dt.datetime.now(_dt.timezone.utc)
    next_month = (now_utc.replace(day=1) + _dt.timedelta(days=32)).replace(day=1)
    return now_utc.strftime("%Y-%m"), next_month
