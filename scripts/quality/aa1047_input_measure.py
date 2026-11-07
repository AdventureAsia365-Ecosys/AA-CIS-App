"""AA-1047 (Quality W1, root ADR 0020) — the W1 input measurements, read-only, $0, aggregates only.

The audit's W1 proof (root docs/audits/2026-11-06-S271-writing-quality-audit/README.md, W1) asks for four numbers
about the inputs every writer and check rests on, measured before any gate is tuned:

  anchors   share of unanchored atoms: each active platform atom's evidence (else its label) against its tour's raw
            source (factsheet.anchor_check; critic fix F10: an unanchored atom is never the sole support of a PASS);
  pointers  pointer failures: s1_facts.verify_pointers over the tour FactSheet of every active raw source (bar: 0);
  excluded  excluded facts per object: per stored T9 piece, the facts the facts budget leaves out for its tenant and
            country today (reason facts_budget) and the lines a reader capped at 16000 characters (the eval judge's cut)
            would drop (FactSheet.render_budgeted, reason reader_budget); per S1 tour, figures over the FACT SHEET cap;
  drift     the master_drift census: s1_checks.run_checks of every active Master against its raw source; a Master is
            blocking when a finding of a fact class (number, name, inclusion, day structure) remains and the version
            is not human-edited (a human-edited Master is a source, provenance human_verified). Reports the blocking
            rate (the W1 bar: tune the provenance rules before W3 when it is above 15%).

Output: one JSON object on stdout with counts, shares and distributions; ids only where a follow-up needs them (tour
ids of pointer failures, at most 20); never a text column, never a personal column. Database errors print their class
only. Read-only: SELECTs on CIS schemas through the container's own DATABASE_URL (cis_app).

Run (S3-mediated ECS exec, root runbook docs/ops/runbooks/ecs-exec-s3-mediated.md), from the root repo:
  tools/ecs/ecsx.sh apps/AA-CIS-App/scripts/quality/aa1047_input_measure.py MEASURE=all PIECE_DAYS=60
MEASURE = all | anchors | pointers | excluded | drift; LIMIT = rows per pass (a trial). Needs the code of this PR on
the api task (it imports services.quality.factsheet from /app).
"""
from __future__ import annotations

import asyncio
import json
import os
import statistics
import sys
from collections import Counter
from typing import Iterable, Optional

if os.path.isdir("/app") and "/app" not in sys.path:   # inside the api container (ECS exec)
    sys.path.insert(0, "/app")

READER_CAP = 16000          # the eval judge's seed cut (AA-900 RC2); the first reader render_budgeted serves
MAX_IDS = 20
FACT_CLASSES = ("number", "name", "inclusion", "day_structure")   # s1_checks CLASS_* that change a fact

RAW_COLS = ("rt.tour_id, rt.src_name, rt.src_subtitle, rt.src_summary, rt.src_description, "
            "rt.src_highlights::text AS src_highlights, rt.src_itineraries, rt.country, rt.duration, rt.price_raw, "
            "rt.inclusions, rt.exclusions, rt.group_size, rt.best_time_to_go")
ACTIVE_SOURCES_SQL = ("SELECT " + RAW_COLS + " FROM silver_aa_internal.raw_tours rt "
                      "WHERE rt.source_status = 'active' AND rt.deleted_at IS NULL ORDER BY rt.tour_id")
RAW_FOR_TOURS_SQL = ("SELECT " + RAW_COLS + " FROM silver_aa_internal.raw_tours rt "
                     "WHERE rt.tour_id = ANY($1::uuid[]) ORDER BY rt.tour_id")
ATOMS_SQL = ("SELECT a.atom_id, a.tour_id, a.text, a.evidence FROM acp_contract.v_active_tour_atoms a "
             "WHERE a.owner_scope = 'platform' AND NOT a.deleted AND NOT a.is_empty_marker ORDER BY a.atom_id")
