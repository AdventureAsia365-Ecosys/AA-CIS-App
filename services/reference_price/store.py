"""services.reference_price.store — AA-1091 item 5: read and write the structured reference price of the AA tours
(silver_aa_internal.tour_reference_price, CIS 401), one row per catalog key (raw_tours.source_key).

Scope: the AA master tenant's active, not deleted source rows with a catalog key (one per key: the active version of a
source group). ``live`` says whether the key's Master is published (catalog._live_tour's rule: published_tours of the AA
master tenant, master_status active, not deleted). The free text stays in raw_tours.price_raw (never written here).

Status of a key (``status_of``):
  confirmed     a saved price a person confirmed, and the source text has not changed since
  parsed        a saved price set by the parser with no doubts, nobody confirmed it yet
  needs_review  a saved price the parser had doubts about, or whose source text changed since it was set
  suggested     nothing saved, but the parser finds a price in the text (Accept saves it)
  missing       nothing saved and nothing to parse
"no price" (the page filter) = suggested + missing: no saved reference price.

The functions take a connection the caller holds (inside its own transaction for writes) and return plain dicts; the
router writes one acp_shared.audit_log row per changed key (before / after). Same status rule as the Booking contract
view catalog.v_tour_price_reference_v2 (CIS 402).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from typing import Any, Iterable, Optional

from services.reference_price import parse as rp

AA_MASTER_TENANT = "00000000-0000-0000-0000-000000000001"
TABLE = "silver_aa_internal.tour_reference_price"
STATUSES: tuple[str, ...] = ("missing", "suggested", "needs_review", "parsed", "confirmed")
FILTERS: tuple[str, ...] = ("all", "no_price") + STATUSES
ORIGINS: tuple[str, ...] = ("parsed", "staff", "import")
MAX_NOTE = 500
MAX_KEY = 64
MAX_BULK = 500


class ReferencePriceError(ValueError):
    """A write that cannot be applied: ``status`` is the HTTP status (404 unknown key, 409 stale, 422 bad value)."""

    def __init__(self, status: int, detail: str):
        super().__init__(status, detail)
        self.status = status
        self.detail = detail

    def __str__(self) -> str:
        return self.detail


SCOPE_SQL = f"""
SELECT rt.source_key, rt.tour_id::text AS tour_id, rt.src_name, rt.country, rt.duration, rt.price_raw,
       EXISTS (SELECT 1 FROM gold_aa_internal.published_tours pt
                WHERE pt.tour_id = rt.tour_id AND pt.tenant_id = rt.tenant_id
                  AND pt.master_status = 'active' AND pt.deleted_at IS NULL) AS live,
       p.amount, p.currency, p.basis, p.origin, p.needs_review, p.review_reasons, p.price_raw_seen, p.note,
       p.reviewed_by, p.reviewed_at, p.updated_by, p.updated_at
  FROM silver_aa_internal.raw_tours rt
  LEFT JOIN {TABLE} p ON p.source_key = rt.source_key
 WHERE rt.tenant_id = '{AA_MASTER_TENANT}'::uuid
   AND rt.source_status = 'active' AND rt.deleted_at IS NULL AND rt.source_key IS NOT NULL
"""  # noqa: S608 — constants only
ONE_SQL = SCOPE_SQL + " AND rt.source_key = $1"
MANY_SQL = SCOPE_SQL + " AND rt.source_key = ANY($1::text[])"

UPSERT_SQL = f"""
INSERT INTO {TABLE} (source_key, amount, currency, basis, origin, needs_review, review_reasons, price_raw_seen, note,
                     reviewed_by, reviewed_at, updated_by, updated_at)
VALUES ($1, $2, $3, $4, $5, $6, $7::text[], $8, $9, $10, CASE WHEN $10::text IS NULL THEN NULL ELSE now() END,
        $11, now())
