"""
services/acp_contract/a3_atomize.py — A3 (platform) atomize.

Atomize runs ONLY at A3 (platform), once per tour when it is published to Master
(`a3_atomize` job from services/export/handler.py::process_export()) or when an admin re-runs it
(POST /admin/atoms/atomize). Tenants NEVER atomize — a tenant rewrites a tour (T2/T3) and then
uses the platform atoms, Segments, routes and hubs of that tour (AA-526, 04/09/2026). Every atom
produced here carries owner_scope = "platform".

This module was split out of services/acp_produce/tenant_pipeline.py (AA-757, S224): the names
there still said "T5"/"tenant" (pre-AA-526) and misled S224. tenant_pipeline.py now keeps only
the T3 (tenant rewrite QA) functions. The atomize entry point, the per-day and whole-tour paths,
`ground_day_atoms`, `atom_subject_key`, `ATOM_STAGE`/`ATOM_Q`, `_atom_forbidden_words`,
`_strip_atom_action` and their private helpers moved here unchanged in behaviour.

Decisions (unchanged, carried from the pre-AA-757 code):
- The day fingerprint's "model" input is read from the live `a3_atomize` stage config
  (`_a3_cfg.model_id`) at the top of `_atomize_per_day()`, NOT a hardcoded constant, so a model
  change (e.g. Sonnet->Haiku at AA-619, Haiku->GPT-6 Luna at AA-757) correctly invalidates every
  day's fingerprint and the atoms re-atomize with the new model (AA-508/AA-619).
- Atomize goes through the LLM gateway (`LLMClient.generate(stage="a3_atomize")`) since AA-757
  (#642), so the admin stage route (fallback + shadow) applies and a non-Claude model can be
  selected. Before that it called `invoke_claude` directly.
"""
from __future__ import annotations

import asyncio
import json
import uuid

import structlog

from services.acp_shared.atom_extraction import (
    SYSTEM_PROMPT as _SYSTEM_PROMPT,
    build_day_user_prompt as _build_day_user_prompt,
    checkable_evidence as _checkable_evidence,
    is_logistics_atom as _is_logistics_atom,
    build_user_prompt as _build_user_prompt,
    content_hash_atom_id as _content_hash_atom_id,
    day_fingerprint as _day_fingerprint,
    derive_atom_text as _derive_atom_text,
    source_hash as _source_hash,
    strip_json_fence as _strip_json_fence,
)
from services.content_generation.itinerary_utils import parse_canonical_itinerary_days
from shared.llm_client.client import LLMClient
from shared.llm_client.models import LLMRequest
from shared.llm_client.role_config import get_stage_config
from shared.llm_client.call_log import record_call_with_pool
from shared.llm_client.decide import decide

logger = structlog.get_logger()

# AA-757 — atomize is A3 platform only; owner_scope is always this constant (tenants never
# atomize). Was `run_t5_atomize`'s own `tenant_id` param, which every caller only ever passed
# "platform" since AA-526.
OWNER_SCOPE = "platform"

_MASTER_TENANT_ID = "00000000-0000-0000-0000-000000000001"


# AA-526 — record_call_with_pool(tenant_id=...)'s INSERT casts the value `$1::uuid`, so the
# owner_scope string "platform" (a non-UUID) would silently fail that INSERT on every atomize LLM
# call (swallowed by that function's own try/except, but a real loss of cost/usage visibility for
# a stage that runs on every published tour). Logged with no tenant attribution instead.
def _llm_log_tenant_id(owner_scope: str) -> str | None:
    try:
        uuid.UUID(owner_scope)
        return owner_scope
    except (ValueError, AttributeError, TypeError):
        return None  # not a real tenant (e.g. "platform") — log with no tenant attribution


