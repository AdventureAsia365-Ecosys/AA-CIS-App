"""AA-1091 item 5 — the structured reference price: parser, status rule, filters / paging, validation, the Excel
workbook round trip and the store writes against a fake connection. Invented tour names and keys only."""
from __future__ import annotations

import io
from datetime import datetime, timezone
from decimal import Decimal

import pytest
from openpyxl import Workbook, load_workbook

from services.reference_price import parse as rp
from services.reference_price import store, workbook
from services.reference_price.store import ImportRow, ReferencePriceError, Values

D = Decimal


# ── parser ───────────────────────────────────────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("text, amount, currency, basis, reasons", [
    ("$1,722/person", "1722.00", "USD", "per_person", ()),
    ("from USD 495", "495.00", "USD", "from", ()),
    ("USD1450 pp", "1450.00", "USD", "per_person", ()),
    ("1.200 EUR pp", "1200.00", "EUR", "per_person", ()),
    ("1.200,50 EUR per person", "1200.50", "EUR", "per_person", ()),
    ("US$2,590 per group", "2590.00", "USD", "per_group", ()),
    ("THB 35,000 per vehicle", "35000.00", "THB", "per_group", ()),
    ("₩1,450,000KRW per person", "1450000.00", "KRW", "per_person", ()),
    ("Starting at 990 USD per person", "990.00", "USD", "from", ()),
    ("Price: 1,450 (all prices in USD)", "1450.00", "USD", "per_person", ("basis_assumed",)),
    ("¥35,000", "35000.00", "JPY", "per_person", ("basis_assumed",)),
    ("2-3 pax: USD 1,200; 4-6 pax: USD 980", "1200.00", "USD", "per_person",
     ("several_amounts", "tiered", "basis_assumed")),
    ("USD 900 or EUR 850 per person", "900.00", "USD", "per_person", ("several_currencies", "several_amounts")),
    ("USD 3 per person", "3.00", "USD", "per_person", ("amount_small",)),
    ("USD 800 per person, USD 2,400 per group", "800.00", "USD", "per_group", ("several_amounts", "basis_unclear")),
])
def test_parse_finds_amount_currency_basis_and_doubts(text, amount, currency, basis, reasons):
    p = rp.parse(text)
    assert (str(p.amount), p.currency, p.basis, p.reasons) == (amount, currency, basis, reasons)
    assert p.found and p.needs_review is bool(reasons)


@pytest.mark.parametrize("text, reason", [
    (None, "no_text"), ("   ", "no_text"), ("On request", "no_amount"), ("1450", "no_currency"),
    ("3 days 2 nights", "no_currency"), ("USD 0", "no_amount"),
])
def test_parse_never_invents_a_price(text, reason):
    p = rp.parse(text)
    assert not p.found and p.reasons == (reason,)
    assert p.as_dict() == {"amount": None, "currency": None, "basis": None, "reasons": [reason], "needs_review": True}


def test_number_and_currency_helpers():
    assert rp.number("1,722") == D("1722") and rp.number("1.722") == D("1722") and rp.number("12,5") == D("12.5")
    assert rp.number("x") is None
    assert rp.currency_of("usd") == "USD" and rp.currency_of("US$") == "USD" and rp.currency_of("baht") == "THB"
    assert rp.currency_of("PAX") is None
    assert rp.clean_text("  a \n b ") == "a b" and rp.clean_text("") is None


# ── status, item, filters ────────────────────────────────────────────────────────────────────────────────────────

NOW = datetime(2026, 11, 12, 8, 0, tzinfo=timezone.utc)


def _row(key="k1", *, name="Invented Ridge Trek", country="Laos", live=True, price_raw="USD 1,450 per person",
         amount=None, **saved):
    row = {"source_key": key, "tour_id": f"t-{key}", "src_name": name, "country": country, "duration": "5 days",
           "price_raw": price_raw, "live": live, "amount": amount, "currency": None, "basis": None, "origin": None,
           "needs_review": None, "review_reasons": None, "price_raw_seen": None, "note": None, "reviewed_by": None,
           "reviewed_at": None, "updated_by": None, "updated_at": None}
    if amount is not None:
        row.update({"currency": "USD", "basis": "per_person", "origin": "staff", "needs_review": False,
                    "review_reasons": [], "price_raw_seen": price_raw, "updated_by": "admin:a", "updated_at": NOW})
    row.update(saved)
    return row