ON CONFLICT (source_key) DO UPDATE SET
    amount = EXCLUDED.amount, currency = EXCLUDED.currency, basis = EXCLUDED.basis, origin = EXCLUDED.origin,
    needs_review = EXCLUDED.needs_review, review_reasons = EXCLUDED.review_reasons,
    price_raw_seen = EXCLUDED.price_raw_seen, note = EXCLUDED.note, reviewed_by = EXCLUDED.reviewed_by,
    reviewed_at = EXCLUDED.reviewed_at, updated_by = EXCLUDED.updated_by, updated_at = now()
"""  # noqa: S608 — constants only
DELETE_SQL = f"DELETE FROM {TABLE} WHERE source_key = $1"  # noqa: S608
LOCK_SQL = f"SELECT updated_at FROM {TABLE} WHERE source_key = $1 FOR UPDATE"  # noqa: S608


# ── Pure helpers ─────────────────────────────────────────────────────────────────────────────────────────────────

def _iso(v: Any) -> Optional[str]:
    return v.isoformat() if isinstance(v, datetime) else (None if v is None else str(v))


def saved_of(row: dict) -> Optional[dict]:
    """The saved price of a scope row, or None."""
    if row.get("amount") is None:
        return None
    return {"amount": str(Decimal(row["amount"]).quantize(Decimal("0.01"))), "currency": row["currency"],
            "basis": row["basis"], "origin": row["origin"], "note": row.get("note"),
            "needs_review": bool(row.get("needs_review")), "review_reasons": list(row.get("review_reasons") or []),
            "price_raw_seen": row.get("price_raw_seen"), "reviewed_by": row.get("reviewed_by"),
            "reviewed_at": _iso(row.get("reviewed_at")), "updated_by": row.get("updated_by"),
            "updated_at": _iso(row.get("updated_at"))}


def text_changed(row: dict) -> bool:
    """The source text moved away from the one the saved price was set against (the 402 view's IS DISTINCT FROM)."""
    return row.get("amount") is not None and row.get("price_raw_seen") != row.get("price_raw")


def status_of(row: dict, parsed: Optional[rp.Parsed] = None) -> str:
    if row.get("amount") is not None:
        if row.get("needs_review") or text_changed(row):
            return "needs_review"
        return "confirmed" if row.get("reviewed_at") is not None else "parsed"
    parsed = parsed if parsed is not None else rp.parse(row.get("price_raw"))
    return "suggested" if parsed.found else "missing"


def item_of(row: dict) -> dict:
    """The API shape of one scope row."""
    parsed = rp.parse(row.get("price_raw"))
    status = status_of(row, parsed)
    saved = saved_of(row)
    suggestion = parsed.as_dict() if parsed.found and (saved is None or text_changed(row)) else None
    return {
        "source_key": row["source_key"], "tour_id": row.get("tour_id"), "name": row.get("src_name"),
        "country": row.get("country"), "duration": row.get("duration"), "live": bool(row.get("live")),
        "price_raw": row.get("price_raw"), "status": status, "text_changed": text_changed(row),
        "saved": saved, "suggestion": suggestion,
        "parse_reasons": list(parsed.reasons),
    }


def matches(item: dict, *, status: str = "all", country: Optional[str] = None, q: Optional[str] = None,
            live_only: bool = True) -> bool:
    if live_only and not item["live"]:
        return False
    if country and (item.get("country") or "").lower() != country.lower():
        return False
    if q:
        needle = q.strip().lower()
        hay = " ".join(str(item.get(k) or "") for k in ("name", "source_key", "price_raw")).lower()
        if needle and needle not in hay:
            return False
    if status == "all":
        return True
    if status == "no_price":
        return item["status"] in ("missing", "suggested")
    return item["status"] == status


def _sort_key(item: dict) -> tuple:
    return ((item.get("country") or "~").lower(), (item.get("name") or "").lower(), item["source_key"])


def page_of(items: list[dict], *, status: str = "all", country: Optional[str] = None, q: Optional[str] = None,
            live_only: bool = True, page: int = 1, page_size: int = 50) -> dict:
    """Filter, count and page the scope items (server side: the page shows the counts and countries of the whole
    filtered set, not of one page — lesson S218)."""
    if status not in FILTERS:
        raise ReferencePriceError(422, f"status must be one of {', '.join(FILTERS)}")
    base = [i for i in items if matches(i, country=country, q=q, live_only=live_only)]
    counts = {s: 0 for s in STATUSES}
    for i in base:
        counts[i["status"]] += 1
    counts["no_price"] = counts["missing"] + counts["suggested"]
    counts["all"] = len(base)
    rows = sorted((i for i in base if matches(i, status=status, live_only=False)), key=_sort_key)
    countries = sorted({i["country"] for i in items if i.get("country") and (i["live"] or not live_only)})
    page = max(1, page)
    start = (page - 1) * page_size
    return {"items": rows[start:start + page_size], "total": len(rows), "page": page, "page_size": page_size,
            "counts": counts, "countries": countries}


@dataclass
class Values:
    """A validated reference price."""
    amount: Decimal
    currency: str
    basis: str
    note: Optional[str] = None


_BASIS_ALIASES = {
    "per_person": "per_person", "per person": "per_person", "pp": "per_person", "person": "per_person",
    "per pax": "per_person", "pax": "per_person", "/person": "per_person",
    "per_group": "per_group", "per group": "per_group", "group": "per_group", "/group": "per_group",
    "from": "from", "from (per person)": "from", "starting from": "from",
}


def basis_value(value: Any) -> Optional[str]:
    """A basis as stored (per_person | per_group | from), from the stored value or a plain-words alias; None if not."""
    if value is None:
        return None
    return _BASIS_ALIASES.get(" ".join(str(value).strip().lower().replace("-", " ").split()))


def amount_value(value: Any) -> Optional[Decimal]:
    """An amount from a number or a text ("1,450", "1.450,50"), rounded to cents; None when it is not a number."""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float, Decimal)):
        try:
            d = Decimal(str(value))
        except ArithmeticError:
            return None
    else:
        text = str(value).strip()
        for sym, _ in rp.SYMBOLS:
            text = text.replace(sym, "")
        d = rp.number(text) if text else None
    if d is None or not d.is_finite():
        return None
    return d.quantize(Decimal("0.01"))


