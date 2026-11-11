"""AA-1049 W3 (root ADR 0020 S5) — the ONE T9 route table: what the attempt loop does with a failed gate.

Before this module the T9 repair loop routed a failed gate through about nine hand-kept sets spread over three modules
(sentence_repair `_ELIGIBLE_GATES` / `_QUOTED_GATES` / `OWNED_ELSEWHERE` / `TRUTH_GATES`, quality_gates
`CHEAP_REPAIR_GATES` / `STYLE_ONLY_GATES` / `OWNED_BY_FIX_STEP`, service `TRUTH_CUT_GATES` / `F17_CUT_GATE` /
`F9_CUT_NOT_HOLD_CHANNELS`). Each of them is now a VIEW of one row per gate. A row joins, on `gate`:

- the POLICY half from the compiled writing policy (`writing_policy.yaml` route_rows: `repair` stage, `style_only`);
- the loop MECHANICS from `route_table.yaml` next to this module (`action`, `parse`, `section`, `cut`, field
  overrides; kept out of the policy file because the policy_sha is hashed into 16 registered prompt versions and a
  mechanics change is not a prompt change);
- the gate's error classes from the taxonomy (`taxonomy.yaml`, through `registry.gate_classes`).

A row's route for a failed blocking entry:

- ``repair_sentence``: one sentence repair (`sentence_repair.plan`); `parse` says how its violations quote a sentence;
- ``regenerate_section``: a deterministic / one-call step owns it (FAQ regeneration, first-line rewrite, SEO title
  and meta fit); it never sends the piece to a rewrite (a `slug` violation does: the fit never writes a slug);
- ``rewrite``: the full rewrite (attempt 2 only);

and what is left after the repair budget: ``cut`` (a `truth` row's item may be cut in code on the last attempt; a
`cut_finding` row's item only when its finding says to cut) or ``hold`` (`quality_gates.hold_failure`, never for a
`style_only` row; the F9 classes by `F9_CLASS_ACTIONS`).

The same mechanics file carries the T2 rows (``t2``, the linear orchestrator) and the S1 rows (``s1``, the S1 train of
``services/content_generation/s1_train.py``: one row per S1 code of the taxonomy, actions cut / regenerate_section /
rewrite / hold; no model sentence repair since S277); both are opt-in paths and the default paths never read them.

Pure: reads the compiled policy, `route_table.yaml` and the registry, imports nothing from the content-writing modules
(quality_gates and sentence_repair import it). The derived sets are byte-equal to the S271 hand-written ones
(tests/unit/test_aa926_equivalence.py replays every routing decision unchanged; tests/unit/quality/
test_aa1049_route_table.py is the coverage lint: exactly one row per T10 gate id).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Optional

import yaml

from services.quality import registry as qr
from services.quality import writing_policy as _wp

MECHANICS_PATH = Path(__file__).resolve().parent / "route_table.yaml"
MECHANICS_VERSION = "t9-route-table-v1"

REPAIR_SENTENCE = "repair_sentence"
REGENERATE_SECTION = "regenerate_section"
REWRITE = "rewrite"
CUT = "cut"
HOLD = "hold"
ACTIONS = (REPAIR_SENTENCE, REGENERATE_SECTION, REWRITE, CUT, HOLD)

PARSE_QUOTED = "quoted"
PARSE_F1 = "f1"
PARSE_F9 = "f9"
PARSE_BRAND_STYLE = "brand_style"
SECTION_SEO_FIELDS = "seo_fields"
CUT_TRUTH = "truth"
CUT_FINDING = "cut_finding"
ROW_ACTIONS = (REPAIR_SENTENCE, REGENERATE_SECTION, REWRITE)
PARSES = (PARSE_QUOTED, PARSE_F1, PARSE_F9, PARSE_BRAND_STYLE)
SECTIONS = ("faq", "first_line", SECTION_SEO_FIELDS)
CUTS = (CUT_TRUTH, CUT_FINDING)
FIELD_ACTIONS = {"slug": (REWRITE,)}
# Parses whose violations may name the SEO title or meta ("title …" / "meta …"): such a violation belongs to the
# seo_fields section (the SEO fit), not to a sentence of the body.
SEO_FIELD_PARSES = (PARSE_QUOTED, PARSE_F9)


@dataclass(frozen=True)
class Route:
    gate: str
    action: str                      # repair_sentence | regenerate_section | rewrite
    repair: str                      # S271 stage: cheap | fix_step | default
    style_only: bool = False         # may block for one repair, never holds a piece
    parse: Optional[str] = None      # repair_sentence: quoted | f1 | f9 | brand_style
    section: Optional[str] = None    # regenerate_section: faq | first_line | seo_fields
    cut: Optional[str] = None        # truth | cut_finding
    fields: dict = field(default_factory=dict)  # per-field override, e.g. {"slug": "rewrite"}
    classes: tuple = ()              # the gate's taxonomy error classes

    @property
    def cheap(self) -> bool:
        return self.repair == "cheap"

    @property
    def fix_step(self) -> bool:
        return self.repair == "fix_step"

    @property
    def holds(self) -> bool:
        """What is left of this gate after the repair budget holds the piece (style-only gates never do)."""
        return not self.style_only


class RouteTableError(ValueError):
    """route_table.yaml breaks its schema or disagrees with the policy route rows (raised at import: fail closed)."""


@lru_cache(maxsize=1)
def mechanics() -> dict:
    with open(MECHANICS_PATH, encoding="utf-8") as fh:
        return yaml.safe_load(fh)


def _policy_rows() -> list[dict]:
    return list(_wp.compiled()["route_rows"]["gates"])


def _need(cond: bool, msg: str, out: list[str]) -> None:
    if not cond:
        out.append(msg)


def row_errors(row: dict, policy_row: Optional[dict]) -> list[str]:
    """The schema problems of one mechanics row against its policy row: the action vocabulary, a parse exactly on a
    sentence repair, a section exactly on a section regeneration, a cut only on a sentence repair, and the policy's
    repair stage consistent with the action (a `cheap` row is a sentence repair; a `fix_step` row is a faq /
    first-line regeneration and every faq / first-line regeneration is a fix step)."""
    e: list[str] = []
    where = f"route_table.yaml {row.get('gate')}"
    action = row.get("action")
    _need(action in ROW_ACTIONS, f"{where}: action must be one of {ROW_ACTIONS}", e)
    _need((action == REPAIR_SENTENCE) == (row.get("parse") is not None),
          f"{where}: a parse is required on, and only on, a repair_sentence row", e)
    _need(row.get("parse") is None or row["parse"] in PARSES, f"{where}: parse must be one of {PARSES}", e)
    _need((action == REGENERATE_SECTION) == (row.get("section") is not None),
          f"{where}: a section is required on, and only on, a regenerate_section row", e)
    _need(row.get("section") is None or row["section"] in SECTIONS, f"{where}: section must be one of {SECTIONS}", e)
    _need(row.get("cut") is None or (row["cut"] in CUTS and action == REPAIR_SENTENCE),
          f"{where}: cut must be one of {CUTS}, on a repair_sentence row", e)
    for fld, allowed in FIELD_ACTIONS.items():
        _need(row.get(fld) is None or row[fld] in allowed, f"{where}: {fld} must be one of {allowed}", e)
    unknown = set(row) - {"gate", "action", "parse", "section", "cut", *FIELD_ACTIONS}
    _need(not unknown, f"{where}: unknown keys {sorted(unknown)} (repair / style_only belong to the policy)", e)
    if policy_row is None:
        e.append(f"{where}: no writing_policy route_rows row")
        return e
    repair = policy_row.get("repair")
    _need(repair != "cheap" or action == REPAIR_SENTENCE, f"{where}: the policy's cheap row must be a repair_sentence",
          e)
    _need((repair == "fix_step") == (action == REGENERATE_SECTION and row.get("section") != SECTION_SEO_FIELDS),
          f"{where}: the policy's fix_step is exactly a faq / first_line regenerate_section", e)
    return e


def schema_errors(doc: Optional[dict] = None, policy_rows: Optional[list[dict]] = None) -> list[str]:
    """Every problem of the mechanics file against the policy route rows ([] = the table builds)."""
    doc = mechanics() if doc is None else doc
    policy_rows = _policy_rows() if policy_rows is None else policy_rows
    e: list[str] = []
    _need(doc.get("version") == MECHANICS_VERSION, f"route_table.yaml version must be {MECHANICS_VERSION!r}", e)
    rows = [r for r in doc.get("rows") or [] if isinstance(r, dict)]
    gates = [r.get("gate") for r in rows]
    _need(len(rows) == len(doc.get("rows") or []), "route_table.yaml rows must be mappings", e)
    _need(len(gates) == len(set(gates)), "route_table.yaml: exactly one row per gate", e)
    by_gate = {r["gate"]: r for r in policy_rows}
    _need(set(gates) == set(by_gate), "route_table.yaml and writing_policy route_rows must route the same gates "
          f"(only here: {sorted(set(gates) - set(by_gate))}, "
          f"only in the policy: {sorted(set(by_gate) - set(gates))})", e)
    for r in rows:
        e.extend(row_errors(r, by_gate.get(r.get("gate"))))
    channels = (doc.get("truth_cut") or {}).get("cut_not_hold_channels")
    _need(isinstance(channels, list) and all(isinstance(c, str) for c in channels),
          "route_table.yaml truth_cut.cut_not_hold_channels must be a list of channels", e)
    return e


def _build() -> dict[str, Route]:
    errors = schema_errors()
    if errors:
        raise RouteTableError("; ".join(errors))
    policy = {r["gate"]: r for r in _policy_rows()}
    return {r["gate"]: Route(gate=r["gate"], action=r["action"], repair=policy[r["gate"]]["repair"],
                             style_only=bool(policy[r["gate"]]["style_only"]), parse=r.get("parse"),
                             section=r.get("section"), cut=r.get("cut"),
                             fields={f: r[f] for f in FIELD_ACTIONS if r.get(f)},
                             classes=tuple(qr.gate_classes(r["gate"])))
            for r in mechanics()["rows"]}


TABLE: dict[str, Route] = _build()
# Row order = route_table.yaml order (the truth-cut merge preference reads it).
ORDER: tuple[str, ...] = tuple(TABLE)


def route(gate: Optional[str]) -> Optional[Route]:
    """The row of a gate, None for a name with no row (a flag, telemetry, a retired or T2 gate)."""
    return TABLE.get(gate) if gate else None


def action_of(gate: Optional[str]) -> Optional[str]:
    r = route(gate)
    return r.action if r else None


def _gates(pred) -> frozenset:
    return frozenset(g for g, r in TABLE.items() if pred(r))


# ------------------------------------------------------------------------------- the S271 sets, as views of the table
SENTENCE_REPAIR_GATES = _gates(lambda r: r.action == REPAIR_SENTENCE)          # sentence_repair._ELIGIBLE_GATES
QUOTED_GATES = _gates(lambda r: r.parse == PARSE_QUOTED)                       # sentence_repair._QUOTED_GATES
CHEAP_REPAIR_GATES = _gates(lambda r: r.cheap)                                  # quality_gates.CHEAP_REPAIR_GATES
STYLE_ONLY_GATES = _gates(lambda r: r.style_only)                               # quality_gates.STYLE_ONLY_GATES
OWNED_BY_FIX_STEP = _gates(lambda r: r.fix_step)                                # quality_gates.OWNED_BY_FIX_STEP
SECTION_GATES = _gates(lambda r: r.action == REGENERATE_SECTION)               # never sent to a rewrite
SEO_FIELD_GATES = _gates(lambda r: r.section == SECTION_SEO_FIELDS)            # F4_seo_surface
SLUG_REWRITE_GATES = _gates(lambda r: r.fields.get("slug") == REWRITE)
TRUTH_CUT_GATES = _gates(lambda r: r.cut == CUT_TRUTH)                          # service.TRUTH_CUT_GATES
TRUTH_GATES: tuple[str, ...] = tuple(g for g in ORDER if TABLE[g].cut == CUT_TRUTH)  # sentence_repair.TRUTH_GATES
CUT_FINDING_GATES = _gates(lambda r: r.cut == CUT_FINDING)                      # {service.F17_CUT_GATE}
CUT_NOT_HOLD_CHANNELS = frozenset(mechanics()["truth_cut"]["cut_not_hold_channels"])
F9_CLASS_ACTIONS: dict[str, str] = {r["class"]: r["action"] for r in _wp.compiled()["route_rows"]["f9_classes"]}


def parse_of(gate: Optional[str]) -> Optional[str]:
    r = route(gate)
    return r.parse if r else None


def is_seo_field_violation(gate: Optional[str], violation: str) -> bool:
    """A violation of a quoted / F9 gate that names the SEO title or meta (owned by the seo_fields section)."""
    return parse_of(gate) in SEO_FIELD_PARSES and str(violation).startswith(("title ", "meta "))


def coverage_errors() -> list[str]:
    """The lint (tests/unit/quality/test_aa1049_route_table.py): exactly one row per live T10 gate id of the taxonomy,
    every row a registry gate with at least one error class, every merged ledger entry's sources routed, and every
    F9 class of f9_policy with an action."""
    out: list[str] = []
    live = {g for g, info in qr.gates().items()
            if info.get("kind") == "gate" and not info.get("retired") and info.get("channels") != ["t2_version"]}
    rows = [r.get("gate") for r in mechanics().get("rows") or []]
    for g in sorted(live - set(rows)):
        out.append(f"T10 gate {g!r} has no route row")
    for dup in sorted({g for g in rows if rows.count(g) > 1}):
        out.append(f"gate {dup!r} has {rows.count(dup)} route rows")
    known = set(qr.GATE_CONSTANTS.values())
    for g in rows:
        if g not in known:
            out.append(f"route row {g!r} is not a registry gate")
        elif not qr.gate_classes(g):
            out.append(f"route row {g!r} has no taxonomy error class")
    for merged, sources in qr.MERGED_GATES.items():
        for s in sources:
            if s not in TABLE:
                out.append(f"source {s!r} of merged entry {merged!r} has no route row")
    return out


# ------------------------------------------------------------------------------- T2 (AA-1049 W3, T2 train, opt-in)
# The T2 linear orchestrator (services/content_generation/t2_orchestrator.py, T2_ORCHESTRATOR=linear) routes its
# enforce findings through the `t2` section of the same mechanics file. The default T2 path (LangGraph + T3, main)
# never reads it. Rows are keyed by finding code; the status rule does not read them (no row can pass a version).
T2_MECHANICS_VERSION = "t2-route-table-v1"
T2_ACTIONS = (REPAIR_SENTENCE, REGENERATE_SECTION, REWRITE, HOLD)
T2_PARSES = ("t3_grounding",)
T2_SECTIONS = (SECTION_SEO_FIELDS,)


@dataclass(frozen=True)
class T2Route:
    code: str
    action: str                      # repair_sentence | regenerate_section | rewrite | hold
    parse: Optional[str] = None      # repair_sentence: t3_grounding
    section: Optional[str] = None    # regenerate_section: seo_fields


def t2_schema_errors(doc: Optional[dict] = None) -> list[str]:
    """Every problem of the mechanics file's `t2` section ([] = the T2 table builds)."""
    sec = (mechanics() if doc is None else doc).get("t2") or {}
    e: list[str] = []
    _need(sec.get("version") == T2_MECHANICS_VERSION, f"route_table.yaml t2.version must be {T2_MECHANICS_VERSION!r}",
          e)
    _need(sec.get("default") in T2_ACTIONS, f"route_table.yaml t2.default must be one of {T2_ACTIONS}", e)
    rows = sec.get("rows") or []
    codes = [r.get("code") if isinstance(r, dict) else None for r in rows]
    _need(all(isinstance(r, dict) for r in rows), "route_table.yaml t2.rows must be mappings", e)
    _need(len(codes) == len(set(codes)), "route_table.yaml t2: exactly one row per code", e)
    for r in (r for r in rows if isinstance(r, dict)):
        where = f"route_table.yaml t2 {r.get('code')}"
        action = r.get("action")
        _need(bool(r.get("code")), f"{where}: a code is required", e)
        _need(action in T2_ACTIONS, f"{where}: action must be one of {T2_ACTIONS}", e)
        _need((action == REPAIR_SENTENCE) == (r.get("parse") is not None),
              f"{where}: a parse is required on, and only on, a repair_sentence row", e)
        _need(r.get("parse") is None or r["parse"] in T2_PARSES, f"{where}: parse must be one of {T2_PARSES}", e)
        _need((action == REGENERATE_SECTION) == (r.get("section") is not None),
              f"{where}: a section is required on, and only on, a regenerate_section row", e)
        _need(r.get("section") is None or r["section"] in T2_SECTIONS,
              f"{where}: section must be one of {T2_SECTIONS}", e)
        unknown = set(r) - {"code", "action", "parse", "section"}
        _need(not unknown, f"{where}: unknown keys {sorted(unknown)}", e)
    return e