MASTERS_SQL = ("SELECT pt.id AS pt_id, pt.aa_name, pt.aa_subtitle, pt.aa_summary, pt.aa_description, "
               "pt.aa_highlights::text AS aa_highlights, pt.aa_itineraries, pt.seo_title, pt.seo_meta, "
               "coalesce(gc.human_edited, false) AS human_edited, " + RAW_COLS + " "
               "FROM gold_aa_internal.published_tours pt "
               "JOIN silver_aa_internal.raw_tours rt ON rt.tour_id = pt.tour_id "
               "LEFT JOIN silver_aa_internal.generated_content gc ON gc.id = pt.generated_content_id "
               "WHERE pt.master_status = 'active' AND pt.deleted_at IS NULL ORDER BY pt.id")
PIECES_SQL = ("SELECT cp.piece_id, cp.tenant_id, cp.writer_input::text AS writer_input "
              "FROM acp_shared.content_piece cp WHERE cp.writer_input IS NOT NULL "
              "AND cp.created_at >= now() - make_interval(days => $1) ORDER BY cp.piece_id")


# ------------------------------------------------------------------ pure measurements (unit tested)


def distribution(values: Iterable[float]) -> dict:
    vals = sorted(values)
    if not vals:
        return {"n": 0}
    return {"n": len(vals), "with_any": sum(1 for v in vals if v), "mean": round(statistics.fmean(vals), 3),
            "p50": vals[len(vals) // 2], "p90": vals[min(len(vals) - 1, (len(vals) * 9) // 10)], "max": vals[-1]}


def _json_list(value) -> list:
    if isinstance(value, list):
        return value
    if isinstance(value, str) and value.strip():
        try:
            parsed = json.loads(value)
        except ValueError:
            return [value]
        return parsed if isinstance(parsed, list) else [value]
    return []


def measure_anchors(atoms: Iterable[dict], raw_rows: Iterable[dict]) -> dict:
    """atoms: {atom_id, tour_id, text, evidence}; raw_rows: raw_tours source rows of their tours."""
    from services.quality import factsheet as fs

    index = {}
    for row in raw_rows:
        tour, agency = fs.raw_source_tour(row)
        index[str(row["tour_id"])] = fs.RawIndex(fs.raw_source_text(tour, agency))
    counts: Counter = Counter()
    per_tour: dict[str, list[int]] = {}
    for a in atoms:
        raw = index.get(str(a["tour_id"]))
        verdict = fs.anchor_check(a.get("evidence"), a.get("text"), raw) if raw is not None else None
        key = "unknown" if verdict is None else "anchored" if verdict else "unanchored"
        counts[key] += 1
        if verdict is not None:
            per_tour.setdefault(str(a["tour_id"]), []).append(0 if verdict else 1)
        if not (a.get("evidence") or "").strip():
            counts["no_evidence"] += 1
    checked = counts["anchored"] + counts["unanchored"]
    shares = [sum(v) / len(v) for v in per_tour.values() if v]
    return {"atoms": sum(counts[k] for k in ("anchored", "unanchored", "unknown")), "checked": checked,
            "anchored": counts["anchored"], "unanchored": counts["unanchored"], "unknown_no_raw": counts["unknown"],
            "no_evidence": counts["no_evidence"],
            "unanchored_share": round(counts["unanchored"] / checked, 4) if checked else None,
            "tours": len(per_tour),
            "unanchored_share_per_tour": {k: round(v, 3) if isinstance(v, float) else v
                                          for k, v in distribution(shares).items()},
            "rule": {"min_share": fs.ANCHOR_MIN_SHARE, "min_words": fs.ANCHOR_MIN_WORDS}}


def measure_pointers(raw_rows: Iterable[dict]) -> tuple[dict, dict]:
    """(pointer report, S1 figures-cap exclusions per tour) over the tour FactSheets of the raw sources."""
    from services.quality import factsheet as fs

    sources = 0
    failing: list[str] = []
    by_section: Counter = Counter()
    total = 0
    fig_caps: list[int] = []
    errors: Counter = Counter()
    for row in raw_rows:
        sources += 1
        tour, agency = fs.raw_source_tour(row)
        try:
            sheet = fs.build_tour_factsheet(tour, agency=agency)
        except Exception as e:  # noqa: BLE001 — a builder crash is a finding, counted by class
            errors[type(e).__name__] += 1
            continue
        fails = list(sheet.pointer_failures)
        total += len(fails)
        if fails:
            failing.append(str(row["tour_id"]))
            by_section.update(f["section"] for f in fails)
        fig_caps.append(sum(1 for e in sheet.excluded if e["reason"] == fs.EXCLUDED_FIGURES_CAP))
    report = {"active_sources": sources, "sources_with_failures": len(failing), "pointer_failures": total,
              "by_section": dict(sorted(by_section.items())), "failing_tour_ids": failing[:MAX_IDS],
              "builder_errors": dict(errors), "bar_met": total == 0 and not errors}
    return report, {"s1_figures_cap_per_tour": distribution(fig_caps)}


def piece_sheet(writer_input: dict, fact_scopes: Optional[dict] = None, excluded_facts=()):
    """The T9 FactSheet of a stored piece, rebuilt from its writer_input exactly as grounding_from_writer_input."""
    from services.quality import factsheet as fs

    wi = writer_input or {}
    return fs.build_route_factsheet(seed=wi.get("seed") or "", facts_text=wi.get("facts"),
                                    brand_audience=wi.get("brand_pack"),
                                    trip_name=wi["writer_trip"] if "writer_trip" in wi else wi.get("trip"),
                                    destination=wi.get("destination"), fact_scopes=fact_scopes,
                                    excluded_facts=excluded_facts)


def measure_excluded(pieces: Iterable[dict], facts_left_out: dict) -> dict:
    """pieces: {piece_id, tenant_id, writer_input (dict)}; facts_left_out: (tenant_id, destination) -> number of facts
    the budget leaves out today."""
    budget_drops, reader_drops, channels = [], [], Counter()
    reader_kinds: Counter = Counter()
    over_cap = 0
    errors: Counter = Counter()
    for p in pieces:
        wi = p["writer_input"] or {}
        try:
            sheet = piece_sheet(wi)
            _text, excluded = sheet.render_budgeted(READER_CAP)
        except Exception as e:  # noqa: BLE001
            errors[type(e).__name__] += 1
            continue
        channels[wi.get("channel") or "unknown"] += 1
        budget_drops.append(facts_left_out.get((str(p["tenant_id"]), wi.get("destination")), 0))
        dropped = [e for e in excluded if e["reason"] != "over_cap"]
        reader_drops.append(len(dropped))
        reader_kinds.update(e["kind"] for e in dropped)
        over_cap += any(e["reason"] == "over_cap" for e in excluded)
    return {"pieces": len(budget_drops), "channels": dict(channels),
            "facts_budget_per_piece": distribution(budget_drops),
            "reader_budget_per_piece": {**distribution(reader_drops), "cap_chars": READER_CAP,
                                        "by_kind": dict(reader_kinds), "still_over_cap": over_cap},
            "rebuild_errors": dict(errors)}


def master_output(row: dict) -> dict:
    return {"name": row.get("aa_name"), "subtitle": row.get("aa_subtitle"), "summary": row.get("aa_summary"),
            "description": row.get("aa_description"), "highlights": _json_list(row.get("aa_highlights")),
            "itineraries": row.get("aa_itineraries"), "seo_title": row.get("seo_title"),
            "seo_meta": row.get("seo_meta")}


def measure_drift(masters: Iterable[dict]) -> dict:
    """The master_drift census (see the module docstring)."""
    from services.content_generation import s1_writer_v2 as v2
    from services.quality import factsheet as fs
    from services.quality.s1_checks import run_checks

    n = human = blocking = 0
    by_class: Counter = Counter()
    by_code: Counter = Counter()
    masters_by_class: Counter = Counter()
    findings_per_master: list[int] = []
    errors: Counter = Counter()
    for row in masters:
        n += 1
        if row.get("human_edited"):
            human += 1
            continue
        tour, agency = fs.raw_source_tour(row)
        try:
            findings = run_checks(v2.with_agency_fields(tour, agency), master_output(row))
        except Exception as e:  # noqa: BLE001
            errors[type(e).__name__] += 1
            continue
        fact = [f for f in findings if f["cls"] in FACT_CLASSES]
        findings_per_master.append(len(fact))
        by_class.update(f["cls"] for f in findings)
        by_code.update(f["code"] for f in findings)
        masters_by_class.update({f["cls"] for f in fact})
        blocking += bool(fact)
    machine = n - human - sum(errors.values())
    return {"active_masters": n, "human_edited": human, "checked": machine, "blocking": blocking,
            "blocking_rate": round(blocking / machine, 4) if machine else None,
            "blocking_classes": list(FACT_CLASSES), "masters_by_fact_class": dict(masters_by_class),
            "findings_by_class": dict(by_class), "findings_by_code": dict(by_code),
            "fact_findings_per_master": distribution(findings_per_master), "check_errors": dict(errors),
            "tune_provenance_before_w3": (blocking / machine > 0.15) if machine else None}


# ------------------------------------------------------------------ database (ECS exec)


async def _facts_left_out(conn, keys: set) -> dict:
    """(tenant_id str, destination) -> facts the budget leaves out today (pick_facts, then budget_facts)."""
    from uuid import UUID

    from services.acp_content_writing import facts as writer_facts

    out = {}
    for tenant_id, country in sorted(keys, key=lambda k: (k[0], k[1] or "")):
        rows = await conn.fetch(writer_facts._FACTS_QUERY, UUID(tenant_id), country)
        picked = writer_facts.pick_facts([dict(r) for r in rows])
        out[(tenant_id, country)] = len(picked) - len(writer_facts.budget_facts(picked))
    return out


def _limited(rows, limit: Optional[int]):
    return rows[:limit] if limit else rows


async def run(measure: str, *, piece_days: int, limit: Optional[int]) -> dict:
    import asyncpg

    conn = await asyncpg.connect(os.environ["DATABASE_URL"], ssl="require",
                                 server_settings={"application_name": "cis-exec"})
    out: dict = {"measure": measure, "issue": "AA-1047"}
    tr = conn.transaction(readonly=True)     # read-only: the server refuses any write
    await tr.start()
    try:
        if measure in ("all", "pointers"):
            rows = _limited([dict(r) for r in await conn.fetch(ACTIVE_SOURCES_SQL)], limit)
            out["pointers"], out["excluded_s1"] = measure_pointers(rows)
        if measure in ("all", "anchors"):
            atoms = _limited([dict(r) for r in await conn.fetch(ATOMS_SQL)], limit)
            tours = sorted({a["tour_id"] for a in atoms}, key=str)
            raw = [dict(r) for r in await conn.fetch(RAW_FOR_TOURS_SQL, tours)]
            out["anchors"] = measure_anchors(atoms, raw)
        if measure in ("all", "excluded"):
            pieces = []
            for r in _limited(await conn.fetch(PIECES_SQL, piece_days), limit):
                try:
                    wi = json.loads(r["writer_input"]) if r["writer_input"] else {}
                except ValueError:
                    wi = {}
                pieces.append({"piece_id": r["piece_id"], "tenant_id": r["tenant_id"], "writer_input": wi})
            keys = {(str(p["tenant_id"]), p["writer_input"].get("destination")) for p in pieces}
            left = await _facts_left_out(conn, keys)
            out["excluded_t9"] = {**measure_excluded(pieces, left), "days": piece_days}
        masters = None
        if measure in ("all", "drift"):
            masters = _limited([dict(r) for r in await conn.fetch(MASTERS_SQL)], limit)
    finally:
        # The census is long CPU work: close the read-only transaction first, so the server's idle-in-transaction
        # timeout never drops the connection mid-run (S272: ConnectionDoesNotExistError lost the whole result).
        try:
            await tr.rollback()
        finally:
            await conn.close()
    if masters is not None:
        out["master_drift"] = measure_drift(masters)
    return out


def main() -> int:
    measure = os.environ.get("MEASURE", "all")
    if measure not in ("all", "anchors", "pointers", "excluded", "drift"):
        print(json.dumps({"error": f"unknown MEASURE {measure!r}"}))
        return 2
    limit = int(os.environ["LIMIT"]) if os.environ.get("LIMIT") else None
    try:
        result = asyncio.run(run(measure, piece_days=int(os.environ.get("PIECE_DAYS", "60")), limit=limit))
    except Exception as e:  # noqa: BLE001 — print the class only (a database error text can echo a value)
        print(json.dumps({"error": type(e).__name__}))
        return 1
    print(json.dumps(result, indent=1, sort_keys=True, default=str))
    return 0


if __name__ == "__main__":
    sys.exit(main())