def validate(amount: Any, currency: Any, basis: Any, note: Any = None) -> Values:
    """The values of a staff entry, or ReferencePriceError(422) naming the first bad one."""
    a = amount_value(amount)
    if a is None or a <= 0 or a > rp.MAX_AMOUNT:
        raise ReferencePriceError(422, "Amount must be a number above 0 (at most 9,999,999,999.99)")
    c = str(currency or "").strip().upper()
    c = rp.currency_of(c) or c
    if c not in rp.CODES:
        raise ReferencePriceError(
            422, f"Currency must be a 3-letter code such as USD (one of {', '.join(sorted(rp.CODES))})")
    b = basis_value(basis)
    if b is None:
        raise ReferencePriceError(422, "Basis must be per_person, per_group or from")
    n = " ".join(str(note).split()) if note is not None else None
    if n is not None and len(n) > MAX_NOTE:
        raise ReferencePriceError(422, f"Note is at most {MAX_NOTE} characters")
    return Values(amount=a, currency=c, basis=b, note=n or None)


def _key(source_key: Any) -> str:
    k = str(source_key or "").strip()
    if not k or len(k) > MAX_KEY:
        raise ReferencePriceError(404, "Tour not found")
    return k


def _raw_seen(row: dict) -> Optional[str]:
    raw = row.get("price_raw")
    return raw[: rp.RAW_MAX] if isinstance(raw, str) else None


# ── Reads ────────────────────────────────────────────────────────────────────────────────────────────────────────

async def scope_rows(conn) -> list[dict]:
    return [dict(r) for r in await conn.fetch(SCOPE_SQL)]


async def list_items(conn) -> list[dict]:
    return [item_of(r) for r in await scope_rows(conn)]


async def one_row(conn, source_key: str) -> dict:
    row = await conn.fetchrow(ONE_SQL, _key(source_key))
    if row is None:
        raise ReferencePriceError(404, "Tour not found (an active AA tour with this catalog key)")
    return dict(row)


