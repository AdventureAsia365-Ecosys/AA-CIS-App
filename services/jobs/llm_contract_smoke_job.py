"""AA-1026 I6 — job kind ``llm_contract_smoke``: does a judge / validate stage's acc2 fallback model meet the stage's
output contract? One small fixture per stage goes through the stage's OWN prompt (its system prompt, user-prompt
builder, schema, max_tokens and temperature) and its OWN parser, with the acc2 model forced, and the job says per
stage whether the parser accepted the reply. It is the "contract smoke" rung of the qualification ladder (root ADR
0009 decision 12): it proves format, not quality (judge calibration and A/B are the later rungs).

Which model. For each stage: the last model of its live route (``shared.llm_role_config``) that has a deployment on
the acc2 connection (migration 331 / 332 put it there); when the route has none yet, the model the migration plans
(``PLANNED``), so the smoke can run before the apply too; or the model the admin names for the stage in the payload's
``models`` (to smoke a re-pick before it goes into a migration or a route). ``result.stages[].model_source`` says
which (route / planned / override). Migration 331 / 332 refuses to give a stage its acc2 fallback until the latest
smoke row of that stage on that model passed (the order gate: smoke first, apply second).

How. ``LLMClient.probe`` on that model's acc2 deployment (the admin's Test connection path: the gateway's own Converse
adapter on the acc2 task role, structured output as a Converse tool, its outcome counted on the deployment's circuit,
no route, no fallback, no shadow), with the request's stage set to ``adhoc_aa1026`` and its role to the stage's role
(judge / validate), so the ledger keeps these calls apart from the stage's own traffic. Every answered call writes one
``shared.llm_call_log`` row (stage ``adhoc_aa1026``, ``quality_signal.contract_smoke`` = the stage, ``parser_ok``);
a billed but unusable reply (schema mismatch) is logged by the gateway and counts as a contract failure with its cost.
One ``acp_shared.audit_log`` row per live run. Fixtures are invented (no catalogue, tenant or prompt text).

Payload: {"dry_run": bool (default true), "stages": [stage, ...] (default every stage of ``CASES``), "models": {stage:
model key, ...} (optional override), "requested_by": "admin:<id>"}. A DRY RUN lists the stages, the model and its
source, whether the acc2 deployment is callable and the estimated cost, with no call. A live run refuses to start when
the estimate exceeds ``COST_CAP_USD`` and stops between stages once the spent cost reaches it. Live cost on Dev: about
0.01 USD for the twelve stages (Nova Lite 0.077 / 0.308 USD per 1M tokens; Llama 4 Scout 0.17 / 0.66). max_attempts 1
(a failed contract is the answer), background.
"""
from __future__ import annotations

import asyncio
import json
import time
from dataclasses import dataclass
from typing import Callable, Optional

import structlog

from shared.jobs.registry import JobContext, NonRetryable, enqueue, job_kind

logger = structlog.get_logger()

KIND = "llm_contract_smoke"
ACTOR = f"job:{KIND}"
LEDGER_STAGE = "adhoc_aa1026"
CONNECTION = "acc2"
COST_CAP_USD = 0.10           # ADR 0009 decision 12: a contract smoke costs under 0.10 USD
CHARS_PER_TOKEN = 4

# Migration 331 (platform) and 332 (tenant, held for the tech lead's yes): the acc2 model each stage falls back to.
PLANNED = {
    "s1_brand_audit": "nova-lite", "s1_source_facts": "nova-lite", "a3_atomize": "nova-lite",
    "a3_place_card": "nova-lite", "a3_place_card_check": "llama4-scout", "a3_place_days": "nova-lite",
    "a3_place_locate": "nova-lite", "a3_place_dedupe_check": "nova-lite",
    "t2_brand_audit": "nova-lite", "t10_claims": "nova-lite", "t9_faq_answer_fit": "nova-lite",
    "t9_faq_filter": "nova-lite",
}


class ContractError(ValueError):
    """The model answered, but the stage's parser does not accept the reply (or accepts it empty)."""


@dataclass(frozen=True)
class Case:
    stage: str
    role: str                                    # judge | validate (the ledger role of the smoke call)
    run: Callable[[Callable], dict]              # run(call) -> summary; call(LLMRequest) -> LLMResponse
    request: Callable[[], object]                # the first request run() sends (for the cost estimate)


