"""
services.acp_produce.research — N7 C1/C2 (DataForSEO keyword + SERP per
SLOT) + C3 (Brief compile + demand-law reject), AA-369.

STEP 0 (Linear AA-369 comment, 06/08/2026) confirmed: C1/C2 and C3's core
(demand-law reject, Brief assembly) are fully deterministic — no LLM. The
one genuinely semantic piece, `gap_statement` (comparing real top-ranking
page content against our atom set), IS LLM work; Nghiep confirmed keeping it
in this issue's scope using Bedrock satellite acc1 Haiku
(shared/llm_client/bedrock_satellite.py, ADR-2026-031 T2) — explicitly NOT
Palmyra X5 (AA-336/AA-334 bake-off recheck still open, S130-S133 carryover).
See build_gap_statement() below.

Scope boundaries kept on purpose (see docs/implementation-notes/AA-369.md):
- No Facts pack. `aamc/research.py::compile_brief()` also drew on a
  `FactsPack` (destination-scoped facts registry) — grep confirms no real
  equivalent exists anywhere in this repo (services/, api/migrations/).
  Building that subsystem is not what AA-369 asked for ("Brief từ kết quả
  C1/C2 + atom pack của slot" — no facts pack mentioned). `Brief.facts_ids`
  stays empty; FAQ matching here is atom-only.
- No Trip/Registry fetch. `Slot` carries no market field (confirmed by
  reading services/acp_planning/models.py) — `market` is the caller's job to
  resolve (from Trip.market) and pass in, same convention
  run_piece_through_produce_gates() already uses for tenant_id/channel.
- word_count_range (B13) is deliberately NOT fixed here — see
  services.acp_produce.models.SERPProfile docstring.
"""
from __future__ import annotations

import asyncio
import re
from datetime import date
from typing import Literal, Optional

import asyncpg
import structlog

from services.acp_planning.constants import FRAMEWORK_TABLE
from services.acp_planning.models import Slot
from services.acp_produce.dataforseo import LOCATION_CODES, fetch_serp_profile, parse_top_pages
from services.acp_produce.models import Brief, FAQCandidate, KeywordRecord, SERPProfile
from services.seo_intelligence.dataforseo_client import DataForSEOClient
from shared.llm_client.bedrock_satellite import BedrockUnavailable, invoke_claude
from shared.llm_client.role_config import get_stage_config_sync
from shared.llm_client.call_log import record_call_sync
from shared.llm_client.pricing import calc_cost
from shared.secrets import get_dataforseo_creds

logger = structlog.get_logger()

_TOKEN_RE = re.compile(r"[a-z]{4,}")
_FAQ_MATCH_THRESHOLD = 0.25  # ported from aamc/research.py::_match_source()

_VARIANCE_DIRECTIVES = [
    "include ≥1 one-sentence paragraph",
    "make one section notably longer than the others",
    "≤1 bulleted list in the whole article",
    "vary H2 rhythm — no parallel-structure headline run",
]

_GAP_SYSTEM_PROMPT = (
    "You are a content strategist. You compare a set of facts (our atoms) against real "
    "excerpts of top-ranking competitor pages, and state — in 2-3 sentences max — what the "
    "competitors cover that our atoms don't. If our atoms already cover everything relevant, "
    "say so plainly (e.g. \"no significant gap\") — never invent a gap that is not actually "
    "supported by the competitor text in front of you."
)


# ---------------------------------------------------------------- C1/C2

