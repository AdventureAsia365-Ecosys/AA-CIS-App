#!/usr/bin/env python3
"""AA-1170 (S287 Q2 / Q4) — load the common controls and their requirement mapping into schema compliance (CIS 466).

The register (about 40 controls at the start, each mapped onto requirements of several frameworks) is private: it is
built from the S286 matrix and never committed (the repos are public). This loader is the committed half: it reads a
register file the operator provides, validates it, prints what it would change (DRY RUN, the default) and with
``--apply`` writes it in one transaction. The requirement catalogue itself is seeded by CIS 466.

    python3 load_register.py --file register.json              # DRY RUN: validate and print the plan
    python3 load_register.py --url "<presigned GET>" --apply   # write (the orchestrator, ECS exec, S3-mediated)

Connects with DATABASE_URL (inside the CIS api container: role cis_app, which holds DML on schema compliance), always
with ssl=require unless ``--no-ssl`` (a local test database). Needs only asyncpg and the standard library, so it runs
as a single downloaded file.

File format 1 (an invented example: scripts/compliance/register.example.json):

  {"format": 1,
   "controls": [{"control_id": "EX-IAM-01", "title": "...", "intent": "...", "domain": "identity", "owner_role":
                 "tech lead", "frequency": "daily", "test_type": "auto",
                 "checks": [{"check_id": "repo_rulesets", "required": true}], "scope": "dev", "active": true}],
   "mappings": [{"control_id": "EX-IAM-01", "framework": "iso27001:2022", "ref": "A.5.15", "coverage": "full",
                 "note": "..."}]}

What --apply does (one transaction): upsert every control of the file (title, intent, ... and updated_at); a control in
the table but not in the file is set inactive (never deleted: its evidence history stays readable); the whole mapping
table is replaced by the file's (the file is the complete register, so a dropped control keeps no mapping). A mapping
to a requirement the catalogue does not have, a control id or a text field the table would refuse, an owner that looks
like a person's e-mail, or text that carries an account ID, an ARN or an S3 URI stops the run before anything is
written. Exit 0 = done (or DRY RUN clean), 1 = refused.

AA-1174 (S288, CIS 471) — format 2 (format 1 is still accepted): three optional sections for the law frameworks.

  "frameworks":    [{"framework": "ex-law:2025", "title", "jurisdiction": "VN", "citation", "source_url",
                     "in_force_on": "2026-01-01", "sort_order", "active"}]          law rows only (kind law, not scored)
  "requirements":  [{"framework", "ref", "section", "title", "sort_order", "counsel_review", "applies_as":
                     "controller|processor|both", "source_url", "doc_url", "applicable"}]   obligations of those laws
  "applicability": [{"market": "DE", "segment": "traveller|agency|agency_client|staff", "framework", "test",
                     "threshold", "verdict": "applies|not_yet|not_applicable", "source_url", "source_title",
                     "assessed_on", "counsel_review", "note"}]                      the matrix (full replace)

Rules of format 2: a framework of kind standard, or one of the six frameworks CIS 466 / 471 seed, is never written (nor
its requirements); a law framework the file no longer lists is set active = false and a law obligation it no longer
lists is set applicable = false (never deleted: mappings and evidence history stay readable); every source link is
https on an official host (``OFFICIAL_HOSTS``: legislation portals and regulators; the "primary source for every
verdict" rule), our document links are https://github.com/ pages; data text never claims compliance ("compliant",
"complies", "in compliance") and an obligation label is our own short wording. Mappings may point at the law
obligations of the same file. Every applicable law obligation has a link: a ``doc_url``, or a mapping to an active
control with a required automatic check. More than ``MAX_DEACTIVATE`` active controls set inactive by one run needs
``--allow-deactivate`` (a register file missing most controls is a mistake, not a decision).
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import sys
import urllib.request
from typing import Any

CONTROL_ID = re.compile(r"^[A-Z][A-Z0-9]{1,9}(-[A-Z0-9]{1,10}){1,3}$")
CHECK_ID = re.compile(r"^[a-z][a-z0-9_]{1,63}$")
DOMAIN = re.compile(r"^[a-z][a-z0-9_]{1,40}$")
FREQUENCIES = ("continuous", "daily", "weekly", "monthly", "quarterly", "annual")
TEST_TYPES = ("auto", "manual", "hybrid")
SCOPES = ("dev", "prod", "all")
COVERAGE = ("full", "partial")
FRAMEWORKS = ("iso27001:2022", "soc2:2017", "iso42001:2023", "owasp-top10:2021", "owasp-llm:2025", "eu-ai-act:2024")
SEEDED = frozenset(FRAMEWORKS)   # seeded by CIS 466 / 471; the loader never writes them or their requirements
FRAMEWORK_ID = re.compile(r"^[a-z0-9-]+:[0-9a-z.-]+$")
REF = re.compile(r"^[A-Za-z0-9][A-Za-z0-9.()_-]{0,23}$")
MARKET = re.compile(r"^[A-Z]{2}(-[A-Z0-9]{1,3})?$")
SEGMENTS = ("traveller", "agency", "agency_client", "staff")
VERDICTS = ("applies", "not_yet", "not_applicable")
APPLIES_AS = ("controller", "processor", "both")
MAX_DEACTIVATE = 5   # technical-constant: a run that would set more active controls inactive needs --allow-deactivate
# technical-constant: the official hosts a legal source link may point at (legislation portals, official journals,
# regulators). A host and its sub-domains pass. The "primary source for every verdict" rule of AA-1174.
OFFICIAL_HOSTS = ("eur-lex.europa.eu", "edpb.europa.eu", "commission.europa.eu", "legislation.gov.uk", "ico.org.uk",
                  "legislation.gov.au", "oaic.gov.au", "acma.gov.au", "leginfo.legislature.ca.gov", "cppa.ca.gov",
                  "oag.ca.gov", "law.lis.virginia.gov", "uscode.house.gov", "ecfr.gov", "ftc.gov",
                  "gesetze-im-internet.de", "legifrance.gouv.fr", "cnil.fr", "wetten.overheid.nl",
                  "autoriteitpersoonsgegevens.nl", "vbpl.vn", "gov.vn", "chinhphu.vn", "quochoi.vn",
                  "pcisecuritystandards.org")   # gov.vn: every Vietnamese government domain (ministries, provinces)
CLAIM = re.compile(r"\b(compliant|complies|in compliance)\b", re.I)
NORMATIVE = re.compile(r"\b(shall|the organi[sz]ation)\b", re.I)
FORBIDDEN = (
    (re.compile(r"(?<![0-9A-Za-z])\d{12}(?![0-9A-Za-z])"), "an account ID"),
    (re.compile(r"arn:aws", re.I), "an ARN"),
    (re.compile(r"s3://", re.I), "an S3 URI"),
    (re.compile(r"[\w.+-]+@[\w-]+\.[\w.]+"), "an e-mail address"),
)
# The text limits of the CIS 466 CHECK constraints (the database refuses longer text anyway; this says which row).
LIMITS = {"title": (3, 160), "intent": (0, 1000), "owner_role": (2, 60), "note": (0, 300)}


def _text_problems(where: str, field: str, value: Any) -> list[str]:
    if value is None:
        return [] if field in ("intent", "note") else [f"{where}: {field} is missing"]
    if not isinstance(value, str):
        return [f"{where}: {field} is not text"]
    lo, hi = LIMITS[field]
    out = [] if lo <= len(value) <= hi else [f"{where}: {field} must be {lo}-{hi} characters"]
    out += [f"{where}: {field} carries {what}" for rx, what in FORBIDDEN if rx.search(value)]
    return out


ENUMS = (("frequency", FREQUENCIES, None), ("test_type", TEST_TYPES, None), ("scope", SCOPES, "dev"))


def _check_ok(ck: Any) -> bool:
    return (isinstance(ck, dict) and isinstance(ck.get("check_id"), str)
            and CHECK_ID.fullmatch(ck["check_id"]) is not None and isinstance(ck.get("required", True), bool))


def _control_problems(where: str, c: dict) -> list[str]:
    """The problems of one control whose id is valid."""
    out: list[str] = []
    for field in ("title", "intent", "owner_role"):
        out += _text_problems(where, field, c.get(field))
    if not isinstance(c.get("domain"), str) or not DOMAIN.fullmatch(c["domain"]):
        out.append(f"{where}: domain must match {DOMAIN.pattern}")
    out += [f"{where}: {field} must be one of {', '.join(allowed)}" for field, allowed, default in ENUMS
            if c.get(field, default) not in allowed]
    checks = c.get("checks", [])
    if not isinstance(checks, list) or len(checks) > 20:
        return out + [f"{where}: checks must be a list of at most 20"]
    if not all(_check_ok(ck) for ck in checks):
        out.append(f"{where}: a check needs check_id matching {CHECK_ID.pattern} and a boolean required")
    return out


def _controls_problems(controls: list) -> tuple[list[str], set[str]]:
    problems: list[str] = []
    ids: set[str] = set()
    for i, c in enumerate(controls):
        cid = c.get("control_id") if isinstance(c, dict) else None
        where = f"control {cid or i}"
        if not isinstance(cid, str) or not CONTROL_ID.fullmatch(cid):
            problems.append(f"{where}: control_id does not match {CONTROL_ID.pattern}")
            continue
        if cid in ids:
            problems.append(f"{where}: listed twice")
        ids.add(cid)
        problems += _control_problems(where, c)
    return problems, ids


def _mapping_problems(m: dict, ids: set[str], catalogue: set[tuple[str, str]],
                      frameworks: set[str] = frozenset(FRAMEWORKS)) -> list[str]:
    key = (m.get("control_id"), m.get("framework"), m.get("ref"))
    where = f"mapping {key}"
    out = [] if key[0] in ids else [f"{where}: control not in the file"]
    if key[1] not in frameworks:
        out.append(f"{where}: unknown framework")
    elif (key[1], key[2]) not in catalogue:
        out.append(f"{where}: the requirement catalogue (CIS 466) has no such ref")
    if m.get("coverage") not in COVERAGE:
        out.append(f"{where}: coverage must be full or partial")
    return out + _text_problems(where, "note", m.get("note"))


def validate(register: Any, catalogue: set[tuple[str, str]], db_kinds: dict[str, str] | None = None) -> list[str]:
    """Every problem of the file (empty = loadable). ``catalogue`` = the (framework, ref) pairs of
    compliance.requirement; ``db_kinds`` = framework id -> kind of compliance.framework (default: the seeded six)."""
    if not isinstance(register, dict) or register.get("format") not in (1, 2):
        return ["the register file is not format 1 or 2"]
    controls, mappings = register.get("controls"), register.get("mappings")
    if not isinstance(controls, list) or not isinstance(mappings, list):
        return ["controls and mappings must be lists"]
    kinds = dict(db_kinds) if db_kinds is not None else {f: "standard" for f in FRAMEWORKS} | {"eu-ai-act:2024": "law"}
    problems, ids = _controls_problems(controls)
    if register["format"] == 2:
        extra, added = validate_v2(register, kinds)
        problems += extra
        catalogue = set(catalogue) | added
        kinds |= {f["framework"]: "law" for f in register.get("frameworks", []) if isinstance(f, dict)
                  and isinstance(f.get("framework"), str)}
    elif any(k in register for k in ("frameworks", "requirements", "applicability")):
        problems.append("frameworks, requirements and applicability need format 2")
    seen: set[tuple] = set()
    for m in mappings:
        if not isinstance(m, dict):
            problems.append("a mapping is not an object")
            continue
        key = (m.get("control_id"), m.get("framework"), m.get("ref"))
        problems += _mapping_problems(m, ids, catalogue, set(kinds)) + ([f"mapping {key}: listed twice"]
                                                                       if key in seen else [])
        seen.add(key)
    if register["format"] == 2:
        problems += _link_problems(register)
    return problems


def _link_problems(register: dict) -> list[str]:
    """Every applicable law obligation must reach the page with a link: a doc_url, or a mapping to an active control
    of the file with a required automatic check (its evidence row links). Otherwise the Obligations tab would show the
    item with "no automatic check" and nothing to open."""
    checked = {c.get("control_id") for c in register["controls"] if isinstance(c, dict) and c.get("active", True)
               and any(isinstance(k, dict) and k.get("required") for k in (c.get("checks") or []))}
    backed = {(m.get("framework"), m.get("ref")) for m in register["mappings"]
              if isinstance(m, dict) and m.get("control_id") in checked}
    return [f"requirement {(r.get('framework'), r.get('ref'))}: no doc_url and no mapped control with a required "
            "automatic check (the page would show it without any link)"
            for r in register.get("requirements", []) if isinstance(r, dict) and r.get("applicable", True) is True
            and not r.get("doc_url") and (r.get("framework"), r.get("ref")) not in backed]


def _host_ok(url: Any, github_only: bool = False) -> bool:
    if not isinstance(url, str) or len(url) > 500 or not url.startswith("https://"):
        return False
    if github_only:
        return url.startswith("https://github.com/")
    host = url[len("https://"):].split("/", 1)[0].split(":", 1)[0].lower()
    return any(host == h or host.endswith("." + h) for h in OFFICIAL_HOSTS)


def _data_text(where: str, field: str, value: Any, lo: int, hi: int, required: bool = True) -> list[str]:
    if value is None:
        return [f"{where}: {field} is missing"] if required else []
    if not isinstance(value, str):
        return [f"{where}: {field} is not text"]
    out = [] if lo <= len(value) <= hi else [f"{where}: {field} must be {lo}-{hi} characters"]
    out += [f"{where}: {field} carries {what}" for rx, what in FORBIDDEN if rx.search(value)]
    if CLAIM.search(value):
        out.append(f"{where}: {field} claims compliance (readiness, not legal advice)")
    return out


def _framework_problems(f: Any, db_kinds: dict[str, str]) -> list[str]:
    fid = f.get("framework") if isinstance(f, dict) else None
    where = f"framework {fid}"
    if not isinstance(fid, str) or not FRAMEWORK_ID.fullmatch(fid) or len(fid) > 40:
        return [f"{where}: framework id must match {FRAMEWORK_ID.pattern} (at most 40)"]
    if fid in SEEDED or db_kinds.get(fid) == "standard":
        return [f"{where}: a seeded or standard framework is never written by the loader"]
    out = _data_text(where, "title", f.get("title"), 3, 120)
    out += _data_text(where, "citation", f.get("citation"), 0, 200, required=False)
    if f.get("jurisdiction") is not None and not (isinstance(f["jurisdiction"], str)
                                                  and MARKET.fullmatch(f["jurisdiction"])):
        out.append(f"{where}: jurisdiction must match {MARKET.pattern}")
    if not _host_ok(f.get("source_url")):
        out.append(f"{where}: source_url must be https on an official host")
    if f.get("in_force_on") is not None and not _is_date(f["in_force_on"]):
        out.append(f"{where}: in_force_on must be an ISO date")
    if f.get("scored", False) is not False:
        out.append(f"{where}: a law loaded from the register is never scored (a checklist, not a percentage)")
    if not isinstance(f.get("sort_order"), int) or f["sort_order"] < 1:
        out.append(f"{where}: sort_order must be a positive integer")
    return out


def _is_date(v: Any) -> bool:
    import datetime as _dt   # noqa: PLC0415

    try:
        _dt.date.fromisoformat(str(v))
    except ValueError:
        return False
    return isinstance(v, str)


def _requirement_problems(r: Any, laws: set[str]) -> list[str]:
    key = (r.get("framework"), r.get("ref")) if isinstance(r, dict) else (None, None)
    where = f"requirement {key}"
    if key[0] not in laws:
        return [f"{where}: the framework is not a law framework of this file"]
    out = [] if isinstance(key[1], str) and REF.fullmatch(key[1]) else [f"{where}: ref must match {REF.pattern}"]
    out += _data_text(where, "section", r.get("section"), 1, 80)
    out += _data_text(where, "title", r.get("title"), 3, 80)
    if isinstance(r.get("title"), str) and NORMATIVE.search(r["title"]):
        out.append(f"{where}: title reads like normative text (use our own words)")
    if not isinstance(r.get("sort_order"), int) or r["sort_order"] < 1:
        out.append(f"{where}: sort_order must be a positive integer")
    if r.get("applies_as") is not None and r["applies_as"] not in APPLIES_AS:
        out.append(f"{where}: applies_as must be one of {', '.join(APPLIES_AS)}")
    if r.get("source_url") is not None and not _host_ok(r["source_url"]):
        out.append(f"{where}: source_url must be https on an official host")
    if r.get("doc_url") is not None and not _host_ok(r["doc_url"], github_only=True):
        out.append(f"{where}: doc_url must be an https://github.com/ page")
    for flag in ("counsel_review", "applicable"):
        if not isinstance(r.get(flag, True), bool):
            out.append(f"{where}: {flag} must be true or false")
    return out


def _applicability_problems(a: Any, frameworks: set[str]) -> list[str]:
    key = (a.get("market"), a.get("segment"), a.get("framework")) if isinstance(a, dict) else (None, None, None)
    where = f"applicability {key}"
    out = []
    if not isinstance(key[0], str) or not MARKET.fullmatch(key[0]):
        out.append(f"{where}: market must match {MARKET.pattern}")
    if key[1] not in SEGMENTS:
        out.append(f"{where}: segment must be one of {', '.join(SEGMENTS)}")
    if key[2] not in frameworks:
        out.append(f"{where}: unknown framework")
    if not isinstance(a, dict):
        return out
    if a.get("verdict") not in VERDICTS:
        out.append(f"{where}: verdict must be one of {', '.join(VERDICTS)}")
    out += _data_text(where, "test", a.get("test"), 3, 300)
    out += _data_text(where, "threshold", a.get("threshold"), 0, 200, required=False)
    out += _data_text(where, "source_title", a.get("source_title"), 3, 160)
    out += _data_text(where, "note", a.get("note"), 0, 300, required=False)
    if not _host_ok(a.get("source_url")):
        out.append(f"{where}: source_url must be https on an official host (a primary source per verdict)")
    if not _is_date(a.get("assessed_on")):
        out.append(f"{where}: assessed_on must be an ISO date")
    if not isinstance(a.get("counsel_review", True), bool):
        out.append(f"{where}: counsel_review must be true or false")
    return out


def _unique(rows: list, key, label: str) -> list[str]:
    seen, out = set(), []
    for r in rows:
        k = key(r) if isinstance(r, dict) else None
        if k in seen:
            out.append(f"{label} {k}: listed twice")
        seen.add(k)
    return out


def validate_v2(register: dict, db_kinds: dict[str, str]) -> tuple[list[str], set[tuple[str, str]]]:
    """Problems of the format-2 sections and the (framework, ref) pairs of the law obligations they add."""
    fws, reqs, app = (register.get(k, []) for k in ("frameworks", "requirements", "applicability"))
    if not all(isinstance(x, list) for x in (fws, reqs, app)):
        return ["frameworks, requirements and applicability must be lists"], set()
    problems: list[str] = []
    for f in fws:
        problems += _framework_problems(f, db_kinds)
    laws = {f["framework"] for f in fws if isinstance(f, dict) and isinstance(f.get("framework"), str)
            and f["framework"] not in SEEDED and db_kinds.get(f["framework"]) != "standard"}
    for r in reqs:
        problems += _requirement_problems(r, laws)
    known = set(db_kinds) | laws
    for a in app:
        problems += _applicability_problems(a, known)
    problems += _unique(fws, lambda f: f.get("framework"), "framework")
    problems += _unique(reqs, lambda r: (r.get("framework"), r.get("ref")), "requirement")
    problems += _unique(app, lambda a: (a.get("market"), a.get("segment"), a.get("framework")), "applicability")
    added = {(r["framework"], r["ref"]) for r in reqs if isinstance(r, dict) and r.get("framework") in laws}
    return problems, added


def plan(register: dict, existing: set[str], law_state: dict | None = None) -> dict:
    """What --apply changes. ``law_state`` = {"frameworks": law framework ids in the table (not seeded),
    "requirements": their (framework, ref) pairs} (format 2)."""
    ids = {c["control_id"] for c in register["controls"]}
    out = {"insert": sorted(ids - existing), "update": sorted(ids & existing), "deactivate": sorted(existing - ids),
           "mappings": len(register["mappings"])}
    if register.get("format") == 2:
        law_state = law_state or {"frameworks": set(), "requirements": set()}
        fws = {f["framework"] for f in register.get("frameworks", [])}
        reqs = {(r["framework"], r["ref"]) for r in register.get("requirements", [])}
        out.update(frameworks_upsert=len(fws), frameworks_deactivate=sorted(set(law_state["frameworks"]) - fws),
                   requirements_upsert=len(reqs),
                   requirements_not_applicable=len(set(law_state["requirements"]) - reqs),
                   applicability=len(register.get("applicability", [])))
    return out


SQL_UPSERT = """
INSERT INTO compliance.control (control_id, title, intent, domain, owner_role, frequency, test_type, checks, scope,
                                active)