# ── fixtures (invented: no real tour, place, tenant or prompt text) ───────────────────────────────────────────────

_TOUR_NAME = "Orvalen Highlands Walk"
_DAYS = {
    1: "Arrive in Kestrel Bay and transfer 40 km to the Tamsin Lodge. Welcome dinner at the lodge.",
    2: "Walk 12 km along the Orvalen ridge (about 5 hours) to the Varro hut at 2,100 m. Breakfast and lunch.",
}
_GENERATED = {
    "name": "Orvalen Highlands Walk", "subtitle": "Six days on the ridges above Kestrel Bay",
    "summary": "A guided walk from Kestrel Bay over the Orvalen ridge, sleeping in mountain lodges and huts.",
    "highlights": ["Sunrise from the Varro hut", "The ridge path above Kestrel Bay", "Dinner at Tamsin Lodge",
                   "The descent through the Orvalen pines"],
    "itineraries": "Day 1: Arrive in Kestrel Bay, transfer to Tamsin Lodge.\n"
                   "Day 2: Walk the Orvalen ridge to the Varro hut.",
    "seo_title": "Orvalen Highlands Walk: 6 days from Kestrel Bay",
    "seo_meta": "Walk the Orvalen ridge from Kestrel Bay over six days, sleeping in lodges and mountain huts each "
                "night with a local guide.",
    "trip_type": "walking",
}
_ATOM = {"place": "Varro hut", "country": "Orvalia", "tour_name": _TOUR_NAME,
         "evidence": "Walk 12 km along the Orvalen ridge to the Varro hut at 2,100 m."}
_CARD = {"display_name": "Varro hut", "locality": "Orvalen ridge", "region": "Orvalen", "country": "Orvalia",
         "facts": [{"text": "The Varro hut stands on the Orvalen ridge.", "kind": "place_background"},
                   {"text": "The hut sits at about 2,100 m.", "kind": "place_background"}]}


# ── one case per stage: the stage's own prompt pieces and parser ─────────────────────────────────────────────────

def _brand_audit_request(stage: str):
    """The brand audit's system prompt, schema and temperature (brand_audit_node), with a fixture page laid out as its
    user prompt is."""
    from services.content_generation.brand_audit_node import BRAND_AUDIT_SCHEMA
    from services.content_generation.brand_standards import AA_BRAND_IDENTITY_PROMPT, AA_COWORK_STRUCTURE_PROMPT
    from shared.llm_client.models import LLMRequest

    g = _GENERATED
    highlights = "\n".join(f"- {h}" for h in g["highlights"])
    user = (f"Review this tour against the editorial standard above.\n"
            f"Pre-audit deterministic checks found these codes (include in assessment): []\n\n"
            f"CONTENT TO REVIEW:\nAA_NAME: {g['name']}\nAA_SUBTITLE: {g['subtitle']}\nAA_SUMMARY: {g['summary']}\n"
            f"AA_HIGHLIGHTS:\n{highlights}\nAA_ITINERARIES: {g['itineraries']}\nSEO_TITLE: {g['seo_title']}\n"
            f"SEO_META: {g['seo_meta']}\n\nTRIP TYPE: {g['trip_type']}\nSOURCE DURATION: 6 days\nDFS CONTEXT: []\n\n"
            "REVIEW ORDER:\n1. Product truth\n2. Brand fit\n3. Trip type accuracy\n4. Highlights quality\n"
            "5. Human readability\n6. SEO quality\n7. Publish readiness\n\nReturn JSON only per schema.")
    return LLMRequest(system_prompt=AA_BRAND_IDENTITY_PROMPT + "\n\n" + AA_COWORK_STRUCTURE_PROMPT, user_prompt=user,
                      temperature=0.1, stage=stage,
                      json_schema={"name": "brand_audit_result", "schema": BRAND_AUDIT_SCHEMA})


