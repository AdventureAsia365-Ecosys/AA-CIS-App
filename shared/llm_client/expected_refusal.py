"""AA-900 (S271 lane l11) — an "expected refusal (no access yet)": a ledger row the admin pages, the route alerts and
the root status tool count apart from errors.

Tech lead ask (S271): no error noise for refusals the routing already handles. On Dev, AWS has not granted seven models
on the acc3 Bedrock account yet; each half-open probe of one of them by real traffic and each daily connection probe
writes a refused ``shared.llm_call_log`` row (the ledger rule stays: every real provider call has a row, refused
included), and the route moves on to the next model as designed. Counted as errors, those rows hid the real problems in
every 24 h error number.

The rule (one definition for every reader; ``EXPECTED_SQL`` and ``is_expected`` say the same thing):

- the row is ``status = 'refused'`` with ``error_class = 'access_denied'``;
- its deployment (``shared.llm_model_deployment``, keyed by the row's ``connection_key`` and catalog ``model``) has
  ``access_state = 'access_denied'``: the last probe of that deployment (the daily ``llm_connection_probe`` or the
  admin's Test connection, CIS 367) already found no access, so the refusal is known;
- and its connection (``shared.llm_provider_connection``) has ``access_state = 'ok'``: the account itself answers. An
  account-wide refusal (Error 002 on every model, a lapsed credit, a key that cannot be read) is never expected: the
  connection's access_state is then not 'ok' and every refused row counts as an error again.

Why the alerts still fire when a working model starts refusing: such a deployment's access_state is 'ok' (its last
probe answered) until the next probe sees the refusal, so its refused rows count as errors in the meantime. The probe
writing 'access_denied' is the moment the refusal becomes known; a grant flips the probe back to 'ok'. Rows are never
changed or dropped; only how they are counted.

A row without ``connection_key`` (TripPlanner rows, rows before ledger v2) never matches: it counts as before.
"""
from __future__ import annotations

from typing import Optional

from .errors import ACCESS_DENIED

REFUSED = "refused"
KNOWN_DENIED_STATE = "access_denied"     # shared.llm_model_deployment.access_state of a deployment with no access
ACCOUNT_OK_STATE = "ok"                  # shared.llm_provider_connection.access_state of an account that answers
LABEL = "expected refusal (no access yet)"


def expected_sql(table: str = "shared.llm_call_log") -> str:
    """The SQL condition on one ledger row of ``table`` (the outer table name or alias; qualified, so it can sit in
    a select list next to unqualified columns). The deployment / connection aliases are private to the subquery."""
    t = table
    return (f"({t}.status = '{REFUSED}' AND {t}.error_class = '{ACCESS_DENIED}' AND EXISTS ("
            "SELECT 1 FROM shared.llm_model_deployment xr_d "
            "JOIN shared.llm_provider_connection xr_c ON xr_c.connection_key = xr_d.connection_key "
            f"WHERE xr_d.connection_key = {t}.connection_key AND xr_d.model_key = {t}.model "
            f"AND xr_d.access_state = '{KNOWN_DENIED_STATE}' AND xr_c.access_state = '{ACCOUNT_OK_STATE}'))")


EXPECTED_SQL = expected_sql()


def is_expected(status: Optional[str], error_class: Optional[str], deployment_access_state: Optional[str],
                connection_access_state: Optional[str]) -> bool:
    """``EXPECTED_SQL`` in Python (a row already joined to its deployment and connection states). Pure."""
    return (status == REFUSED and error_class == ACCESS_DENIED
            and deployment_access_state == KNOWN_DENIED_STATE and connection_access_state == ACCOUNT_OK_STATE)


def known_denied_model(error_class: Optional[str], deployment_access_state: Optional[str],
                       connection_access_state: Optional[str]) -> bool:
    """A breaker row (shared.llm_provider_health) of class access_denied on a deployment whose last probe found no
    access, on an account that answers: the availability card shows it as "no access yet", not as an open circuit
    of a problem. Pure."""
    return is_expected(REFUSED, error_class, deployment_access_state, connection_access_state)


__all__ = ["ACCOUNT_OK_STATE", "EXPECTED_SQL", "KNOWN_DENIED_STATE", "LABEL", "expected_sql", "is_expected",
           "known_denied_model"]
