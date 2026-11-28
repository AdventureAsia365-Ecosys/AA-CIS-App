"""The pilot fence of the CIS staff surface (AA-1054, S297; tech lead decision S295 sec-Q11; root ADR 0006a; CIS 495).

Root ADR 0006a keeps Dev-only E2E staff identities (A10 ... A10h: every address of the E2E mailbox domain,
:data:`api.core.e2e_staff.E2E_MAIL_DOMAIN`) so the agent's E2E suites can sign in without a person. ADR 0019 d16 lets
one real agency (the pilot) work on Dev. On the pilot day the tech lead turns on ONE switch,
``cis.platform.pilot_e2e_fence`` (Platform › Settings, area platform, maker-checker, audited by the settings registry),
and from then on:

- the CIS API refuses every staff session of an E2E identity, whatever its own Dev-only rules allow:
  :func:`check` raises :class:`api.core.staff_identity.StaffAuthError` 403 :data:`E2E_PILOT_FENCE` from
  :func:`api.core.staff_identity.resolve_staff` (the bearer middleware and ``POST /auth/verify-admin`` both go through
  it), before any other E2E rule, logged ``staff_identity_refused reason=e2e_pilot_fence``. The sign-in page shows
  ``/login?error=pilot_fence`` (frontend/lib/loginErrors.ts). The academy (``/learn/v1``, api/routers/learn.py), the
  one staff token path that does not go through ``resolve_staff``, calls :func:`check` too;
- :func:`api.core.e2e_staff.dev_only_e2e_environment` answers False, so every other E2E exception that asks it (the
  staff directory's create / re-enable / sync of an E2E row, ``docs.owner_read``, the E2E Booking grant rule, the Learn
  test hook) is closed too;
- the AA-Booking API reads the same value through ``catalog.v_pilot_fence_v1`` (CIS 496, a read-only contract view:
  no write grant across apps) and refuses the same identities (AA-Booking ``app/auth/pilot_fence.py``).

Who counts as an E2E identity (:func:`is_e2e_identity`): an address whose domain, after trimming and lower-casing, is
the E2E mailbox domain, on the directory row OR on the token, whatever the local part (a new or a look-alike E2E
address is fenced too). Real staff (``@adventure.asia``) never match and never read the switch.

The switch is read from the settings snapshot (the database only when the process holds no snapshot yet, and only
for an E2E identity: :func:`shared.settings.ensure_loaded`). :func:`state` answers ``on``, ``off`` or ``unknown``:

- ``off`` only on a confirmed false: the approved value, or the compiled default once the reader holds a snapshot
  read from the database (no value stored);
- ``unknown`` when the process has a settings pool but could not read the value: no snapshot yet (a new task before
  its first refresh) or since start-up (every refresh failed), or the stored value was refused by the definition. It
  fails closed: :func:`check` refuses an E2E identity 403 :data:`E2E_PILOT_FENCE_UNKNOWN` (the same reason as the
  AA-Booking fence) and :func:`api.core.e2e_staff.dev_only_e2e_environment` answers False;
- ``on`` otherwise (a stored or env value that is not exactly ``False``).

Without a settings pool (unit tests, scripts) or under ``ENVIRONMENT=test`` the compiled default is the only value
there is and counts as confirmed. A key this build does not know (it was built before CIS 495 was compiled into
shared/settings/compiled_defaults.json, which ships in the migration PR merged before this code) reads off, logged
once: nothing turns the fence on before the key exists.

The E2E Cognito users themselves are disabled on the same day by AA-CIS-Infra ``tools/identity/e2e_identity_fence.py``
(root runbook docs/ops/runbooks/pilot-agency-onboarding.md, "Pilot day"); this fence also holds whatever that step
misses or whatever is re-enabled by mistake.
"""
from __future__ import annotations

import os
from typing import Any

import structlog

from api.core import e2e_staff

logger = structlog.get_logger()