async def fetch_slot_demand_data(
    slot: Slot, market: str, dfs_client: Optional[DataForSEOClient] = None,
) -> tuple[KeywordRecord, SERPProfile]:
    """C1 (keyword volume) + C2 (SERP profile) for ONE seed keyword per
    SLOT — not per TRIP (the mistake AA-298's original issue flagged in the
    aamc/ prototype). `market` = SOURCE market, never destination.
    """
    keyword = slot.keyword_seed or slot.topic_hint
    if not keyword:
        raise ValueError(
            f"slot {slot.slot_id} has neither keyword_seed nor topic_hint — "
            "C1 needs a seed keyword to research demand for"
        )

    login, password = get_dataforseo_creds()
    client = dfs_client or DataForSEOClient(login, password)
    location_code = LOCATION_CODES.get(market.upper(), 2840)

    kw_data: dict = {}
    try:
        kw_data = await client.fetch_keywords(
            keyword, location_code=location_code, location_name=market, language_code="en",
        )
    except Exception as e:  # fetch_keywords() does not self-contain (unlike fetch_keyword_ideas)
        logger.warning("fetch_slot_demand_data_keywords_failed", keyword=keyword, error=str(e))

    sv_map = (kw_data or {}).get("search_volumes") or {}
    volume: Optional[int] = None
    confidence: Literal["dfs", "heuristic"] = "heuristic"
    for k, v in sv_map.items():
        if k.lower() == keyword.lower():
            volume, confidence = v, "dfs"
            break

    demand = KeywordRecord(
        keyword=keyword, location=market, volume=volume, confidence=confidence,
        retrieved_at=date.today() if confidence == "dfs" else None,
    )
    serp = await fetch_serp_profile(login, password, keyword, market)
    return demand, serp


# ---------------------------------------------------------------- atom pack (per SLOT, not per TRIP)

def _row_to_atom(r) -> dict:
    return {"atom_id": r["atom_id"], "text": r["text"]}


async def fetch_slot_atoms(atom_ids: list[str], db: asyncpg.Connection) -> list[dict]:
    """Atoms for THIS slot's `atom_ids` — a subset of a tour's full atom
    pool, not `fetch_curated_atoms(tour_id, ...)` (services/content_generation/
    s1_from_atom.py, S1's whole-tour fetch). Same NOT deleted / NOT
    is_empty_marker filter as S1's fetch (migration 085)."""
    if not atom_ids:
        return []
    rows = await db.fetch(
        """
        SELECT atom_id, text
        FROM acp_contract.tour_atoms
        WHERE atom_id = ANY($1::text[]) AND NOT deleted AND NOT is_empty_marker
        """,
        atom_ids,
    )
    return [_row_to_atom(r) for r in rows]


# ---------------------------------------------------------------- unknown_ledger (AA-369, migration 095)

async def log_unknown(
    db: asyncpg.Connection, tenant_id: str,
    kind: Literal["no_demand_evidence", "no_atom", "no_cta_target", "unanswerable_paa", "other"],
    detail: str, *, slot_id: Optional[str] = None, keyword: Optional[str] = None,
    demand_cost: Optional[str] = None,
) -> None:
    """L6: TOPIC REJECTED and unanswerable PAA never disappear silently —
    always a concrete, queryable row. Dedup-by-repeat via ON CONFLICT
    (tenant_id, kind, detail): a repeat reject bumps `count`/`last_seen`
    instead of piling up duplicate rows (matches aamc/learning.py::
    log_unknown()'s in-memory dedup behavior, done atomically here)."""
    await db.execute(
        """
        INSERT INTO acp_shared.unknown_ledger (tenant_id, kind, detail, demand_cost, slot_id, keyword)
        VALUES ($1, $2, $3, $4, $5, $6)
        ON CONFLICT (tenant_id, kind, detail) DO UPDATE SET
            count       = acp_shared.unknown_ledger.count + 1,
            last_seen   = CURRENT_DATE,
            demand_cost = COALESCE(EXCLUDED.demand_cost, acp_shared.unknown_ledger.demand_cost),
            slot_id     = COALESCE(EXCLUDED.slot_id, acp_shared.unknown_ledger.slot_id),
            keyword     = COALESCE(EXCLUDED.keyword, acp_shared.unknown_ledger.keyword)
        """,
        tenant_id, kind, detail, demand_cost, slot_id, keyword,
    )


# ---------------------------------------------------------------- gap_statement (LLM, Haiku satellite)