def _brand_audit(stage: str) -> Case:
    def run(call) -> dict:
        from services.content_generation.brand_audit_node import derive_status
        resp = call(_brand_audit_request(stage))
        try:
            result = json.loads(resp.content)["brand_audit"]
            for key in ("failure_codes", "issues", "fields_to_fix", "lessons_extracted"):
                if not isinstance(result[key], list):
                    raise ContractError(f"brand_audit.{key} is not a list")
            status = derive_status(list(result["failure_codes"]), result.get("publish_ready"), result.get("status"))
        except (ValueError, KeyError, TypeError) as e:
            raise ContractError(f"brand_audit_node parser refused the reply: {e}") from e
        return {"status": status, "failure_codes": len(result["failure_codes"])}
    return Case(stage, "judge", run, lambda: _brand_audit_request(stage))


def _source_facts_request():
    from services.content_generation.source_facts import _SCHEMA, _SYSTEM_PROMPT, STAGE, _extract_prompt
    from shared.llm_client.models import LLMRequest
    return LLMRequest(system_prompt=_SYSTEM_PROMPT, user_prompt=_extract_prompt(_DAYS), stage=STAGE,
                      temperature=0.1, max_tokens=4096, json_schema=_SCHEMA)


def _source_facts(call) -> dict:
    from services.content_generation.source_facts import _normalize_days, _parse_json
    resp = call(_source_facts_request())
    parsed = _parse_json(resp.content)
    days = _normalize_days(parsed.get("days") if isinstance(parsed, dict) else None)
    if not days:
        raise ContractError("source_facts parser found no day in the reply")
    return {"days": len(days)}


def _atomize_request():
    from services.acp_shared.atom_extraction import SYSTEM_PROMPT, build_day_user_prompt
    from shared.llm_client.models import LLMRequest
    row = {"name": _TOUR_NAME, "aa_summary": _GENERATED["summary"], "aa_highlights": _GENERATED["highlights"]}
    return LLMRequest(system_prompt=SYSTEM_PROMPT, user_prompt=build_day_user_prompt(row, 2, "Orvalen ridge", _DAYS[2]),
                      stage="a3_atomize", max_tokens=4096)


def _atomize(call) -> dict:
    from services.acp_shared.atom_extraction import atoms_from_reply
    resp = call(_atomize_request())
    try:
        atoms = atoms_from_reply(resp.content)
    except (ValueError, KeyError, TypeError) as e:
        raise ContractError(f"a3_atomize parser refused the reply: {e}") from e
    return {"atoms": len(atoms)}


class _Pinned:
    """A client for the code paths that build their request themselves (place cards): ``generate`` is the smoke's
    pinned call."""

    def __init__(self, call):
        self._call = call
        self.last = None

    def generate(self, request):
        self.last = self._call(request)
        return self.last


def _card_request():
    from services.acp_shared.place_cards import DRAFT_MAX_TOKENS, _CARD_SYSTEM
    from shared.llm_client.models import LLMRequest
    return LLMRequest(system_prompt=_CARD_SYSTEM, user_prompt=f"PLACE AS THE ITINERARY NAMES IT: {_ATOM['place']}",
                      stage="a3_place_card", max_tokens=DRAFT_MAX_TOKENS)


def _place_card(call) -> dict:
    from services.acp_shared.place_cards import draft_card
    card, _cost, _resp = draft_card(_ATOM, client=_Pinned(call))
    if not card:
        raise ContractError("place_cards._parse_json found no JSON object in the draft")
    if not isinstance(card.get("facts", []), list) or "display_name" not in card:
        raise ContractError("the draft has no display_name or its facts are not a list")
    return {"display_name": bool(card.get("display_name")), "facts": len(card.get("facts") or [])}


def _check_request():
    from services.acp_shared.place_cards import CHECK_MAX_TOKENS, _CHECK_SYSTEM
    from shared.llm_client.models import LLMRequest
    return LLMRequest(system_prompt=_CHECK_SYSTEM, user_prompt="FACTS:\n1. x\n2. y", stage="a3_place_card_check",
                      max_tokens=CHECK_MAX_TOKENS)


