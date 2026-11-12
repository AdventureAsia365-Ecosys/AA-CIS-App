"""services.reference_price.workbook — AA-1091 item 5: the Excel export / import of the reference prices.

Export: one sheet "Reference prices" (a row per tour of the page's filter: key, tour, country, duration, live, source
text, status, amount, currency, basis, note, the parser's suggestion) and a sheet "How to" (what each column means and
the allowed values). The customer-service team fills Amount / Currency / Basis (and Note) and imports the same file.

Import: the first sheet; columns are found by their header (case and spaces ignored, so a re-ordered sheet works). Key,
Amount, Currency and Basis are required columns; Note is optional; every other column is ignored. Values are checked by
services/reference_price/store.py (plan_import), always as a DRY RUN first. Pure (no I/O besides openpyxl in memory).
"""
from __future__ import annotations

import io
from datetime import datetime, timezone
from typing import Any, Iterable

from openpyxl import Workbook, load_workbook
from openpyxl.styles import Font, PatternFill
from openpyxl.worksheet.datavalidation import DataValidation

from services.reference_price import parse as rp
from services.reference_price.store import ImportRow

SHEET = "Reference prices"
MAX_FILE_BYTES = 5 * 1024 * 1024          # base64 adds ~33%: the request stays under API Gateway's 10 MB
MAX_ROWS = 5000

# (header, item field or callable, width)
COLUMNS: tuple[tuple[str, int], ...] = (
    ("Key", 38), ("Tour", 48), ("Country", 14), ("Duration", 12), ("Live", 7), ("Source price text", 48),
    ("Status", 14), ("Amount", 12), ("Currency", 10), ("Basis", 12), ("Note", 32),
    ("Suggested amount", 12), ("Suggested currency", 10), ("Suggested basis", 12), ("Parser doubts", 28),
)
EDITABLE = ("Amount", "Currency", "Basis", "Note")
REQUIRED = ("key", "amount", "currency", "basis")

HOW_TO = (
    "How to use this file",
    "",
    "1. Fill Amount, Currency and Basis for each tour you want to price (Note is optional). Leave a row's three cells "
    "empty to keep that tour as it is.",
    "2. Do not change the Key column: it is how the import finds the tour. Other columns are for reading only.",
    "3. In CIS admin > AA Catalogue > Reference prices, choose Import Excel. The first run is always a DRY RUN: it "
    "lists what would change and every row with an error. Fix the errors in the file, then import again and apply.",
    "",
    "Amount: a number above 0, e.g. 1450 or 1,450.50 (no currency sign needed).",
    "Currency: a 3-letter code: " + ", ".join(sorted(rp.CODES)) + ".",
    "Basis: per_person (price for one traveller), per_group (price for the whole group or vehicle) or from (the "
    "lowest per-person price the tour starts at).",
    "",
    "A reference price is never a selling price: AA-Booking shows it to staff next to the price its cost sheet "
    "computes, and never quotes it to an agency or a traveller as a contract price.",
    "Suggested amount / currency / basis: what CIS reads in the source price text. Copy them into Amount / Currency / "
    "Basis when they are right.",
)


def _norm(header: Any) -> str:
    return "".join(str(header or "").lower().split())


def _row(item: dict) -> list:
    saved = item.get("saved") or {}
    sug = item.get("suggestion") or {}
    amount = saved.get("amount")
    return [
        item["source_key"], item.get("name"), item.get("country"), item.get("duration"),
        "yes" if item.get("live") else "no", item.get("price_raw"), item.get("status"),
        float(amount) if amount is not None else None, saved.get("currency"), saved.get("basis"), saved.get("note"),
        float(sug["amount"]) if sug.get("amount") else None, sug.get("currency"), sug.get("basis"),
        ", ".join(item.get("parse_reasons") or []) or None,
    ]


def export_bytes(items: Iterable[dict]) -> bytes:
    """The .xlsx file of these items (page filter order)."""
    wb = Workbook()
    ws = wb.active
    ws.title = SHEET
    ws.append([h for h, _ in COLUMNS])
    edit_fill = PatternFill("solid", fgColor="FFF4D6")
    for i, (h, width) in enumerate(COLUMNS, start=1):
        cell = ws.cell(row=1, column=i)
        cell.font = Font(bold=True)
        if h in EDITABLE:
            cell.fill = edit_fill
        ws.column_dimensions[cell.column_letter].width = width
    n = 0
    for item in items:
        ws.append(_row(item))
        n += 1
    ws.freeze_panes = "B2"
    last = max(n + 1, 2)
    col = {h: ws.cell(row=1, column=i).column_letter for i, (h, _) in enumerate(COLUMNS, start=1)}
    basis_dv = DataValidation(type="list", formula1='"per_person,per_group,from"', allow_blank=True)
    basis_dv.add(f"{col['Basis']}2:{col['Basis']}{last}")
    amount_dv = DataValidation(type="decimal", operator="greaterThan", formula1="0", allow_blank=True)
    amount_dv.add(f"{col['Amount']}2:{col['Amount']}{last}")
    ws.add_data_validation(basis_dv)
    ws.add_data_validation(amount_dv)
    how = wb.create_sheet("How to")
    for line in HOW_TO:
        how.append([line])
    how.column_dimensions["A"].width = 120
    how.cell(row=1, column=1).font = Font(bold=True)
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def export_filename(now: datetime | None = None) -> str:
    stamp = (now or datetime.now(timezone.utc)).strftime("%Y%m%d-%H%M")
    return f"reference-prices-{stamp}.xlsx"


class WorkbookError(ValueError):
    """The file cannot be read as a reference price sheet (HTTP 422)."""


def read_rows(content: bytes) -> list[ImportRow]:
    """The import lines of the first sheet. Raises WorkbookError for an unreadable file, a missing required column or
    too many rows."""
    if len(content) > MAX_FILE_BYTES:
        raise WorkbookError(f"The file is larger than {MAX_FILE_BYTES // (1024 * 1024)} MB")
    try:
        wb = load_workbook(io.BytesIO(content), read_only=True, data_only=True)
    except Exception as e:  # noqa: BLE001 — openpyxl raises many types for a bad file
        raise WorkbookError(f"The file is not a readable .xlsx workbook ({type(e).__name__})") from None
    try:
        ws = wb.worksheets[0]
        it = ws.iter_rows(values_only=True)
        header = next(it, None)
        if header is None:
            raise WorkbookError("The first sheet is empty")
        index = {}
        for i, h in enumerate(header):
            key = _norm(h)
            if key and key not in index:
                index[key] = i
        missing = [c for c in REQUIRED if c not in index]
        if missing:
            raise WorkbookError("The first sheet needs the columns Key, Amount, Currency and Basis (missing: "
                                + ", ".join(m.capitalize() for m in missing) + ")")
        note_i = index.get("note")
        out: list[ImportRow] = []
        for n, values in enumerate(it, start=2):
            values = tuple(values or ())

            def cell(i):
                return values[i] if i is not None and i < len(values) else None

            key = cell(index["key"])
            if all(v is None or str(v).strip() == "" for v in values):
                continue
            out.append(ImportRow(line=n, source_key=str(key).strip() if key is not None else "",
                                 amount=cell(index["amount"]), currency=cell(index["currency"]),
                                 basis=cell(index["basis"]), note=cell(note_i)))
            if len(out) > MAX_ROWS:
                raise WorkbookError(f"The file has more than {MAX_ROWS} rows; split it")
        return out
    finally:
        wb.close()
