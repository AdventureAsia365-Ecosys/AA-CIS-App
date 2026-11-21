"""The CIS role editor: CIS custom staff roles as data (AA-1155 spec task T13; root spec R5.6; root ADR 0006a A14).

The same rules as the Booking role editor (AA-Booking ``app/auth/role_editor.py``, task T6): an ``is_owner`` staff
member (the directory's owner re-read, :func:`api.services.staff_directory._require_owner`) creates a CIS custom role
by cloning a built-in one, then ticks its capabilities, relabels or archives it. Built-in roles stay in code
(:data:`api.core.staff_authz.ROLE_PERMS`) and are shown read-only. Every change writes one audit row with the role
before and after (``staff.role_created`` / ``staff.role_updated``, resource ``staff_role``). Rules, all checked here
before any write (400 with a code; the CIS 462 CHECKs refuse the same if the code is ever bypassed):

- the code matches ``^custom_[a-z][a-z0-9_]{1,46}$`` (never a built-in role name);
- ``based_on`` is a built-in CIS role a custom holder may keep in ``admin_users.role``: never admin, viewer or reviewer
  (root spec D5, ``staff_role_based_on_chk``);
- the label is 1-80 characters without control characters;
- every capability is known to the code (``staff_authz.CAPABILITIES``, ``unknown_capability``) and none is in the
  approve class or the staff directory (``approve_class_capability``: ``*.approve``, ``*.approve_<x>``,
  ``staff.write``, ``staff.admin``; root spec D4).

Three guards CIS adds, because a CIS holder is refused when its role is empty or archived (fail closed, R5.1): a
role keeps at least one capability (400 ``no_capabilities``), a role an active account holds cannot be archived (409
``role_in_use``: move its holders to another role on the Staff page first), and a role an active people-role preset
grants cannot be archived either (409 ``role_in_use_by_preset``: archive or edit the preset first, so no preset on the
Staff page points at a dead role). Roles are never deleted.

Readers: ``staff.read`` (the route policy). Writers: owners on a Cognito session (``staff.write`` + the owner re-read
in the transaction). The staff identity cache of this task is dropped after each save; other tasks follow within its
60 s bound.
"""
from __future__ import annotations

from typing import Any, Optional

import asyncpg

from api.core import staff_identity, staff_roles

LABEL_MAX = 80   # technical-constant: the CIS 462 staff_role_label_chk bound (a column size, not a policy value)
# Root spec D5: the built-in roles a custom role may never be based on (staff_role_based_on_chk).
NOT_A_BASE: frozenset[str] = frozenset({"admin", "viewer", "reviewer"})

ROLE_CREATED = "staff.role_created"
ROLE_UPDATED = "staff.role_updated"
RESOURCE_TYPE = "staff_role"

SQL_ROLE_LOCK = "SELECT code FROM shared.staff_role WHERE code = $1 FOR UPDATE"
SQL_INSERT_ROLE = (
    "INSERT INTO shared.staff_role (code, label, based_on, created_by, updated_by) "
    "VALUES ($1, $2, $3::shared.admin_role, $4, $4) ON CONFLICT (code) DO NOTHING RETURNING code"
)
SQL_ADD_CAPS = (
    "INSERT INTO shared.staff_role_capability (role_code, capability, created_by) "
    "SELECT $1, c, $3 FROM unnest($2::text[]) AS c ON CONFLICT DO NOTHING"
)
SQL_REMOVE_CAPS = "DELETE FROM shared.staff_role_capability WHERE role_code = $1 AND capability = ANY($2::text[])"
SQL_UPDATE_ROLE = (
    "UPDATE shared.staff_role SET label = coalesce($2, label), archived = coalesce($3, archived), "
    "updated_at = now(), updated_by = $4 WHERE code = $1"
)
SQL_HOLDERS = (
    "SELECT custom_role, count(*) AS n FROM shared.admin_users WHERE custom_role IS NOT NULL AND is_active "
    "GROUP BY custom_role"
)
SQL_ROLE_HOLDERS = "SELECT count(*) AS n FROM shared.admin_users WHERE custom_role = $1 AND is_active"
# The active presets (CIS 465) that grant this role; the keep lists are left out: keeping a role nobody can hold is
# harmless. CIS 465 not readable here = no preset can name it.
SQL_ROLE_PRESETS = (
    "SELECT label FROM shared.staff_role_preset WHERE cis_role = $1 AND NOT archived ORDER BY sort_order, label"
)
_PRESETS_UNREADABLE = (asyncpg.UndefinedTableError, asyncpg.InsufficientPrivilegeError)


