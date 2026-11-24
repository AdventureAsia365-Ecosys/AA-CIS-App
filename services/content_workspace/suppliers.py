"""AA-1148 (S290) — the AA suppliers of AA-Booking as CIS reads them, and the name match that suggests a supplier for
an old source tour whose supplier is only the sheet's text (raw_tours.provider).

Booking owns the supplier list (booking.suppliers). It opens the read-only view ``booking.v_supply_suppliers``
(Booking bk-072, contract supply.supplier.v1: id, code, company_name, categories, country_code, status, updated_at;
never a contact, note, bank detail or price) with SELECT to cis_app. CIS reads it on its main pool and stores the
chosen id as a soft key (raw_tours.supplier_id, CIS 478). Every read fails soft: before bk-072 is applied, without the
grant or on any error the workspace says "the supplier list is not available" and nothing is linked.

The match is deliberately strict: the names equal after normalising (accents, case, punctuation and company-form words
such as "Co., Ltd" removed) and the countries do not contradict; two candidates = no suggestion. Anything else goes to
the unlinked report, where a person chooses.
"""
from __future__ import annotations

import re
import unicodedata
from typing import Any, Iterable, Optional

import structlog

logger = structlog.get_logger()

VIEW = "booking.v_supply_suppliers"
_COLS = ("id::text AS id, code, company_name, categories, country_code::text AS country_code, status, updated_at")
_LIST_SQL = f"SELECT {_COLS} FROM {VIEW}"
_BY_IDS_SQL = f"SELECT {_COLS} FROM {VIEW} WHERE id = ANY($1::uuid[])"
# Company-form words that never tell two suppliers apart (English, Vietnamese, Thai, Malay and the like).
_FORM_WORDS = frozenset({
    "co", "company", "ltd", "limited", "llc", "jsc", "inc", "corp", "corporation", "plc", "pte", "pty", "sdn", "bhd",
    "gmbh", "sa", "sarl", "the", "cty", "tnhh", "cong", "ty", "mtv", "group",
})


def _out(r: Any) -> dict:
    cc = r["country_code"]
    return {"id": r["id"], "code": r["code"], "company_name": r["company_name"],
            "categories": list(r["categories"] or []), "country_code": cc.strip() if isinstance(cc, str) else None,
            "status": r["status"],
            "updated_at": r["updated_at"].isoformat() if hasattr(r["updated_at"], "isoformat") else r["updated_at"]}


async def _fetch(pool, sql: str, *args) -> Optional[list]:
    """Rows of the view, or None when it cannot be read (not there yet, no grant, any error)."""
    try:
        # A transaction of its own: on a shared connection a failed read is a savepoint rollback, not an aborted outer
        # transaction (the same rule as the price.from.v1 reader in api/routers/v1_tours.py).
        async with pool.acquire() as conn, conn.transaction():
            return await conn.fetch(sql, *args)
    except Exception as exc:  # noqa: BLE001 — degrade to "the supplier list is not available"
        logger.warning("aa1148_supplier_view_unreadable", error=type(exc).__name__)
        return None


async def list_all(pool) -> Optional[list[dict]]:
    """Every supplier of the view (Booking has hundreds at most), or None when unreadable."""
    rows = await _fetch(pool, _LIST_SQL)
    return None if rows is None else [_out(r) for r in rows]


async def by_ids(pool, ids: Iterable[Optional[str]]) -> Optional[dict[str, dict]]:
    """{id: supplier} of the given ids (unknown ids are absent), or None when the view cannot be read."""
    wanted = sorted({i for i in ids if i})
    if not wanted:
        return {}
    rows = await _fetch(pool, _BY_IDS_SQL, wanted)
    return None if rows is None else {r["id"]: _out(r) for r in rows}


def rank(suppliers: list[dict], *, query: str = "", country: Optional[str] = None,
         include_inactive: bool = False) -> list[dict]:
    """The picker's list: matching the query (name or code, accent- and case-blind), active first, then DMCs, then the
    tour's country, then by name. Inactive suppliers only when asked (to show what a tour is linked to)."""
    q, raw = normalize(query), (query or "").strip().lower()
    out = []
    for s in suppliers:
        if s["status"] == "inactive" and not include_inactive:
            continue
        if raw and q not in normalize(s["company_name"]) and raw not in (s["code"] or "").lower():
            continue
        out.append(s)
    cc = (country or "").upper()
    order = {"active": 0, "pending": 1}
    return sorted(out, key=lambda s: (order.get(s["status"], 2), "dmc" not in s["categories"],
                                      bool(cc) and s["country_code"] != cc, s["company_name"].lower()))


def normalize(name: Optional[str]) -> str:
    """Lower case, no accents (đ -> d), punctuation as spaces, company-form words dropped, spaces collapsed."""
    if not name:
        return ""
    text = unicodedata.normalize("NFKD", name.replace("đ", "d").replace("Đ", "D"))
    text = "".join(ch for ch in text if not unicodedata.combining(ch)).lower()
    words = [w for w in re.split(r"[^a-z0-9]+", text) if w and w not in _FORM_WORDS]
    return " ".join(words)


def suggestion(provider: Optional[str], country_iso2: Optional[str], suppliers: list[dict]) -> Optional[dict]:
    """The one supplier whose normalised name equals the sheet's provider text, in the same country when both have one;
    None when there is none or more than one (a person chooses)."""
    key = normalize(provider)
    if not key:
        return None
    cc = (country_iso2 or "").upper() or None
    hits = [s for s in suppliers if s["status"] != "inactive" and normalize(s["company_name"]) == key
            and (cc is None or s["country_code"] is None or s["country_code"] == cc)]
    return hits[0] if len(hits) == 1 else None


__all__ = ["VIEW", "by_ids", "list_all", "normalize", "rank", "suggestion"]