def test_status_rule_matches_the_403_view():
    assert store.status_of(_row()) == "suggested"
    assert store.status_of(_row(price_raw="On request")) == "missing"
    assert store.status_of(_row(price_raw=None)) == "missing"
    assert store.status_of(_row(amount=D("1450"), reviewed_at=NOW, reviewed_by="admin:a")) == "confirmed"
    assert store.status_of(_row(amount=D("1450"), origin="parsed")) == "parsed"
    assert store.status_of(_row(amount=D("1450"), needs_review=True, reviewed_at=NOW)) == "needs_review"
    moved = _row(amount=D("1450"), reviewed_at=NOW, reviewed_by="admin:a", price_raw_seen="USD 1,300 per person")
    assert store.text_changed(moved) and store.status_of(moved) == "needs_review"


def test_item_shape_carries_the_saved_price_and_the_suggestion():
    item = store.item_of(_row())
    assert item["status"] == "suggested" and item["saved"] is None
    assert item["suggestion"]["amount"] == "1450.00" and item["name"] == "Invented Ridge Trek"
    saved = store.item_of(_row(amount=D("1450"), reviewed_at=NOW, reviewed_by="admin:a", note="Low season"))
    assert saved["suggestion"] is None
    assert saved["saved"]["amount"] == "1450.00" and saved["saved"]["note"] == "Low season"
    assert saved["saved"]["updated_at"] == NOW.isoformat()
    moved = store.item_of(_row(amount=D("1300"), reviewed_at=NOW, price_raw_seen="USD 1,300 per person"))
    assert moved["text_changed"] and moved["suggestion"]["amount"] == "1450.00"


def test_page_counts_filters_and_paging():
    items = [store.item_of(r) for r in (
        _row("a", name="Invented Alpha Loop"),
        _row("b", name="Invented Beta Walk", price_raw="On request"),
        _row("c", name="Invented Gamma Camp", amount=D("900"), reviewed_at=NOW, reviewed_by="admin:a"),
        _row("d", name="Invented Delta Day", country="Nepal", amount=D("700"), origin="parsed", needs_review=True),
        _row("e", name="Invented Echo Hill", live=False),
    )]
    page = store.page_of(items, status="no_price", page_size=1)
    assert page["total"] == 2 and len(page["items"]) == 1 and page["items"][0]["source_key"] == "a"
    assert page["counts"] == {"missing": 1, "suggested": 1, "needs_review": 1, "parsed": 0, "confirmed": 1,
                              "no_price": 2, "all": 4}
    assert page["countries"] == ["Laos", "Nepal"]
    assert store.page_of(items, status="no_price", page=2, page_size=1)["items"][0]["source_key"] == "b"
    assert [i["source_key"] for i in store.page_of(items, country="nepal")["items"]] == ["d"]
    assert [i["source_key"] for i in store.page_of(items, q="gamma")["items"]] == ["c"]
    assert store.page_of(items, live_only=False)["counts"]["all"] == 5
    with pytest.raises(ReferencePriceError) as e:
        store.page_of(items, status="bogus")
    assert e.value.status == 422


# ── validation ───────────────────────────────────────────────────────────────────────────────────────────────────

def test_validate_accepts_plain_words_and_refuses_bad_values():
    v = store.validate("1,450.5", "usd", "Per person", "  Low   season ")
    assert v == Values(amount=D("1450.50"), currency="USD", basis="per_person", note="Low season")
    assert store.validate(990, "$", "from").currency == "USD"
    assert store.validate(D("10"), "EUR", "per-group").basis == "per_group"
    for bad in [(0, "USD", "from"), ("x", "USD", "from"), (5, "XYZ", "from"), (5, "USD", "per day"),
                (5, "USD", "from", "n" * 501), (True, "USD", "from")]:
        with pytest.raises(ReferencePriceError) as e:
            store.validate(*bad)
        assert e.value.status == 422


