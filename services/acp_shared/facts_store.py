"""services.acp_shared.facts_store — AA-814 write side of the Facts Entry table (`acp_shared.facts`, CIS 145 / 302).

Two callers, one set of rules:
  - api/routers/admin_facts.py — staff CRUD of platform facts (scope 'platform', tenant_id NULL), main pool;
  - api/routers/v1_facts.py — a tenant's CRUD of its own facts (scope 'tenant'), tenant pool (aa_app_user, RLS).

Every statement here names its scope and, for tenant rows, its tenant: the tenant pool is isolated by RLS (CIS 229
`tenant_isolation`), and the explicit filter is the second lock (and the only one on the main pool, whose `cis_app`
policy is `USING (true)`). The functions take a connection the caller already holds inside its own transaction and
return plain dicts (`before` / `after`) the caller writes into acp_shared.audit_log.

Read side for the writers: services/acp_content_writing/facts.py (precedence + expiry).
"""
from __future__ import annotations

import re
import secrets
from datetime import date, datetime, timezone
from typing import Any, Literal, Optional
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

# CIS 302 facts_kind_check — keep both in step.
FACT_KINDS: tuple[str, ...] = ("price", "season", "visa_entry", "transfer_time", "booking_terms", "other")
FactKind = Literal["price", "season", "visa_entry", "transfer_time", "booking_terms", "other"]

MAX_TITLE = 200
MAX_BODY = 1200
MAX_PROVENANCE = 500
MAX_COUNTRY = 80
# a tenant's own facts; enough for prices / terms per destination, small enough for the writer seed
MAX_TENANT_FACTS = 200

_COLUMNS = ("fact_id, scope, tenant_id, kind, title, body, provenance, country, stated_on, valid_until, "
            "updated_by, created_at, updated_at")
# fields an edit may change and the audit compares
EDITABLE = ("kind", "title", "body", "provenance", "country", "stated_on", "valid_until")


class FactError(ValueError):
    """A fact write that cannot be applied (bad field, limit reached): HTTP 422."""


class FactConflict(ValueError):
    """The row changed since the editor loaded it (expected_updated_at differs): HTTP 409."""


def _clean(text: Optional[str]) -> Optional[str]:
    if text is None:
        return None
    return " ".join(str(text).split())


class FactFields(BaseModel):
    """The editable fields of one fact (create, and the full form of an edit). Whitespace is collapsed; title, body and
    provenance must keep at least one character; country is optional (NULL = any country)."""
    model_config = ConfigDict(extra="forbid")

    kind: FactKind = "other"
    title: str = Field(min_length=1, max_length=MAX_TITLE)
    body: str = Field(min_length=1, max_length=MAX_BODY)
    provenance: str = Field(min_length=1, max_length=MAX_PROVENANCE)
    country: Optional[str] = Field(None, max_length=MAX_COUNTRY)
    stated_on: Optional[date] = None
    valid_until: Optional[date] = None

    @field_validator("title", "body", "provenance", mode="after")
    @classmethod
    def _required_text(cls, v: str) -> str:
        v = _clean(v) or ""
        if not v:
            raise ValueError("cannot be blank")
        return v

    @field_validator("country", mode="after")
    @classmethod
    def _country(cls, v: Optional[str]) -> Optional[str]:
        return _clean(v) or None

    @model_validator(mode="after")
    def _dates(self):
        if self.valid_until and self.stated_on and self.valid_until < self.stated_on:
            raise ValueError("valid_until cannot be before stated_on")
        return self


class FactPatch(BaseModel):
    """A partial edit: only the fields sent change. `expected_updated_at` (the row's updated_at as loaded; the UI always
    sends it) turns a stale form into a 409 instead of overwriting someone else's edit."""
    model_config = ConfigDict(extra="forbid")

    kind: Optional[FactKind] = None
    title: Optional[str] = Field(None, max_length=MAX_TITLE)
    body: Optional[str] = Field(None, max_length=MAX_BODY)
    provenance: Optional[str] = Field(None, max_length=MAX_PROVENANCE)
    country: Optional[str] = Field(None, max_length=MAX_COUNTRY)
    stated_on: Optional[date] = None
    valid_until: Optional[date] = None
    expected_updated_at: Optional[datetime] = None