VALUES ($1, $2, $3, $4, $5, $6, $7, $8::jsonb, $9, $10)
ON CONFLICT (control_id) DO UPDATE
   SET title = EXCLUDED.title, intent = EXCLUDED.intent, domain = EXCLUDED.domain, owner_role = EXCLUDED.owner_role,
       frequency = EXCLUDED.frequency, test_type = EXCLUDED.test_type, checks = EXCLUDED.checks, scope = EXCLUDED.scope,
       active = EXCLUDED.active, updated_at = now()
"""
SQL_DEACTIVATE = ("UPDATE compliance.control SET active = false, updated_at = now() "
                  "WHERE NOT (control_id = ANY($1::text[])) AND active")
SQL_DELETE_MAPPINGS = "DELETE FROM compliance.requirement_mapping"
SQL_INSERT_MAPPING = ("INSERT INTO compliance.requirement_mapping (control_id, framework, ref, coverage, note) "
                      "VALUES ($1, $2, $3, $4, $5)")


SQL_UPSERT_FRAMEWORK = """
INSERT INTO compliance.framework (framework, kind, title, jurisdiction, citation, source_url, in_force_on, scored,
                                  sort_order, active)
VALUES ($1, 'law', $2, $3, $4, $5, $6::text::date, false, $7, $8)
ON CONFLICT (framework) DO UPDATE
   SET title = EXCLUDED.title, jurisdiction = EXCLUDED.jurisdiction, citation = EXCLUDED.citation,
       source_url = EXCLUDED.source_url, in_force_on = EXCLUDED.in_force_on, sort_order = EXCLUDED.sort_order,
       active = EXCLUDED.active
 WHERE compliance.framework.kind = 'law'
