"""AA-917 WM-11 — the asynchronous fact filter of writing signals (app ADR 0010 decision 3, audit §5.2 graft 7).

A human edit can add a promise the source lacks (a tenant writes "lunch included"); learning from it would teach a
writer to invent facts. The filter runs in a job (`writing_signal_fact_filter`), never on the request path, and sets
`shared.writing_signal.fact_status` of each `pending` signal:

  fail  the edit adds a specific neither the text before nor the source gives (a number / time / price —
        `repair_contract.new_specifics` —, or a service / inclusion promise word), or the admin said
        `fact_not_in_source`: quarantined (`adds_unsourced_claim`), never mined;
  grey  the edit adds a capitalised name (place, operator) the source does not have: ambiguous, waits for an admin
        confirmation (AA-803 is not calibrated, so no Jev call here);
  pass  everything else: a cut, a pure style edit, a reason-only signal (reject / dismiss / regenerate).

`classify` is pure (unit-tested); `load_source` reads the source the writer had for the signal's object, through
`cis_app`, always by the object's id (one tenant's row).
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Optional
from uuid import UUID

from services.quality.repair_contract import new_names, new_specifics
from services.writing_memory.reason_codes import FACT_NOT_IN_SOURCE

PASS, GREY, FAIL, PENDING = "pass", "grey", "fail", "pending"
ADDS_UNSOURCED_CLAIM = "adds_unsourced_claim"
ADMIN_FACT_NOT_IN_SOURCE = "admin_fact_not_in_source"
ADDS_NAME = "adds_name"

# Words that turn a mention into a promise of a service or an inclusion ("lunch" -> "lunch included"). Added by an
# edit and absent from the text before and from the source, they fail the signal. "including" is ordinary prose
# ("temples, including …") and is not here.
PROMISE_WORDS = (
    "included", "includes", "inclusive", "all-inclusive", "free", "complimentary", "guaranteed", "guarantee",
    "private", "pick-up", "pickup", "drop-off", "dropoff", "transfer", "transfers", "refundable", "refund",
    "upgrade", "upgraded", "insurance", "entrance fees", "tickets included", "no extra cost", "at no cost",
)
_PROMISE_RES = tuple((w, re.compile(r"(?<![a-z])" + re.escape(w) + r"(?![a-z])", re.I)) for w in PROMISE_WORDS)


@dataclass(frozen=True)
class FactVerdict:
    status: str
    reasons: list[str] = field(default_factory=list)
    code: Optional[str] = None


def added_promises(before: str, after: str, source: str = "") -> list[str]:
    """Promise words `after` has that neither `before` nor `source` contains."""
    known = f"{before or ''}\n{source or ''}"
    return [w for w, rx in _PROMISE_RES if rx.search(after or "") and not rx.search(known)]


def classify(before: Optional[str], after: Optional[str], source: Optional[str] = "",
             reason_code: Optional[str] = None) -> FactVerdict:
    """The fact status of one signal (see the module docstring). Deterministic, no model call."""
    if reason_code == FACT_NOT_IN_SOURCE:
        return FactVerdict(FAIL, ["the admin marked it fact_not_in_source"], ADMIN_FACT_NOT_IN_SOURCE)
    if not (after or "").strip():
        return FactVerdict(PASS)          # a cut, or a reason-only signal: nothing added
    b, src = before or "", source or ""
    added = new_specifics(b, after, source=src) + [f"adds the promise '{w}', which neither the text nor the source "
                                                   f"makes" for w in added_promises(b, after, src)]
    if added:
        return FactVerdict(FAIL, added, ADDS_UNSOURCED_CLAIM)
    names = new_names(b, after, src)
    if names:
        return FactVerdict(GREY, [f"adds the name(s) {', '.join(names[:5])}, not in the text or the source"],
                           ADDS_NAME)
    return FactVerdict(PASS)


# ── the source of a signal's object ─────────────────────────────────────────────────────────────────────────────────

_PIECE_SQL = "SELECT writer_input FROM acp_shared.content_piece WHERE piece_id = $1::uuid"
_VERSION_SQL = """
    SELECT pt.aa_name, pt.aa_subtitle, pt.aa_summary, pt.aa_description, pt.aa_highlights, pt.aa_itineraries,
           pt.seo_title, pt.seo_meta, rt.country, rt.duration
      FROM gold_aa_internal.tenant_tour_versions v
      JOIN gold_aa_internal.published_tours pt ON pt.id = v.published_tour_id
      LEFT JOIN silver_aa_internal.raw_tours rt ON rt.tour_id = pt.tour_id
     WHERE v.id = $1::uuid