async def _atom_forbidden_words(pool, owner_scope: str) -> list[str]:
    """S218 audit: the forbidden list (AA core + the owner's brand words) atoms must not carry —
    platform atoms use the AA master brand. 863/14,091 platform atoms held "explore" etc. because
    atoms were extracted before the AA-738 strip existed. Never raises: a lookup failure means no
    strip, not a failed atomize."""
    from services.content_generation.forbidden_words import all_forbidden
    tenant = _MASTER_TENANT_ID if owner_scope == "platform" else owner_scope
    try:
        async with pool.acquire() as conn:
            raw = await conn.fetchval(
                "SELECT forbidden_words FROM shared.tenant_brand_rules WHERE tenant_id = $1::uuid "
                "AND is_active ORDER BY (brand_name = 'default') DESC, version DESC LIMIT 1", tenant)
        words = json.loads(raw) if isinstance(raw, str) else raw
        return all_forbidden(words if isinstance(words, list) else [])
    except Exception as e:  # noqa: BLE001 — best effort, see docstring
        logger.warning("atom_forbidden_words_lookup_failed", owner_scope=owner_scope, error=str(e)[:200])
        return all_forbidden([])


def _strip_atom_action(action: str, words: list[str]) -> str:
    """Substitution-only strip of one atom's action (AA-738 map; no sentence drop — an action is a
    phrase). A word with no safe substitute is left as is."""
    if not action or not words:
        return action
    from services.content_generation.forbidden_strip import strip_forbidden
    out, _ = strip_forbidden({"action": action}, words)
    return out.get("action", action)


async def run_a3_atomize(
    tour_id: str, rewritten: dict, pool, country: str = "",
    version_id: str | None = None,
) -> dict:
    """A3 atomize — decompose atoms from a published Master Content tour, owner_scope="platform".
    Reuses AA-299's proven prompt/parse pipeline (services/acp_shared/atom_extraction.py:
    _build_user_prompt, _SYSTEM_PROMPT, _strip_json_fence) and the gateway (LLMClient.generate).

    tour_id MUST be silver_aa_internal.raw_tours.tour_id (acp_contract.tour_atoms.tour_id's FK
    target) — the caller passes published_tours.tour_id (same value), not tenant_tour_versions.id
    or published_tours.id.

    AA-445-02 / AA-754 — country: the tour's raw_tours.country. It used to drive the
    competitor-index lookup for distinctiveness scoring; AA-754 removed that end-to-end, so the
    param is now retained only for call-site compatibility and is no longer read.

    AA-508 — dispatches to one of two paths, per STEP0/STEP0b (docs/claude_audit/AA-508-step0*.md):

    - `_atomize_per_day()`: the new default. Splits `rewritten["itineraries"]` into individual
      days (parse_canonical_itinerary_days()), then atomizes/fingerprints/UPSERTs one day at a
      time instead of the whole tour in one shot. Requires `version_id` (the fingerprint table's
      key, acp_contract.atomize_day_fingerprint) — the one real call site always has it.
    - `_atomize_whole_tour_legacy()`: the pre-AA-508 behavior, byte-for-byte. Used when the
      itinerary isn't in canonical day format (parse_canonical_itinerary_days() returns {}) or
      `version_id` is omitted (defensive).
    """
    row = {
        "id": tour_id,
        "name": rewritten.get("name") or "",
        "aa_summary": rewritten.get("summary") or "",
        "aa_highlights": rewritten.get("highlights") or [],
        "itinerary_source": rewritten.get("itineraries") or "",
    }
    # Whole-tour hash — AA-508 keeps this (STEP0 build-task instruction: "giữ lại source_hash
    # cấp-tour hiện có làm fallback/audit"). Still written onto every atom row either path
    # produces; no longer what decides skip-or-not in the per-day path (the fingerprint table
    # does), but still readable for audit/debugging and still what the legacy path skips on.
    source_hash = _source_hash(row)

    forbidden_words = await _atom_forbidden_words(pool, OWNER_SCOPE)   # S218 audit
    days = parse_canonical_itinerary_days(row["itinerary_source"])
    if not days or not version_id:
        return await _atomize_whole_tour_legacy(tour_id, row, source_hash, pool, country,
                                                forbidden_words=forbidden_words)
    return await _atomize_per_day(
        tour_id, version_id, row, days, source_hash, pool, country,
        forbidden_words=forbidden_words,
    )