# ── fake connection for the writes ──────────────────────────────────────────────────────────────────────────────

class FakeConn:
    """The scope rows by key; UPSERT / DELETE change the joined price columns like the real LEFT JOIN would."""

    def __init__(self, rows):
        self.rows = {r["source_key"]: dict(r) for r in rows}
        self.executed = []

    async def fetch(self, sql, *args):
        if "ANY($1::text[])" in sql:
            return [dict(self.rows[k]) for k in args[0] if k in self.rows]
        return [dict(r) for r in self.rows.values()]

    async def fetchrow(self, sql, key):
        r = self.rows.get(key)
        return dict(r) if r else None

    async def fetchval(self, sql, key):
        return self.rows[key]["updated_at"] if key in self.rows else None

    async def execute(self, sql, *args):
        self.executed.append((sql.split()[0], args))
        if sql.lstrip().startswith("INSERT"):
            (key, amount, currency, basis, origin, needs_review, reasons, seen, note, reviewed_by, actor) = args
            self.rows[key].update(amount=amount, currency=currency, basis=basis, origin=origin,
                                  needs_review=needs_review, review_reasons=reasons, price_raw_seen=seen, note=note,
                                  reviewed_by=reviewed_by, reviewed_at=NOW if reviewed_by else None,
                                  updated_by=actor, updated_at=NOW)
        elif sql.lstrip().startswith("DELETE"):
            for col in ("amount", "currency", "basis", "origin", "needs_review", "review_reasons", "price_raw_seen",
                        "note", "reviewed_by", "reviewed_at", "updated_by", "updated_at"):
                self.rows[args[0]][col] = None


@pytest.mark.asyncio
async def test_save_confirms_against_the_current_text_and_checks_staleness():
    conn = FakeConn([_row("k1")])
    change = await store.save(conn, "k1", store.validate(1500, "USD", "from"), actor="admin:a")
    assert change.action == "saved" and change.before is None
    assert change.after["amount"] == "1500.00" and change.after["reviewed_by"] == "admin:a"
    assert conn.rows["k1"]["price_raw_seen"] == "USD 1,450 per person"
    assert store.status_of(conn.rows["k1"]) == "confirmed"
    with pytest.raises(ReferencePriceError) as e:
        await store.save(conn, "k1", store.validate(1, "USD", "from"), actor="admin:b",
                         expected_updated_at="2020-01-01T00:00:00+00:00")
    assert e.value.status == 409
    ok = await store.save(conn, "k1", store.validate(1600, "USD", "from"), actor="admin:b",
                          expected_updated_at=NOW.isoformat())
    assert ok.before["amount"] == "1500.00" and ok.after["amount"] == "1600.00"
    with pytest.raises(ReferencePriceError) as e:
        await store.save(conn, "nope", store.validate(1, "USD", "from"), actor="admin:a")
    assert e.value.status == 404


@pytest.mark.asyncio
async def test_confirm_accepts_suggestions_and_saved_prices_and_skips_the_rest():
    conn = FakeConn([
        _row("s"),                                                            # suggestion
        _row("m", price_raw="On request"),                                    # nothing to accept
        _row("p", amount=D("700"), origin="parsed", needs_review=True),       # parsed with doubts
        _row("c", amount=D("900"), reviewed_at=NOW, reviewed_by="admin:z"),   # already confirmed
    ])
    changes, skipped = await store.confirm(conn, ["s", "m", "p", "c", "x", "s"], actor="admin:a")
    assert [c.source_key for c in changes] == ["s", "p"]
    assert conn.rows["s"]["origin"] == "parsed" and conn.rows["s"]["reviewed_by"] == "admin:a"
    assert conn.rows["p"]["needs_review"] is False and store.status_of(conn.rows["p"]) == "confirmed"
    assert {s["source_key"]: s["reason"] for s in skipped} == {
        "m": "nothing_to_accept", "c": "already_confirmed", "x": "not_found"}
    with pytest.raises(ReferencePriceError):
        await store.confirm(conn, [], actor="admin:a")