def new_fact_id() -> str:
    """`fact_<10 hex>`: the shape the hand-written rows already use; short, because the writer cites it as [F:<id>]."""
    return f"fact_{secrets.token_hex(5)}"


def subject_key(fact: dict) -> tuple[str, str, str]:
    """The subject of a fact: (kind, title, country), case- and whitespace-insensitive. A tenant fact replaces the
    platform facts with the same subject in the writer seed (services/acp_content_writing/facts.pick_facts)."""
    title = re.sub(r"\s+", " ", str(fact.get("title") or "")).strip().lower()
    country = str(fact.get("country") or "").strip().lower()
    return (str(fact.get("kind") or "other"), title, country)


def is_expired(fact: dict, today: Optional[date] = None) -> bool:
    until = fact.get("valid_until")
    return bool(until) and until < (today or date.today())


def to_api(row: Any, today: Optional[date] = None) -> dict:
    """One row as the API returns it (dates ISO, tenant_id text, `expired` computed)."""
    d = dict(row)
    out = {
        "fact_id": d["fact_id"], "scope": d["scope"],
        "tenant_id": str(d["tenant_id"]) if d.get("tenant_id") else None,
        "kind": d.get("kind") or "other", "title": d["title"], "body": d["body"], "provenance": d["provenance"],
        "country": d.get("country"),
        "stated_on": d["stated_on"].isoformat() if d.get("stated_on") else None,
        "valid_until": d["valid_until"].isoformat() if d.get("valid_until") else None,
        "updated_by": d.get("updated_by"),
        "created_at": d["created_at"].isoformat() if d.get("created_at") else None,
        "updated_at": d["updated_at"].isoformat() if d.get("updated_at") else None,
    }
    out["expired"] = is_expired(d, today)
    return out


def _audit_view(row: Any) -> dict:
    """The editable fields of a row, JSON-safe, for the audit row's before / after."""
    d = dict(row)
    return {k: (d.get(k).isoformat() if isinstance(d.get(k), date) else d.get(k)) for k in EDITABLE}


def _scope_where(scope: str, tenant_id: Optional[UUID], first: int) -> tuple[str, list]:
    if scope == "platform":
        return "scope = 'platform' AND tenant_id IS NULL", []
    if tenant_id is None:
        raise FactError("a tenant fact needs a tenant")
    return f"scope = 'tenant' AND tenant_id = ${first}::uuid", [tenant_id]


async def list_facts(conn, *, scope: str, tenant_id: Optional[UUID] = None, kind: Optional[str] = None,
                     country: Optional[str] = None, q: Optional[str] = None,
                     include_expired: bool = True) -> list[dict]:
    """The facts of one scope (a tenant's own, or the platform's), newest edit first."""
    where, params = _scope_where(scope, tenant_id, 1)
    clauses = [where]
    if kind:
        params.append(kind)
        clauses.append(f"kind = ${len(params)}")
    if country:
        params.append(country)
        clauses.append(f"lower(country) = lower(${len(params)}::text)")
    if q:
        params.append(f"%{q.strip()}%")
        clauses.append(f"(title ILIKE ${len(params)} OR body ILIKE ${len(params)} OR provenance ILIKE ${len(params)})")
    if not include_expired:
        clauses.append("(valid_until IS NULL OR valid_until >= current_date)")
    sql = (f"SELECT {_COLUMNS} FROM acp_shared.facts WHERE {' AND '.join(clauses)} "
           "ORDER BY updated_at DESC, fact_id LIMIT 500")
    return [to_api(r) for r in await conn.fetch(sql, *params)]


async def get_fact(conn, fact_id: str, *, scope: str, tenant_id: Optional[UUID] = None, lock: bool = False):
    where, params = _scope_where(scope, tenant_id, 2)
    sql = (f"SELECT {_COLUMNS} FROM acp_shared.facts WHERE fact_id = $1 AND {where}"
           + (" FOR UPDATE" if lock else ""))
    return await conn.fetchrow(sql, fact_id, *params)