# ── Writes (caller holds a transaction; the router audits each change) ───────────────────────────────────────────

@dataclass
class Change:
    source_key: str
    action: str            # saved | confirmed | cleared | parsed | imported
    before: Optional[dict]
    after: Optional[dict]


async def _write(conn, row: dict, *, amount: Decimal, currency: str, basis: str, origin: str, needs_review: bool,
                 reasons: Iterable[str], note: Optional[str], reviewed_by: Optional[str], actor: str) -> dict:
    await conn.execute(UPSERT_SQL, row["source_key"], Decimal(amount), currency, basis, origin, needs_review,
                       list(reasons), _raw_seen(row), note, reviewed_by, actor)
    return dict(await conn.fetchrow(ONE_SQL, row["source_key"]))


def _snapshot(row: dict) -> Optional[dict]:
    s = saved_of(row)
    if s is None:
        return None
    return {k: s[k] for k in ("amount", "currency", "basis", "origin", "note", "needs_review", "reviewed_by")}


async def save(conn, source_key: str, values: Values, *, actor: str, origin: str = "staff",
               expected_updated_at: Optional[str] = None) -> Change:
    """A staff entry (inline edit or one import row): saved as confirmed by ``actor`` against the current text."""
    row = await one_row(conn, source_key)
    current = await conn.fetchval(LOCK_SQL, row["source_key"])
    if expected_updated_at is not None and _iso(current) != expected_updated_at:
        raise ReferencePriceError(409, "Someone changed this price since you opened it: refresh and try again")
    before = _snapshot(row)
    after_row = await _write(conn, row, amount=values.amount, currency=values.currency, basis=values.basis,
                             origin=origin, needs_review=False, reasons=(), note=values.note, reviewed_by=actor,
                             actor=actor)
    return Change(row["source_key"], "imported" if origin == "import" else "saved", before, _snapshot(after_row))


async def confirm(conn, source_keys: list[str], *, actor: str) -> tuple[list[Change], list[dict]]:
    """Accept: a saved price becomes confirmed against the current text (origin kept); a suggestion is saved as a
    confirmed parse (origin parsed). Keys with nothing to accept are skipped with a reason."""
    keys = list(dict.fromkeys(_key(k) for k in source_keys))
    if not keys or len(keys) > MAX_BULK:
        raise ReferencePriceError(422, f"Give 1 to {MAX_BULK} tours")
    rows = {r["source_key"]: dict(r) for r in await conn.fetch(MANY_SQL, keys)}
    changes, skipped = [], []
    for k in keys:
        row = rows.get(k)
        if row is None:
            skipped.append({"source_key": k, "reason": "not_found"})
            continue
        await conn.fetchval(LOCK_SQL, k)
        saved = saved_of(row)
        if saved is not None:
            if status_of(row) == "confirmed":
                skipped.append({"source_key": k, "reason": "already_confirmed"})
                continue
            after = await _write(conn, row, amount=Decimal(saved["amount"]), currency=saved["currency"],
                                 basis=saved["basis"], origin=saved["origin"], needs_review=False, reasons=(),
                                 note=saved["note"], reviewed_by=actor, actor=actor)
        else:
            parsed = rp.parse(row.get("price_raw"))
            if not parsed.found:
                skipped.append({"source_key": k, "reason": "nothing_to_accept"})
                continue
            after = await _write(conn, row, amount=parsed.amount, currency=parsed.currency, basis=parsed.basis,
                                 origin="parsed", needs_review=False, reasons=(), note=None, reviewed_by=actor,
                                 actor=actor)
        changes.append(Change(k, "confirmed", _snapshot(row), _snapshot(after)))
    return changes, skipped


async def clear(conn, source_key: str) -> Change:
    row = await one_row(conn, source_key)
    if row.get("amount") is None:
        raise ReferencePriceError(404, "This tour has no saved reference price")
    await conn.execute(DELETE_SQL, row["source_key"])
    return Change(row["source_key"], "cleared", _snapshot(row), None)