def _place_card_check(call) -> dict:
    from services.acp_shared.place_cards import _parse_json, check_card
    client = _Pinned(call)
    ok, verdicts, _cost, resp, _placement = check_card(json.loads(json.dumps(_CARD)), _ATOM, client=client)
    data = _parse_json(resp.content)
    raw = data.get("verdicts")
    if not isinstance(raw, list) or len(raw) != len(_CARD["facts"]) or "identity_ok" not in data:
        raise ContractError("the check reply has no identity_ok or not one verdict per fact (check_card would keep "
                            "nothing)")
    return {"identity_ok": ok, "verdicts": verdicts}


def _days_request():
    from services.places.days import MAX_TOKENS, SYSTEM_PROMPT, DayBlock, build_user_prompt
    from shared.llm_client.models import LLMRequest
    blocks = [DayBlock(d, d, f"Day {d}", text) for d, text in sorted(_DAYS.items())]
    return LLMRequest(system_prompt=SYSTEM_PROMPT, user_prompt=build_user_prompt(_TOUR_NAME, blocks),
                      stage="a3_place_days", max_tokens=MAX_TOKENS, temperature=0.0)


def _place_days(call) -> dict:
    from services.places.days import parse_llm_days
    resp = call(_days_request())
    try:
        days = parse_llm_days(resp.content)
    except ValueError as e:
        raise ContractError(f"days.parse_llm_days refused the reply: {e}") from e
    if set(days) != set(_DAYS):
        raise ContractError(f"one entry per day block expected, got days {sorted(days)}")
    return {"days": len(days)}


_LOCATE_NAMES = ("Tamsin Lodge", "Varro hut")


def _locate_request():
    from services.places.locate import MAX_TOKENS, SYSTEM_PROMPT, Item, build_prompt
    from shared.llm_client.models import LLMRequest
    items = [Item(key=(n.lower(), "orvalia", ""), name=n, country="Orvalia", tour_name=_TOUR_NAME)
             for n in _LOCATE_NAMES]
    return LLMRequest(system_prompt=SYSTEM_PROMPT, user_prompt=build_prompt(items), stage="a3_place_locate",
                      max_tokens=MAX_TOKENS, temperature=0.0)


def _place_locate(call) -> dict:
    from services.places.locate import REASON_MALFORMED, REASON_MISSING, read_answer
    resp = call(_locate_request())
    try:
        answer = read_answer(resp.content, list(_LOCATE_NAMES))
    except ValueError as e:
        raise ContractError(f"locate.read_answer refused the reply: {e}") from e
    bad = {k: v for k, v in answer.items() if v in (REASON_MISSING, REASON_MALFORMED)}
    if bad:
        raise ContractError(f"names missing or malformed in the answer: {sorted(bad)}")
    return {"located": sum(1 for v in answer.values() if not isinstance(v, str)), "names": len(answer)}


def _dedupe_request():
    from services.places.dedupe import MAX_TOKENS, SYSTEM_PROMPT, VERDICT_SCHEMA
    from shared.llm_client.models import LLMRequest

    def side(name: str, point: list) -> dict:              # the shape of dedupe.pair_input()
        return {"name": name, "kinds": ["lodging"], "geometry": "point", "settlement": "Tamsin", "point": point,
                "point_source": "estimate", "geocoder_match": None, "geocoder_type": None, "place_card": None,
                "tour_sentences": [f"Transfer to the {name} for the night."]}
    user = {"country": "Orvalia", "distance_km": 0.3, "shared": [],
            "a": side("Tamsin Lodge", [12.3456, 98.7654]), "b": side("Tamsin Mountain Lodge", [12.3471, 98.7660])}
    return LLMRequest(system_prompt=SYSTEM_PROMPT, user_prompt=json.dumps(user, ensure_ascii=False),
                      stage="a3_place_dedupe_check", max_tokens=MAX_TOKENS, temperature=0.0, json_schema=VERDICT_SCHEMA)


def _dedupe_check(call) -> dict:
    from services.places.dedupe import VERDICTS, parse_verdict
    resp = call(_dedupe_request())
    try:
        obj = json.loads(resp.content)
    except ValueError as e:
        raise ContractError(f"the verdict is not JSON: {e}") from e
    if not isinstance(obj, dict) or obj.get("verdict") not in VERDICTS:
        raise ContractError("no verdict of same / different / unsure")
    verdict, confidence = parse_verdict(resp.content)
    return {"verdict": verdict, "confidence": confidence}