@pytest.mark.asyncio
async def test_clear_removes_the_saved_price_only():
    conn = FakeConn([_row("k", amount=D("900"), reviewed_at=NOW, reviewed_by="admin:a"), _row("n")])
    change = await store.clear(conn, "k")
    assert change.action == "cleared" and change.before["amount"] == "900.00" and change.after is None
    assert conn.rows["k"]["amount"] is None and conn.rows["k"]["price_raw"] == "USD 1,450 per person"
    with pytest.raises(ReferencePriceError) as e:
        await store.clear(conn, "n")
    assert e.value.status == 404


@pytest.mark.asyncio
async def test_parse_missing_dry_run_then_apply_never_touches_confirmed_or_staff_prices():
    conn = FakeConn([
        _row("new"),
        _row("doubt", price_raw="Adult $500, child $250"),
        _row("none", price_raw="On request"),
        _row("staff", amount=D("900"), origin="staff", price_raw_seen="older text"),        # staff entry, text moved
        _row("stale", amount=D("700"), origin="parsed", price_raw_seen="USD 700 per person"),  # unconfirmed parse
        _row("conf", amount=D("800"), origin="parsed", reviewed_at=NOW, reviewed_by="admin:z",
             price_raw_seen="older text"),
    ])
    summary, changes = await store.parse_missing(conn, actor="admin:a", dry_run=True)
    assert summary["would_create"] == 2 and summary["would_update"] == 1 and summary["unparseable"] == 1
    assert summary["needs_review"] == 1 and not changes and not conn.executed
    summary, changes = await store.parse_missing(conn, actor="admin:a", dry_run=False)
    assert summary["created"] == 2 and summary["updated"] == 1
    assert sorted(c.source_key for c in changes) == ["doubt", "new", "stale"]
    assert conn.rows["doubt"]["needs_review"] is True and "several_amounts" in conn.rows["doubt"]["review_reasons"]
    assert conn.rows["stale"]["amount"] == D("1450.00") and conn.rows["stale"]["reviewed_by"] is None
    assert conn.rows["staff"]["amount"] == D("900") and conn.rows["conf"]["amount"] == D("800")


@pytest.mark.asyncio
async def test_import_plan_and_apply():
    conn = FakeConn([_row("a"), _row("b"), _row("c", amount=D("900"), reviewed_at=NOW, reviewed_by="admin:z",
                                                note="Kept")])
    lines = [
        ImportRow(2, "a", "1,450", "usd", "per person", "High season"),
        ImportRow(3, "b", None, None, None),                 # skip
        ImportRow(4, "c", 900, "USD", "per_person", "Kept"),  # unchanged
        ImportRow(5, "zz", 10, "USD", "from"),               # unknown key
        ImportRow(6, "a", 10, "USD", "from"),                # duplicate
        ImportRow(7, "b", "abc", "USD", "from"),             # bad amount
        ImportRow(8, "", None, None, None),                  # empty line
    ]
    plan, values = await store.plan_import(conn, lines)
    assert [r["action"] for r in plan.rows] == ["create", "skip", "unchanged", "error", "error", "error"]
    assert plan.summary() == {"rows": 6, "errors": 3, "changes": 1,
                              "actions": {"create": 1, "skip": 1, "unchanged": 1, "error": 3}}
    assert plan.rows[3]["error"].startswith("No active AA tour")
    assert list(values) == ["a"] and values["a"].amount == D("1450.00") and values["a"].note == "High season"
    applied = await store.apply_import(conn, values, actor="admin:a")
    assert [c.action for c in applied] == ["imported"] and conn.rows["a"]["origin"] == "import"
    assert store.status_of(conn.rows["a"]) == "confirmed"


