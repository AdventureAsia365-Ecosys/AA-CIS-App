"""AA-900 / AA-741 (S271 W0, tech lead D7) — CI lint: no writer route falls to another model family, and no judge
instrument falls to Nova Lite.

The routes are the decided route table the migrations leave: the LATEST ``snapshots/*_routes_after_<NNN>.json`` (each
one proved equal to what its migration produces by ``tests/integration/test_*_migration_<NNN>.py``; today
``aa900_routes_after_383.json``, migration 383). A later route migration adds its own snapshot and is linted the
same way, as are the code fallbacks the gateway uses when the database cannot be read (``role_config.SAFE_DEFAULTS``).
The families come from the catalog the route tests share (``test_aa1042_anthropic_catalog_376._models``: the 304 /
306 / 324 / 366 / 367 / 376 rows, ``CatalogModel.family_key``).

Rules (audit design-review arch.roadmap[0] W0 and crit F4, migration 383):
- W0-1: every model of a ``role = writer`` route (primary, fallbacks, shadow) is of its primary's family. A writer
  that silently answers on another maker's model writes text no writer eval qualified (and that the same family
  then verifies). Before 383 the OpenAI API rung (``gpt-6-luna-openai``) ended 13 writer routes.
- W0-2: no ``role = judge`` route and no validate instrument (``INSTRUMENT_VALIDATE``) names ``nova-lite`` (primary,
  fallbacks, shadow): its kappa against the human labels was 0.137 (eval run 08c34cb4). A validate row may keep it
  only when ``NOVA_LITE_KEPT`` names the row with the reason 383 recorded; a new validate stage is refused by default.

Also: the decided table still keeps every chain rule (``llm_route_chains.violations``), and the admin route control
accepts 383's chains as a proposal from the 376 table (valid, every stage serving, the writer / judge changes flagged
as needing evidence: 383's break-glass).
"""
from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from tests.unit.test_aa1042_anthropic_catalog_376 import _live, _models, _preview, _registry

SNAPSHOTS = Path(__file__).resolve().parent / "snapshots"
SNAPSHOT_RE = re.compile(r"^[a-z0-9]+_routes_after_([0-9]{3})\.json$")
UNCALIBRATED_JUDGE_RUNG = "nova-lite"
OTHER_MAKER_WRITER_RUNG = "gpt-6-luna-openai"
# validate stages scored as instruments (W2), held to W0-2 like every judge
INSTRUMENT_VALIDATE = frozenset({"t10_claims"})
# validate rows that keep nova-lite on purpose (migration 383 header, decided per row)
NOVA_LITE_KEPT = {
    "t9_faq_filter": "drop-only FAQ check behind T9_FAQ_LLM_FILTER; on failure the deterministic list is kept; "
                     "primary is Claude, nova-lite is its only other-maker rung",
    "t9_faq_answer_fit": "drop-only FAQ answer check behind T9_FAQ_LLM_FILTER, never a hold; primary is Claude",
    "a3_atomize": "A3 evidence producer, measured in W1 / W2 (F10) before any change",
    "a3_place_card": "A3 place card producer (GPT + Amazon, disjoint from its Claude + Meta check)",
    "a3_place_days": "A3 place day skeleton, an evidence producer",
    "a3_place_dedupe_check": "A3 duplicate-place check (Claude + Amazon, disjoint from the GPT + Meta dedupe)",
    "a3_place_locate": "A3 place locate, an evidence producer",
    "s1_source_facts": "S1 per-day source facts, an evidence producer",
}
# 383's decided removals (the migration's table)
WRITERS_383 = ("a0_column_map", "s1_flag_fix", "s1_generate", "s1_itinerary_nudge", "segment_research",
               "segment_research_batch", "supply_content_writer", "t2_flag_fix", "t2_generate", "t2_itinerary_nudge",
               "t8_angle_gen", "t9_repair", "t9_write")
INSTRUMENTS_383 = ("s1_brand_audit", "s1_judge", "t10_adjudicate", "t10_claims", "t10_judge", "t2_brand_audit",
                   "t2_judge", "t7_debate_judge", "writing_memory_eval", "writing_memory_mine")
AFTER_376 = SNAPSHOTS / "aa1042_routes_after_376.json"


def latest_snapshot() -> Path:
    """The route snapshot of the highest migration number (the decided final route state)."""
    found = [(int(m.group(1)), p) for p in SNAPSHOTS.iterdir() if (m := SNAPSHOT_RE.match(p.name))]
    assert found, "no route snapshot"
    return max(found)[1]


def _routes(path: Path) -> dict:
    return {r["stage"]: r for r in json.loads(path.read_text(encoding="utf-8"))["routes"]}


def _get(r, key: str):
    """A field of a snapshot row (dict) or of a ``StageConfig`` (SAFE_DEFAULTS)."""
    return r.get(key) if isinstance(r, dict) else getattr(r, key, None)


def _chain(r) -> list:
    return [k for k in [_get(r, "model_id"), *(_get(r, "fallback_model_ids") or ()), _get(r, "shadow_model_id")] if k]


def writer_family_breaks(routes, families: dict) -> list[tuple[str, str]]:
    """W0-1 (pure): (stage, model) for each model of a writer route that is not of the primary's family (a model
    the catalog does not know counts as a break: its family cannot be shown to match)."""
    out = []
    for r in routes:
        if _get(r, "role") != "writer":
            continue
        chain = _chain(r)
        primary = families.get(chain[0])
        out += [(_get(r, "stage"), k) for k in chain if primary is None or families.get(k) != primary]
    return out