def _claims_inputs():
    from services.acp_content_writing.claim_rules import KIND_FACT, KIND_TRIP, PieceSentence, SourceLine
    lines = [SourceLine("S1", KIND_TRIP, "Day 2: walk 12 km along the Orvalen ridge to the Varro hut."),
             SourceLine("F1", KIND_FACT, "The Varro hut sits at 2,100 m.")]
    sentences = [PieceSentence("1", "body", "sentence", "On day two you walk the Orvalen ridge to the Varro hut."),
                 PieceSentence("2", "body", "sentence", "The hut sits at 2,100 m above the bay.")]
    return lines, sentences


def _claims_request():
    from services.acp_content_writing.claim_rules import EXTRACTION_SYSTEM, build_extraction_prompt, extraction_schema
    from shared.llm_client.models import LLMRequest
    lines, sentences = _claims_inputs()
    return LLMRequest(system_prompt=EXTRACTION_SYSTEM, user_prompt=build_extraction_prompt(lines, sentences),
                      stage="t10_claims", max_tokens=2048, temperature=0.0, json_schema=extraction_schema())


def _t10_claims(call) -> dict:
    from services.acp_content_writing.claim_rules import parse_rows
    _lines, sentences = _claims_inputs()
    resp = call(_claims_request())
    try:
        rows, missing = parse_rows(json.loads(resp.content), sentences)
    except (ValueError, TypeError) as e:
        raise ContractError(f"claim_rules.parse_rows refused the reply: {e}") from e
    if missing:
        raise ContractError(f"no claim row for sentence(s) {missing}")
    return {"rows": len(rows)}


_FAQ = [("How long is the walk on day two?", "About 12 km, which takes around five hours."),
        ("Where do we sleep on the ridge?", "In the Varro hut at about 2,100 m.")]


def _fit_request():
    from services.acp_content_writing.faq_answer_fit import build_request
    return build_request(_FAQ)


def _faq_answer_fit(call) -> dict:
    from services.acp_content_writing.faq_answer_fit import FitReplyError, parse_fit_verdicts
    resp = call(_fit_request())
    try:
        verdicts = parse_fit_verdicts(resp.content, len(_FAQ))
    except FitReplyError as e:
        raise ContractError(f"faq_answer_fit parser refused the reply: {e}") from e
    return {"verdicts": [v["verdict"] for v in verdicts]}


_QUESTIONS = ("How hard is the Orvalen ridge walk?", "Is the Varro hut heated?", "What does orvalen mean in Latin?")


def _filter_request():
    from services.acp_content_writing.faq_filter import MAX_TOKENS, SYSTEM_PROMPT, FaqScope, build_user_prompt
    from shared.llm_client.models import LLMRequest
    scope = FaqScope(country="Orvalia", trip=_TOUR_NAME, scope_text="the ridge walk from Kestrel Bay to the Varro hut",
                     places=("Kestrel Bay", "Varro hut"), source_text=_DAYS[2])
    return LLMRequest(system_prompt=SYSTEM_PROMPT, user_prompt=build_user_prompt(scope, _QUESTIONS),
                      max_tokens=MAX_TOKENS, temperature=0.0, stage="t9_faq_filter")


def _faq_filter(call) -> dict:
    from services.acp_content_writing.faq_filter import FaqFilterReplyError, parse_verdicts
    resp = call(_filter_request())
    try:
        verdicts = parse_verdicts(resp.content, len(_QUESTIONS))
    except FaqFilterReplyError as e:
        raise ContractError(f"faq_filter parser refused the reply: {e}") from e
    return {"labels": [v["label"] for v in verdicts]}


def _cases() -> dict[str, Case]:
    cases = [
        _brand_audit("s1_brand_audit"), _brand_audit("t2_brand_audit"),
        Case("s1_source_facts", "validate", _source_facts, _source_facts_request),
        Case("a3_atomize", "validate", _atomize, _atomize_request),
        Case("a3_place_card", "validate", _place_card, _card_request),
        Case("a3_place_card_check", "validate", _place_card_check, _check_request),
        Case("a3_place_days", "validate", _place_days, _days_request),
        Case("a3_place_locate", "validate", _place_locate, _locate_request),
        Case("a3_place_dedupe_check", "validate", _dedupe_check, _dedupe_request),
        Case("t10_claims", "validate", _t10_claims, _claims_request),
        Case("t9_faq_answer_fit", "validate", _faq_answer_fit, _fit_request),
        Case("t9_faq_filter", "validate", _faq_filter, _filter_request),
    ]
    return {c.stage: c for c in cases}


