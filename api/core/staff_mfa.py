"""Administrator MFA check of the CIS staff surface (AA-1111 slice S2; root ADR 0006c; CIS 459).

Tech lead decisions: S283 "admin MFA = TOTP" (an authenticator app code, never SMS), S285 "the WAF geo rule of the
staff hosts goes once admin MFA is enforced". The WORKFORCE pool allows TOTP as optional MFA; a staff member with TOTP
enrolled signs in with password + TOTP and Amazon Cognito writes the factors into the access token's ``amr`` claim
(``["pwd", "otp", "mfa"]``; an e-mail code sign-in gives ``["email_otp"]``). Cognito computes ``amr``; a pre token
trigger cannot change it, and the verifier already checked the token's signature, so the claim is read here from the
same verified token string.

Rule (:func:`check`), after :func:`api.core.staff_identity.resolve_staff`:

- only an **administrator** session is checked: the ``admin`` role (group ``cis-admin``) or a staff member with the
  owner flag (``shared.admin_users.is_owner``). Every other CIS role is left as it is;
- never checked: the capped Preview session (viewer, never an owner) and the Dev-only read-only E2E identities
  (``e2e_read_only``; they cannot exist outside Dev and never write beyond their allow-list, root ADR 0006a A10b/A10d);
- MFA is satisfied when ``amr`` holds ``otp`` (TOTP from an authenticator app, :data:`MFA_AMR`);
- not satisfied and the switch ``cis.platform.admin_mfa_required`` is **on** -> :class:`StaffAuthError` 403
  ``mfa_required`` (the BFF sends the person back to the sign-in page with a message);
- not satisfied and the switch is **off** (the default) -> nothing is refused; the session is logged as
  ``staff_admin_mfa_check outcome=would_refuse`` so the tech lead can see, before turning the switch on, which
  administrator sessions would be refused (and ``outcome=satisfied`` for one that used the code). The log is kept to
  one line per identity and outcome per :data:`LOG_EVERY_S` (the bearer middleware runs on every request).

The switch is read from the settings snapshot (never the database on the request path). When the key is unknown to
this build (the code deployed before CIS 459 was applied) the check reads off, which is today's behaviour.
"""
from __future__ import annotations

import time
from typing import Any

import jwt
import structlog

from api.core import staff_mfa_settings
from api.core.staff_identity import StaffAuthError, StaffIdentity

logger = structlog.get_logger()

MFA_REQUIRED = "mfa_required"
# The Cognito amr value of a TOTP code from an authenticator app (the tech lead chose TOTP, S283).
MFA_AMR = "otp"
ADMIN_ROLES = frozenset({"admin"})
LOG_EVERY_S = 600.0  # technical-constant: log throttle of the check line, not a policy value
_LOG_MAX = 1024  # technical-constant: size of the throttle table

# (iss, sub, outcome) -> monotonic time of the last log line
_logged: dict[tuple[str, str, str], float] = {}


def clear_log_throttle() -> None:
    _logged.clear()


def token_amr(token: Any) -> tuple[str, ...]:
    """The ``amr`` values of an already verified access token (empty when absent or malformed)."""
    if not isinstance(token, str) or not token:
        return ()
    try:
        claims = jwt.decode(token, options={"verify_signature": False})
    except jwt.PyJWTError:
        return ()
    value = claims.get("amr") if isinstance(claims, dict) else None
    if isinstance(value, str):
        value = [value]
    if not isinstance(value, (list, tuple)):
        return ()
    return tuple(v for v in value if isinstance(v, str))


def mfa_used(amr: tuple[str, ...]) -> bool:
    return MFA_AMR in amr


def is_admin_session(identity: StaffIdentity) -> bool:
    """An administrator session the check applies to (never the Preview viewer or a Dev-only E2E identity)."""
    if identity.preview or identity.e2e_read_only:
        return False
    return identity.role in ADMIN_ROLES or identity.is_owner


def required() -> bool:
    """The switch in force (settings snapshot). Unknown key (CIS 459 not applied yet) -> off, logged once."""
    from shared import settings

    try:
        return bool(settings.get(staff_mfa_settings.ADMIN_MFA_REQUIRED))
    except KeyError:
        _log_once("-", "-", "switch_unknown", logger.warning, key=staff_mfa_settings.ADMIN_MFA_REQUIRED)
        return False


def _log_once(iss: str, sub: str, outcome: str, emit: Any, **fields: Any) -> None:
    now = time.monotonic()
    key = (iss, sub, outcome)
    last = _logged.get(key)
    if last is not None and now - last < LOG_EVERY_S:
        return
    if len(_logged) >= _LOG_MAX:
        _logged.clear()
    _logged[key] = now
    emit("staff_admin_mfa_check", outcome=outcome, **fields)


def check(identity: StaffIdentity, token: str) -> None:
    """Refuse an administrator session without an authenticator code while the switch is on (403 mfa_required)."""
    if not is_admin_session(identity):
        return
    amr = token_amr(token)
    fields = {"admin_id": identity.admin_id, "role": identity.role, "is_owner": identity.is_owner,
              "client_id": identity.client_id, "amr": list(amr)}
    if mfa_used(amr):
        _log_once(identity.iss, identity.sub, "satisfied", logger.info, **fields)
        return
    if not required():
        _log_once(identity.iss, identity.sub, "would_refuse", logger.info, **fields)
        return
    _log_once(identity.iss, identity.sub, "refused", logger.warning, **fields)
    raise StaffAuthError(403, MFA_REQUIRED, MFA_REQUIRED)
