"""AA-1003 R4 part 2 — security reports: SEC-03 monthly security hygiene, SEC-08 privileged actions and audit log.

  SEC-08  monthly (semi, section of SEC-03): the acp_shared.audit_log hash chain (CIS 248) re-verified the way
          GET /admin/platform/audit/verify does (api/services/platform_audit.py: every chained row recomputed, rows
          without a chain_seq after the chain boundary counted), staff directory changes of the month (actions
          staff.created / roles_changed / disabled / enabled / cognito_*, resource_type admin_user), staff and machine
          calls by action, method and status, refused calls (401 / 403) per actor, and off-hours bulk activity
          (>= OFF_HOURS_BULK calls by one actor in one ICT hour between 22:00 and 06:00). Actors are shown as their
          audit tag (admin:<id>, machine), never a name or e-mail.
  SEC-03  monthly (semi, draft): the security facts CIS can read: staff accounts (active, owners, password logins
          still set, not signed in for 90 days), the audit chain verdict (SEC-08), refused staff calls, the nightly
          integrity probes of the month (K-DATA-PROBES snapshots). Dependabot, pip-audit / npm ignores, Trivy,
          secret scanning, the secrets register drift, the tenant isolation probe and the CRM key policy diff are
          GitHub, CI or AWS sources the worker does not read: each is a "Source not available" line with where the
          number lives today.

Stored rows only (cis_app); no LLM, no paid API, no AWS call.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta
from typing import Any, Optional

from api.services import platform_audit
from services.reports import money_sources as src
from services.reports import periods, store
from services.reports.generate import Rendered
from services.reports.money import NOT_WIRED, _kpi_line, failed, table
from services.reports.registry import Registry, Report
from services.reports.reliability import ICT_NOTE, _iso, _render, _unreadable

OFF_HOURS = (22, 6)              # ICT [22:00, 06:00)
OFF_HOURS_BULK = 50              # calls by one actor in one ICT hour
STALE_LOGIN_DAYS = 90
TOP_N = 20
STAFF_DIRECTORY_ACTIONS = ("staff.created", "staff.roles_changed", "staff.disabled", "staff.enabled",
                           "staff.signed_out", "staff.cognito_synced", "staff.cognito_pending",
                           "staff.invite_requested")
NOT_AVAILABLE = "Source not available"

# Calls of the month by actor type, action, method and status class (indexed on (action, created_at)).
ACTIONS_SQL = """
    SELECT coalesce(actor_type::text, 'none') AS actor_type, action, coalesce(http_method, '-') AS method,
           CASE WHEN status IS NULL THEN '-' WHEN status >= 500 THEN '5xx' WHEN status >= 400 THEN '4xx'
                ELSE '2xx/3xx' END AS status_class,
           count(*)::int AS n
      FROM acp_shared.audit_log
     WHERE created_at >= $1 AND created_at < $2
     GROUP BY 1, 2, 3, 4
     ORDER BY n DESC
     LIMIT 200
"""
DIRECTORY_SQL = """
    SELECT action, actor, resource_id, created_at
      FROM acp_shared.audit_log
     WHERE created_at >= $1 AND created_at < $2 AND action = ANY($3::text[])
     ORDER BY created_at
     LIMIT 200
"""
REFUSED_SQL = """
    SELECT actor, status, count(*)::int AS n, max(created_at) AS last_at
      FROM acp_shared.audit_log
     WHERE created_at >= $1 AND created_at < $2 AND status IN (401, 403)
     GROUP BY 1, 2
     ORDER BY n DESC
     LIMIT 50
"""
# Per actor per ICT hour; the hour filter is applied in Python (periods.ICT), not by the DB time zone.
HOURLY_SQL = """
    SELECT actor, date_trunc('hour', created_at) AS hour, count(*)::int AS n
      FROM acp_shared.audit_log
     WHERE created_at >= $1 AND created_at < $2 AND actor_type IN ('staff', 'machine')
     GROUP BY 1, 2
    HAVING count(*) >= $3
     ORDER BY n DESC
     LIMIT 500
"""
STAFF_SQL = """
    SELECT count(*) FILTER (WHERE is_active)::int AS active,
           count(*) FILTER (WHERE is_active AND is_owner)::int AS owners,
           count(*) FILTER (WHERE is_active AND password_hash IS NOT NULL)::int AS password_logins,
           count(*) FILTER (WHERE is_active AND (last_login_at IS NULL OR last_login_at < $1))::int AS stale,
           count(*) FILTER (WHERE is_active AND cognito_sub IS NULL)::int AS not_linked,
           count(*) FILTER (WHERE NOT is_active)::int AS disabled
      FROM shared.admin_users