def _build_t2() -> tuple[dict[str, T2Route], str]:
    errors = t2_schema_errors()
    if errors:
        raise RouteTableError("; ".join(errors))
    sec = mechanics()["t2"]
    return ({r["code"]: T2Route(code=r["code"], action=r["action"], parse=r.get("parse"), section=r.get("section"))
             for r in sec["rows"]}, sec["default"])


T2_TABLE, T2_DEFAULT = _build_t2()


def t2_route(code: Optional[str]) -> T2Route:
    """The T2 row of a finding code; a code with no row gets the `default` action (fail closed: still enforced)."""
    return T2_TABLE.get(code or "") or T2Route(code=code or "", action=T2_DEFAULT)


# ------------------------------------------------------------------------------- S1 (AA-1049 W3, S1 train, opt-in)
# The S1 control plane of services/content_generation/s1_train.py (S1_CONTROL=contract) routes every S1 code through
# the `s1` section of the same mechanics file; the default A1 path (main) never reads it. Rows are keyed by the S1
# codes of the taxonomy (`a1_codes`); the publish gate does not read them (no row can publish a tour).
S1_MECHANICS_VERSION = "s1-route-table-v1"
# S277 (AA-1049): no repair_sentence on S1: the train's path makes no s1_flag_fix call, so the schema refuses a row that
# would bring a model sentence repair back (the grounding codes are `cut` rows, grounding.cut_and_recheck).
S1_ACTIONS = (CUT, REGENERATE_SECTION, REWRITE, HOLD)