def _refused(status: int, detail: str, code: str) -> Exception:
    from api.services.staff_directory import StaffDirectoryError   # noqa: PLC0415 - one error type for the Staff page

    return StaffDirectoryError(status, detail, code)


def known_capabilities() -> frozenset[str]:
    from api.core.staff_authz import CAPABILITIES   # noqa: PLC0415

    return CAPABILITIES


def bases() -> tuple[str, ...]:
    """The built-in CIS roles a custom role may be cloned from (ROLE_PERMS order)."""
    from api.core.staff_authz import ROLE_PERMS   # noqa: PLC0415

    return tuple(r for r in ROLE_PERMS if r not in NOT_A_BASE)


def check_code(code: Any) -> str:
    if not staff_roles.is_custom_code(code):
        raise _refused(400, "A custom role code is custom_ then a lower-case letter and 2-47 letters, digits or _",
                       "invalid_role_code")
    return code


def check_label(label: Any) -> str:
    text = label.strip() if isinstance(label, str) else ""
    if not 1 <= len(text) <= LABEL_MAX or any(ord(ch) < 32 or ord(ch) == 127 for ch in text):
        raise _refused(400, f"A label is 1-{LABEL_MAX} characters, no control characters", "invalid_label")
    return text


def check_base(based_on: Any) -> str:
    if based_on not in bases():
        raise _refused(400, f"based_on must be one of {', '.join(bases())}", "unknown_base_role")
    return str(based_on)


def check_capabilities(capabilities: Any) -> frozenset[str]:
    """Root spec R5.6 / R2.2: 400 on a capability the code does not know, one in the approve class, or none."""
    if not isinstance(capabilities, (list, tuple, set, frozenset)) or not all(
            isinstance(c, str) for c in capabilities):
        raise _refused(400, "capabilities is a list of capability names", "invalid_capabilities")
    names = frozenset(capabilities)
    unknown = sorted(names - known_capabilities())
    if unknown:
        raise _refused(400, f"Unknown capability: {', '.join(unknown)}", "unknown_capability")
    approve = sorted(c for c in names if staff_roles.is_approve_class(c))
    if approve:
        raise _refused(400, f"A custom role never holds an approval or the staff directory: {', '.join(approve)}",
                       "approve_class_capability")
    if not names:
        raise _refused(400, "A custom role needs at least one capability (an empty role refuses its holders)",
                       "no_capabilities")
    return names


def clone_of(based_on: str) -> frozenset[str]:
    """The capabilities a new role starts with: the built-in role's, without the approve class and owner rights."""
    from api.core.staff_authz import ROLE_PERMS   # noqa: PLC0415

    return ROLE_PERMS[based_on] & staff_roles.custom_allowed()


def _view(role: staff_roles.CustomRole, holders: int) -> dict[str, Any]:
    return {**role.as_dict(), "holders": holders}


def _snapshot(role: dict[str, Any]) -> dict[str, Any]:
    """What the audit row keeps of a role (before / after)."""
    return {k: role[k] for k in ("code", "label", "based_on", "archived", "capabilities")}


async def _one(conn: Any, code: str) -> Optional[dict[str, Any]]:
    record = await conn.fetchrow(staff_roles.SQL_CIS_ROLE, code)
    if record is None:
        return None
    raw = record["capabilities"] or []
    role = staff_roles.CustomRole(code=record["code"], label=record["label"], based_on=record["based_on"],
                                  archived=bool(record["archived"]), capabilities=frozenset(raw))
    return _view(role, int(await conn.fetchval(SQL_ROLE_HOLDERS, code) or 0))


async def editor_view(conn: Any, *, can_edit: bool) -> dict[str, Any]:
    """The editor page: built-in roles (read-only, ``clonable``), custom roles with their active holder counts, every
    capability with whether a custom role may hold it, and whether this viewer may edit."""
    roles = await staff_roles.list_cis_roles(conn)
    holders: dict[str, int] = {}
    if roles is not None:
        holders = {r["custom_role"]: int(r["n"]) for r in await conn.fetch(SQL_HOLDERS)}
    allowed = staff_roles.custom_allowed()
    return {
        "builtin": [{**r, "clonable": r["code"] in bases()} for r in staff_roles.cis_builtin_roles()],
        "custom": [_view(r, holders.get(r.code, 0)) for r in roles or []],
        "custom_available": roles is not None,
        "capabilities": [{"name": c, "assignable": c in allowed} for c in sorted(known_capabilities())],
        "can_edit": can_edit,
    }