@pytest.mark.asyncio
async def test_export_then_import_unchanged_confirms_nothing():
    """AA-1091 review: the export prefills Amount / Currency / Basis / Note with every saved price, also an unreviewed
    parse (parsed, needs_review with doubts, a confirmed price whose text moved). Re-importing the same file must
    give 0 changes and leave every status as it was."""
    rows = [
        _row("parsed", amount=D("1450"), origin="parsed"),
        _row("doubt", price_raw="2-3 pax: USD 1,200; 4-6 pax: USD 980", amount=D("1200"), origin="parsed",
             needs_review=True, review_reasons=["several_amounts", "tiered"]),
        _row("moved", amount=D("900"), reviewed_at=NOW, reviewed_by="admin:z", price_raw_seen="older text"),
        _row("conf", amount=D("800"), reviewed_at=NOW, reviewed_by="admin:z", note="Kept"),
        _row("sugg"), _row("none", price_raw="On request"),
    ]
    conn = FakeConn(rows)
    before = {k: store.status_of(r) for k, r in conn.rows.items()}
    content = workbook.export_bytes([store.item_of(r) for r in rows])
    plan, values = await store.plan_import(conn, workbook.read_rows(content))
    assert plan.changes == 0 and plan.errors == 0 and values == {} and plan.confirm == []
    assert {r["source_key"]: r["action"] for r in plan.rows} == {
        "parsed": "unchanged", "doubt": "unchanged", "moved": "unchanged", "conf": "unchanged",
        "sugg": "skip", "none": "skip"}
    assert {r["source_key"]: r.get("status") for r in plan.rows if r["action"] == "unchanged"} == {
        "parsed": "parsed", "doubt": "needs_review", "moved": "needs_review", "conf": "confirmed"}
    assert await store.apply_import(conn, values, actor="admin:a", confirm_keys=plan.confirm) == []
    assert not conn.executed and {k: store.status_of(r) for k, r in conn.rows.items()} == before


@pytest.mark.asyncio
async def test_import_confirm_column_confirms_as_is_and_a_changed_value_is_a_staff_entry():
    conn = FakeConn([
        _row("p", amount=D("1450"), origin="parsed"),                       # yes on the prefilled row: confirm
        _row("q", amount=D("700"), origin="parsed", needs_review=True),     # yes on an emptied row: confirm saved
        _row("s"),                                                          # yes with no saved price: suggestion
        _row("m", price_raw="On request"),                                  # yes, nothing to confirm: skip
        _row("c", amount=D("800"), reviewed_at=NOW, reviewed_by="admin:z"),  # yes, already confirmed: unchanged
        _row("e", amount=D("600"), origin="parsed"),                        # a changed value: staff entry
        _row("b"),                                                          # a bad Confirm value
    ])
    lines = [
        ImportRow(2, "p", 1450, "USD", "per_person", None, "Yes"),
        ImportRow(3, "q", None, None, None, None, "x"),
        ImportRow(4, "s", None, None, None, None, "yes"),
        ImportRow(5, "m", None, None, None, None, "yes"),
        ImportRow(6, "c", 800, "USD", "per_person", None, "yes"),
        ImportRow(7, "e", 650, "USD", "per_person", None, None),
        ImportRow(8, "b", None, None, None, None, "maybe"),
    ]
    plan, values = await store.plan_import(conn, lines)
    assert [r["action"] for r in plan.rows] == ["confirm", "confirm", "confirm", "skip", "unchanged", "update",
                                               "error"]
    assert plan.rows[6]["error"] == "Confirm must be yes or empty" and plan.errors == 1
    assert plan.confirm == ["p", "q", "s"] and list(values) == ["e"] and plan.changes == 4
    assert plan.rows[2]["after"] == {"amount": "1450.00", "currency": "USD", "basis": "per_person", "note": None}
    applied = await store.apply_import(conn, values, actor="admin:a", confirm_keys=plan.confirm)
    assert [(c.source_key, c.action) for c in applied] == [("e", "imported"), ("p", "confirmed"), ("q", "confirmed"),
                                                           ("s", "confirmed")]
    assert all(store.status_of(conn.rows[k]) == "confirmed" for k in "pqse")
    assert conn.rows["p"]["origin"] == "parsed" and conn.rows["e"]["origin"] == "import"
    assert store.status_of(conn.rows["m"]) == "missing"