@dataclass(frozen=True)
class S1Route:
    code: str
    action: str                      # cut | regenerate_section | rewrite | hold
    classes: tuple = ()              # the code's taxonomy error classes


def s1_schema_errors(doc: Optional[dict] = None) -> list[str]:
    """Every problem of the mechanics file's `s1` section ([] = the S1 table builds)."""
    sec = (mechanics() if doc is None else doc).get("s1") or {}
    e: list[str] = []
    _need(sec.get("version") == S1_MECHANICS_VERSION, f"route_table.yaml s1.version must be {S1_MECHANICS_VERSION!r}",
          e)
    _need(sec.get("default") in S1_ACTIONS, f"route_table.yaml s1.default must be one of {S1_ACTIONS}", e)
    rows = sec.get("rows") or []
    _need(all(isinstance(r, dict) for r in rows), "route_table.yaml s1.rows must be mappings", e)
    codes = [r.get("code") for r in rows if isinstance(r, dict)]
    _need(len(codes) == len(set(codes)), "route_table.yaml s1: exactly one row per code", e)
    for r in (r for r in rows if isinstance(r, dict)):
        where = f"route_table.yaml s1 {r.get('code')}"
        _need(bool(r.get("code")), f"{where}: a code is required", e)
        _need(r.get("action") in S1_ACTIONS, f"{where}: action must be one of {S1_ACTIONS}", e)
        unknown = set(r) - {"code", "action"}
        _need(not unknown, f"{where}: unknown keys {sorted(unknown)}", e)
    return e