# Equals api/core/pilot_fence_settings.PILOT_E2E_FENCE and the CIS 495 seed (the migration PR; tests/unit/
# test_aa1054_pilot_fence.py checks the two once both are on main).
PILOT_E2E_FENCE = "cis.platform.pilot_e2e_fence"
# The 403 detail and log reason (the BFF maps it to /login?error=pilot_fence; the agent's E2E helpers match on it).
E2E_PILOT_FENCE = "e2e_pilot_fence"
# The switch could not be read in this process (no snapshot, or the stored value was refused): refused, fail closed.
# Same detail as AA-Booking app/auth/pilot_fence.E2E_PILOT_FENCE_UNKNOWN.
E2E_PILOT_FENCE_UNKNOWN = "e2e_pilot_fence_unknown"
OFF, ON, UNKNOWN = "off", "on", "unknown"

_logged: set = set()


def reset() -> None:
    """Forget the log throttle (tests)."""
    _logged.clear()


def _log_once(event: str, **kw: Any) -> None:
    mark = (event, tuple(sorted(kw.items())))
    if mark in _logged:
        return
    _logged.add(mark)
    logger.warning(event, key=PILOT_E2E_FENCE, **kw)


def is_e2e_identity(email: Any) -> bool:
    """True when ``email`` is an address of the E2E mailbox domain (after trimming and lower-casing, any local part)."""
    local, sep, domain = e2e_staff.normalise(email).rpartition("@")
    return bool(sep) and domain == e2e_staff.E2E_MAIL_DOMAIN


def _defaults_are_the_truth() -> bool:
    """No settings pool in this process (unit tests, scripts) or ENVIRONMENT=test: the compiled default is the value."""
    from shared import settings

    return not settings.installed() or os.environ.get("ENVIRONMENT") == "test"


def state() -> str:
    """:data:`ON` / :data:`OFF` / :data:`UNKNOWN` (module docstring). Sync: reads the snapshot, never the database."""
    from shared import settings

    try:
        res = settings.resolve(PILOT_E2E_FENCE)
    except KeyError:
        _log_once("pilot_fence_switch_unknown")
        return OFF
    if res.source == "default" and not _defaults_are_the_truth():
        if res.refused:
            _log_once("pilot_fence_switch_unconfirmed", reason="stored_value_refused")
            return UNKNOWN
        if not settings.loaded():
            _log_once("pilot_fence_switch_unconfirmed", reason="no_snapshot")
            return UNKNOWN
    return OFF if res.value is False else ON


def on() -> bool:
    """True unless the switch is confirmed off (an unknown state counts as on: fail closed)."""
    return state() != OFF


def refused(*emails: Any) -> bool:
    """True when one of ``emails`` is an E2E identity and the fence is on or unknown (the switch is read only for an
    E2E identity, so a real staff session never depends on it)."""
    return any(is_e2e_identity(e) for e in emails) and on()


async def check(*, row_email: Any, token_email: Any, admin_id: Any = None, sub: Any = None,
                client_id: Any = None) -> None:
    """Refuse an E2E identity while the fence is on (403 :data:`E2E_PILOT_FENCE`) or unknown (403
    :data:`E2E_PILOT_FENCE_UNKNOWN`). Before deciding, a process with no snapshot yet reads the settings once."""
    if not (is_e2e_identity(row_email) or is_e2e_identity(token_email)):
        return
    from shared import settings

    await settings.ensure_loaded()
    current = state()
    if current == OFF:
        return
    from api.core.staff_identity import StaffAuthError  # noqa: PLC0415 - staff_identity imports this module

    reason = E2E_PILOT_FENCE if current == ON else E2E_PILOT_FENCE_UNKNOWN
    logger.warning("staff_identity_refused", reason=reason, admin_id=admin_id, sub=sub, client_id=client_id)
    raise StaffAuthError(403, reason, reason)