"""
_RAW_SQL = """
    SELECT rt.src_name, rt.src_subtitle, rt.src_summary, rt.src_description, rt.src_highlights, rt.src_itineraries,
           rt.country, rt.duration, rt.price_raw, rt.inclusions, rt.exclusions
      FROM silver_aa_internal.generated_content gc
      JOIN silver_aa_internal.raw_tours rt ON rt.tour_id = gc.tour_id
     WHERE gc.id = $1::uuid
"""


def _jsonish(value):
    if isinstance(value, str):
        try:
            return json.loads(value)
        except ValueError:
            return value
    return value


async def load_source(conn, source_table: str, source_id: str) -> str:
    """The source text the writer had for the edited object ('' when unknown: then a number or promise counts as new
    unless the text before had it)."""
    from services.content_generation.grounding import source_text
    try:
        source_id = str(UUID(str(source_id)))
    except (ValueError, TypeError):
        return ""                         # a source_id that is not a uuid: no source
    if source_table == "content_piece":
        wi = _jsonish(await conn.fetchval(_PIECE_SQL, source_id))
        if not isinstance(wi, dict) or not wi:
            return ""
        from services.acp_content_writing.service import grounding_from_writer_input
        return grounding_from_writer_input(wi)
    if source_table == "tenant_tour_version":
        row = await conn.fetchrow(_VERSION_SQL, source_id)
        if row is None:
            return ""
        from services.jobs.t2_rewrite_job import source_tour_dict
        return source_text(source_tour_dict(dict(row)))
    if source_table == "generated_content":
        row = await conn.fetchrow(_RAW_SQL, source_id)
        if row is None:
            return ""
        r = dict(row)
        return source_text({"name": r["src_name"], "subtitle": r["src_subtitle"], "summary": r["src_summary"],
                            "description": r["src_description"], "highlights": _jsonish(r["src_highlights"]),
                            "itineraries": r["src_itineraries"], "country": r["country"],
                            "duration": r["duration"], "price": r["price_raw"], "inclusions": r["inclusions"],
                            "exclusions": r["exclusions"]})
    return ""




# ── the job body ────────────────────────────────────────────────────────────────────────────────────────────────────

_PENDING_SQL = """
    SELECT signal_id::text AS signal_id, before_text, after_text, reason_code, source_table, source_id
      FROM shared.writing_signal
     WHERE fact_status = 'pending'
     ORDER BY created_at, signal_id
     LIMIT $1
"""
_SET_SQL = "UPDATE shared.writing_signal SET fact_status = $2 WHERE signal_id = $1::uuid AND fact_status = 'pending'"
_STATUS_COUNTS_SQL = "SELECT fact_status, count(*) AS n FROM shared.writing_signal GROUP BY 1"


async def run_fact_filter(pool, *, limit: int = 5000) -> dict:
    """Classify up to `limit` pending signals (oldest first). Returns the verdicts of this run (status, code) and the
    table's counts per status after it. A signal changed meanwhile (an admin set it) is left as it is."""
    from collections import Counter
    run: Counter = Counter()
    codes: Counter = Counter()
    sources: dict[tuple[str, str], str] = {}
    async with pool.acquire() as conn:
        rows = [dict(r) for r in await conn.fetch(_PENDING_SQL, limit)]
        for r in rows:
            key = (r["source_table"], r["source_id"])
            if key not in sources:
                sources[key] = await load_source(conn, *key)
            v = classify(r["before_text"], r["after_text"], sources[key], r["reason_code"])
            await conn.execute(_SET_SQL, r["signal_id"], v.status)
            run[v.status] += 1
            if v.code:
                codes[v.code] += 1
        totals = {r["fact_status"]: int(r["n"]) for r in await conn.fetch(_STATUS_COUNTS_SQL)}
    return {"checked": len(rows), "verdicts": dict(run), "codes": dict(codes), "totals": totals,
            "more_pending": len(rows) >= limit}


__all__ = ["ADDS_NAME", "ADDS_UNSOURCED_CLAIM", "FAIL", "FactVerdict", "GREY", "PASS", "PENDING", "PROMISE_WORDS",
           "added_promises", "classify", "load_source", "run_fact_filter"]
