"""AA-1003 R4 — catalog and AI-quality reports: CIS-01 weekly catalog health, CIS-06 eval and release report,
CIS-07 rerun wave and paid-run report.

  CIS-01  weekly (ISO week, ICT days): tours live / paused / retired and the week-over-week change (K-CAT-LIVE
          snapshots, else the live count), lifecycle events of the week, S1 writer coverage, photo coverage and
          depth per country, the nightly downstream checks (DATA-01 snapshots) and a generated action list.
  CIS-06  event (manual run over a period, default the last 7 closed days): every eval run that finished in the
          period with its quality delta, gate pass rate and cost vs cap, the writing-memory releases that changed,
          their approval record and the worst item results.
  CIS-07  event (same default period): every rerun wave open during the period through the wave report the
          /admin/waves page uses (services/waves/report.py), plus cost per stage, retry waste, USD per tour and a
          projection for the rest of the wave's country.

Sources (names: root docs/architecture/db-schema-reference.md; all read as cis_app, stored rows only, no LLM, no paid
call): gold_aa_internal.published_tours + silver_aa_internal.raw_tours (the live-tour rule of the data coverage page,
api/services/data_coverage/defs.LIVE), silver_aa_internal.generated_content.metadata writer_version,
shared.v_tour_photos (catalog.v_tour is not granted to cis_app; it takes cover_photo_path = position 1 and
photo_count from this view, CIS 253, so the counts are the same), gold_aa_internal.v_tour_liveness,
gold_aa_internal.tour_lifecycle_event, shared.kpi_daily, shared.prompt_eval_runs, shared.writing_memory_release,
shared.eval_item_result, shared.rerun_wave, shared.job, shared.llm_call_log, shared.dfs_call_log.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta
from typing import Any, Optional

from services.reports import money_sources as src
from services.reports import periods, store
from services.reports.generate import GENERATED_BY, Rendered, fmt
from services.reports.money import (KPI_HEADERS, NOT_WIRED, _kpi_line, _kpi_rows, _output, failed, pct, ratio,
                                    signed, table, tours_live_change, usd)
from services.reports.registry import Registry, Report

DEV_NOTE = "> Dev environment: counts reflect development data and test runs; trends only."
S1_COUNTRY_FLAG = 0.90           # K-CAT-S1: countries below 90 % are listed
COVER_RED, COVER_AMBER = 0.90, 0.95
PHOTO_AMBER = 0.80
PHOTO_DEPTH = 3                  # K-CAT-PHOTO: tours with at least 3 photos
REVIEW_REASONS = ("source_deleted", "master_trashed")      # K-CAT-LIFECYCLE: retirements listed for review
ACTIONS_MAX = 10
WAVE_RETRY_FLAG = 0.10           # K-WAVE-RETRY
WAVES_MAX = 20
FAILED_JOB_STATUSES = ("failed", "cancelled", "stopped_budget")


def _header(report: Report, first: date, last: date, generated_at: datetime, title: str, days_note: str) -> list[str]:
    period = first.isoformat() if first == last else f"{first.isoformat()} to {last.isoformat()}"
    return [f"# {title}", "", DEV_NOTE, "",
            f"Report {report.id} ({report.name_en}), period {period}. Generated "
            f"{generated_at.strftime('%Y-%m-%d %H:%M UTC')} by the {GENERATED_BY}. {days_note}", ""]


# The unit a value here is computed in, where kpis.yaml (units inferred at import) says otherwise: the completion and
# retry waste are ratios; the running total is the USD sum, because the approved cap it is defined against is not
# stored; the lifecycle value is the number of events; the eval delta a quality-score difference (two decimals).
UNIT_OF = {"K-WAVE-DONE": "ratio", "K-WAVE-RETRY": "ratio", "K-WAVE-RUNNING": "usd", "K-CAT-LIFECYCLE": "count",
           "K-EVAL-DELTA": "score"}


def _render(reg: Registry, report: Report, first: date, last: date, generated_at: datetime, title: str,
            headline: str, kpis: list[dict], body: list[str], days_note: str, **data) -> Rendered:
    md = _header(report, first, last, generated_at, title, days_note)
    md += ["## KPIs", ""] + table(KPI_HEADERS, _kpi_rows(reg, kpis, UNIT_OF)) + [""] + body
    out = _output(report, first, last, generated_at, headline, kpis=kpis, **data)
    return Rendered(headline[:1000], out, "\n".join(md))


def _iso(v: Any) -> Optional[str]:
    return v.isoformat() if hasattr(v, "isoformat") else (None if v is None else str(v))


def _f(v: Any) -> Optional[float]:
    return None if v is None else float(v)


ICT_NOTE = "Days are ICT (UTC+7) calendar days."


# ═══ CIS-01 weekly catalog health ═══════════════════════════════════════════════════════════════════════════════


def catalog_sql() -> str:
    """Live tours per country (the data coverage page's live rule): current S1 writer, kept on the old writer on
    purpose (AA-645 kept wave), tour photos. $1 = AA master tenant, $2 = current writer version, $3 = kept wave."""
    from api.services.data_coverage.defs import LIVE

    return f"""
    WITH {LIVE},
    kept AS (SELECT unnest(expected_tour_ids) AS tour_id
               FROM (SELECT expected_tour_ids FROM shared.rerun_wave WHERE name = $3
                      ORDER BY created_at DESC LIMIT 1) k),
    ph AS (SELECT tour_id, count(*) AS n FROM shared.v_tour_photos GROUP BY tour_id),
    t AS (SELECT l.tour_id, rt.country, g.metadata ->> 'writer_version' AS writer_version,
                 coalesce(ph.n, 0) AS photos, (l.tour_id IN (SELECT tour_id FROM kept)) AS kept
            FROM live l
            JOIN silver_aa_internal.raw_tours rt ON rt.tour_id = l.tour_id
            LEFT JOIN silver_aa_internal.generated_content g ON g.id = l.generated_content_id
            LEFT JOIN ph ON ph.tour_id = l.tour_id)
    SELECT coalesce(nullif(btrim(country), ''), 'unknown') AS country, count(*)::int AS live,
           count(*) FILTER (WHERE writer_version = $2)::int AS current_writer,
           count(*) FILTER (WHERE writer_version IS DISTINCT FROM $2 AND kept)::int AS kept_old_writer,
           count(*) FILTER (WHERE photos >= 1)::int AS with_photo,
           count(*) FILTER (WHERE photos >= {PHOTO_DEPTH})::int AS photo_depth
      FROM t GROUP BY 1 ORDER BY 1
    """


LIFECYCLE_SQL = """
    SELECT event, reason, count(*)::int AS n
      FROM gold_aa_internal.tour_lifecycle_event
     WHERE occurred_at >= $1 AND occurred_at < $2
     GROUP BY 1, 2 ORDER BY 1, 2
"""

LIFECYCLE_REVIEW_SQL = """
    SELECT e.event, e.reason, e.source_id, e.tour_id::text AS tour_id, e.replaced_by_tour_id::text AS replaced_by,
           e.occurred_at, rt.src_name AS name, rt.country
      FROM gold_aa_internal.tour_lifecycle_event e
      LEFT JOIN silver_aa_internal.raw_tours rt ON rt.tour_id = e.tour_id
     WHERE e.occurred_at >= $1 AND e.occurred_at < $2
       AND ((e.event = 'retired' AND e.reason = ANY($3::text[])) OR e.event = 'superseded')
     ORDER BY e.occurred_at DESC
     LIMIT 50
"""

DATA_KPIS = ("K-DATA-PROBES", "K-DATA-LIVENESS", "K-DATA-STALE")


async def catalog_by_country(conn) -> list[dict]:
    from api.services.data_coverage.defs import MASTER_TENANT
    from api.services.data_coverage.queries import KEPT_WAVE_NAME
    from services.content_generation.s1_writer_v2_live import WRITER_VERSION

    rows = await conn.fetch(catalog_sql(), MASTER_TENANT, WRITER_VERSION, KEPT_WAVE_NAME)
    return [{**dict(r), "writer_version": WRITER_VERSION} for r in rows]


async def lifecycle(conn, first: date, last: date) -> dict:
    start, end = periods.range_bounds(first, last)
    counts = [dict(r) for r in await conn.fetch(LIFECYCLE_SQL, start, end)]
    review = [{**dict(r), "occurred_at": _iso(r["occurred_at"])}
              for r in await conn.fetch(LIFECYCLE_REVIEW_SQL, start, end, list(REVIEW_REASONS))]
    return {"counts": counts, "review": review}


async def collect_cis01(conn, first: date, last: date) -> dict:
    facts = await src.run_parts({
        "catalog": lambda: catalog_by_country(conn),
        "liveness_now": lambda: src.liveness_now(conn),
        "tours_live": lambda: store.kpi_series(conn, ["K-CAT-LIVE"], first - timedelta(days=7), last),
        "lifecycle": lambda: lifecycle(conn, first, last),
        "data_kpis": lambda: store.kpi_series(conn, list(DATA_KPIS), first, last),
        "published": lambda: src.published_tours(conn, first, last + timedelta(days=1)),
    })
    facts["window"] = {"first": first.isoformat(), "last": last.isoformat(), "days": "ICT"}
    return facts


def catalog_totals(rows: Any) -> dict:
    if rows is None or failed(rows):
        return {"error": failed(rows) or "not read"}
    keys = ("live", "current_writer", "kept_old_writer", "with_photo", "photo_depth")
    tot = {k: sum(int(r[k]) for r in rows) for k in keys}
    countries = []
    for r in rows:
        live = int(r["live"])
        countries.append({"country": r["country"], **{k: int(r[k]) for k in keys},
                          "s1": ratio(r["current_writer"], live), "cover": ratio(r["with_photo"], live),
                          "depth": ratio(r["photo_depth"], live)})
    return {**tot, "s1": ratio(tot["current_writer"], tot["live"]), "cover": ratio(tot["with_photo"], tot["live"]),
            "depth": ratio(tot["photo_depth"], tot["live"]), "countries": countries,
            "writer_version": rows[0]["writer_version"] if rows else None}


def lifecycle_view(lc: Any) -> dict:
    if lc is None or failed(lc):
        return {"error": failed(lc) or "not read"}
    by_event: dict[str, int] = {}
    for r in lc["counts"]:
        by_event[r["event"]] = by_event.get(r["event"], 0) + int(r["n"])
    review = [r for r in lc["review"] if r["event"] == "retired"]
    return {"total": sum(by_event.values()), "by_event": by_event, "counts": lc["counts"], "review": review,
            "superseded": [r for r in lc["review"] if r["event"] == "superseded"]}


def data_checks(rows: Any) -> dict:
    """The newest DATA-01 snapshot value of each nightly-check KPI in the week."""
    if rows is None or failed(rows):
        return {"error": failed(rows) or "not read"}
    latest: dict[str, tuple] = {}
    for r in rows:
        if r["scope"] != "all":
            continue
        d = r["day"]
        if r["kpi_id"] not in latest or d > latest[r["kpi_id"]][0]:
            latest[r["kpi_id"]] = (d, float(r["value"]))
    return {k: {"day": _iso(v[0]), "value": v[1]} for k, v in latest.items()}


def catalog_actions(cat: dict, lc: dict, checks: dict) -> list[str]:
    """Top actions of the week, most urgent first: data checks red, retirements to review, countries furthest from
    the S1, cover and photo targets."""
    out: list[str] = []
    if "error" not in checks:
        stale = (checks.get("K-DATA-STALE") or {}).get("value")
        if stale:
            out.append(f"Downstream freshness: {stale:.0f} aged stale rows in the last nightly check (DATA-01); "
                       "re-run the stale consumers.")
        viol = (checks.get("K-DATA-LIVENESS") or {}).get("value")
        if viol:
            out.append(f"Tour liveness gate: {viol:.0f} violations in the last nightly check (DATA-01).")
    if "error" not in lc and lc["review"]:
        out.append(f"Review {len(lc['review'])} retirement(s) with reason source_deleted / master_trashed (listed "
                   "under Lifecycle events).")
    if "error" not in cat:
        for c in sorted(cat["countries"], key=lambda c: -(c["live"] - c["current_writer"] - c["kept_old_writer"])):
            pending = c["live"] - c["current_writer"] - c["kept_old_writer"]
            if c["s1"] is not None and c["s1"] < S1_COUNTRY_FLAG and pending > 0:
                out.append(f"{c['country']}: rewrite {pending} tour(s) with the current S1 writer "
                           f"({pct(c['s1'])} done).")
        for c in sorted(cat["countries"], key=lambda c: -(c["live"] - c["with_photo"])):
            if c["cover"] is not None and c["cover"] < COVER_AMBER:
                out.append(f"{c['country']}: {c['live'] - c['with_photo']} tour(s) without a photo "
                           f"(cover {pct(c['cover'])}).")
        for c in sorted(cat["countries"], key=lambda c: -(c["live"] - c["photo_depth"])):
            if c["depth"] is not None and c["depth"] < PHOTO_AMBER:
                out.append(f"{c['country']}: {c['live'] - c['photo_depth']} tour(s) with fewer than {PHOTO_DEPTH} "
                           f"photos ({pct(c['depth'])} at depth).")
    return out[:ACTIONS_MAX]


def cis01_kpis(cat: dict, tl: dict, lc: dict) -> list[dict]:
    def live_note() -> str:
        if "error" in tl:
            return f"not readable: {tl['error']}"
        if tl.get("source") == "live":
            return "live count at generation: no snapshot in this week"
        return f"week over week {signed(tl.get('change'))} (snapshot {tl.get('as_of')})"

    out = [_kpi_line("K-CAT-LIVE", tl.get("value"), "no_data" if tl.get("value") is None else
                     ("amber" if tl.get("flag") else "ok"), live_note())]
    if "error" in cat:
        note = f"not readable: {cat['error']}"
        out += [_kpi_line(k, None, "no_data", note) for k in ("K-CAT-COVER", "K-CAT-PHOTO", "K-CAT-S1")]
    else:
        cover, depth, s1 = cat["cover"], cat["depth"], cat["s1"]
        out.append(_kpi_line("K-CAT-COVER", cover, "no_data" if cover is None else "red" if cover < COVER_RED else
                             "amber" if cover < COVER_AMBER else "ok",
                             f"{cat['with_photo']} of {cat['live']} live tours have a tour photo (the cover)"))
        out.append(_kpi_line("K-CAT-PHOTO", depth, "no_data" if depth is None else
                             "amber" if depth < PHOTO_AMBER else "ok",
                             f"{cat['photo_depth']} of {cat['live']} live tours have {PHOTO_DEPTH}+ photos"))
        low = [c["country"] for c in cat["countries"] if c["s1"] is not None and c["s1"] < S1_COUNTRY_FLAG]
        out.append(_kpi_line("K-CAT-S1", s1, "no_data" if s1 is None else "ok" if s1 >= 1.0 else "amber",
                             f"writer {cat['writer_version']}: {cat['current_writer']} of {cat['live']}; "
                             f"{cat['kept_old_writer']} kept on the old writer on purpose"
                             + (f"; below 90 %: {', '.join(low)}" if low else "")))
    if "error" in lc:
        out.append(_kpi_line("K-CAT-LIFECYCLE", None, "no_data", f"not readable: {lc['error']}"))
    else:
        events = ", ".join(f"{k} {v}" for k, v in sorted(lc["by_event"].items())) or "none"
        out.append(_kpi_line("K-CAT-LIFECYCLE", float(lc["total"]), "amber" if lc["review"] else "info",
                             f"{events}; {len(lc['review'])} retirement(s) to review"))
    return out


def render_cis01(reg: Registry, report: Report, first: date, last: date, facts: dict,
                 generated_at: datetime) -> Rendered:
    cat = catalog_totals(facts.get("catalog"))
    tl = tours_live_change(facts.get("tours_live"), first, last, facts.get("liveness_now"))
    lc = lifecycle_view(facts.get("lifecycle"))
    checks = data_checks(facts.get("data_kpis"))
    now_states = facts.get("liveness_now") if not failed(facts.get("liveness_now")) else None
    kpis = cis01_kpis(cat, tl, lc)
    actions = catalog_actions(cat, lc, checks)
    states = ", ".join(f"{k} {fmt(float(now_states.get(k, 0)), 'count')}" for k in ("served", "paused", "retired")) \
        if now_states else "not readable"
    headline = (f"Catalog health {first.isoformat()} to {last.isoformat()}: tours live "
                f"{fmt(tl.get('value'), 'count')}, S1 {pct(cat.get('s1'))}, cover {pct(cat.get('cover'))}, "
                f"{len(actions)} action(s)")
    body = ["## Headline", "", f"- Tours by liveness now: {states}.",
            f"- Tours live (K-CAT-LIVE): {fmt(tl.get('value'), 'count')}; end of the week before "
            f"{fmt(tl.get('previous'), 'count')}; change {signed(tl.get('change'))}.", ""]
    pub = facts.get("published")
    if isinstance(pub, dict) and not failed(pub):
        body[-1:-1] = [f"- Tours published or republished this week: {pub['total']:,} ({pub['first_published']:,} "
                       "new)."]
    body += ["## Lifecycle events this week", ""]
    if "error" in lc:
        body += [f"- Not readable: {lc['error']}", ""]
    else:
        body += table(["Event", "Reason", "Count"], [[r["event"], r["reason"], r["n"]] for r in lc["counts"]]) \
            if lc["counts"] else ["- No lifecycle event this week."]
        body.append("")
        if lc["review"]:
            body += ["Retirements to review (source deleted or Master trashed):", ""]
            body += table(["When (UTC)", "Tour", "Country", "Reason"],
                          [[str(r["occurred_at"])[:16], r.get("name") or r["source_id"], r.get("country"),
                            r["reason"]] for r in lc["review"]]) + [""]
        if lc["superseded"]:
            body += [f"Superseded this week: {len(lc['superseded'])} (each with its replacement tour id in the JSON "
                     "output).", ""]
    body += ["## S1 rewrite, cover and photo coverage per country", ""]
    if "error" in cat:
        body += [f"- Not readable: {cat['error']}", ""]
    else:
        body += table(["Country", "Live", "Current writer", "Kept on old writer", "S1", "Cover",
                       f"{PHOTO_DEPTH}+ photos"],
                      [[c["country"], c["live"], c["current_writer"], c["kept_old_writer"], pct(c["s1"]),
                        pct(c["cover"]), pct(c["depth"])] for c in cat["countries"]])
        body += ["", f"Total: {cat['live']} live tours; current writer {cat['writer_version']}. Cover and photo counts "
                     "come from shared.v_tour_photos, the source catalog.v_tour reads (its cover is photo 1).",
                 ""]
    body += ["## Downstream freshness (DATA-01)", ""]
    if "error" in checks:
        body += [f"- Nightly check snapshots not readable: {checks['error']}", ""]
    elif not checks:
        body += ["- No nightly check snapshot this week (the probe log was not readable; see OPS-12).", ""]
    else:
        body += [f"- {k}: {fmt(v['value'], 'ratio' if k == 'K-DATA-PROBES' else 'count')} ({v['day']})"
                 for k, v in sorted(checks.items())] + [""]
    body += ["## Top actions", ""] + ([f"{i}. {a}" for i, a in enumerate(actions, 1)] or ["- Nothing to act on."])
    body.append("")
    return _render(reg, report, first, last, generated_at, f"Catalog health {first.isoformat()} to {last.isoformat()}",
                   headline, kpis, body, ICT_NOTE, catalog=cat, tours_live=tl, lifecycle=lc, data_checks=checks,
                   actions=actions, sources_unreadable={k: failed(v) for k, v in facts.items() if failed(v)})


# ═══ CIS-06 eval and release report ═════════════════════════════════════════════════════════════════════════════

EVAL_RUNS_SQL = """
    SELECT id::text AS id, pipeline, run_kind, writer, prompt_version, release_id::text AS release_id,
           baseline_prompt_version, avg_quality_score, baseline_avg_quality_score, gate_pass_count, gate_fail_count,
           cost_usd, cost_cap_usd, verdict, stop_reason, judge_kappa, regression_detected, tour_count, triggered_by,
           created_at, finished_at
      FROM shared.prompt_eval_runs
     WHERE coalesce(finished_at, created_at) >= $1 AND coalesce(finished_at, created_at) < $2
     ORDER BY coalesce(finished_at, created_at)
     LIMIT 200
"""

RELEASES_SQL = """
    SELECT release_id::text AS release_id, writer, scope, release_no, train_kind, content_class, status,
           eval_run_id::text AS eval_run_id, r0_run_id::text AS r0_run_id, shadow_run_id::text AS shadow_run_id,
           approved_by, approved_at, approval_ref, activated_at, retired_at, rejected_by, created_by, created_at,
           updated_at
      FROM shared.writing_memory_release
     WHERE updated_at >= $1 AND updated_at < $2
     ORDER BY updated_at
     LIMIT 100
"""

# Item results of the period's runs that failed or hit a hard stop; output text is never read (pii:free_text).
ITEM_REGRESSIONS_SQL = """
    SELECT run_id::text AS run_id, item_id::text AS item_id, writer, arm, phase, verdict, score, failure_codes,
           hard_stop
      FROM shared.eval_item_result
     WHERE run_id = ANY($1::text[]::uuid[]) AND (hard_stop OR cardinality(failure_codes) > 0)
     ORDER BY hard_stop DESC, score ASC NULLS LAST, created_at
     LIMIT 10
"""


def _row(r) -> dict:
    return {k: (_iso(v) if isinstance(v, datetime) else float(v) if hasattr(v, "as_tuple") else v)
            for k, v in dict(r).items()}


async def eval_period(conn, first: date, last: date) -> dict:
    start, end = periods.range_bounds(first, last)
    runs = [_row(r) for r in await conn.fetch(EVAL_RUNS_SQL, start, end)]
    releases = [_row(r) for r in await conn.fetch(RELEASES_SQL, start, end)]
    items = [_row(r) for r in await conn.fetch(ITEM_REGRESSIONS_SQL, [r["id"] for r in runs])] if runs else []
    return {"runs": runs, "releases": releases, "items": items}


def eval_run_view(r: dict) -> dict:
    avg, base = _f(r.get("avg_quality_score")), _f(r.get("baseline_avg_quality_score"))
    gp, gf = r.get("gate_pass_count"), r.get("gate_fail_count")
    cost, cap = _f(r.get("cost_usd")), _f(r.get("cost_cap_usd"))
    return {**r, "delta": None if avg is None or base is None else round(avg - base, 3),
            "gate_pass": ratio(gp, (gp or 0) + (gf or 0)) if gp is not None and gf is not None else None,
            "over_cap": cost is not None and cap is not None and cost > cap}


def cis06_kpis(runs: list[dict]) -> list[dict]:
    with_delta = [r for r in runs if r["delta"] is not None]
    with_gate = [r for r in runs if r["gate_pass"] is not None]
    worst_delta = min(with_delta, key=lambda r: r["delta"]) if with_delta else None
    worst_gate = min(with_gate, key=lambda r: r["gate_pass"]) if with_gate else None
    cost = sum(_f(r.get("cost_usd")) or 0.0 for r in runs)
    over = [r for r in runs if r["over_cap"]]
    return [
        _kpi_line("K-EVAL-DELTA", worst_delta["delta"] if worst_delta else None,
                  "no_data" if worst_delta is None else "amber" if worst_delta["delta"] < 0 else "ok",
                  f"lowest of {len(with_delta)} run(s) with a baseline" if with_delta else "no run with a baseline"),
        _kpi_line("K-EVAL-GATEPASS", worst_gate["gate_pass"] if worst_gate else None,
                  "no_data" if worst_gate is None else "info",
                  f"lowest of {len(with_gate)} run(s); compare with the baseline run" if with_gate else
                  "no run with gate counts"),
        _kpi_line("K-EVAL-COST", round(cost, 4) if runs else None,
                  "no_data" if not runs else "red" if over else "ok",
                  f"{len(over)} run(s) over their cost cap" if over else f"{len(runs)} run(s), none over its cap"),
    ]


def render_cis06(reg: Registry, report: Report, first: date, last: date, facts: dict,
                 generated_at: datetime) -> Rendered:
    data = facts.get("eval")
    if data is None or failed(data):
        err = failed(data) or "not read"
        runs, releases, items = [], [], []
    else:
        err = None
        runs = [eval_run_view(r) for r in data["runs"]]
        releases, items = data["releases"], data["items"]
    kpis = cis06_kpis(runs)
    verdicts: dict[str, int] = {}
    for r in runs:
        verdicts[r.get("verdict") or "running"] = verdicts.get(r.get("verdict") or "running", 0) + 1
    headline = (f"Evals {first.isoformat()} to {last.isoformat()}: {len(runs)} run(s) "
                f"({', '.join(f'{k} {v}' for k, v in sorted(verdicts.items())) or 'none'}), "
                f"{len(releases)} release change(s)")
    body: list[str] = []
    if err:
        body += [f"- Eval tables not readable: {err}", ""]
    body += ["## Eval runs", ""]
    body += table(["Finished (UTC)", "Pipeline / kind", "Writer", "Release or prompt", "Verdict", "Quality delta",
                   "Gate pass", "Kappa", "Cost / cap", "Stop"],
                  [[str(r.get("finished_at") or r.get("created_at"))[:16], f"{r['pipeline']} / {r['run_kind']}",
                    r.get("writer"), (r.get("release_id") or r.get("prompt_version") or "")[:12], r.get("verdict"),
                    r["delta"], pct(r["gate_pass"]), _f(r.get("judge_kappa")),
                    f"{usd(_f(r.get('cost_usd')))} / {usd(_f(r.get('cost_cap_usd')))}", r.get("stop_reason")]
                   for r in runs]) if runs else ["- No eval run finished in the period."]
    body += ["", "## Releases and approval record", ""]
    body += table(["Release", "Writer / scope", "No.", "Kind", "Status", "Eval run", "Approved by", "Approved at",
                   "Activated", "Retired / rejected"],
                  [[r["release_id"][:8], f"{r['writer']} / {r['scope']}", r["release_no"],
                    f"{r['train_kind']} ({r['content_class']})", r["status"], (r.get("eval_run_id") or "—")[:8],
                    r.get("approved_by"), str(r.get("approved_at") or "—")[:16], str(r.get("activated_at") or "—")[:16],
                    str(r.get("retired_at") or "—")[:16] if r["status"] == "retired" else (r.get("rejected_by") or "—")]
                   for r in releases]) if releases else ["- No release created or changed in the period."]
    body += ["", "## Worst item results", ""]
    body += table(["Run", "Item", "Writer / arm", "Verdict", "Score", "Failure codes", "Hard stop"],
                  [[r["run_id"][:8], r["item_id"][:8], f"{r['writer']} / {r['arm']}", r["verdict"], r.get("score"),
                    ", ".join(r.get("failure_codes") or []) or "—", "yes" if r["hard_stop"] else "no"]
                   for r in items]) if items else ["- No failed or hard-stopped item in these runs."]
    body += ["", "Decision: approve, reject or roll back each release above on /admin/writing-memory; this report "
                 "only collects the evidence.", ""]
    return _render(reg, report, first, last, generated_at, f"Eval and release report {first.isoformat()} to "
                   f"{last.isoformat()}", headline, kpis, body, ICT_NOTE, runs=runs, releases=releases, items=items,
                   sources_unreadable={"eval": err} if err else {})


# ═══ CIS-07 rerun waves and paid runs ═══════════════════════════════════════════════════════════════════════════

WAVES_SQL = """
    SELECT id::text AS id FROM shared.rerun_wave
     WHERE created_at < $2 AND (closed_at IS NULL OR closed_at >= $1) AND name <> $3
     ORDER BY created_at
     LIMIT $4
"""


def wave_stage_sql() -> str:
    """LLM cost of a wave's jobs per stage, and the part spent by jobs that did not succeed ($1 wave, $2 kinds)."""
    from services.waves.report import _JOBS_CTE

    return f"""
    WITH {_JOBS_CTE}
    SELECT coalesce(l.stage, 'unknown') AS stage, count(*)::int AS calls,
           coalesce(sum(l.cost_usd), 0)::float AS cost_usd,
           coalesce(sum(l.cost_usd) FILTER (WHERE j.status = ANY($3::text[])), 0)::float AS failed_cost_usd
      FROM shared.llm_call_log l JOIN jobs j ON j.id = l.job_id, w
     WHERE l.created_at >= w.created_at
     GROUP BY 1 ORDER BY 3 DESC
    """


async def waves_period(conn, first: date, last: date) -> dict:
    from api.services.data_coverage.queries import KEPT_WAVE_NAME
    from services.waves import report as wave_report

    start, end = periods.range_bounds(first, last)
    ids = [r["id"] for r in await conn.fetch(WAVES_SQL, start, end, KEPT_WAVE_NAME, WAVES_MAX)]
    out = []
    for wid in ids:
        rep = await wave_report.report(conn, wid)
        if rep is None:
            continue
        stages = [dict(r) for r in await conn.fetch(wave_stage_sql(), wid, list(wave_report.WAVE_JOB_KINDS),
                                                    list(FAILED_JOB_STATUSES))]
        out.append({"report": {k: rep[k] for k in ("wave", "expected", "columns", "states", "terminal",
                                                   "jobs_finished", "complete", "jev_coverage", "cost", "jobs")},
                    "not_terminal": [{k: t.get(k) for k in ("tour_id", "name", "country", "state")}
                                     for t in rep["not_terminal"]][:50],
                    "stages": stages})
    return {"waves": out, "capped": len(ids) >= WAVES_MAX}


async def collect_cis07(conn, first: date, last: date) -> dict:
    facts = await src.run_parts({"waves": lambda: waves_period(conn, first, last),
                                 "catalog": lambda: catalog_by_country(conn)})
    facts["window"] = {"first": first.isoformat(), "last": last.isoformat(), "days": "ICT"}
    return facts


def wave_view(w: dict, live_by_country: dict[str, int]) -> dict:
    rep = w["report"]
    expected, done = rep["expected"], rep["columns"]["to_master"]
    total = rep["cost"]["total_usd"]
    llm_total = sum(s["cost_usd"] for s in w["stages"])
    failed_cost = sum(s["failed_cost_usd"] for s in w["stages"])
    unit = ratio(total, done)
    country = rep["wave"].get("country")
    country_live = live_by_country.get(country) if country else None
    stuck = len(w["not_terminal"]) if rep["jobs_finished"] or rep["wave"].get("closed_at") else 0
    return {"id": rep["wave"]["id"], "name": rep["wave"]["name"], "country": country, "status": rep["wave"]["status"],
            "created_at": rep["wave"].get("created_at"), "closed_at": rep["wave"].get("closed_at"),
            "expected": expected, "terminal": rep["terminal"], "done": done, "states": rep["states"],
            "done_ratio": ratio(rep["terminal"], expected), "stuck": stuck,
            "running": rep["states"].get("running", 0), "jev_pct": rep["jev_coverage"]["pct"],
            "cost_usd": total, "llm_usd": rep["cost"]["llm_usd"], "dfs_usd": rep["cost"]["dfs_usd"],
            "unit_usd": unit, "retry_ratio": ratio(failed_cost, llm_total), "failed_cost_usd": round(failed_cost, 4),
            "stages": w["stages"], "not_terminal": w["not_terminal"],
            "projection_usd": None if unit is None or country_live is None else round(unit * country_live, 2),
            "country_live": country_live}


def next_wave_advice(v: dict) -> str:
    if v["running"] and not v["closed_at"]:
        return "still running: wait for the wave's jobs before deciding"
    problems = []
    if v["stuck"]:
        problems.append(f"{v['stuck']} tour(s) not terminal")
    if v["retry_ratio"] is not None and v["retry_ratio"] >= WAVE_RETRY_FLAG:
        problems.append(f"retry waste {pct(v['retry_ratio'])}")
    if problems:
        return "fix before the next wave: " + "; ".join(problems)
    return "complete and within the retry limit: the next wave can scale up (3 tours, then <= 50, then by country)"


def cis07_kpis(views: list[dict]) -> list[dict]:
    if not views:
        note = "no wave open during the period"
        return [_kpi_line(k, None, "no_data", note) for k in ("K-WAVE-DONE", "K-WAVE-UNIT", "K-WAVE-RETRY",
                                                               "K-WAVE-STUCK")] + [
            _kpi_line("K-WAVE-RUNNING", None, NOT_WIRED, "the approved cap is not stored; compare the totals below "
                                                         "with the cap in the run approval")]
    expected = sum(v["expected"] for v in views)
    terminal = sum(v["terminal"] for v in views)
    done = sum(v["done"] for v in views)
    cost = sum(v["cost_usd"] for v in views)
    llm = sum(sum(s["cost_usd"] for s in v["stages"]) for v in views)
    failed_cost = sum(v["failed_cost_usd"] for v in views)
    stuck = sum(v["stuck"] for v in views)
    done_ratio, retry = ratio(terminal, expected), ratio(failed_cost, llm)
    return [
        _kpi_line("K-WAVE-DONE", done_ratio, "no_data" if done_ratio is None else "ok" if done_ratio >= 1 else "amber",
                  f"{terminal} of {expected} expected tours terminal over {len(views)} wave(s)"),
        _kpi_line("K-WAVE-UNIT", ratio(cost, done), "no_data" if not done else "info",
                  f"{usd(cost)} over {done} tour(s) to Master"),
        _kpi_line("K-WAVE-RETRY", retry, "no_data" if retry is None else
                  "amber" if retry >= WAVE_RETRY_FLAG else "ok", f"{usd(failed_cost)} spent by jobs that failed"),
        _kpi_line("K-WAVE-STUCK", float(stuck), "red" if stuck else "ok",
                  "not terminal after the wave's jobs finished"),
        _kpi_line("K-WAVE-RUNNING", round(cost, 4), NOT_WIRED,
                  "the approved cap is not stored; compare this running total with the cap in the run approval"),
    ]


def render_cis07(reg: Registry, report: Report, first: date, last: date, facts: dict,
                 generated_at: datetime) -> Rendered:
    data = facts.get("waves")
    err = failed(data) or (None if data is not None else "not read")
    cat = facts.get("catalog")
    live_by_country = {} if cat is None or failed(cat) else {r["country"]: int(r["live"]) for r in cat}
    views = [] if err else [wave_view(w, live_by_country) for w in data["waves"]]
    kpis = cis07_kpis(views)
    total = sum(v["cost_usd"] for v in views)
    headline = (f"Waves {first.isoformat()} to {last.isoformat()}: {len(views)} wave(s), "
                f"{sum(v['terminal'] for v in views)} of {sum(v['expected'] for v in views)} tours terminal, "
                f"{usd(total)}")
    body: list[str] = []
    if err:
        body += [f"- Waves not readable: {err}", ""]
    if not err and data.get("capped"):
        body += [f"- Only the first {WAVES_MAX} waves of the period are shown.", ""]
    body += ["## Scope", ""]
    body += table(["Wave", "Country", "Status", "Expected", "Terminal", "To Master", "Review", "Running", "Stuck",
                   "Jev coverage"],
                  [[v["name"], v["country"], v["status"], v["expected"], v["terminal"], v["done"],
                    v["states"].get("review_pending", 0), v["running"], v["stuck"], f"{v['jev_pct']} %"]
                   for v in views]) if views else ["- No rerun wave was open during the period."]
    body += ["", "## Cost per wave", ""]
    body += table(["Wave", "Total", "LLM", "DataForSEO", "USD per tour", "Retry waste", "Projection for the country"],
                  [[v["name"], usd(v["cost_usd"]), usd(v["llm_usd"]), usd(v["dfs_usd"]), usd(v["unit_usd"]),
                    pct(v["retry_ratio"]), "—" if v["projection_usd"] is None else
                    f"{usd(v['projection_usd'])} for {v['country_live']} live tours"] for v in views]) \
        if views else ["- Nothing spent by a wave in the period."]
    body += ["", f"Running total of the waves above: {usd(total)}. Geocoding is not attributed to a wave (its ledger "
                 "has no job link).", ""]
    for v in views:
        body += [f"### {v['name']}", ""]
        body += table(["Stage", "Calls", "Cost", "Spent by failed jobs"],
                      [[s["stage"], s["calls"], usd(s["cost_usd"]), usd(s["failed_cost_usd"])] for s in v["stages"]]) \
            if v["stages"] else ["- No LLM call linked to this wave's jobs."]
        if v["not_terminal"]:
            body += ["", "Not terminal: " + ", ".join(f"{t.get('name') or t['tour_id']} ({t['state']})"
                                                      for t in v["not_terminal"][:10])
                     + (" …" if len(v["not_terminal"]) > 10 else "")]
        body += ["", f"Next wave: {next_wave_advice(v)}.", ""]
    return _render(reg, report, first, last, generated_at, f"Rerun waves {first.isoformat()} to {last.isoformat()}",
                   headline, kpis, body, ICT_NOTE + " Money is per call as logged (shared.llm_call_log, "
                   "shared.dfs_call_log).", waves=views, sources_unreadable={"waves": err} if err else {})


# ── dispatch (generate.RENDERERS) ────────────────────────────────────────────────────────────────────────────────


async def render(conn, reg: Registry, report: Report, first: date, last: date, now: datetime) -> Rendered:
    if report.id == "CIS-01":
        return render_cis01(reg, report, first, last, await collect_cis01(conn, first, last), now)
    if report.id == "CIS-06":
        facts = await src.run_parts({"eval": lambda: eval_period(conn, first, last)})
        return render_cis06(reg, report, first, last, facts, now)
    if report.id == "CIS-07":
        return render_cis07(reg, report, first, last, await collect_cis07(conn, first, last), now)
    raise LookupError(f"quality.render has no report {report.id}")