def build_gap_statement(top_pages: list[dict], atoms: list[dict], keyword: str) -> Optional[str]:
    """The one genuinely semantic step in C1-C3 (STEP 0 conclusion) — real
    prose comparison, not rule-able. Best-effort only: a Bedrock failure
    (timeout/throttle/AssumeRole) degrades to None + a logged warning, and
    NEVER blocks Brief compile or the demand-law reject decision (that
    decision is made entirely in compile_brief() before this is even
    called). No top_pages (offline DataForSEO, or nothing crawled in the
    poll budget) also degrades to None — nothing real to compare against,
    stated as such rather than fabricated."""
    if not top_pages:
        return None

    pages_block = "\n\n".join(
        f"PAGE ({p.get('crawled_url') or p.get('url')}):\n{p.get('content', '')[:2000]}"
        for p in top_pages if p.get("content")
    )
    if not pages_block:
        return None
    atoms_block = "\n".join(f"- {a['text']}" for a in atoms) or "(no atoms for this slot)"

    prompt = (
        f"KEYWORD: {keyword}\n\n"
        f"OUR ATOMS (facts we can write about):\n{atoms_block}\n\n"
        f"TOP-RANKING COMPETITOR PAGES:\n{pages_block}\n\n"
        "In 2-3 sentences, state what the competitor pages cover that our atoms do not. "
        "If our atoms already cover everything relevant, say so plainly."
    )
    # AA-518 — "n7_gap_research" stage config (seeded haiku/acc3, matching the prior hardcoded
    # literals). role="validate" (schema's own 3rd role) — this is a small demand-comparison
    # utility call, not tenant-facing content and not a judge/gate, matching the build prompt's
    # own "chỗ validate/judge, không chỉ writer" instruction.
    cfg = get_stage_config_sync("n7_gap_research")
    try:
        result = invoke_claude(
            prompt, model=cfg.model_id, max_tokens=300, system=_GAP_SYSTEM_PROMPT,
            account=cfg.account_route or "acc3",
        )
        statement = result.text.strip() or None
        record_call_sync(
            stage="n7_gap_research", role="validate", model=result.model_used,
            tokens_in=result.usage.get("input_tokens"), tokens_out=result.usage.get("output_tokens"),
            cost_usd=calc_cost(cfg.model_id, result.usage.get("input_tokens", 0),
                                result.usage.get("output_tokens", 0)),
            tenant_id=None,
            quality_signal={"produced_statement": statement is not None,
                             "statement_len_chars": len(statement) if statement else 0},
            stop_reason=result.stop_reason,
            account=cfg.account_route or "acc3", fallback_used=False, provider="bedrock-satellite",
        )
        return statement
    except BedrockUnavailable as e:
        logger.warning("build_gap_statement_bedrock_unavailable", keyword=keyword, error=str(e))
        return None


# ---------------------------------------------------------------- C3 Brief compile + demand-law reject

def _match_faq_source(question: str, atoms: list[dict]) -> Optional[str]:
    """DET token-overlap match of a PAA question to an atom — ported from
    aamc/research.py::_match_source() minus the facts-pack half (no real
    facts source exists yet, see module docstring)."""
    qtok = set(_TOKEN_RE.findall(question.lower()))
    if not qtok:
        return None
    best_id, best = None, 0.0
    for a in atoms:
        atok = set(_TOKEN_RE.findall(a["text"].lower()))
        if not atok:
            continue
        score = len(qtok & atok) / len(qtok)
        if score > best:
            best, best_id = score, a["atom_id"]
    return best_id if best >= _FAQ_MATCH_THRESHOLD else None


def _new_brief_id() -> str:
    import uuid
    return f"brief_{uuid.uuid4().hex[:12]}"


