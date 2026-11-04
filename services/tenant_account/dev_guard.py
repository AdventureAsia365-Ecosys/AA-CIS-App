"""The Dev guard of the pilot exception (AA-1028 R1 slice 3, root ADR 0019 d16).

On Dev a tenant is synthetic (``kind`` ``demo``, ``is_canary`` true), with one exception: the one pilot agency
(``is_pilot`` + ``pilot_ends_at``, CIS 354: a unique partial index allows one, a CHECK makes it ``kind`` agency), for
at most ``PILOT_MAX_WEEKS`` weeks. So on Dev:

- a new tenant with no ``kind`` is a ``demo`` tenant (``default_kind``): the admin create and the saga keep working
  for test tenants with no extra field;
- a new ``agency`` (a real business) is refused (``dev_real_tenant_refused``) unless it is created as the pilot
  (``pilot_weeks`` 1-8; a second pilot is refused by the saga, ``pilot_taken``);
- elsewhere (``ENVIRONMENT`` not exactly ``dev``: staging, prod, tests) nothing changes: the default is ``agency``
  and no pilot marker is accepted (the exception exists for Dev only).

Rows made before this guard keep their ``kind`` (Dev's older test agencies are fictional data; the guard decides
new tenants only). ``ENVIRONMENT`` is read per call and compared exactly, like ``api/core/e2e_staff.py``.
"""
from __future__ import annotations

import os
from typing import Optional

DEV = "dev"
PILOT_MAX_WEEKS = 8


def on_dev() -> bool:
    return os.environ.get("ENVIRONMENT") == DEV


def default_kind() -> str:
    return "demo" if on_dev() else "agency"


def refusal(kind: str, pilot_weeks: Optional[int]) -> Optional[tuple[str, str]]:
    """(code, message) when a new tenant of ``kind`` (with ``pilot_weeks`` or None) is refused here, else None."""
    if pilot_weeks is not None:
        if not on_dev():
            return ("pilot_dev_only", "the pilot marker exists for the Dev pilot only (root ADR 0019 d16)")
        if kind != "agency":
            return ("pilot_needs_agency", "the pilot is a real agency: kind agency")
        if isinstance(pilot_weeks, bool) or not isinstance(pilot_weeks, int) or not 1 <= pilot_weeks <= PILOT_MAX_WEEKS:
            return ("invalid_pilot_weeks", f"pilot_weeks: 1-{PILOT_MAX_WEEKS}")
        return None
    if on_dev() and kind == "agency":
        return ("dev_real_tenant_refused",
                "Dev holds synthetic tenants only (kind demo); a real agency only as the one pilot (pilot_weeks)")
    return None
