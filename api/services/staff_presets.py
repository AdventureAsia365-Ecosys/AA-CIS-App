"""The Staff page's people-role presets as data (AA-1155 spec task T14, root spec R5.7; was code since AA-1031,
AA-1091, AA-1150).

A preset is a row of ``shared.staff_role_preset`` (CIS 465): its label and description, the CIS role it grants (a
built-in assignable role or a CIS custom role code; none = kept), the Booking mode (keep / set a role / clear the
grant), the CRM mode (keep / replace with a set / add a set) and ``additive_only``. Seeded with the four presets the
code held before (platform_owner "Administrator", business_owner, content_ops, pricing_team). An ``is_owner`` user
creates, edits and archives presets on the Staff page (:func:`create_preset`, :func:`update_preset`), each change
audited before and after; ``api/services/staff_directory.py`` (the only writer of staff roles) applies one with
:func:`load_preset` + :func:`plan` inside its guarded transaction.

The code keeps only the guards:

- a preset never sets the owner flag (the table has no such column; nothing here or in the directory writes it);
- the directory's self-demote and last-owner checks run on what the preset changes, like on any role change;
- an additive-only preset never replaces a held role: it fills an empty slot (:data:`CIS_EMPTY_ROLES`, no Booking
  grant or a retired one), keeps a role listed in its keep lists (or the role it grants), and refuses anything else
  with 409 ``preset_would_replace`` (:func:`plan` lists what would be lost); it never clears the Booking grant and
  never replaces the CRM roles (also a table CHECK).

``RETIRED_BOOKING_ROLES`` (AA-1150) stays here: a Booking role the directory no longer grants is a code rule, not a
preset.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field, replace
from typing import Any, Optional

import asyncpg

# AA-1150: Booking roles the directory no longer grants (S285 T13). A grant row that still holds one is retired by Sync
# (deleted in the same audited transaction) and its WORKFORCE group is removed like any other unwanted group.
RETIRED_BOOKING_ROLES: tuple[str, ...] = ("content_creator",)

# Guard: the CIS role an additive-only preset may raise (the read-only role: nothing is lost by raising it). CIS has
# no "no role" (admin_users.role is NOT NULL); the Booking empty slot is no grant or a retired one.
CIS_EMPTY_ROLES = frozenset({"viewer"})

BOOKING_MODES: tuple[str, ...] = ("keep", "set", "clear")
CRM_MODES: tuple[str, ...] = ("keep", "replace", "add")
PRESET_CODE_RE = re.compile(r"^[a-z][a-z0-9_]{1,47}$")
LABEL_MAX = 80          # technical-constant: the CIS 465 staff_role_preset_label_chk bound (a column size)
DESCRIPTION_MAX = 300   # technical-constant: the CIS 465 staff_role_preset_description_chk bound (a column size)
KEEP_MAX = 16           # technical-constant: the CIS 465 staff_role_preset_keep_chk bound
SORT_MAX = 999          # technical-constant: the CIS 465 staff_role_preset_sort_chk bound

PRESET_CREATED = "staff.preset_created"
PRESET_UPDATED = "staff.preset_updated"
RESOURCE_TYPE = "staff_preset"

_COLUMNS = ("code, label, description, cis_role, booking_mode, booking_role, crm_mode, crm_roles, additive_only, "
            "cis_keep_roles, booking_keep_roles, sort_order, archived")
SQL_PRESET = f"SELECT {_COLUMNS} FROM shared.staff_role_preset WHERE code = $1"
SQL_PRESET_LOCK = SQL_PRESET + " FOR UPDATE"
SQL_PRESETS = f"SELECT {_COLUMNS} FROM shared.staff_role_preset ORDER BY archived, sort_order, label, code"
SQL_INSERT_PRESET = (
    "INSERT INTO shared.staff_role_preset (code, label, description, cis_role, booking_mode, booking_role, crm_mode, "
    "crm_roles, additive_only, cis_keep_roles, booking_keep_roles, sort_order, archived, created_by, updated_by) "
    "VALUES ($1, $2, $3, $4, $5, $6, $7, $8::text[], $9, $10::text[], $11::text[], $12, $13, $14, $14) "
    "ON CONFLICT (code) DO NOTHING RETURNING code"
)
SQL_UPDATE_PRESET = (
    "UPDATE shared.staff_role_preset SET label = $2, description = $3, cis_role = $4, booking_mode = $5, "
    "booking_role = $6, crm_mode = $7, crm_roles = $8::text[], additive_only = $9, cis_keep_roles = $10::text[], "
    "booking_keep_roles = $11::text[], sort_order = $12, archived = $13, updated_at = now(), updated_by = $14 "
    "WHERE code = $1"
)

# What a missing table or grant raises (CIS 465 not applied): presets are "unavailable", never a 500.
_UNAVAILABLE = (asyncpg.UndefinedTableError, asyncpg.InsufficientPrivilegeError, asyncpg.UndefinedColumnError)

# The fields an editor write may send (never an owner flag: the route body forbids extra fields too).
EDITABLE: tuple[str, ...] = ("label", "description", "cis_role", "booking_mode", "booking_role", "crm_mode",
                             "crm_roles", "additive_only", "cis_keep_roles", "booking_keep_roles", "sort_order",
                             "archived")


@dataclass(frozen=True)
class Preset:
    """One preset row."""
    code: str
    label: str
    description: str = ""
    cis_role: Optional[str] = None
    booking_mode: str = "keep"
    booking_role: Optional[str] = None
    crm_mode: str = "keep"
    crm_roles: tuple[str, ...] = ()
    additive_only: bool = False
    cis_keep_roles: tuple[str, ...] = ()
    booking_keep_roles: tuple[str, ...] = ()
    sort_order: int = 100
    archived: bool = False

    def as_dict(self) -> dict[str, Any]:
        return {"code": self.code, "label": self.label, "description": self.description, "cis_role": self.cis_role,
                "booking_mode": self.booking_mode, "booking_role": self.booking_role, "crm_mode": self.crm_mode,
                "crm_roles": list(self.crm_roles), "additive_only": self.additive_only,
                "cis_keep_roles": list(self.cis_keep_roles), "booking_keep_roles": list(self.booking_keep_roles),
                "sort_order": self.sort_order, "archived": self.archived}


def from_record(r: Any) -> Preset:
    return Preset(code=r["code"], label=r["label"], description=r["description"] or "", cis_role=r["cis_role"],
                  booking_mode=r["booking_mode"], booking_role=r["booking_role"], crm_mode=r["crm_mode"],
                  crm_roles=tuple(r["crm_roles"] or ()), additive_only=bool(r["additive_only"]),
                  cis_keep_roles=tuple(r["cis_keep_roles"] or ()),
                  booking_keep_roles=tuple(r["booking_keep_roles"] or ()), sort_order=int(r["sort_order"]),
                  archived=bool(r["archived"]))


def _error(status: int, detail: str, code: str) -> Exception:
    from api.services.staff_directory import StaffDirectoryError   # noqa: PLC0415 - staff_directory imports this module

    return StaffDirectoryError(status, detail, code)


def _unavailable() -> Exception:
    return _error(503, "Staff presets are not available yet: the database update that stores them (CIS migration "
                       "465) is not applied on this server; nothing was changed", "presets_unavailable")


async def load_preset(conn: Any, code: Any) -> Preset:
    """The active preset ``code`` for an apply: unknown or archived -> 400 ``preset``; the table unreadable -> 503."""
    if not isinstance(code, str) or not PRESET_CODE_RE.match(code):
        raise _error(400, "Unknown preset", "preset")
    try:
        record = await conn.fetchrow(SQL_PRESET, code)
    except _UNAVAILABLE:
        raise _unavailable() from None
    if record is None:
        raise _error(400, f"Unknown preset {code}", "preset")
    preset = from_record(record)
    if preset.archived:
        raise _error(400, f"The preset {preset.label} is archived", "preset")
    return preset


async def list_presets(conn: Any) -> Optional[list[Preset]]:
    """Every preset (archived last, then by sort order), or None when CIS 465 is not readable here."""
    try:
        rows = await conn.fetch(SQL_PRESETS)
    except _UNAVAILABLE:
        return None
    return [from_record(r) for r in rows]


# ── Applying a preset ──────────────────────────────────────────────────────────────────────────────────────────

KEEP: Any = object()   # "this app's role is not changed"


@dataclass
class Plan:
    """What a preset changes for these current roles: ``role`` / ``booking`` / ``crm`` are :data:`KEEP` or the new
    value (``booking`` None = the grant removed); ``lost`` names each held role an additive preset would replace."""
    role: Any = KEEP
    booking: Any = KEEP
    crm: Any = KEEP
    lost: list[str] = field(default_factory=list)


def plan(preset: Preset, old_role: Optional[str], old_booking: Optional[str],
         old_crm: tuple[str, ...] | list[str] = ()) -> Plan:
    """The roles ``preset`` sets on an account holding ``old_role`` (its CIS code, a custom code included),
    ``old_booking`` and ``old_crm``. Pure: the directory checks the roles, the guards and ``lost``."""
    out = Plan()
    if preset.cis_role is not None:
        out.role = _slot(preset, preset.cis_role, old_role, preset.cis_keep_roles, CIS_EMPTY_ROLES, f"CIS {old_role}",
                         out.lost)
    if preset.booking_mode == "clear":
        out.booking = None
    elif preset.booking_mode == "set":
        out.booking = _slot(preset, preset.booking_role, old_booking, preset.booking_keep_roles,
                            RETIRED_BOOKING_ROLES, f"Booking {old_booking}", out.lost)   # AA-1150: retired = empty
    if preset.crm_mode == "replace":
        out.crm = tuple(preset.crm_roles)
    elif preset.crm_mode == "add":
        out.crm = tuple(sorted(set(old_crm) | set(preset.crm_roles)))
    return out


def _slot(preset: Preset, new: Any, old: Optional[str], keep: tuple[str, ...], empty: Any, name: str,
          lost: list[str]) -> Any:
    """One app's planned role: ``new`` for a plain preset; for an additive-only one KEEP when the held role is listed
    (or is ``new``), ``new`` in an empty slot (None or one of ``empty``), else KEEP with ``name`` added to ``lost``."""
    if not preset.additive_only:
        return new
    if old in keep or old == new:
        return KEEP
    if old is None or old in empty:
        return new
    lost.append(name)
    return KEEP


def would_replace(preset: Preset, lost: list[str]) -> Exception:
    """409 ``preset_would_replace`` for an additive preset that would replace ``lost``."""
    return _error(409, f"The {preset.label} preset only adds roles and would replace " + " and ".join(lost)
                       + " (one role per app): set the roles by hand", "preset_would_replace")


# ── The preset editor (is_owner only, audited) ─────────────────────────────────────────────────────────────────

def _text(value: Any, *, name: str, low: int, high: int) -> str:
    text = value.strip() if isinstance(value, str) else None
    if text is None or not low <= len(text) <= high or any(ord(ch) < 32 or ord(ch) == 127 for ch in text):
        raise _error(400, f"The {name} is {low}-{high} characters, no control characters", f"invalid_{name}")
    return text


def _roles(values: Any, *, name: str, known: tuple[str, ...]) -> tuple[str, ...]:
    from api.core import staff_roles   # noqa: PLC0415

    if values is None:
        return ()
    if isinstance(values, (str, bytes)) or not isinstance(values, (list, tuple)):
        raise _error(400, f"{name} is a list of role codes", f"invalid_{name}")
    bad = [v for v in values if v not in known and not staff_roles.is_custom_code(v)]
    if bad or len(values) > KEEP_MAX:
        raise _error(400, f"{name}: at most {KEEP_MAX} of {', '.join(known)} or custom role codes", f"invalid_{name}")
    return tuple(dict.fromkeys(values))


def _live(p: Preset, before: Optional[Preset], name: str) -> bool:
    """Whether the custom role in field ``name`` must be live (active, readable): always for a new preset; on an edit
    only when that field changed or the preset is restored, and never when the result is archived (it cannot be
    applied). So an owner can relabel or archive a preset whose custom role was archived since (CIS here, Booking in
    Booking): the apply refuses such a preset, the edit that retires it does not."""
    if before is None:
        return True
    if p.archived:
        return False
    return before.archived or getattr(before, name) != getattr(p, name)


async def check_preset(conn: Any, p: Preset, before: Optional[Preset] = None) -> Preset:
    """The editor's rules for one preset (400 with a code; the CIS 465 CHECKs refuse the same if this is bypassed):
    roles are assignable built-in ones or custom codes, ACTIVE when :func:`_live` says so (``before`` = the row an
    edit changes); the modes agree with their roles; an additive preset never clears or replaces; the preset sets
    something."""
    from api.services import staff_custom_roles as scr   # noqa: PLC0415
    from api.services import staff_directory as sd   # noqa: PLC0415

    if not PRESET_CODE_RE.match(p.code or ""):
        raise _error(400, "A preset code is a lower-case letter then 1-47 letters, digits or _", "invalid_code")
    label = _text(p.label, name="label", low=1, high=LABEL_MAX)
    description = _text(p.description or "", name="description", low=0, high=DESCRIPTION_MAX)
    if p.cis_role is not None:
        sd.require_cis_role(p.cis_role)
        if _live(p, before, "cis_role"):
            await scr.resolve_cis_custom(conn, p.cis_role)    # a custom code: active, with capabilities (400)
    await _check_booking(conn, p, live=_live(p, before, "booking_role"))
    crm = _check_crm(p)
    cis_keep = _roles(list(p.cis_keep_roles), name="cis_keep_roles", known=sd.CIS_ROLES)
    booking_keep = _roles(list(p.booking_keep_roles), name="booking_keep_roles", known=sd.BOOKING_ROLES)
    if p.additive_only and (p.booking_mode == "clear" or p.crm_mode == "replace"):
        raise _error(400, "An additive-only preset never removes the Booking grant or replaces the CRM roles",
                     "additive_conflict")
    if not p.additive_only and (cis_keep or booking_keep):
        raise _error(400, "Keep lists belong to an additive-only preset", "additive_conflict")
    if p.cis_role is None and p.booking_mode == "keep" and p.crm_mode == "keep":
        raise _error(400, "A preset must set at least one role", "empty")
    if not isinstance(p.sort_order, int) or isinstance(p.sort_order, bool) or not 0 <= p.sort_order <= SORT_MAX:
        raise _error(400, f"sort_order is 0-{SORT_MAX}", "sort_order")
    return replace(p, label=label, description=description, crm_roles=crm, cis_keep_roles=cis_keep,
                   booking_keep_roles=booking_keep, archived=bool(p.archived), additive_only=bool(p.additive_only))


async def _check_booking(conn: Any, p: Preset, *, live: bool) -> None:
    """booking_mode is known and a Booking role goes with "set" only: a built-in one or a custom code, active when
    ``live``."""
    from api.services import staff_custom_roles as scr   # noqa: PLC0415
    from api.services import staff_directory as sd   # noqa: PLC0415

    if p.booking_mode not in BOOKING_MODES:
        raise _error(400, f"booking_mode is one of {', '.join(BOOKING_MODES)}", "booking_mode")
    if (p.booking_mode == "set") != (p.booking_role is not None):
        raise _error(400, "A Booking role goes with booking_mode set, and only with it", "booking_mode")
    if p.booking_role is not None:
        sd.require_booking_role(p.booking_role)
    if p.booking_role is not None and live:
        await scr.check_booking_custom(conn, p.booking_role)  # a custom code: active (400), the view readable (503)


def _check_crm(p: Preset) -> tuple[str, ...]:
    """crm_mode is known, its roles are CRM staff roles; keep takes none and add at least one."""
    from api.services import staff_directory as sd   # noqa: PLC0415

    if p.crm_mode not in CRM_MODES:
        raise _error(400, f"crm_mode is one of {', '.join(CRM_MODES)}", "crm_mode")
    crm = sd.require_crm_roles(list(p.crm_roles))
    if (p.crm_mode == "keep" and crm) or (p.crm_mode == "add" and not crm):
        raise _error(400, "crm_mode keep takes no CRM role; add takes at least one", "crm_mode")
    return crm


def _args(p: Preset, actor: str) -> tuple:
    return (p.code, p.label, p.description, p.cis_role, p.booking_mode, p.booking_role, p.crm_mode,
            list(p.crm_roles), p.additive_only, list(p.cis_keep_roles), list(p.booking_keep_roles), p.sort_order,
            p.archived, actor)


async def create_preset(pool: Any, *, actor_id: str, fields: dict[str, Any]) -> Preset:
    """A new preset (owners only, 409 ``preset_exists``), audited with its rules (``after``)."""
    from api.services import staff_directory as sd   # noqa: PLC0415

    unknown = set(fields) - set(EDITABLE) - {"code"}
    if unknown:
        raise _error(400, f"Unknown preset field(s): {', '.join(sorted(unknown))}", "preset_field")
    preset = Preset(code=fields.get("code") or "", label=fields.get("label") or "",
                    **{k: _coerce(k, fields[k]) for k in EDITABLE if k in fields and k != "label"})
    async with pool.acquire() as conn:
        async with conn.transaction():
            await sd._require_owner(conn, actor_id)
            preset = await check_preset(conn, preset)
            try:
                inserted = await conn.fetchval(SQL_INSERT_PRESET, *_args(preset, sd._actor_tag(actor_id)))
            except _UNAVAILABLE:
                raise _unavailable() from None
            if inserted is None:
                raise _error(409, f"A preset {preset.code} already exists", "preset_exists")
            await _audit(conn, actor_id, PRESET_CREATED, preset.code, {"after": preset.as_dict()})
    return preset


async def update_preset(pool: Any, *, actor_id: str, code: str, fields: dict[str, Any]) -> Preset:
    """Change the fields sent of one preset (owners only; 404 unknown), audited before and after; a no-op writes
    nothing. A custom role is re-checked only when its field changes or the preset is restored (:func:`_live`)."""
    from api.services import staff_directory as sd   # noqa: PLC0415

    unknown = set(fields) - set(EDITABLE)
    if unknown:
        raise _error(400, f"Unknown preset field(s): {', '.join(sorted(unknown))}", "preset_field")
    async with pool.acquire() as conn:
        async with conn.transaction():
            await sd._require_owner(conn, actor_id)
            try:
                record = await conn.fetchrow(SQL_PRESET_LOCK, code)
            except _UNAVAILABLE:
                raise _unavailable() from None
            if record is None:
                raise _error(404, f"No preset {code}", "preset_not_found")
            before = from_record(record)
            after = replace(before, **{k: _coerce(k, v) for k, v in fields.items()})
            after = await check_preset(conn, after, before)
            if after == before:
                return before
            await conn.execute(SQL_UPDATE_PRESET, *_args(after, sd._actor_tag(actor_id)))
            await _audit(conn, actor_id, PRESET_UPDATED, code, {"before": before.as_dict(), "after": after.as_dict()})
    return after


def _coerce(key: str, value: Any) -> Any:
    if key in ("crm_roles", "cis_keep_roles", "booking_keep_roles"):
        return tuple(value or ())
    if key == "description":
        return value or ""
    return value


async def _audit(conn: Any, actor_id: str, action: str, code: str, details: dict[str, Any]) -> None:
    from api.services.staff_directory import _actor_tag   # noqa: PLC0415
    from services.acp_shared.audit_log import write_audit_log   # noqa: PLC0415

    await write_audit_log(conn, tenant_id=None, actor=_actor_tag(actor_id), action=action,
                          resource_type=RESOURCE_TYPE, resource_id=code, details=details)
