"""AA-1146 D1 (tech lead S285) — the two-person rule of tour content publishing.

Who may publish a tour version (Review Queue approve, promote, publish of a draft version) is a route capability:
``content.publish`` (admin, content_lead), or ``review.act`` (content) while the switch below is off. Whether the person
who wrote a draft may also publish it is the setting ``cis.content.publish_two_person`` (CIS 458, Platform › Settings,
admin only, maker_checker): a policy value, never a code literal or an env var (tech lead S279).

- :func:`refusal` is the pure decision: with the switch on, a caller without ``content.publish`` gets
  ``two_person_required`` and the author of the draft gets ``same_person``; with it off, nothing is refused here (the
  route capability and the re-check gate still apply).
- :func:`switch_on` reads the switch inside the caller's transaction (never the 30 s process cache, the rule of
  ``settings_registry.two_person_on``), failing closed: an unreadable row or a value that is not a boolean counts as
  on. Before CIS 458 is applied the row cannot exist and the definition's default (off) applies.

Same file in the migration PR (CIS 457-458, which seeds the key) and the code PR that wires it.
"""
from __future__ import annotations

import json
from typing import Any, Iterable, Optional

import structlog

logger = structlog.get_logger()

PUBLISH_TWO_PERSON_KEY = "cis.content.publish_two_person"
CONTENT_PUBLISH = "content.publish"     # api/core/staff_authz.CONTENT_PUBLISH (kept literal: no import cycle)
TWO_PERSON_REQUIRED = "two_person_required"
SAME_PERSON = "same_person"

_SWITCH_SQL = """
    SELECT value::text AS value_text FROM shared.setting_value
     WHERE key = $1 AND tenant_id IS NULL AND status = 'approved' AND effective_from <= now()
     ORDER BY effective_from DESC, version DESC LIMIT 1
"""

MESSAGES = {
    TWO_PERSON_REQUIRED: ("Two-person publishing is on: a content lead or an administrator publishes this version. "
                          "Ask one of them to publish it."),
    SAME_PERSON: ("Two-person publishing is on: you wrote this version, so someone else must publish it."),
}


def _default() -> bool:
    """The definition's default (off on Dev); True when the definition is unknown or unreadable (fail closed)."""
    try:
        from shared import settings

        return settings.definition(PUBLISH_TWO_PERSON_KEY).default_value is not False
    except Exception:  # noqa: BLE001 — no definition yet (CIS 458 not compiled in): the platform default is off
        return False


async def switch_on(conn: Any) -> bool:
    """Is two-person publishing on, read in the caller's transaction. Fails closed (True) on a read error or a value
    that is not a boolean; the definition's default when no value was ever approved."""
    try:
        async with conn.transaction():       # a savepoint: a failed read leaves the caller's transaction usable
            row = await conn.fetchrow(_SWITCH_SQL, PUBLISH_TWO_PERSON_KEY)
    except Exception as exc:  # noqa: BLE001 — fail closed
        if type(exc).__name__ == "UndefinedTableError":   # the registry is not migrated (a fresh test DB): default
            return _default()
        logger.warning("content_publish_switch_read_failed", error=repr(exc)[:200])
        return True
    if row is None:
        return _default()
    try:
        value = json.loads(row["value_text"])
    except (TypeError, ValueError):
        return True
    return value is not False


def refusal(*, switch: bool, perms: Iterable[str], actor: Optional[str],
            author: Optional[str]) -> Optional[str]:
    """The refusal code for a publish, or None. Pure.

    ``perms`` are the caller's role capabilities; ``actor`` the publisher (admin:<id> or admin), ``author`` the
    draft's author (None when unknown: a writer run, a version from before CIS 457)."""
    if not switch:
        return None
    if CONTENT_PUBLISH not in set(perms):
        return TWO_PERSON_REQUIRED
    if actor and author and actor == author:
        return SAME_PERSON
    return None


__all__ = ["CONTENT_PUBLISH", "MESSAGES", "PUBLISH_TWO_PERSON_KEY", "SAME_PERSON", "TWO_PERSON_REQUIRED", "refusal",
           "switch_on"]