CASES = _cases()
assert set(CASES) == set(PLANNED)


# ── plan: which model, on which deployment, at what estimated cost ───────────────────────────────────────────────

def acc2_model_of(stage: str) -> tuple[Optional[str], str]:
    """(model key, source): the last model of the stage's route with a deployment on acc2 ('route'), else the planned
    one ('planned'). Reads the route and registry caches (sync; never raises on an unreadable DB: 'planned')."""
    from shared.llm_client import deployments
    from shared.llm_client.role_config import get_stage_config_sync
    try:
        cfg = get_stage_config_sync(stage)
        for key in reversed([cfg.model_id, *cfg.fallback_model_ids]):
            if any(d.connection_key == CONNECTION for d in deployments.deployments_for_sync(key, enabled_only=False)):
                return key, "route"
    except Exception as e:  # noqa: BLE001 — the plan falls back to the planned model
        logger.warning("llm_contract_smoke_route_unreadable", stage=stage, error=str(e)[:200])
    return PLANNED.get(stage), "planned"


def _target(model_key: str):
    """(catalog model, VendorDeployment on acc2, why not callable or None)."""
    from shared.llm_client import connections, deployments
    from shared.llm_client.catalog import get_model_sync
    from shared.llm_client.vendor_route import VendorDeployment
    model = get_model_sync(model_key)
    if model is None:
        return None, None, "not in the model catalog"
    if model.vendor == "anthropic" or model.family_key == "anthropic":
        return model, None, "an Anthropic model (acc2 cannot serve Claude, and a judge must not be Anthropic)"
    conn = connections.registry_rows_sync().get(CONNECTION)
    dep = next((d for d in deployments.deployments_for_sync(model_key, enabled_only=False)
                if d.connection_key == CONNECTION), None)
    if conn is None or dep is None:
        return model, None, f"no deployment of {model_key} on {CONNECTION}"
    if not (model.enabled and dep.enabled and conn.enabled):
        return model, None, "catalog row, deployment or connection switched off"
    return model, VendorDeployment(dep, conn), None


def estimate_usd(model, request) -> Optional[float]:
    """Upper bound of one call: the prompt at ~4 characters a token plus the request's whole max_tokens."""
    p_in, p_out = getattr(model, "price_in_per_mtok", None), getattr(model, "price_out_per_mtok", None)
    if p_in is None or p_out is None:
        return None
    prompt = len(request.system_prompt or "") + len(request.user_prompt or "")
    if request.json_schema:
        prompt += len(json.dumps(request.json_schema))
    return round((prompt / CHARS_PER_TOKEN * float(p_in) + request.max_tokens * float(p_out)) / 1_000_000, 6)


def build_plan(stages: Optional[list[str]] = None, models: Optional[dict[str, str]] = None) -> dict:
    """The smoke plan (sync: reads the route, catalog and registry caches). Unknown stage names are listed in
    ``unknown`` and never run. ``models`` overrides the model of a stage (``model_source`` 'override'); the override
    goes through the same checks (catalog row, not Anthropic, enabled acc2 deployment)."""
    wanted = list(stages) if stages else sorted(CASES)
    models = models or {}
    entries, unknown = [], [s for s in wanted if s not in CASES]
    for stage in [s for s in wanted if s in CASES]:
        key, source = (models[stage], "override") if models.get(stage) else acc2_model_of(stage)
        entry = {"stage": stage, "role": CASES[stage].role, "model_key": key, "model_source": source,
                 "connection_key": CONNECTION, "callable": False, "why_not": None, "estimated_usd": None}
        if key is None:
            entry["why_not"] = "no acc2 model on the route and none planned"
        else:
            model, target, why = _target(key)
            entry.update(callable=target is not None, why_not=why)
            if model is not None:
                entry["estimated_usd"] = estimate_usd(model, CASES[stage].request())
        entries.append(entry)
    known = [e["estimated_usd"] for e in entries if e["callable"] and e["estimated_usd"] is not None]
    return {"stages": entries, "unknown": unknown, "estimated_usd": round(sum(known), 6),
            "unpriced": sum(1 for e in entries if e["callable"] and e["estimated_usd"] is None)}