async def compile_brief(
    db: asyncpg.Connection, tenant_id: str, slot: Slot,
    demand: KeywordRecord, serp: Optional[SERPProfile],
    top_pages: Optional[list[dict]] = None,
) -> Optional[Brief]:
    """C3. Returns None when the topic is rejected — a row is always written
    to acp_shared.unknown_ledger first (L6: reject is visible, never silent).

    Demand law (ported from aamc/research.py::compile_brief(), same
    threshold): no search-volume evidence (volume is None, confidence=
    "heuristic") AND no stated campaign reason → TOPIC REJECTED. A slot
    authored as `kind="campaign"` supplies its own campaign reason (there is
    no real DecisionsStore/campaign-override registry in this repo to check
    instead — Slot.kind is the only real signal available; see AA-369.md).
    """
    demand_reason: Optional[str] = None
    if demand.volume is None and demand.confidence == "heuristic":
        if slot.kind == "campaign":
            demand_reason = f"slot kind=campaign (week {slot.week})"
        else:
            await log_unknown(
                db, tenant_id, "no_demand_evidence",
                f"Topic '{demand.keyword}' rejected: no demand evidence and no stated campaign reason.",
                slot_id=slot.slot_id, keyword=demand.keyword,
            )
            return None

    atoms = await fetch_slot_atoms(slot.atom_ids, db)
    if not atoms:
        await log_unknown(db, tenant_id, "no_atom", f"Slot {slot.slot_id}: no live atoms — topic rejected.",
                           slot_id=slot.slot_id, keyword=demand.keyword)
        return None

    cta = slot.cta_target
    if not cta:
        await log_unknown(db, tenant_id, "no_cta_target",
                           f"Slot {slot.slot_id}: no live CTA target — piece would be a dead end; rejected.",
                           slot_id=slot.slot_id, keyword=demand.keyword)
        return None

    faq_candidates: list[FAQCandidate] = []
    for q in (serp.paa_questions if serp else [])[:8]:
        src = _match_faq_source(q, atoms)
        if src:
            faq_candidates.append(FAQCandidate(question=q, source_id=src))
        else:
            vol = f", ~{demand.volume}/mo keyword" if demand.volume else ""
            await log_unknown(db, tenant_id, "unanswerable_paa", f"PAA '{q}' — no atom answers it{vol}.",
                               slot_id=slot.slot_id, keyword=demand.keyword,
                               demand_cost=f"answer this and unlock the '{demand.keyword}' FAQ block")

    fw_key = (slot.funnel_stage, "blog") if slot.channel == "blog" else ("ANY", slot.channel)
    fw_cfg = FRAMEWORK_TABLE.get(fw_key, {"framework": "hub", "faq": True})
    framework = slot.framework or fw_cfg["framework"]

    required_h2s = [f"{demand.keyword.title()}: what it's actually like"]
    required_h2s += [a["text"][:60] for a in atoms[:3]]
    if fw_cfg.get("faq"):
        required_h2s.append("FAQ")

    atoms_by_section: dict[str, list[str]] = {}
    for i, a in enumerate(atoms):
        section = required_h2s[min(i, len(required_h2s) - 1)]
        atoms_by_section.setdefault(section, []).append(a["atom_id"])

    # AA-416: build_gap_statement() calls invoke_claude() (sync boto3, blocking) — off the
    # event loop via to_thread so this coroutine's caller (and /health, sharing the same
    # single ECS task's event loop) doesn't stall for the call's duration.
    gap_statement = await asyncio.to_thread(build_gap_statement, top_pages or [], atoms, demand.keyword)

    return Brief(
        brief_id=_new_brief_id(), slot_id=slot.slot_id, keyword=demand.keyword,
        demand=demand, serp=serp, demand_reason=demand_reason,
        required_h2s=required_h2s, faq_candidates=faq_candidates,
        gap_statement=gap_statement, atoms_by_section=atoms_by_section,
        facts_ids=[], framework=framework,
        word_range=(serp.word_count_range if serp and serp.word_count_range else (900, 1400)),
        variance_directives=list(_VARIANCE_DIRECTIVES),
        cta_target=cta, language="en", internal_links=[cta],
    )


# ---------------------------------------------------------------- end-to-end (C1+C2+C3 for one slot)

async def run_slot_research(
    db: asyncpg.Connection, tenant_id: str, slot: Slot, market: str,
    dfs_client: Optional[DataForSEOClient] = None,
) -> Optional[Brief]:
    """The per-slot entry point future pipeline assembly (E1, AA-370) will
    call. Wires C1 (fetch_slot_demand_data) -> parse_top_pages (P0-2, for
    gap_statement input) -> C3 (compile_brief, demand-law reject +
    gap_statement). Returns None on TOPIC REJECTED — the reason is already
    in acp_shared.unknown_ledger by then (compile_brief's job), never just a
    bare None with no trace."""
    demand, serp = await fetch_slot_demand_data(slot, market, dfs_client)
    login, password = get_dataforseo_creds()
    top_pages = await parse_top_pages(login, password, demand.keyword, market)
    return await compile_brief(db, tenant_id, slot, demand, serp, top_pages)