async def create_fact(conn, fields: FactFields, *, scope: str, tenant_id: Optional[UUID], actor: str) -> dict:
    """INSERT one fact; returns the new row (API shape). A tenant may hold at most MAX_TENANT_FACTS facts."""
    if scope == "tenant":
        if tenant_id is None:
            raise FactError("a tenant fact needs a tenant")
        n = await conn.fetchval("SELECT count(*) FROM acp_shared.facts WHERE scope = 'tenant' AND tenant_id = $1::uuid",
                                tenant_id)
        if n >= MAX_TENANT_FACTS:
            raise FactError(f"an agency can keep at most {MAX_TENANT_FACTS} facts; delete one first")
    elif tenant_id is not None:
        raise FactError("a platform fact has no tenant")
    row = await conn.fetchrow(
        f"""INSERT INTO acp_shared.facts
               (fact_id, scope, tenant_id, kind, title, body, provenance, country, stated_on, valid_until, updated_by)
            VALUES ($1, $2, $3::uuid, $4, $5, $6, $7, $8, $9, $10, $11)
            RETURNING {_COLUMNS}""",
        new_fact_id(), scope, tenant_id, fields.kind, fields.title, fields.body, fields.provenance, fields.country,
        fields.stated_on, fields.valid_until, actor)
    return to_api(row)


def apply_patch(current: dict, patch: FactPatch) -> tuple[FactFields, dict]:
    """The full fields after the patch (validated as a whole) and the changed fields {name: [before, after]}.
    Raises FactConflict on a stale expected_updated_at, FactError when nothing changes or the result is invalid."""
    exp = patch.expected_updated_at
    cur_at = current.get("updated_at")
    if exp is not None and cur_at is not None:
        if exp.tzinfo is None:  # a client that dropped the offset: the API sends UTC timestamps
            exp = exp.replace(tzinfo=timezone.utc)
        if cur_at.tzinfo is None:
            cur_at = cur_at.replace(tzinfo=timezone.utc)
        if exp != cur_at:
            raise FactConflict("this fact changed since it was loaded; reload and try again")
    sent = patch.model_dump(exclude_unset=True, exclude={"expected_updated_at"})
    merged = {k: current.get(k) for k in EDITABLE}
    merged["kind"] = merged.get("kind") or "other"
    merged.update(sent)
    try:
        fields = FactFields(**merged)
    except ValueError as e:  # pydantic ValidationError is a ValueError
        raise FactError(str(e)) from e
    before = _audit_view(current)
    after = fields.model_dump(mode="json")
    changes = {k: [before[k], after[k]] for k in EDITABLE if before[k] != after[k]}
    if not changes:
        raise FactError("nothing to change: the fact already has these values")
    return fields, changes


async def update_fact(conn, fact_id: str, patch: FactPatch, *, scope: str, tenant_id: Optional[UUID],
                      actor: str) -> Optional[tuple[dict, dict]]:
    """Edit one fact of the scope (row locked). Returns (new row in API shape, changes) or None when there is no such
    fact in the scope (another tenant's fact is "no such fact")."""
    cur = await get_fact(conn, fact_id, scope=scope, tenant_id=tenant_id, lock=True)
    if cur is None:
        return None
    fields, changes = apply_patch(dict(cur), patch)
    where, params = _scope_where(scope, tenant_id, 10)
    row = await conn.fetchrow(
        f"""UPDATE acp_shared.facts
               SET kind = $2, title = $3, body = $4, provenance = $5, country = $6, stated_on = $7, valid_until = $8,
                   updated_by = $9, updated_at = now()
             WHERE fact_id = $1 AND {where}
         RETURNING {_COLUMNS}""",
        fact_id, fields.kind, fields.title, fields.body, fields.provenance, fields.country, fields.stated_on,
        fields.valid_until, actor, *params)
    if row is None:  # pragma: no cover - the row is locked above
        return None
    return to_api(row), changes


async def delete_fact(conn, fact_id: str, *, scope: str, tenant_id: Optional[UUID]) -> Optional[dict]:
    """Delete one fact of the scope; returns its last values (audit view) or None when there is no such fact."""
    where, params = _scope_where(scope, tenant_id, 2)
    row = await conn.fetchrow(
        f"DELETE FROM acp_shared.facts WHERE fact_id = $1 AND {where} RETURNING {_COLUMNS}", fact_id, *params)
    return None if row is None else {"fact_id": row["fact_id"], **_audit_view(row)}


__all__ = [
    "FACT_KINDS", "FactConflict", "FactError", "FactFields", "FactPatch", "MAX_TENANT_FACTS", "apply_patch",
    "create_fact", "delete_fact", "get_fact", "is_expired", "list_facts", "new_fact_id", "subject_key", "to_api",
    "update_fact",
]