def parse_plan(rows: list[dict]) -> dict:
    """What "Parse source texts" would do: a parse for every key with nothing saved whose text names a price, and a
    re-parse of an unconfirmed parse whose text changed. Confirmed prices and staff / import entries are never
    touched."""
    create, update, unparseable = [], [], 0
    for row in rows:
        saved = saved_of(row)
        if saved is not None and not (saved["origin"] == "parsed" and saved["reviewed_at"] is None
                                      and text_changed(row)):
            continue
        parsed = rp.parse(row.get("price_raw"))
        if not parsed.found:
            if row.get("price_raw") and saved is None:
                unparseable += 1
            continue
        (create if saved is None else update).append((row, parsed))
    return {"create": create, "update": update, "unparseable": unparseable}


async def parse_missing(conn, *, actor: str, dry_run: bool = True,
                        live_only: bool = False) -> tuple[dict, list[Change]]:
    rows = [r for r in await scope_rows(conn) if r.get("live") or not live_only]
    plan = parse_plan(rows)
    summary = {
        "dry_run": dry_run, "would_create" if dry_run else "created": len(plan["create"]),
        "would_update" if dry_run else "updated": len(plan["update"]), "unparseable": plan["unparseable"],
        "needs_review": sum(1 for _, p in plan["create"] + plan["update"] if p.needs_review),
        "sample": [{"source_key": r["source_key"], "name": r.get("src_name"), "price_raw": r.get("price_raw"),
                    **p.as_dict()} for r, p in (plan["create"] + plan["update"])[:20]],
    }
    changes: list[Change] = []
    if dry_run:
        return summary, changes
    for row, parsed in plan["create"] + plan["update"]:
        await conn.fetchval(LOCK_SQL, row["source_key"])
        after = await _write(conn, row, amount=parsed.amount, currency=parsed.currency, basis=parsed.basis,
                             origin="parsed", needs_review=parsed.needs_review, reasons=parsed.reasons, note=None,
                             reviewed_by=None, actor=actor)
        changes.append(Change(row["source_key"], "parsed", _snapshot(row), _snapshot(after)))
    return summary, changes


# ── Import (rows from services/reference_price/workbook.py) ───────────────────────────────────────────────────────

@dataclass
class ImportRow:
    line: int                       # the spreadsheet row number (header = 1)
    source_key: str
    amount: Any
    currency: Any
    basis: Any
    note: Any = None
    confirm: Any = None             # the optional Confirm column: "yes" confirms the row's price as it is


@dataclass
class ImportPlan:
    rows: list[dict] = field(default_factory=list)   # per line: line, source_key, action, error?, before?, after?
    errors: int = 0
    changes: int = 0
    confirm: list[str] = field(default_factory=list)  # keys whose saved price / suggestion a "yes" confirms as is

    def summary(self) -> dict:
        counts: dict[str, int] = {}
        for r in self.rows:
            counts[r["action"]] = counts.get(r["action"], 0) + 1
        return {"rows": len(self.rows), "errors": self.errors, "changes": self.changes, "actions": counts}


_YES = frozenset({"yes", "y", "x", "true", "1", "confirm", "confirmed", "ok"})
_NO = frozenset({"", "no", "n", "false", "0"})


def confirm_value(value: Any) -> bool:
    """The Confirm cell: True for yes (y, x, true, 1, ok), False when empty or no; ReferencePriceError(422) else."""
    text = "" if value is None or value is False else str(value).strip().lower()
    if value is True or text in _YES:
        return True
    if text in _NO:
        return False
    raise ReferencePriceError(422, "Confirm must be yes or empty")


def _same(saved: Optional[dict], v: Values) -> bool:
    """The line repeats the saved values (an export prefills them): it is not a staff entry, whatever the status."""
    return (saved is not None and Decimal(saved["amount"]) == v.amount and saved["currency"] == v.currency
            and saved["basis"] == v.basis and (saved["note"] or None) == v.note)