def test_confirm_value():
    for v in ("yes", " YES ", "y", "x", "true", "1", "ok", True):
        assert store.confirm_value(v) is True
    for v in (None, "", "  ", "no", "N", False, 0):
        assert store.confirm_value(v) is False
    with pytest.raises(ReferencePriceError):
        store.confirm_value("maybe")


def test_export_writes_text_never_a_formula():
    """AA-1091 review: openpyxl stores a str starting with "=" as a formula; a source text, a tour name or a note such
    as =HYPERLINK(...) must reach the file as text (and come back unchanged on import)."""
    item = store.item_of(_row("k", name="=HYPERLINK(\"http://invented.example\",\"x\")", price_raw="=1+1",
                              amount=D("10"), reviewed_at=NOW, reviewed_by="admin:a", note="=2+2"))
    content = workbook.export_bytes([item])
    ws = load_workbook(io.BytesIO(content))["Reference prices"]
    cells = {ws.cell(row=1, column=i).value: ws.cell(row=2, column=i) for i in range(1, ws.max_column + 1)}
    for h in ("Tour", "Source price text", "Note"):
        assert cells[h].data_type == "s", h
    assert cells["Source price text"].value == "=1+1"
    assert workbook.read_rows(content)[0].note == "=2+2"


# ── workbook ─────────────────────────────────────────────────────────────────────────────────────────────────────

def test_export_then_read_round_trip():
    items = [store.item_of(_row("a")),
             store.item_of(_row("b", amount=D("900"), reviewed_at=NOW, reviewed_by="admin:a", note="Kept"))]
    content = workbook.export_bytes(items)
    wb = load_workbook(io.BytesIO(content))
    assert wb.sheetnames == ["Reference prices", "How to"]
    ws = wb["Reference prices"]
    header = [c.value for c in ws[1]]
    assert header[:11] == ["Key", "Tour", "Country", "Duration", "Live", "Source price text", "Status", "Amount",
                           "Currency", "Basis", "Note"]
    assert header[11] == "Confirm" and ws.cell(row=2, column=12).value is None   # never prefilled
    assert ws.cell(row=2, column=13).value == 1450.0                     # suggested amount of "a"
    lines = workbook.read_rows(content)
    assert [(ln.line, ln.source_key, ln.amount, ln.currency, ln.basis, ln.note, ln.confirm) for ln in lines] == [
        (2, "a", None, None, None, None, None), (3, "b", 900, "USD", "per_person", "Kept", None)]
    assert workbook.export_filename(NOW) == "reference-prices-20261112-0800.xlsx"


def _xlsx(rows):
    wb = Workbook()
    for r in rows:
        wb.active.append(r)
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def test_read_rows_finds_columns_by_header_and_refuses_bad_files():
    content = _xlsx([["basis", " AMOUNT ", "Currency", "key", "Other"], ["from", 10, "USD", "k1", "x"],
                     [None, None, None, None, None]])
    lines = workbook.read_rows(content)
    assert len(lines) == 1 and (lines[0].source_key, lines[0].amount, lines[0].basis, lines[0].note) == \
        ("k1", 10, "from", None)
    with pytest.raises(workbook.WorkbookError, match="missing: Basis"):
        workbook.read_rows(_xlsx([["Key", "Amount", "Currency"], ["k", 1, "USD"]]))
    with pytest.raises(workbook.WorkbookError, match="not a readable"):
        workbook.read_rows(b"not a workbook")
    with pytest.raises(workbook.WorkbookError, match="empty"):
        workbook.read_rows(_xlsx([]))
    with pytest.raises(workbook.WorkbookError, match="larger"):
        workbook.read_rows(b"x" * (workbook.MAX_FILE_BYTES + 1))