async def _atomize_whole_tour_legacy(
    tour_id: str, row: dict, source_hash: str, pool, country: str,
    forbidden_words: list[str] | None = None,
) -> dict:
    """Pre-AA-508 behavior, unchanged (see run_a3_atomize()'s own docstring for when this runs).
    Random atom_id, one LLM call for the whole itinerary, source_hash-over-the-whole-tour skip."""
    async with pool.acquire() as conn:
        latest_hash = await conn.fetchval(
            """SELECT source_hash FROM acp_contract.tour_atoms
               WHERE tour_id = $1::uuid AND owner_scope = $2
               ORDER BY created_at DESC LIMIT 1""",
            tour_id, OWNER_SCOPE,
        )
    if latest_hash is not None and latest_hash == source_hash:
        logger.info("a3_atomize_skipped", tour_id=tour_id, owner_scope=OWNER_SCOPE,
                    reason="source unchanged (hash match)")
        return {"status": "skipped", "atom_count": 0}

    prompt = _build_user_prompt(row)
    # AA-757 — atomize runs through the gateway (LLMClient.generate), stage "a3_atomize", so the
    # admin stage route (fallback + shadow) applies and a non-Claude model can be selected. No
    # model_tier is passed: the stage config decides the model. _a3_cfg is still read for the
    # record_call role (the call itself no longer reads account_route — the gateway owns routing).
    _a3_cfg = await get_stage_config("a3_atomize")
    try:
        llm_result = await asyncio.to_thread(
            LLMClient().generate,
            LLMRequest(system_prompt=_SYSTEM_PROMPT, user_prompt=prompt,
                       stage="a3_atomize", max_tokens=4096),
        )
    except Exception as e:
        logger.error("a3_atomize_llm_failed", tour_id=tour_id, owner_scope=OWNER_SCOPE,
                     error_type=type(e).__name__, error=str(e))
        return {"status": "failed", "error": f"{type(e).__name__}: {e}"}

    try:
        atoms = json.loads(_strip_json_fence(llm_result.content))["atoms"]
    except (json.JSONDecodeError, KeyError, TypeError) as e:
        logger.error("a3_atomize_parse_failed", tour_id=tour_id, owner_scope=OWNER_SCOPE, error=str(e))
        return {"status": "failed", "error": f"invalid atom JSON from model: {e}"}
    atoms = [a for a in atoms if not _is_logistics_atom(a.get("place") or "", a.get("action") or "")]  # S224

    # AA-754 — distinctiveness scoring was removed end-to-end. Atoms no longer carry a
    # distinctiveness value. The `country` param is retained for call-site compatibility but no
    # longer read here.
    inserted = 0
    async with pool.acquire() as conn:
        if atoms:
            for atom in atoms:
                atom_id = f"atom_{uuid.uuid4().hex[:10]}"
                place = atom.get("place") or ""
                action = _strip_atom_action(atom.get("action") or "", forbidden_words)   # S218 audit
                text = _derive_atom_text(place, action)
                await conn.execute("""
                    INSERT INTO acp_contract.tour_atoms
                        (atom_id, tour_id, owner_scope, text, place, action, activity_type,
                         emotional_hook, visual_potential, persona_fit, season_note, starred,
                         deleted, weight, source_hash, itinerary_day,
                         created_at, updated_at)
                    VALUES ($1, $2::uuid, $3, $4, $5, $6, $7, $8, $9, $10::jsonb, $11, $12, $13,
                            $14, $15, $16, now(), now())
                """, atom_id, tour_id, OWNER_SCOPE, text, place or None, action or None,
                    atom.get("activity_type"), atom.get("emotional_hook"),
                    atom.get("visual_potential", 1), json.dumps(atom.get("persona_fit") or []),
                    atom.get("season_note"), False, False, 1.0, source_hash,
                    atom.get("itinerary_day"))
                inserted += 1
        else:
            marker_id = f"atom_marker_{uuid.uuid4().hex[:10]}"
            await conn.execute("""
                INSERT INTO acp_contract.tour_atoms
                    (atom_id, tour_id, owner_scope, text, starred, deleted,
                     is_empty_marker, weight, source_hash, created_at, updated_at)
                VALUES ($1, $2::uuid, $3, $4, $5, $6, $7, $8, $9, now(), now())
            """, marker_id, tour_id, OWNER_SCOPE,
                "(zero-atom marker — no content, see is_empty_marker)",
                False, False, True, 1.0, source_hash)

    logger.info("a3_atomize_done", tour_id=tour_id, owner_scope=OWNER_SCOPE, atom_count=inserted)
    # AA-505 — real, computed quality_signal: how many atoms this exact call actually produced
    # vs. a zero-atom marker (a real proxy for a stage with no judge — see docs/implementation-
    # notes/AA-518.md).
    # AA-757 — cost/model/account/fallback/provider all come from the gateway response now
    # (LLMClient.generate computes cost + routing), not recomputed here from _a3_cfg.
    await record_call_with_pool(
        pool, stage="a3_atomize", role=_a3_cfg.role, model=llm_result.model_used,
        tokens_in=llm_result.input_tokens, tokens_out=llm_result.output_tokens,
        cost_usd=llm_result.cost_usd,
        tenant_id=_llm_log_tenant_id(OWNER_SCOPE),
        quality_signal={"atoms_extracted": inserted, "is_empty_marker": inserted == 0},
        stop_reason=llm_result.stop_reason,
        account=llm_result.satellite_account, fallback_used=llm_result.fallback_used,
        provider=llm_result.provider,
    )
    return {"status": "success", "atom_count": inserted}