"""
SQL_DEACTIVATE_FRAMEWORKS = ("UPDATE compliance.framework SET active = false WHERE kind = 'law' AND active "
                             "AND NOT (framework = ANY($1::text[])) AND NOT (framework = ANY($2::text[]))")
SQL_UPSERT_REQUIREMENT = """
INSERT INTO compliance.requirement (framework, ref, section, title, applicable, sort_order, counsel_review, applies_as,
                                    source_url, doc_url)
VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10)
ON CONFLICT (framework, ref) DO UPDATE
   SET section = EXCLUDED.section, title = EXCLUDED.title, applicable = EXCLUDED.applicable,
       sort_order = EXCLUDED.sort_order, counsel_review = EXCLUDED.counsel_review, applies_as = EXCLUDED.applies_as,
       source_url = EXCLUDED.source_url, doc_url = EXCLUDED.doc_url
"""
SQL_LAW_REQUIREMENTS_OFF = """
UPDATE compliance.requirement r SET applicable = false
  FROM compliance.framework f
 WHERE f.framework = r.framework AND f.kind = 'law' AND NOT (f.framework = ANY($2::text[])) AND r.applicable
   AND NOT ((r.framework || '|' || r.ref) = ANY($1::text[]))
"""
SQL_DELETE_APPLICABILITY = "DELETE FROM compliance.applicability"
SQL_INSERT_APPLICABILITY = """
INSERT INTO compliance.applicability (market, segment, framework, test, threshold, verdict, source_url, source_title,
                                      assessed_on, counsel_review, note)
VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9::text::date, $10, $11)
"""


async def _apply_laws(conn: Any, register: dict) -> None:
    fws = register.get("frameworks", [])
    for f in fws:
        await conn.execute(SQL_UPSERT_FRAMEWORK, f["framework"], f["title"], f.get("jurisdiction"), f.get("citation"),
                           f.get("source_url"), f.get("in_force_on"), f["sort_order"], bool(f.get("active", True)))
    await conn.execute(SQL_DEACTIVATE_FRAMEWORKS, [f["framework"] for f in fws], sorted(SEEDED))
    reqs = register.get("requirements", [])
    for r in reqs:
        await conn.execute(SQL_UPSERT_REQUIREMENT, r["framework"], r["ref"], r["section"], r["title"],
                           bool(r.get("applicable", True)), r["sort_order"], bool(r.get("counsel_review", False)),
                           r.get("applies_as"), r.get("source_url"), r.get("doc_url"))
    await conn.execute(SQL_LAW_REQUIREMENTS_OFF, [f"{r['framework']}|{r['ref']}" for r in reqs], sorted(SEEDED))


async def _apply_applicability(conn: Any, register: dict) -> None:
    await conn.execute(SQL_DELETE_APPLICABILITY)
    for a in register.get("applicability", []):
        await conn.execute(SQL_INSERT_APPLICABILITY, a["market"], a["segment"], a["framework"], a["test"],
                           a.get("threshold"), a["verdict"], a["source_url"], a["source_title"], a["assessed_on"],
                           bool(a.get("counsel_review", True)), a.get("note"))


async def apply(conn: Any, register: dict) -> None:
    ids = [c["control_id"] for c in register["controls"]]
    async with conn.transaction():
        if register.get("format") == 2:
            await _apply_laws(conn, register)   # before the mappings: they may point at the new obligations
        for c in register["controls"]:
            checks = [{"check_id": k["check_id"], "required": k.get("required", True)} for k in c.get("checks", [])]
            await conn.execute(SQL_UPSERT, c["control_id"], c["title"], c.get("intent"), c["domain"], c["owner_role"],
                               c["frequency"], c["test_type"], json.dumps(checks), c.get("scope", "dev"),
                               bool(c.get("active", True)))
        await conn.execute(SQL_DEACTIVATE, ids)
        await conn.execute(SQL_DELETE_MAPPINGS)
        for m in register["mappings"]:
            await conn.execute(SQL_INSERT_MAPPING, m["control_id"], m["framework"], m["ref"], m["coverage"],
                               m.get("note"))
        if register.get("format") == 2:
            await _apply_applicability(conn, register)


def _read(args: argparse.Namespace) -> Any:
    if args.file:
        with open(args.file, encoding="utf-8") as fh:
            return json.load(fh)
    with urllib.request.urlopen(args.url, timeout=30) as resp:   # noqa: S310 - an https presigned URL
        return json.loads(resp.read(2 * 1024 * 1024))


async def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    src = ap.add_mutually_exclusive_group(required=True)
    src.add_argument("--file")
    src.add_argument("--url")
    ap.add_argument("--apply", action="store_true", help="write (default: DRY RUN)")
    ap.add_argument("--allow-deactivate", action="store_true",
                    help=f"allow setting more than {MAX_DEACTIVATE} active controls inactive in one run")
    ap.add_argument("--no-ssl", action="store_true", help="local test database only")
    args = ap.parse_args(argv)
    if args.url and not args.url.startswith("https://"):
        print("refused: --url must be https", file=sys.stderr)
        return 1
    register = _read(args)
    import asyncpg   # noqa: PLC0415 - validation and --help work without it

    conn = await asyncpg.connect(os.environ["DATABASE_URL"], ssl=None if args.no_ssl else "require")
    try:
        kinds = {r["framework"]: r["kind"] for r in await conn.fetch(
            "SELECT framework, kind FROM compliance.framework")}
        catalogue = {(r["framework"], r["ref"]) for r in await conn.fetch(
            "SELECT framework, ref FROM compliance.requirement")}
        problems = validate(register, catalogue, kinds)
        if problems:
            print(json.dumps({"refused": problems[:50], "problems": len(problems)}, indent=1))
            return 1
        rows = await conn.fetch("SELECT control_id, active FROM compliance.control")
        existing = {r["control_id"] for r in rows}
        active = {r["control_id"] for r in rows if r["active"]}
        law_fws = {f for f, k in kinds.items() if k == "law" and f not in SEEDED}
        law_reqs = {(r["framework"], r["ref"]) for r in await conn.fetch(
            "SELECT framework, ref FROM compliance.requirement WHERE applicable AND framework = ANY($1::text[])",
            sorted(law_fws))}
        out = {"mode": "apply" if args.apply else "dry_run",
               **plan(register, existing, {"frameworks": law_fws, "requirements": law_reqs})}
        turned_off = sorted(set(out["deactivate"]) & active)
        out["deactivate_active"] = len(turned_off)
        if len(turned_off) > MAX_DEACTIVATE and not args.allow_deactivate:
            out["refused"] = (f"{len(turned_off)} active controls would be set inactive (more than "
                              f"{MAX_DEACTIVATE}); is the file complete? Re-run with --allow-deactivate if intended.")
            print(json.dumps(out, indent=1))
            return 1
        if args.apply:
            await apply(conn, register)
            out["controls_now"] = await conn.fetchval("SELECT count(*) FROM compliance.control WHERE active")
            out["mappings_now"] = await conn.fetchval("SELECT count(*) FROM compliance.requirement_mapping")
            if register.get("format") == 2:
                out["law_frameworks_now"] = await conn.fetchval(
                    "SELECT count(*) FROM compliance.framework WHERE kind = 'law' AND active")
                out["law_obligations_now"] = await conn.fetchval(
                    "SELECT count(*) FROM compliance.requirement r JOIN compliance.framework f USING (framework) "
                    "WHERE f.kind = 'law' AND r.applicable")
                out["applicability_now"] = await conn.fetchval("SELECT count(*) FROM compliance.applicability")
        print(json.dumps(out, indent=1))
        return 0
    finally:
        await conn.close()


if __name__ == "__main__":
    sys.exit(asyncio.run(main(sys.argv[1:])))