async def plan_import(conn, lines: list[ImportRow]) -> tuple[ImportPlan, dict[str, Values]]:
    """Validate every line against the scope: an unknown key, a bad value or a key twice is an error; a line with no
    amount, currency and basis is skipped. A line whose values equal the saved price is unchanged WHATEVER the status
    (the export prefills Amount / Currency / Basis / Note with the saved price, also an unreviewed parse: re-importing
    the file must not confirm prices nobody looked at, AA-1091 review); only a value that differs is a staff entry
    (saved as confirmed). Confirm = yes on such a line, or on a line left empty with a parser suggestion, confirms the
    saved price / the suggestion as it is (store.confirm, origin kept)."""
    plan, values = ImportPlan(), {}
    keys = [str(ln.source_key or "").strip() for ln in lines]
    rows = {r["source_key"]: dict(r) for r in await conn.fetch(MANY_SQL, [k for k in keys if k])}
    seen: set[str] = set()
    for ln, k in zip(lines, keys):
        entry: dict[str, Any] = {"line": ln.line, "source_key": k}
        blank = all(v is None or str(v).strip() == "" for v in (ln.amount, ln.currency, ln.basis))
        try:
            yes = confirm_value(ln.confirm)
        except ReferencePriceError as e:
            yes, bad_confirm = False, e.detail
        else:
            bad_confirm = None
        if not k:
            if blank and not yes and bad_confirm is None:
                continue
            entry.update(action="error", error="Key is empty")
        elif k in seen:
            entry.update(action="error", error="This key is on an earlier line too")
        elif k not in rows:
            entry.update(action="error", error="No active AA tour has this key")
        elif bad_confirm is not None:
            entry.update(action="error", error=bad_confirm, name=rows[k].get("src_name"))
        else:
            row = rows[k]
            entry["name"] = row.get("src_name")
            saved = saved_of(row)
            status = status_of(row)
            if blank:
                parsed = rp.parse(row.get("price_raw")) if saved is None else None
                if not yes or status == "confirmed" or (saved is None and not parsed.found):
                    entry["action"] = "unchanged" if yes and status == "confirmed" else "skip"
                else:
                    entry["before"] = _snapshot(row)
                    entry["after"] = ({k2: saved[k2] for k2 in ("amount", "currency", "basis", "note")} if saved
                                      else {"amount": str(parsed.amount), "currency": parsed.currency,
                                            "basis": parsed.basis, "note": None})
                    entry["action"] = "confirm"
                    plan.confirm.append(k)
                    plan.changes += 1
            else:
                try:
                    v = validate(ln.amount, ln.currency, ln.basis, ln.note)
                except ReferencePriceError as e:
                    entry.update(action="error", error=e.detail)
                else:
                    entry["before"] = _snapshot(row)
                    entry["after"] = {"amount": str(v.amount), "currency": v.currency, "basis": v.basis,
                                      "note": v.note}
                    if not _same(saved, v):
                        entry["action"] = "create" if saved is None else "update"
                        values[k] = v
                        plan.changes += 1
                    elif yes and status != "confirmed":
                        entry["action"] = "confirm"
                        plan.confirm.append(k)
                        plan.changes += 1
                    else:
                        entry["action"] = "unchanged"
                        entry["status"] = status   # e.g. still "parsed": shown so nobody reads it as confirmed
        if k:
            seen.add(k)
        if entry["action"] == "error":
            plan.errors += 1
        plan.rows.append(entry)
    return plan, values


async def apply_import(conn, values: dict[str, Values], *, actor: str,
                       confirm_keys: Iterable[str] = ()) -> list[Change]:
    """The staff entries of the file (origin import, confirmed), then the Confirm = yes keys (store.confirm)."""
    changes = [await save(conn, k, v, actor=actor, origin="import") for k, v in values.items()]
    keys = list(confirm_keys)
    for i in range(0, len(keys), MAX_BULK):
        done, _ = await confirm(conn, keys[i:i + MAX_BULK], actor=actor)
        changes.extend(done)
    return changes