def s1_coverage_errors(doc: Optional[dict] = None) -> list[str]:
    """The S1 lint: exactly one row per S1 code of the taxonomy (`a1_codes`), no row for anything else, and every
    routed code has at least one taxonomy error class."""
    sec = (mechanics() if doc is None else doc).get("s1") or {}
    rows = [r.get("code") for r in sec.get("rows") or [] if isinstance(r, dict)]
    codes = qr.a1_codes()
    out = [f"S1 code {c!r} has no route row" for c in sorted(set(codes) - set(rows))]
    out += [f"S1 route row {c!r} is not an S1 code of the taxonomy (a1_codes)" for c in sorted(set(rows) - set(codes))]
    out += [f"S1 code {d!r} has {rows.count(d)} route rows" for d in sorted({c for c in rows if rows.count(c) > 1})]
    out += [f"S1 route row {c!r} has no taxonomy error class" for c in rows
            if c in codes and not (codes[c] or {}).get("classes")]
    return out


def _build_s1() -> tuple[dict[str, S1Route], str]:
    errors = s1_schema_errors()
    if errors:
        raise RouteTableError("; ".join(errors))
    sec = mechanics()["s1"]
    codes = qr.a1_codes()
    return ({r["code"]: S1Route(code=r["code"], action=r["action"],
                                classes=tuple((codes.get(r["code"]) or {}).get("classes") or ()))
             for r in sec["rows"]}, sec["default"])


S1_TABLE, S1_DEFAULT = _build_s1()


def s1_route(code: Optional[str]) -> S1Route:
    """The S1 row of a code; a code with no row gets the `default` action (fail closed: hold)."""
    return S1_TABLE.get(code or "") or S1Route(code=code or "", action=S1_DEFAULT)