# ── live run ─────────────────────────────────────────────────────────────────────────────────────────────────────

def _pinned_call(client, model, target, role: str, spent: list):
    """call(request) for one stage: the request on ``target`` through ``LLMClient.probe``, as stage LEDGER_STAGE with
    the stage's role. Each reply (or billed unusable reply) is appended to ``spent``."""
    from shared.llm_client.client import UnusableReply, unusable_reply_cost

    def call(request):
        req = request.model_copy(update={"stage": LEDGER_STAGE, "role": role})
        try:
            resp = client.probe(req, model, target)
        except UnusableReply as e:                 # billed; the gateway wrote its billed_unusable row
            spent.append(("unusable", unusable_reply_cost(e)))
            raise
        spent.append(("reply", resp))
        return resp
    return call


def run_case(case: Case, model, target, *, client=None) -> dict:
    """One stage (sync). Returns the stage result; never raises. ``replies`` holds the answered LLMResponses (each gets
    its llm_call_log row from the caller)."""
    from shared.llm_client.client import LLMClient, UnusableReply
    from shared.llm_client.errors import classify

    client = client or LLMClient()
    spent: list = []
    started = time.monotonic()
    out = {"stage": case.stage, "model_key": model.model_key, "contract_ok": False, "called": True}
    try:
        out["summary"] = case.run(_pinned_call(client, model, target, case.role, spent))
        out["contract_ok"] = True
    except ContractError as e:
        out.update(error_class="contract", error=str(e)[:300])
    except UnusableReply as e:
        out.update(error_class="unusable_reply", error=str(e)[:300])
    except Exception as e:  # noqa: BLE001 — a refused or failed provider call is the stage's answer
        out.update(error_class=classify(e), error=f"{type(e).__name__}: {e}"[:300])
        out["called"] = bool(spent)
    out["latency_ms"] = int((time.monotonic() - started) * 1000)
    out["replies"] = [r for kind, r in spent if kind == "reply"]
    out["cost_usd"] = round(sum(float(getattr(r, "cost_usd", 0.0) or 0.0) for r in out["replies"])
                            + sum(float(c) for kind, c in spent if kind == "unusable"), 6)
    return out


async def _log_replies(pool, case: Case, result: dict) -> None:
    from shared.llm_client.call_log import billed_fields, record_call_with_pool
    for resp in result["replies"]:
        await record_call_with_pool(
            pool, stage=LEDGER_STAGE, role=case.role, model=resp.model_used, tokens_in=resp.input_tokens,
            tokens_out=resp.output_tokens, cost_usd=resp.cost_usd,
            quality_signal={"contract_smoke": case.stage, "parser_ok": result["contract_ok"], "actor": ACTOR,
                            "error_class": result.get("error_class")},
            stop_reason=resp.stop_reason, account=CONNECTION, fallback_used=False, **billed_fields(resp))


async def run_smoke(pool, plan: dict, *, client=None, cap_usd: float = COST_CAP_USD) -> list[dict]:
    """Each callable stage of ``plan`` in turn (sequential: a few calls, no burst on acc2), until the spent cost
    reaches ``cap_usd`` (the rest listed as ``skipped``)."""
    from shared.llm_client.catalog import get_model_sync

    results: list[dict] = []
    spent = 0.0
    for entry in plan["stages"]:
        stage = entry["stage"]
        if not entry["callable"]:
            results.append({"stage": stage, "model_key": entry["model_key"], "called": False, "contract_ok": None,
                            "skipped": entry["why_not"], "cost_usd": 0.0})
            continue
        if spent >= cap_usd:
            results.append({"stage": stage, "model_key": entry["model_key"], "called": False, "contract_ok": None,
                            "skipped": f"cost cap {cap_usd} USD reached", "cost_usd": 0.0})
            continue
        model = await asyncio.to_thread(get_model_sync, entry["model_key"])
        _model, target, why = await asyncio.to_thread(_target, entry["model_key"])
        if target is None:
            results.append({"stage": stage, "model_key": entry["model_key"], "called": False, "contract_ok": None,
                            "skipped": why, "cost_usd": 0.0})
            continue
        result = await asyncio.to_thread(run_case, CASES[stage], model, target, client=client)
        await _log_replies(pool, CASES[stage], result)
        spent += result["cost_usd"]
        results.append({k: v for k, v in result.items() if k != "replies"})
        logger.info("llm_contract_smoke_stage", stage=stage, model=entry["model_key"], ok=result["contract_ok"],
                    cost_usd=result["cost_usd"], error_class=result.get("error_class"))
    return results


