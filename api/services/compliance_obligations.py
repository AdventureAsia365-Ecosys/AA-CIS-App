"""AA-1174 (S288) — the Obligations tab of Platform › Compliance: the legal obligations of every law framework as a
met / not met / not yet checked checklist, and the applicability matrix (which law applies to which market and data
segment, with the test, the threshold and a primary source per verdict). Readiness, not legal advice.

Data (CIS 471): compliance.framework (kind law: citation, official source, in-force date), compliance.requirement (the
obligations of a law framework: article reference, our own short label, counsel_review, applies_as controller |
processor | both, the official source link and our own document page), compliance.requirement_mapping onto the common
controls, compliance.evidence (the newest row per check) and compliance.applicability. The law rows, their obligations
and the matrix come from the private register (scripts/compliance/load_register.py format 2); nothing here is a legal
literal.

State of an obligation (api/services/compliance_score.py obligation_state): met when its requirement is met on fresh,
passing evidence; not_checked when no active control maps to it or every mapped control has no automatic check; else
not_met (``partly`` when partially covered). Laws are never a percentage (except a law framework an administrator
marked scored, today the EU AI Act whose card predates this tab; its obligations are listed here too).

Evidence per obligation: the newest evidence row of each required check of its mapped active controls (state,
collected date, summary, a GitHub link; detail carries the document status the collector read, for example a DPA that
is still a draft) plus ``doc_url``, our own document page, so every obligation has a link even when nothing is checked
automatically. ``review_due`` on a matrix row: assessed longer ago than
cis.compliance.applicability_review_max_age_days (display only).
"""
from __future__ import annotations

from datetime import date, datetime, timezone
from typing import Any, Optional

from api.services import compliance
from api.services import compliance_score as cs
from api.services import compliance_settings as keys
from shared import settings

SQL_APPLICABILITY = """
SELECT market, segment, framework, test, threshold, verdict, source_url, source_title, assessed_on, counsel_review, note
  FROM compliance.applicability
 ORDER BY market, segment, framework
"""
VERDICTS = ("applies", "not_yet", "not_applicable")


def _iso(v: Any) -> Any:
    return v.isoformat() if isinstance(v, (datetime, date)) else v


def _evidence(check_id: str, data: dict, result: dict) -> dict:
    row = data["latest"].get(check_id) or {}
    st = result["checks"].get(check_id)
    detail = row.get("detail") if isinstance(row.get("detail"), dict) else {}
    return {"check_id": check_id, "state": st.state if st else "missing", "reason": st.reason if st else None,
            "collected_at": _iso(row.get("collected_at")), "summary": row.get("summary"), "link": row.get("link"),
            "doc_status": detail.get("doc_status")}


def build(data: dict, result: dict, applicability: list[dict], review_days: int, now: datetime) -> dict:
    """The Obligations answer from the evaluated register (pure apart from its inputs)."""
    laws = [f for f in data["frameworks"] if f["active"] and f["kind"] == "law"]
    law_ids = {f["framework"] for f in laws}
    controls = {c["control_id"]: c for c in data["controls"] if c.get("active", True)}
    by_req: dict[tuple[str, str], list[dict]] = {}
    for m in data["mappings"]:
        if m["framework"] in law_ids and m["control_id"] in controls:
            by_req.setdefault((m["framework"], m["ref"]), []).append(m)
    items = []
    for r in data["requirements"]:
        if r["framework"] not in law_ids or not r["applicable"]:
            continue
        maps = by_req.get((r["framework"], r["ref"]), [])
        ctl_states = [result["controls"].get(m["control_id"], "no_evidence") for m in maps]
        state, partly = cs.obligation_state(result["requirements"].get((r["framework"], r["ref"])), ctl_states)
        checks = sorted({cid for m in maps for cid in cs.required_checks(controls[m["control_id"]])})
        items.append({
            "framework": r["framework"], "ref": r["ref"], "section": r["section"], "title": r["title"],
            "sort_order": r["sort_order"], "state": state, "partly": partly,
            "counsel_review": bool(r.get("counsel_review")), "applies_as": r.get("applies_as"),
            "source_url": r.get("source_url"), "doc_url": r.get("doc_url"),
            "controls": [{"control_id": m["control_id"], "coverage": m["coverage"],
                          "state": result["controls"].get(m["control_id"], "no_evidence")} for m in maps],
            "evidence": [_evidence(cid, data, result) for cid in checks],
        })
    verdicts: dict[str, dict[str, int]] = {}
    rows = []
    for a in applicability:
        assessed = a["assessed_on"]
        age = (now.date() - assessed).days if isinstance(assessed, date) else None
        rows.append({k: _iso(v) for k, v in a.items()} | {"review_due": age is not None and age > review_days})
        verdicts.setdefault(a["framework"], {v: 0 for v in VERDICTS})[a["verdict"]] += 1
    out_laws = []
    for f in laws:
        mine = [i for i in items if i["framework"] == f["framework"]]
        out_laws.append({"framework": f["framework"], "title": f["title"], "jurisdiction": f.get("jurisdiction"),
                         "citation": f.get("citation"), "source_url": f.get("source_url"),
                         "in_force_on": _iso(f.get("in_force_on")), "scored": f["scored"],
                         "counts": cs.law_counts(mine),
                         "verdicts": verdicts.get(f["framework"], {v: 0 for v in VERDICTS})})
    return {"laws": out_laws, "items": items, "applicability": rows}


async def obligations(pool: Any, now: Optional[datetime] = None) -> dict:
    now = now or datetime.now(timezone.utc)
    async with pool.acquire() as conn:
        data, result, s = await compliance._evaluate(conn, now)
        applicability = [dict(r) for r in await conn.fetch(SQL_APPLICABILITY)]
    review_days = int(await settings.aget(keys.APPLICABILITY_REVIEW_MAX_AGE_DAYS))
    body = build(data, result, applicability, review_days, now)
    body.update(generated_at=now.isoformat(), environment="dev",
                settings={"applicability_review_max_age_days": review_days, **s.as_json()})
    return body
