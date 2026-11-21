"""AA-1155 T13 / T14: the in-memory staff directory of test_aa816 plus shared.staff_role (+ capabilities), the Booking
custom-role view and shared.staff_role_preset, answering the SQL of api/services/staff_role_editor.py and
api/services/staff_presets.py. ``transaction()`` rolls those back too."""
from __future__ import annotations

from contextlib import asynccontextmanager
from copy import deepcopy
from typing import Any, Optional

from api.core import staff_roles
from api.services import staff_presets
from api.services import staff_role_editor as editor
from tests.unit.test_aa816_staff_directory_writes import StaffDB


class EditorDB(StaffDB):
    def __init__(self) -> None:
        super().__init__()
        self.booking_custom: dict[str, dict[str, Any]] = {}   # booking.v_staff_role_capability, by role code
        self.writes = 0

    @asynccontextmanager
    async def transaction(self):
        saved = deepcopy((self.custom_roles, self.presets, self.booking_custom))
        try:
            async with super().transaction():
                yield
        except BaseException:
            self.custom_roles, self.presets, self.booking_custom = saved
            raise

    def add_booking_role(self, code: str, *, archived: bool = False) -> str:
        self.booking_custom[code] = {"label": "Invented ops", "based_on": "contracting", "archived": archived,
                                     "caps": ["supply.read"]}
        return code

    def _holders(self, code: str) -> int:
        return sum(1 for r in self.rows if r.get("custom_role") == code and r["is_active"])

    async def fetch(self, sql: str, *args: Any) -> list[dict[str, Any]]:
        if sql == staff_roles.SQL_CIS_ROLES:
            self.sql.append(sql)
            return [dict(r) for r in sorted(self.custom_roles.values(),
                                            key=lambda r: (r["archived"], r["label"], r["code"]))]
        if sql == editor.SQL_HOLDERS:
            self.sql.append(sql)
            codes = {r.get("custom_role") for r in self.rows if r.get("custom_role") and r["is_active"]}
            return [{"custom_role": c, "n": self._holders(c)} for c in codes]
        if sql == staff_presets.SQL_PRESETS:
            self.sql.append(sql)
            return [dict(p) for p in sorted(self.presets.values(),
                                            key=lambda p: (p["archived"], p["sort_order"], p["label"], p["code"]))]
        if sql == staff_roles.SQL_BOOKING_ROLE:
            self.sql.append(sql)
            role = self.booking_custom.get(args[0])
            if role is None:
                return []
            return [{"role_code": args[0], "label": role["label"], "based_on": role["based_on"],
                     "archived": role["archived"], "capability": c} for c in role["caps"]]
        return await super().fetch(sql, *args)

    async def fetchval(self, sql: str, *args: Any) -> Any:
        if sql == editor.SQL_INSERT_ROLE:
            self.sql.append(sql)
            code, label, based_on, _actor = args
            if code in self.custom_roles:
                return None
            self.custom_roles[code] = {"code": code, "label": label, "based_on": based_on, "archived": False,
                                       "capabilities": []}
            self.writes += 1
            return code
        if sql == editor.SQL_ROLE_LOCK:
            self.sql.append(sql)
            return args[0] if args[0] in self.custom_roles else None
        if sql == editor.SQL_ROLE_HOLDERS:
            self.sql.append(sql)
            return self._holders(args[0])
        if sql == staff_presets.SQL_INSERT_PRESET:
            self.sql.append(sql)
            code = args[0]
            if code in self.presets:
                return None
            self.presets[code] = _preset_row(args)
            self.writes += 1
            return code
        return await super().fetchval(sql, *args)

    async def execute(self, sql: str, *args: Any) -> str:
        if sql == editor.SQL_ADD_CAPS:
            self.sql.append(sql)
            caps = self.custom_roles[args[0]]["capabilities"]
            caps.extend(c for c in args[1] if c not in caps)
            self.writes += 1
            return "INSERT 0 1"
        if sql == editor.SQL_REMOVE_CAPS:
            self.sql.append(sql)
            role = self.custom_roles[args[0]]
            role["capabilities"] = [c for c in role["capabilities"] if c not in args[1]]
            self.writes += 1
            return "DELETE 1"
        if sql == editor.SQL_UPDATE_ROLE:
            self.sql.append(sql)
            role = self.custom_roles[args[0]]
            if args[1] is not None:
                role["label"] = args[1]
            if args[2] is not None:
                role["archived"] = args[2]
            self.writes += 1
            return "UPDATE 1"
        if sql == staff_presets.SQL_UPDATE_PRESET:
            self.sql.append(sql)
            self.presets[args[0]] = _preset_row(args)
            self.writes += 1
            return "UPDATE 1"
        return await super().execute(sql, *args)

    async def fetchrow(self, sql: str, *args: Any) -> Optional[dict[str, Any]]:
        if sql == staff_roles.SQL_CIS_ROLE:   # the base fake returns the row; keep the capability order sorted
            row = await super().fetchrow(sql, *args)
            return None if row is None else {**row, "capabilities": sorted(row["capabilities"])}
        return await super().fetchrow(sql, *args)


def _preset_row(args: tuple) -> dict[str, Any]:
    (code, label, description, cis_role, booking_mode, booking_role, crm_mode, crm_roles, additive_only, cis_keep,
     booking_keep, sort_order, archived, _actor) = args
    return {"code": code, "label": label, "description": description, "cis_role": cis_role,
            "booking_mode": booking_mode, "booking_role": booking_role, "crm_mode": crm_mode,
            "crm_roles": list(crm_roles), "additive_only": additive_only, "cis_keep_roles": list(cis_keep),
            "booking_keep_roles": list(booking_keep), "sort_order": sort_order, "archived": archived}