def summarize(results: list[dict]) -> dict:
    ran = [r for r in results if r.get("called")]
    return {"stages_run": len(ran), "passed": sum(1 for r in ran if r.get("contract_ok")),
            "failed": sorted(r["stage"] for r in ran if not r.get("contract_ok")),
            "skipped": sorted(r["stage"] for r in results if not r.get("called")),
            "cost_usd": round(sum(float(r.get("cost_usd") or 0.0) for r in results), 6)}


async def _audit(pool, details: dict) -> None:
    async with pool.acquire() as db:
        await db.execute(
            "INSERT INTO acp_shared.audit_log (actor, action, resource_type, resource_id, details) "
            "VALUES ($1, $2, $3, $4, $5::jsonb)",
            ACTOR, "llm_contract_smoke", "llm_provider_connection", CONNECTION, json.dumps(details, default=str))


@job_kind(KIND, concurrency=1, max_attempts=1, expected_seconds=300, background=True)
async def run(ctx: JobContext) -> dict:
    payload = ctx.payload or {}
    dry_run = bool(payload.get("dry_run", True))
    plan = await asyncio.to_thread(build_plan, payload.get("stages"), payload.get("models"))
    result = {"kind": KIND, "dry_run": dry_run, "cost_cap_usd": COST_CAP_USD, "estimated_usd": plan["estimated_usd"],
              "unpriced": plan["unpriced"], "planned": plan["stages"], "unknown_stages": plan["unknown"]}
    if dry_run:
        result.update(results=[], **summarize([]))
        ctx.set_result(result)
        return result
    if plan["estimated_usd"] > COST_CAP_USD:
        raise NonRetryable(f"estimated {plan['estimated_usd']} USD is over the {COST_CAP_USD} USD cap: run fewer "
                           "stages")
    results = await run_smoke(ctx.pool, plan)
    result.update(results=results, **summarize(results))
    await _audit(ctx.pool, {k: result[k] for k in ("stages_run", "passed", "failed", "skipped", "cost_usd")}
                 | {"requested_by": payload.get("requested_by")})
    logger.info("llm_contract_smoke_done", job_id=ctx.job_id, passed=result["passed"], failed=result["failed"],
                cost_usd=result["cost_usd"])
    ctx.set_result(result)
    return result


async def enqueue_manual(pool, *, dry_run: bool, stages: Optional[list[str]], actor: str,
                         models: Optional[dict[str, str]] = None) -> tuple[str, bool]:
    """An admin's run (no idempotency key). Unknown stage names, an override for a stage the run leaves out and an
    empty model key are refused here (ValueError); whether the model can be called is the plan's answer."""
    unknown = [s for s in [*(stages or []), *(models or {})] if s not in CASES]
    if unknown:
        raise ValueError(f"no contract smoke for stage(s) {sorted(set(unknown))}; known: {sorted(CASES)}")
    if stages and models and set(models) - set(stages):
        raise ValueError(f"models names stage(s) the run leaves out: {sorted(set(models) - set(stages))}")
    if models and not all(isinstance(k, str) and k.strip() for k in models.values()):
        raise ValueError("models: every stage needs a model key")
    payload = {"dry_run": dry_run, "requested_by": actor[:64]}
    if stages:
        payload["stages"] = list(dict.fromkeys(stages))
    if models:
        payload["models"] = {s: k.strip() for s, k in models.items()}
    return await enqueue(pool, KIND, payload, created_by=actor[:64])