"""


async def chain(conn) -> dict:
    """The audit chain verdict (platform_audit.verify_chain + unchained_rows, not cached)."""
    verdict = await platform_audit.verify_chain(conn)
    unchained = await platform_audit.unchained_rows(conn)
    ok = bool(verdict["ok"]) and unchained["unchained_after_genesis"] == 0
    return {**verdict, **{k: v for k, v in unchained.items() if k != "unchained_after_genesis_ids"},
            "ok": ok, "chain_ok": verdict["ok"]}


async def actions(conn, start: datetime, end: datetime) -> list[dict]:
    return [dict(r) for r in await conn.fetch(ACTIONS_SQL, start, end)]


async def directory(conn, start: datetime, end: datetime) -> list[dict]:
    return [{**dict(r), "created_at": _iso(r["created_at"])}
            for r in await conn.fetch(DIRECTORY_SQL, start, end, list(STAFF_DIRECTORY_ACTIONS))]


async def refused(conn, start: datetime, end: datetime) -> list[dict]:
    return [{**dict(r), "last_at": _iso(r["last_at"])} for r in await conn.fetch(REFUSED_SQL, start, end)]


async def hourly(conn, start: datetime, end: datetime) -> list[dict]:
    return [dict(r) for r in await conn.fetch(HOURLY_SQL, start, end, OFF_HOURS_BULK)]


async def staff(conn, now: datetime) -> dict:
    return dict(await conn.fetchrow(STAFF_SQL, now - timedelta(days=STALE_LOGIN_DAYS)) or {})


async def collect_sec08(conn, first: date, last: date) -> dict:
    start, end = periods.range_bounds(first, last)
    return await src.run_parts({
        "chain": lambda: chain(conn),
        "actions": lambda: actions(conn, start, end),
        "directory": lambda: directory(conn, start, end),
        "refused": lambda: refused(conn, start, end),
        "hourly": lambda: hourly(conn, start, end),
    })


async def collect_sec03(conn, first: date, last: date, now: datetime) -> dict:
    facts = await collect_sec08(conn, first, last)
    facts.update(await src.run_parts({
        "staff": lambda: staff(conn, now),
        "probes": lambda: store.kpi_series(conn, ["K-DATA-PROBES"], first, last),
    }))
    return facts


# ── computations (pure) ──────────────────────────────────────────────────────────────────────────────────────────


def off_hours(rows: Any) -> Optional[list[dict]]:
    """Actor-hours with >= OFF_HOURS_BULK calls whose ICT hour is in [22:00, 06:00)."""
    if rows is None or failed(rows):
        return None
    lo, hi = OFF_HOURS
    out = []
    for r in rows:
        h = r["hour"].astimezone(periods.ICT)
        if h.hour >= lo or h.hour < hi:
            out.append({"actor": r["actor"], "hour_ict": h.strftime("%Y-%m-%d %H:00"), "calls": r["n"]})
    return out


def chain_line(ch: Any) -> dict:
    if ch is None or failed(ch):
        return _kpi_line("K-AUD-CHAIN", None, "no_data", f"not readable: {failed(ch) or 'not read'}")
    if ch["ok"]:
        return _kpi_line("K-AUD-CHAIN", "pass", "ok", f"{ch['checked']} chained rows verified (last seq "
                         f"{ch['last_seq']}); {ch['pre_chain_rows']} rows written before the chain began")
    why = ch.get("problem") or (f"{ch['unchained_after_genesis']} row(s) without a chain_seq after the chain "
                                "boundary (an insert that skipped the trigger)")
    return _kpi_line("K-AUD-CHAIN", "fail", "red", f"first bad row {ch.get('first_bad_seq')}: {why}")


def sec08_kpis(facts: dict) -> list[dict]:
    return [
        chain_line(facts.get("chain")),
        _kpi_line("K-AUD-BREAKGLASS", None, NOT_WIRED,
                  "the audit row does not record the sign-in app client (cis-admin vs breakglass); break-glass uses "
                  "are in the WORKFORCE pool sign-in events and the docs/ops/runbooks/break-glass.md records"),
        _kpi_line("K-AUD-OVERRIDES", None, NOT_WIRED,
                  "deploy-gate overrides are Deploy Dev workflow inputs (GitHub Actions): not read by the worker"),
    ]


def _sec08_body(facts: dict) -> list[str]:
    ch = facts.get("chain")
    body = ["## Audit hash chain (CIS 248)", ""]
    if ch is None or failed(ch):
        body += [f"- Not readable: {failed(ch) or 'not read'}", ""]
    else:
        body += [f"- Verdict: {'pass' if ch['ok'] else 'FAIL'}; {ch['checked']} chained rows recomputed, last seq "
                 f"{ch['last_seq']}, {ch['pre_chain_rows']} rows before the chain boundary "
                 f"({ch.get('chain_boundary') or 'unknown'}), {ch['unchained_after_genesis']} unchained after it.",
                 ""]
        if not ch["ok"]:
            body += [f"- First bad row {ch.get('first_bad_seq')}: {ch.get('problem') or 'unchained rows'}. Open the "
                     "audit page (Platform > Audit) and treat it as an incident (OPS-04).", ""]
    body += ["## Staff directory changes", ""]
    d = facts.get("directory")
    if d is None or failed(d):
        body += [f"- Not readable: {failed(d) or 'not read'}"]
    elif not d:
        body += ["- No staff account created, changed, disabled or enabled in the period."]
    else:
        body += table(["When (UTC)", "Action", "By", "Staff account"],
                      [[str(r["created_at"])[:16], r["action"], r["actor"], r["resource_id"]] for r in d])
    body += ["", "- Booking staff role sync and CRM role changes: Booking and CRM audit tables are not granted to the "
             "CIS role (not readable from CIS).", "", "## Calls by type", ""]
    a = facts.get("actions")
    if a is None or failed(a):
        body += [f"- Not readable: {failed(a) or 'not read'}"]
    else:
        body += table(["Actor type", "Action", "Method", "Status", "Calls"],
                      [[r["actor_type"], r["action"], r["method"], r["status_class"], r["n"]] for r in a[:TOP_N]]) \
            if a else ["- No audit row in the period."]
    body += ["", "## Refused calls (401 / 403)", ""]
    rf = facts.get("refused")
    if rf is None or failed(rf):
        body += [f"- Not readable: {failed(rf) or 'not read'}"]
    else:
        body += table(["Actor", "Status", "Calls", "Last (UTC)"],
                      [[r["actor"], r["status"], r["n"], str(r["last_at"])[:16]] for r in rf]) \
            if rf else ["- No refused staff or machine call."]
    oh = off_hours(facts.get("hourly"))
    body += ["", f"## Off-hours bulk activity (>= {OFF_HOURS_BULK} calls in one ICT hour, 22:00-06:00)", ""]
    if oh is None:
        body += [f"- Not readable: {failed(facts.get('hourly')) or 'not read'}"]
    else:
        body += table(["Actor", "ICT hour", "Calls"], [[r["actor"], r["hour_ict"], r["calls"]] for r in oh]) \
            if oh else ["- None."]
    return body + [""]


def render_sec08(reg: Registry, report: Report, first: date, last: date, facts: dict,
                 generated_at: datetime) -> Rendered:
    kpis = sec08_kpis(facts)
    ch = kpis[0]
    oh = off_hours(facts.get("hourly")) or []
    headline = (f"Privileged actions {first.isoformat()} to {last.isoformat()}: audit chain {ch['value'] or 'not read'}"
                f", {len(facts.get('directory') or []) if not failed(facts.get('directory')) else '?'} staff "
                f"directory change(s), {len(oh)} off-hours bulk hour(s)")
    body = _sec08_body(facts)
    body += ["## Not wired", "",
             f"- {NOT_AVAILABLE}: break-glass uses (WORKFORCE sign-in events), deploy-gate overrides (GitHub workflow "
             "inputs), Booking and CRM admin actions (their own audit tables).", ""]
    return _render(reg, report, first, last, generated_at, f"Privileged action and audit-log review "
                   f"{first.isoformat()} to {last.isoformat()}", headline, kpis, body, ICT_NOTE,
                   chain=facts.get("chain"), off_hours=oh, sources_unreadable=_unreadable(facts))


# ═══ SEC-03 monthly security hygiene ═══════════════════════════════════════════════════════════════════════════

# (section, KPI, where the number lives today) for every SEC-03 source the worker cannot read.
UNAVAILABLE: tuple[tuple[str, Optional[str], str], ...] = (
    ("Dependabot alerts per repo by severity and age", "K-SEC-VULNSLA",
     "GitHub Dependabot (six repos); SLA in docs/security/vulnerability-management.md"),
    ("pip-audit / npm audit ignores and their expiry", None, "the ignore files and CI Security Audit job of each repo"),
    ("Trivy image findings (report only)", None, "the Docker Build Check job of each app repo (GitHub Actions)"),
    ("Secret scanning results", "K-SEC-SECRETFIND", "secret-scan and secret-scan-history workflows (GitHub)"),
    ("Secrets register drift", "K-SEC-DRIFT",
     "Secrets Manager, Lambda env, GitHub and Vercel names vs docs/security/secrets-register.yaml (root repo); the CIS "
     "role may not list secrets"),
    ("Tenant isolation probe and CRM key policy diff", "K-SEC-ISOLATION",
     "db-isolation-probe.yml and the CRM key policy diff (AA-CIS-Infra workflow runs)"),
)


def probes_month(rows: Any) -> dict:
    """Days of the period with a K-DATA-PROBES snapshot and the ones below 1.0 (a probe not passed)."""
    if rows is None or failed(rows):
        return {"error": failed(rows) or "not read"}
    vals = {r["day"]: float(r["value"]) for r in rows if r["kpi_id"] == "K-DATA-PROBES" and r["scope"] == "all"}
    red = sorted(d.isoformat() for d, v in vals.items() if v < 1.0)
    return {"days": len(vals), "red_days": red}


def sec03_actions(facts: dict, pm: dict) -> list[str]:
    out = []
    ch = facts.get("chain")
    if isinstance(ch, dict) and not failed(ch) and not ch["ok"]:
        out.append("Audit chain broken: investigate as an incident (OPS-04) before anything else.")
    st = facts.get("staff")
    if isinstance(st, dict) and not failed(st):
        if st.get("password_logins"):
            out.append(f"{st['password_logins']} active staff account(s) still have a password hash: the API refuses "
                       "password sessions; clear them in the staff directory.")
        if st.get("stale"):
            out.append(f"{st['stale']} active staff account(s) not signed in for {STALE_LOGIN_DAYS} days: confirm or "
                       "disable at the next access review (SEC-05).")
    if pm.get("red_days"):
        out.append(f"Nightly integrity probes not green on {len(pm['red_days'])} day(s): see DATA-01 in OPS-12.")
    out.append("Read the GitHub and CI sources listed under \"Source not available\" and write their counts into the "
               "root security review before approval.")
    return out


def render_sec03(reg: Registry, report: Report, first: date, last: date, facts: dict,
                 generated_at: datetime) -> Rendered:
    pm = probes_month(facts.get("probes"))
    kpis = [_kpi_line(kid, None, NOT_WIRED, f"{NOT_AVAILABLE.lower()}: {where}")
            for _section, kid, where in UNAVAILABLE if kid]
    chain_kpi = chain_line(facts.get("chain"))
    acts = sec03_actions(facts, pm)
    headline = (f"Security hygiene {first.isoformat()} to {last.isoformat()} (draft): audit chain "
                f"{chain_kpi['value'] or 'not read'}, {len(acts) - 1} action(s) from CIS sources; GitHub and CI "
                "sources to add by hand")
    body = ["## Staff accounts (CIS)", ""]
    st = facts.get("staff")
    if st is None or failed(st):
        body += [f"- Not readable: {failed(st) or 'not read'}"]
    else:
        body += table(["Active", "Owners", "With a password hash", f"Not signed in for {STALE_LOGIN_DAYS} days",
                       "Not linked to WORKFORCE", "Disabled"],
                      [[st.get("active"), st.get("owners"), st.get("password_logins"), st.get("stale"),
                        st.get("not_linked"), st.get("disabled")]])
    body += ["", "## Audit chain and refused calls (from SEC-08)", "", f"- Audit chain: {chain_kpi['value'] or '—'} "
             f"({chain_kpi['note']})."]
    rf = facts.get("refused")
    if rf is not None and not failed(rf):
        body.append(f"- Refused staff or machine calls (401 / 403): {sum(r['n'] for r in rf)} from {len(rf)} "
                    "actor/status pair(s).")
    body += ["", "## Nightly integrity probes (DATA-01 snapshots)", ""]
    body += [f"- Not readable: {pm['error']}"] if "error" in pm else [
        f"- {pm['days']} day(s) with a snapshot; not green on: {', '.join(pm['red_days']) or 'none'}."]
    body += ["", f"## {NOT_AVAILABLE}", ""]
    body += table(["Section", "KPI", "Where the number lives today"],
                  [[section, kid or "—", where] for section, kid, where in UNAVAILABLE])
    body += ["", "## Actions", ""] + [f"- {a}" for a in acts] + [""]
    return _render(reg, report, first, last, generated_at, f"Security hygiene {first.isoformat()} to "
                   f"{last.isoformat()} (draft)", headline, kpis, body, ICT_NOTE, staff=st, chain=facts.get("chain"),
                   probes=pm, actions=acts, not_available=[s for s, _k, _w in UNAVAILABLE],
                   sources_unreadable=_unreadable(facts))


# ── dispatch (generate.RENDERERS) ────────────────────────────────────────────────────────────────────────────────


async def render(conn, reg: Registry, report: Report, first: date, last: date, now: datetime) -> Rendered:
    if report.id == "SEC-08":
        return render_sec08(reg, report, first, last, await collect_sec08(conn, first, last), now)
    if report.id == "SEC-03":
        return render_sec03(reg, report, first, last, await collect_sec03(conn, first, last, now), now)
    raise LookupError(f"security.render has no report {report.id}")