def instrument_nova_breaks(routes) -> list[str]:
    """W0-2 (pure): the judge / instrument stages that name nova-lite, and the validate stages that name it without
    a NOVA_LITE_KEPT reason."""
    out = []
    for r in routes:
        if UNCALIBRATED_JUDGE_RUNG not in _chain(r):
            continue
        stage, role = _get(r, "stage"), _get(r, "role")
        if role == "judge" or stage in INSTRUMENT_VALIDATE or (role == "validate" and stage not in NOVA_LITE_KEPT):
            out.append(stage)
    return out


@pytest.fixture(scope="module")
def families() -> dict:
    return {k: m.family_key for k, m in _models().items()}


# ── the lint ───────────────────────────────────────────────────────────────────────────────────────────────────────

def test_the_lint_reads_the_route_state_383_or_later():
    n = int(SNAPSHOT_RE.match(latest_snapshot().name).group(1))
    assert n >= 383


def test_w0_1_no_writer_route_names_a_model_of_another_family(families):
    assert writer_family_breaks(_routes(latest_snapshot()).values(), families) == []


def test_w0_2_no_judge_or_instrument_route_names_nova_lite():
    assert instrument_nova_breaks(_routes(latest_snapshot()).values()) == []


def test_the_nova_lite_allowlist_names_only_validate_rows_that_still_keep_it():
    routes = _routes(latest_snapshot())
    for stage, reason in NOVA_LITE_KEPT.items():
        assert stage in routes and routes[stage]["role"] == "validate" and reason, stage
        assert UNCALIBRATED_JUDGE_RUNG in _chain(routes[stage]), f"{stage}: no longer needs the allowlist"
    assert not set(NOVA_LITE_KEPT) & INSTRUMENT_VALIDATE


def test_the_code_fallback_routes_keep_the_w0_rules(families):
    """SAFE_DEFAULTS: the routes the gateway runs when the database cannot be read (legacy keys included)."""
    from shared.llm_client.role_config import SAFE_DEFAULTS
    assert writer_family_breaks(SAFE_DEFAULTS.values(), families) == []
    assert instrument_nova_breaks(SAFE_DEFAULTS.values()) == []


def test_the_lint_is_not_vacuous(families):
    """The 376 table (before 383) breaks both rules on exactly the stages 383 changes."""
    before = _routes(AFTER_376).values()
    assert sorted({s for s, _ in writer_family_breaks(before, families)}) == sorted(WRITERS_383)
    assert {k for _, k in writer_family_breaks(before, families)} == {OTHER_MAKER_WRITER_RUNG}
    assert sorted(instrument_nova_breaks(before)) == sorted(INSTRUMENTS_383)
    bad_new_validate = {"stage": "zz_new_check", "role": "validate", "model_id": "haiku-5-5",
                        "fallback_model_ids": ["nova-lite"], "shadow_model_id": None}
    assert instrument_nova_breaks([bad_new_validate]) == ["zz_new_check"]
    unknown = {"stage": "zz_writer", "role": "writer", "model_id": "sonnet-5-5",
               "fallback_model_ids": ["zz-unknown-model"], "shadow_model_id": None}
    assert writer_family_breaks([unknown], families) == [("zz_writer", "zz-unknown-model")]


# ── 383's table: only the rung removed, the chain rules and the route control ────────────────────────────────────

def test_383_removes_only_the_rung_and_keeps_the_order():
    before, after = _routes(AFTER_376), _routes(SNAPSHOTS / "aa900_routes_after_383.json")
    assert set(before) == set(after)
    for stage, r in after.items():
        drop = (OTHER_MAKER_WRITER_RUNG if stage in WRITERS_383 else
                UNCALIBRATED_JUDGE_RUNG if stage in INSTRUMENTS_383 else None)
        want = [k for k in before[stage]["fallback_model_ids"] if k != drop]
        assert r == {**before[stage], "fallback_model_ids": want}, stage
        if drop:
            assert r["fallback_model_ids"], stage                          # every changed route keeps a fallback


def test_every_route_keeps_the_chain_rules_after_383():
    from api.services.llm_route_chains import violations
    models, _, _, reach, scopes, acc2_only = _registry()
    assert violations(_routes(latest_snapshot()).values(), models.values(), reach, acc2_only, scopes) == []


def test_the_route_control_accepts_383_as_a_proposal_from_the_376_table():
    """llm_route_control.preview_world: chain_problems (the S235 vendor_last_resort rule, the writer / judge family
    options, scopes) on every changed stage, the chain rules, and the $0 resolver: every stage still serves."""
    _, _, deps, _, _, _ = _registry()
    after = _routes(SNAPSHOTS / "aa900_routes_after_383.json")
    out = _preview(_live(_routes(AFTER_376), deps),
                   routes={s: [r["model_id"], *r["fallback_model_ids"]] for s, r in after.items()})
    assert out["valid"] is True and out["errors"] == [], out["errors"]
    assert not [w for w in out["warnings"] if not w.get("existing")], out["warnings"]
    assert {c["stage"] for c in out["diff"] if c["kind"] == "route"} == set(WRITERS_383) | set(INSTRUMENTS_383)
    served = {s["stage"]: s["first"] for s in out["stages"]}
    assert served and all(served.values()), served
    assert served["t9_write"] == "sonnet-5-5@anthropic-direct"           # the Anthropic API first (route version 4)
    assert served["t10_judge"] == f"{OTHER_MAKER_WRITER_RUNG}@openai-direct"   # GPT-6 Luna's twin, acc3 refusing
    # a writer / judge change needs evidence: 383 is the break-glass (tech lead D7); t10_claims is a validate row
    assert out["needs_evidence"] is True
    assert set(out["evidence_stages"]) >= set(WRITERS_383) | (set(INSTRUMENTS_383) - INSTRUMENT_VALIDATE)