async def create_role(pool: Any, *, actor_id: str, code: Any, label: Any, based_on: Any,
                      capabilities: Any = None) -> dict[str, Any]:
    """Clone ``based_on`` into a new custom role (its capabilities without the approve class, or ``capabilities``)."""
    from api.services import staff_directory as sd   # noqa: PLC0415

    code = check_code(code)
    label = check_label(label)
    based_on = check_base(based_on)
    caps = check_capabilities(clone_of(based_on) if capabilities is None else capabilities)
    actor = sd._actor_tag(actor_id)
    async with pool.acquire() as conn:
        async with conn.transaction():
            await sd._require_owner(conn, actor_id)
            if await conn.fetchval(SQL_INSERT_ROLE, code, label, based_on, actor) is None:
                raise _refused(409, f"A custom role {code} already exists", "role_exists")
            await conn.execute(SQL_ADD_CAPS, code, sorted(caps), actor)
            after = await _one(conn, code)
            await _audit(conn, actor_id, ROLE_CREATED, code, {
                "after": _snapshot(after), "summary": f"Custom role created from {based_on} with {len(caps)} "
                                                      "capabilities"})
    staff_identity.clear_cache()
    return after


async def update_role(pool: Any, *, actor_id: str, code: Any, label: Any = None, capabilities: Any = None,
                      archived: Optional[bool] = None) -> dict[str, Any]:
    """Relabel, set the capabilities (the full set) or archive / restore a custom role; audited before and after.
    A change that changes nothing writes nothing."""
    from api.services import staff_directory as sd   # noqa: PLC0415

    code = check_code(code)
    new_label = None if label is None else check_label(label)
    new_caps = None if capabilities is None else check_capabilities(capabilities)
    actor = sd._actor_tag(actor_id)
    async with pool.acquire() as conn:
        async with conn.transaction():
            await sd._require_owner(conn, actor_id)
            if await conn.fetchval(SQL_ROLE_LOCK, code) is None:
                raise _refused(404, f"No custom role {code}", "role_not_found")
            before = await _one(conn, code)
            held = frozenset(before["capabilities"])
            changes: list[str] = []
            if new_caps is not None and new_caps != held:
                removed, added = sorted(held - new_caps), sorted(new_caps - held)
                if removed:
                    await conn.execute(SQL_REMOVE_CAPS, code, removed)
                if added:
                    await conn.execute(SQL_ADD_CAPS, code, added, actor)
                changes.append("capabilities " + " ".join([*(f"+{c}" for c in added), *(f"-{c}" for c in removed)]))
            label_changed = new_label is not None and new_label != before["label"]
            archive_changed = archived is not None and bool(archived) != before["archived"]
            if archive_changed and archived:
                await _require_unused(conn, code, before["holders"])
            if label_changed:
                changes.append(f"label {before['label']} -> {new_label}")
            if archive_changed:
                changes.append("archived" if archived else "restored")
            if not changes:
                return before
            await conn.execute(SQL_UPDATE_ROLE, code, new_label if label_changed else None,
                               bool(archived) if archive_changed else None, actor)
            after = await _one(conn, code)
            await _audit(conn, actor_id, ROLE_UPDATED, code, {
                "before": _snapshot(before), "after": _snapshot(after), "summary": "; ".join(changes)})
    staff_identity.clear_cache()
    return after


async def _require_unused(conn: Any, code: str, holders: int) -> None:
    """Archiving: 409 ``role_in_use`` while an active account holds the role, 409 ``role_in_use_by_preset`` while an
    active preset grants it (its labels in the detail)."""
    if holders:
        raise _refused(409, f"{holders} active account(s) hold this role; give them another role on the Staff page "
                            "before archiving it", "role_in_use")
    try:
        async with conn.transaction():   # a savepoint: an unreadable table must not abort the editor's transaction
            presets = [r["label"] for r in await conn.fetch(SQL_ROLE_PRESETS, code)]
    except _PRESETS_UNREADABLE:
        presets = []
    if presets:
        raise _refused(409, "The preset(s) " + ", ".join(presets) + " grant this role; archive them or give them "
                            "another role before archiving it", "role_in_use_by_preset")


async def _audit(conn: Any, actor_id: str, action: str, code: str, details: dict[str, Any]) -> None:
    from api.services.staff_directory import _actor_tag   # noqa: PLC0415
    from services.acp_shared.audit_log import write_audit_log   # noqa: PLC0415

    await write_audit_log(conn, tenant_id=None, actor=_actor_tag(actor_id), action=action,
                          resource_type=RESOURCE_TYPE, resource_id=code, details=details)