# AA-694 A3-1 — atoms are "verbatim-derived" by prompt only. The Jev Question a3_atom_in_text
# (migration 188) checks each extracted atom against its day's text before it is stored; a
# confident no (enforce only, ADR 0007) drops it.
ATOM_STAGE = "a3_atomize"
ATOM_Q = "a3_atom_in_text"


def atom_subject_key(day_text: str, place: str, action: str) -> str:
    """The subject_key the a3_atom_in_text verdict is logged under: `atom:<md5(day_text)[:10]>:<label>`
    where label = derive_atom_text(place, action)[:200]. The single builder for this format — reused
    by ground_day_atoms (write) and the AA-756 atom soft-delete outcome hook (read)."""
    import hashlib

    text_key = hashlib.md5((day_text or "").strip().encode("utf-8")).hexdigest()[:10]
    label = _derive_atom_text(place or "", action or "")
    return f"atom:{text_key}:{label[:200]}"


async def ground_day_atoms(atoms: list[dict], day: dict, owner_scope: str, tour_id: str, day_num: int,
                           pool) -> list[dict]:
    """The atoms Jev does not confidently reject for this day's text (all of them in shadow)."""
    if not atoms:
        return atoms

    day_text = f"{day.get('title') or ''}\n{day.get('body') or ''}".strip()
    tenant = _llm_log_tenant_id(owner_scope)

    async def _keep(atom: dict) -> bool:
        key = atom_subject_key(day_text, atom.get("place") or "", atom.get("action") or "")
        dec = await decide(ATOM_STAGE, key, {"day_text": day_text, "atom": _derive_atom_text(
            atom.get("place") or "", atom.get("action") or "")}, [ATOM_Q], tenant_id=tenant, pool=pool)
        return not dec.rejected(ATOM_Q)

    keep = await asyncio.gather(*[_keep(a) for a in atoms])
    kept = [a for a, k in zip(atoms, keep) if k]
    if len(kept) < len(atoms):
        logger.info("a3_atomize_atoms_dropped_by_jev", tour_id=tour_id, day_number=day_num,
                    dropped=len(atoms) - len(kept), kept=len(kept))
    return kept


async def _atomize_per_day(
    tour_id: str, version_id: str, row: dict, days: dict,
    source_hash: str, pool, country: str, forbidden_words: list[str] | None = None,
) -> dict:
    """AA-508 — one day at a time: fingerprint-gated skip (blocks the LLM call, not just logged
    after one), content-hash atom_id, real UPSERT. See atom_extraction.py::content_hash_atom_id()/
    day_fingerprint() for the two hash formulas and their reasoning vs. the reference repo's.

    Days are read SEQUENTIALLY, not concurrently (unlike aa-social-media's own 16-wide
    ThreadPoolExecutor) — AA-418's own prior investigation found concurrent invoke_claude() calls
    unverified-safe on this codebase's Bedrock satellite setup; one call at a time keeps this
    inside the pattern already load-tested here. A day that fails does not lose days already read:
    each day's atoms + fingerprint row are written (and committed) before moving to the next day.
    A day whose LLM call or JSON parse fails simply keeps no fingerprint row for itself, so the
    next call re-asks exactly that day and only that day.
    """
    # AA-619 — resolve the stage config ONCE, up front, so the day-fingerprint is keyed to the
    # SAME model the call below actually uses (read from DB config, not a hardcoded constant).
    # This is what makes a model change (e.g. Sonnet->Haiku, AA-619) correctly invalidate every
    # day's fingerprint so they re-atomize with the new model, instead of silently keeping the
    # old model's atoms.
    _a3_cfg = await get_stage_config("a3_atomize")

    async with pool.acquire() as conn:
        existing = await conn.fetch(
            """SELECT day_number, fingerprint_hash FROM acp_contract.atomize_day_fingerprint
               WHERE tenant_tour_version_id = $1::uuid""",
            version_id,
        )
    existing_fp = {r["day_number"]: r["fingerprint_hash"] for r in existing}

    to_ask = []
    for day_num in sorted(days):
        day = days[day_num]
        fp = _day_fingerprint(day["title"], day["body"], _a3_cfg.model_id)
        if existing_fp.get(day_num) != fp:
            to_ask.append((day_num, day, fp))

    if not to_ask:
        logger.info("a3_atomize_all_days_skipped", tour_id=tour_id, version_id=version_id,
                    day_count=len(days))
        return {
            "status": "skipped", "atom_count": 0,
            "days_total": len(days), "days_read": 0, "days_skipped": len(days),
        }

    # AA-754 — distinctiveness scoring was removed end-to-end. Atoms no longer carry a
    # distinctiveness value. The `country` param is retained for call-site compatibility but no
    # longer read here.

    inserted = 0
    days_read = 0
    days_failed = []
    for day_num, day, fp in to_ask:
        prompt = _build_day_user_prompt(row, day_num, day["title"], day["body"])
        try:
            # AA-757 — through the gateway: stage "a3_atomize" (admin route/fallback/shadow), no
            # model_tier so the stage config picks the model.
            llm_result = await asyncio.to_thread(
                LLMClient().generate,
                LLMRequest(system_prompt=_SYSTEM_PROMPT, user_prompt=prompt,
                           stage="a3_atomize", max_tokens=4096),
            )
        except Exception as e:
            logger.error("a3_atomize_day_llm_failed", tour_id=tour_id, version_id=version_id,
                         day_number=day_num, error_type=type(e).__name__, error=str(e))
            days_failed.append(day_num)
            continue
        try:
            atoms = json.loads(_strip_json_fence(llm_result.content))["atoms"]
        except (json.JSONDecodeError, KeyError, TypeError) as e:
            logger.error("a3_atomize_day_parse_failed", tour_id=tour_id, version_id=version_id,
                         day_number=day_num, error=str(e))
            days_failed.append(day_num)
            continue

        atoms = [a for a in atoms if not _is_logistics_atom(a.get("place") or "", a.get("action") or "")]  # S224
        atoms = await ground_day_atoms(atoms, day, OWNER_SCOPE, tour_id, day_num, pool)   # AA-694 A3-1

        new_atom_ids = []
        async with pool.acquire() as conn:
            if atoms:
                for atom in atoms:
                    place = atom.get("place") or ""
                    # S218 audit: strip before the content-hash id and text are derived.
                    action = _strip_atom_action(atom.get("action") or "", forbidden_words)
                    text = _derive_atom_text(place, action)
                    # AA-610 — evidence: the verbatim source-text span `said` (atom_ranking.py)
                    # should measure, not `text`'s place—action join. checkable_evidence()
                    # falls back to the whole day's body if the model's quote doesn't actually
                    # check out against it, per its own docstring — never None here.
                    evidence = _checkable_evidence(atom.get("evidence") or "", day["body"])
                    atom_id = _content_hash_atom_id(OWNER_SCOPE, tour_id, day_num, place, action)
                    # ON CONFLICT never touches starred/weight — starred is a human curation
                    # flag, weight is content_metrics.py's own learned value (usage-log-derived).
                    await conn.execute("""
                        INSERT INTO acp_contract.tour_atoms
                            (atom_id, tour_id, owner_scope, text, place, action, evidence,
                             activity_type, emotional_hook, visual_potential, persona_fit,
                             season_note, starred, deleted, weight, source_hash, itinerary_day,
                             created_at, updated_at)
                        VALUES ($1, $2::uuid, $3, $4, $5, $6, $7, $8, $9, $10, $11::jsonb, $12,
                                $13, $14, $15, $16, $17, now(), now())
                        ON CONFLICT (atom_id) DO UPDATE SET
                            text = excluded.text, place = excluded.place,
                            action = excluded.action, evidence = excluded.evidence,
                            activity_type = excluded.activity_type,
                            emotional_hook = excluded.emotional_hook,
                            visual_potential = excluded.visual_potential,
                            persona_fit = excluded.persona_fit,
                            season_note = excluded.season_note,
                            source_hash = excluded.source_hash,
                            deleted = false, updated_at = now()
                    """, atom_id, tour_id, OWNER_SCOPE, text, place or None, action or None,
                        evidence, atom.get("activity_type"), atom.get("emotional_hook"),
                        atom.get("visual_potential", 1),
                        json.dumps(atom.get("persona_fit") or []), atom.get("season_note"),
                        False, False, 1.0, source_hash, day_num)
                    new_atom_ids.append(atom_id)
                    inserted += 1
            else:
                marker_id = _content_hash_atom_id(
                    OWNER_SCOPE, tour_id, day_num, "__empty__", "__empty__",
                )
                await conn.execute("""
                    INSERT INTO acp_contract.tour_atoms
                        (atom_id, tour_id, owner_scope, text, starred, deleted,
                         is_empty_marker, weight, source_hash, itinerary_day, created_at, updated_at)
                    VALUES ($1, $2::uuid, $3, $4, $5, $6, $7, $8, $9, $10, now(), now())
                    ON CONFLICT (atom_id) DO UPDATE SET
                        deleted = false, source_hash = excluded.source_hash, updated_at = now()
                """, marker_id, tour_id, OWNER_SCOPE,
                    "(zero-atom marker — no content, see is_empty_marker)",
                    False, False, True, 1.0, source_hash, day_num)
                new_atom_ids.append(marker_id)

            # AA-508 — this day's content changed (that's why it was in `to_ask`) and may now
            # produce FEWER atoms than a prior read of the same day did; soft-delete whichever of
            # THIS day's previously-live atoms this read did not reproduce. Mirrors aa-social-
            # media's own delete_missing() — soft, not hard, per this table's existing admin-PATCH
            # soft-delete convention (no hard DELETE precedent on tour_atoms).
            await conn.execute("""
                UPDATE acp_contract.tour_atoms SET deleted = true, updated_at = now()
                WHERE tour_id = $1::uuid AND owner_scope = $2 AND itinerary_day = $3
                  AND NOT deleted AND atom_id != ALL($4::text[])
            """, tour_id, OWNER_SCOPE, day_num, new_atom_ids)

            await conn.execute("""
                INSERT INTO acp_contract.atomize_day_fingerprint
                    (tenant_tour_version_id, day_number, fingerprint_hash, atomized_at)
                VALUES ($1::uuid, $2, $3, now())
                ON CONFLICT (tenant_tour_version_id, day_number) DO UPDATE SET
                    fingerprint_hash = excluded.fingerprint_hash, atomized_at = now()
            """, version_id, day_num, fp)
        days_read += 1
        # AA-505 — per-day atom count, real and immediate (same reasoning as the legacy path).
        # AA-757 — cost/model/account/fallback/provider from the gateway response.
        await record_call_with_pool(
            pool, stage="a3_atomize", role=_a3_cfg.role, model=llm_result.model_used,
            tokens_in=llm_result.input_tokens,
            tokens_out=llm_result.output_tokens,
            cost_usd=llm_result.cost_usd,
            tenant_id=_llm_log_tenant_id(OWNER_SCOPE),
            quality_signal={"atoms_extracted": len(new_atom_ids), "day_number": day_num,
                             "is_empty_marker": not atoms},
            stop_reason=llm_result.stop_reason,
            account=llm_result.satellite_account, fallback_used=llm_result.fallback_used,
            provider=llm_result.provider,
        )

    result = {
        "status": "failed" if days_failed else "success",
        "atom_count": inserted, "days_total": len(days),
        "days_read": days_read, "days_skipped": len(days) - len(to_ask),
    }
    if days_failed:
        result["error"] = (
            f"day(s) {days_failed} failed to atomize "
            f"({days_read} day(s) succeeded and were kept)"
        )
        result["days_failed"] = days_failed
    logger.info("a3_atomize_per_day_done", tour_id=tour_id, version_id=version_id, **{
        k: v for k, v in result.items() if k not in ("error",)
    })
    return result
