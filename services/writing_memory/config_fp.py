"""AA-900 W0 (root ADR 0020, roadmap W0 "Widen config_fp"; S271 critic fix "Change control") — the release config
fingerprint: one hash over everything that changes a writer's output at run time without a code release of the
writing-memory items themselves.

Before W0 a release fingerprint (`releases.current_fingerprint`) named the writer's ONE stage route, the eval judge
route, the golden set, the gate taxonomy and the system-prompt sha. A route reorder of t9_repair, an effort row of
`shared.stage_model_params` (migration 381), a mode env switch, a prompt edit of a repair or check stage, an L0 lexicon
activation or an AA house-style edit all changed what the writer produced and never flagged a release for reverify.

`config_fp` (stored under the `config_fp` key of the fingerprint, next to the legacy keys) holds one sha16 per
component and one sha256 over all of them:

  routes         every `shared.llm_role_config` row of the writer's path (COMPONENT stages below): role, provider,
                 model, fallback chain IN ORDER, account route, on/off, allowed scopes, deadline, degrade mode, pii
                 class; plus `shared.llm_route_policy.hold_fallback_writes` of those stages (and of '*'). Shadow
                 columns are left out (a shadow never changes the stored output).
  stage_params   the `shared.stage_model_params` rows of those stages (max_tokens, min_output_tokens, temperature,
                 effort, reasoning_mode), every model.
  catalog        the `shared.llm_model_catalog` rows (wire model, api style, provider, enabled, max output tokens,
                 temperature support) and the `shared.llm_model_deployment` rows (connection, wire id, priority,
                 enabled, api style, request defaults) of every model the routes name. Probe state (access_state,
                 last_probe_*) is availability, not config: the answered model of each write is stamped instead.
  route_control  `shared.llm_route_settings.order_mode` (model_first / aws_first). The route-control version id is
                 recorded as `route_config_version` for context but not hashed: every save of ANY stage bumps it, and
                 the rows themselves are already in `routes` (an unrelated stage must not flag a release).
  mode_flags     the env switches that change the writer path's output (MODE_FLAGS, plus the per-class families in
                 MODE_FLAG_PREFIXES), normalised (strip, lower). Only names and a hash are stored, never a value.
                 The writers run in the job worker, and the api and worker task definitions carry different env (the
                 worker alone has T2_JUDGE_GATE, S1_WRITER_V2 ...), so each record names the `service` that computed
                 it (`set_service`, WRITER_SERVICE = the worker) and mode_flags is compared only between two records
                 of the worker: on a worker scan, against the release's own fingerprint when the worker drafted it,
                 else against the worker-run evidence of the release (its R2 / shadow / R0 run fingerprints,
                 `reverify_components(references=...)`). An api scan or the admin drawer never compares mode_flags.
  prompts        the deployed source sha of every registered prompt of the path's stages
                 (`shared/llm_client/prompt_versions.json` symbols, `prompt_registry.prompt_hash_of`): a prompt edit
                 of a repair or check stage moves it even before its version bump.
  memory_items   the items of the OTHER active release that renders with this one: a tenant release renders after
                 the writer's active platform release, so its items_sha256 is part of the tenant release's config
                 (a platform release: none; the release's own items are its items_sha256).
  l0_lexicon     the active L0 lexicon rules of the writer (`writing_rule` kind lexicon, status active, platform +
                 the release's tenant): rule id, phrase, replacement, channels (t9 writers only).
  policy         (AA-1047) the writing policy's policy_sha (services/quality/writing_policy.yaml): prompt fragments,
                 rubrics, RulesConfig and route rows are compiled from it.
  brand_config   the owner's house style: active `shared.tenant_brand_rules` rows and `shared.tenant_seo_config` of
                 the AA master tenant for A1, of the release's tenant for a tenant release; none for a platform T2 /
                 T9 release (each write reads its tenant's own pack: an input, not config).

`reverify_components(stored, now)` names the components that changed; `REVERIFY_REASON` maps each to one of the four
reverify reasons the CIS 287 CHECK allows (no migration): route / params / catalog / route control / mode flags ->
`model_route`; prompts / memory items / L0 / brand config -> `prompt_version_mismatch`. A stored fingerprint without
`config_fp` (every release drafted before W0) is not a change, as for the legacy keys.

Every read is best effort inside a savepoint (`_rows`): a missing table or a read error gives the component the value
"unavailable" and never fails a draft or a scan. A component that is "unavailable" on EITHER side is not compared (a
transient read error must not flag a release: the flags are sticky), and a component whose input depends on a failed
read is "unavailable" too (catalog when routes failed, routes when the route policy failed). $0: no model call.
"""
from __future__ import annotations

import hashlib
import json
import os
from functools import lru_cache
from typing import Any, Iterable, Mapping, Optional

import structlog

logger = structlog.get_logger()

FP_VERSION = 2
UNAVAILABLE = "unavailable"
# The service whose env the writers run with: the job worker (shared/jobs/worker.py; the api's in-process worker in
# local dev sets it too). Every other process records its own name and never compares mode_flags.
WRITER_SERVICE = "worker"
_SERVICE = {"name": "api"}


def set_service(name: str) -> None:
    """Name the service this process runs as (the worker calls it at start, before its reverify scan)."""
    _SERVICE["name"] = name


def current_service() -> str:
    return _SERVICE["name"]
AA_MASTER_TENANT_ID = "00000000-0000-0000-0000-000000000001"

# The stages on each writer's path, by name prefix: every llm_role_config / stage_model_params / prompt row whose
# stage starts with one of these belongs to the writer (a new repair or check stage joins without a code change).
# a1 = the S1 writer graph (s1_generate, s1_flag_fix, s1_itinerary_nudge, s1_source_facts, s1_grounding, s1_judge,
# s1_judge_tiebreak, s1_brand_audit); t2 = the T2 graph and its T3 repair rounds (t2_generate, t2_flag_fix,
# t2_itinerary_nudge, t2_judge, t2_brand_audit); t9 = the angle writer, the T9 writer, its repairs and FAQ checks and
# the T10 checks (t8_angle_gen, t9_write, t9_repair, t9_faq_filter, t9_faq_answer_fit, t10_judge, t10_claims,
# t10_adjudicate).
STAGE_PREFIXES: dict[str, tuple[str, ...]] = {
    "a1": ("s1_",),
    "t2": ("t2_", "t3_"),
    "t9_blog": ("t8_", "t9_", "t10_"),
    "t9_social": ("t8_", "t9_", "t10_"),
}
# The eval judge every writer's R2 runs on (WM-19): part of every writer's config.
EVAL_STAGES = ("writing_memory_eval",)

# Env switches that change the output of a writer path (read from the code, S271). A new switch on a writer path is
# added here; tests/unit/test_aa900_w0_config_fp.py fails when a `*_ENV = "..."` constant of a writer module is
# neither here nor in NOT_OUTPUT_FLAGS.
MODE_FLAGS: dict[str, tuple[str, ...]] = {
    # S1_JUDGE_LIVE (judge_node, S271 W0 t2s1 lane), T2_INTERIM (t2_interim, same lane), T10_F9_POLICY_MODE
    # (f9_policy) and T9_LAST_ATTEMPT_MODE (acp_content_writing.service, S271 W0 t9 lane): classified here ahead of
    # those PRs, so the env-switch guard below stays green whichever PR merges first.
    "a1": ("S1_WRITER_V2", "S1_STRUCTURED_FACTS", "S1_PER_DAY_TARGETS", "S1_WRITER_TEMPERATURE",
           "A1_BRAND_FIT_RUBRIC", "A1_V2_BRAND_FIT_GATE", "S1_JUDGE_LIVE"),
    "t2": ("T2_JUDGE_GATE", "T2_JUDGE_REPEAT", "T2_JUDGE_ANCHORS", "T2_BRAND_CLAIM_CHECK", "T2_FACTS",
           "T2_BEST_DRAFT", "T2_TARGETED_REPAIR", "T3_BACKGROUND_FACTS", "T2_INTERIM",
           # AA-1049 W3 (T2 train): the linear orchestrator (opt-in; unset / unknown = main's graph path)
           "T2_ORCHESTRATOR"),
    "t9_blog": ("T9_BRIEF_GROUNDING_MODE", "T9_READER_CHECKLIST_MODE", "T9_FAQ_LLM_FILTER", "T10_F12_SCOPE_MODE",
                "T10_F9_BRAND_STYLE_MODE", "T10_F14_FLOOR_MODE", "T10_SA_MODE", "T10_ADJ_MODE",
                "T10_F9_POLICY_MODE", "T9_LAST_ATTEMPT_MODE", "T9_REGRESSION_GUARD_MODE", "T9_SEO_BUILDER"),
    "t9_social": ("T9_BRIEF_GROUNDING_MODE", "T9_READER_CHECKLIST_MODE", "T10_F12_SCOPE_MODE",
                  "T10_F9_BRAND_STYLE_MODE", "T10_F14_FLOOR_MODE", "T10_SA_MODE", "T10_ADJ_MODE",
                  "T10_F9_POLICY_MODE", "T9_LAST_ATTEMPT_MODE", "T9_REGRESSION_GUARD_MODE"),
}
# Gateway switches that change which model answers or how it is asked, on every writer path.
GATEWAY_FLAGS = ("LLM_STAGE_PARAMS", "LLM_ROUTE_AWS_FIRST", "LLM_DEPLOYMENT_ROUTING", "LLM_LEGACY_ALIASES",
                 "LLM_LEGACY_ACC2_NATIVE", "LLM_TOKEN_FLOOR", "LLM_BUDGET_RETRY", "LLM_CONVERSE_CACHE_POINT")
# Writing-memory switches that change what the writer or its gates do (l0.L0_ENFORCE_ENV: the L0 lexicon blocks).
MEMORY_FLAGS = ("WRITING_MEMORY_L0_ENFORCE",)
# Per-class families (seed_align modes.py / adjudicate.py read <PREFIX>_<CLASS>): every env var with the prefix.
MODE_FLAG_PREFIXES: dict[str, tuple[str, ...]] = {"t9_blog": ("T10_SA_MODE_", "T10_ADJ_MODE_"),
                                                  "t9_social": ("T10_SA_MODE_", "T10_ADJ_MODE_")}
# Env constants of writer modules that do NOT change a stored output (budgets, breakers, capacity, the learning loop's
# own schedule, batch submission, the record-only reply cassette of the S271 W0 cassette lane): read by the guard
# test only.
NOT_OUTPUT_FLAGS = ("LLM_CIRCUIT_BREAKER", "LLM_CENTRAL_BUDGETS", "LLM_TENANT_BUDGETS", "LLM_APP_PROFILE",
                    "LLM_CAPACITY", "LLM_CONNECTION_BUDGETS", "BATCH_SUBMIT_SWITCH", "WRITING_MEMORY_AUTO_MINE",
                    "WRITING_MEMORY_PREF_SUMMARY", "WRITING_MEMORY_R1_GATE", "LLM_CASSETTE")

# Which reverify reason (CIS 287 CHECK: golden_set, model_route, gate_taxonomy, prompt_version_mismatch) a changed
# component raises.
REVERIFY_REASON: dict[str, str] = {
    "routes": "model_route", "stage_params": "model_route", "catalog": "model_route", "route_control": "model_route",
    "mode_flags": "model_route", "prompts": "prompt_version_mismatch", "memory_items": "prompt_version_mismatch",
    "l0_lexicon": "prompt_version_mismatch", "brand_config": "prompt_version_mismatch",
    "policy": "prompt_version_mismatch",
}
COMPONENTS = tuple(REVERIFY_REASON)

_ROUTE_COLS = ("role", "provider", "model_id", "fallback_model_ids", "account_route", "is_active", "allowed_scopes",
               "route_deadline_s", "degrade_mode", "pii_class")
_PARAM_COLS = ("model_key", "max_tokens", "min_output_tokens", "temperature", "effort", "reasoning_mode")
_CATALOG_COLS = ("model_key", "wire_model", "api_style", "provider", "enabled", "max_output_tokens",
                 "supports_temperature")
_DEPLOYMENT_COLS = ("model_key", "connection_key", "wire_id", "priority", "enabled", "api_style", "request_defaults")

_ROUTES_SQL = ("SELECT stage, role, provider, model_id, fallback_model_ids, account_route, is_active, allowed_scopes, "
               "route_deadline_s, degrade_mode, pii_class FROM shared.llm_role_config")
_POLICY_SQL = "SELECT stage, hold_fallback_writes FROM shared.llm_route_policy"
_PARAMS_SQL = ("SELECT stage, model_key, max_tokens, min_output_tokens, temperature, effort, reasoning_mode "
               "FROM shared.stage_model_params")
_CATALOG_SQL = ("SELECT model_key, wire_model, api_style, provider, enabled, max_output_tokens, supports_temperature "
                "FROM shared.llm_model_catalog WHERE model_key = ANY($1::text[])")
_DEPLOYMENT_SQL = ("SELECT model_key, connection_key, wire_id, priority, enabled, api_style, request_defaults "
                   "FROM shared.llm_model_deployment WHERE model_key = ANY($1::text[])")
_ROUTE_SETTINGS_SQL = "SELECT order_mode, current_version FROM shared.llm_route_settings"
_PLATFORM_ITEMS_SQL = ("SELECT items_sha256 FROM shared.writing_memory_release "
                       "WHERE writer = $1 AND scope = 'platform' AND status = 'active'")
_L0_SQL = ("SELECT rule_id::text AS rule_id, scope, rule_text, replacement, applies_to FROM shared.writing_rule "
           "WHERE writer = $1 AND kind = 'lexicon' AND status = 'active' "
           "AND (scope = 'platform' OR tenant_id = $2::uuid)")
_BRAND_SQL = ("SELECT to_jsonb(b) - ARRAY['updated_at', 'created_at'] AS row FROM shared.tenant_brand_rules b "
              "WHERE b.tenant_id = $1::uuid AND b.is_active ORDER BY b.id")
_SEO_SQL = ("SELECT to_jsonb(s) - ARRAY['updated_at', 'id'] AS row FROM shared.tenant_seo_config s "
            "WHERE s.tenant_id = $1::uuid")


# ── pure parts ─────────────────────────────────────────────────────────────────────────────────────────────────────

def _canon(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)


def sha(value: Any, n: int = 16) -> str:
    """sha256 hex (first `n` characters) of the canonical JSON of `value`."""
    return hashlib.sha256(_canon(value).encode("utf-8")).hexdigest()[:n]


def _jsonish(v: Any) -> Any:
    if isinstance(v, str):
        try:
            return json.loads(v)
        except ValueError:
            return v
    return v


def on_path(writer: str, stage: Optional[str]) -> bool:
    """True when `stage` belongs to `writer`'s path (its prefixes, or the eval judge)."""
    if not stage:
        return False
    return stage in EVAL_STAGES or stage.startswith(STAGE_PREFIXES.get(writer, ()))


def on_any_path(stage: Optional[str]) -> bool:
    """True when `stage` is on some writer's path (a change of it may flag a release)."""
    return any(on_path(w, stage) for w in STAGE_PREFIXES)


def path_stages(writer: str, stages: Iterable[str]) -> list[str]:
    return sorted({s for s in stages if on_path(writer, s)})


def _t2_orchestrator_value(value: str, env: Mapping[str, str]) -> Optional[str]:
    """AA-1049 W3: ``T2_ORCHESTRATOR`` changes the T2 path only as ``linear`` with the T2 interim profile on
    (``t2_orchestrator.mode``); any other value runs main's graph path, exactly like unset, so it is not recorded."""
    from services.content_generation.t2_interim import _OFF, T2_INTERIM_ENV
    interim_on = (env.get(T2_INTERIM_ENV) or "").strip().lower() not in _OFF
    return value if value == "linear" and interim_on else None


# Switches whose set values can still mean the default path: {name: (normalised value, env) -> value or None}.
_DEFAULT_PATH_VALUES = {"T2_ORCHESTRATOR": _t2_orchestrator_value}


def mode_flags(writer: str, env: Optional[Mapping[str, str]] = None) -> dict[str, str]:
    """{name: normalised value} of the output-changing env switches of `writer` that are set (unset ones omitted, and a
    value that maps to the default path, ``_DEFAULT_PATH_VALUES``, is omitted like unset)."""
    env = os.environ if env is None else env
    names = set(MODE_FLAGS.get(writer, ())) | set(GATEWAY_FLAGS) | set(MEMORY_FLAGS)
    prefixes = MODE_FLAG_PREFIXES.get(writer, ())
    names |= {k for k in env if prefixes and k.startswith(prefixes)}
    out = {}
    for k in sorted(names):
        v = (env.get(k) or "").strip().lower()
        if v and k in _DEFAULT_PATH_VALUES:
            v = _DEFAULT_PATH_VALUES[k](v, env) or ""
        if v:
            out[k] = v[:64]
    return out


@lru_cache(maxsize=256)
def _deployed_prompt_sha(stage: str) -> str:
    """The sha256 of the deployed source of the stage's newest manifest entry ("" when the stage has none,
    UNAVAILABLE when its file or symbols are missing). The source is fixed in an image: cached per process."""
    from shared.llm_client import prompt_registry as pr
    try:
        entry = pr.newest(pr.load_manifest(), stage)
        if entry is None:
            return ""
        return pr.prompt_hash_of(entry["source_path"], entry["symbols"])
    except Exception as e:  # noqa: BLE001 — a fingerprint never fails on a manifest problem
        logger.warning("wm_config_fp_prompt_unreadable", stage=stage, error=f"{type(e).__name__}: {str(e)[:200]}")
        return UNAVAILABLE


def prompt_shas(writer: str) -> dict[str, str]:
    """{stage: deployed source sha} of every registered prompt on the writer's path (the manifest's stages)."""
    from shared.llm_client import prompt_registry as pr
    try:
        stages = path_stages(writer, pr.load_manifest())
    except Exception as e:  # noqa: BLE001
        logger.warning("wm_config_fp_manifest_unreadable", error=f"{type(e).__name__}: {str(e)[:200]}")
        return {"*": UNAVAILABLE}
    return {s: _deployed_prompt_sha(s) for s in stages if _deployed_prompt_sha(s)}


def _pick(row: Mapping, cols: tuple[str, ...]) -> dict:
    out = {}
    for c in cols:
        v = row.get(c) if hasattr(row, "get") else row[c]
        if isinstance(v, tuple):
            v = list(v)
        out[c] = _jsonish(v) if c == "request_defaults" else v
    return out


def route_models(routes: Iterable[Mapping]) -> list[str]:
    """Every model a route row can answer with (primary and fallbacks; not the shadow)."""
    out: set[str] = set()
    for r in routes:
        if r.get("model_id"):
            out.add(str(r["model_id"]))
        out |= {str(m) for m in (r.get("fallback_model_ids") or []) if m}
    return sorted(out)


def build(writer: str, *, routes: Any, policies: Any = (), params: Any = (), catalog: Any = (),
          deployments: Any = (), route_settings: Optional[Mapping] = None, env: Optional[Mapping[str, str]] = None,
          prompts: Optional[Mapping[str, str]] = None, memory_items: Any = None, l0_rules: Any = (),
          brand_rows: Any = None, service: Optional[str] = None) -> dict:
    """The `config_fp` record of `writer` from rows already read (pure; `collect` reads them). A row source given as
    UNAVAILABLE makes its component UNAVAILABLE. Routes / policies / params are filtered to the writer's path here,
    so a caller may pass every row of the table."""
    parts: dict[str, Any] = {}
    stage_shas: dict[str, str] = {}

    if UNAVAILABLE in (routes, policies):      # the policy is part of the routes component: both or nothing
        parts["routes"] = UNAVAILABLE
    else:
        path_routes = sorted((r for r in routes if on_path(writer, r.get("stage"))), key=lambda r: r["stage"])
        pol = {p["stage"]: bool(p.get("hold_fallback_writes")) for p in policies
               if p.get("stage") == "*" or on_path(writer, p.get("stage"))}
        route_view = [{"stage": r["stage"], **_pick(r, _ROUTE_COLS), "hold": pol.get(r["stage"])} for r in path_routes]
        parts["routes"] = sha({"rows": route_view, "hold_default": pol.get("*"), "policy": None})
        for rv in route_view:
            stage_shas[rv["stage"]] = sha(rv, 12)

    if params == UNAVAILABLE:
        parts["stage_params"] = UNAVAILABLE
    else:
        rows = sorted((_pick(p, ("stage",) + _PARAM_COLS) for p in params if on_path(writer, p.get("stage"))),
                      key=lambda p: (p["stage"], p["model_key"]))
        parts["stage_params"] = sha(rows)
        for stage in {p["stage"] for p in rows}:
            mine = [p for p in rows if p["stage"] == stage]
            stage_shas[stage] = sha({"route": stage_shas.get(stage), "params": mine}, 12)

    if UNAVAILABLE in (catalog, deployments, routes):    # the catalog rows read are the models the routes name
        parts["catalog"] = UNAVAILABLE
    else:
        cat = sorted((_pick(c, _CATALOG_COLS) for c in catalog), key=lambda c: c["model_key"])
        dep = sorted((_pick(d, _DEPLOYMENT_COLS) for d in deployments),
                     key=lambda d: (d["model_key"], d["connection_key"]))
        parts["catalog"] = sha({"catalog": cat, "deployments": dep})

    version = None
    if route_settings == UNAVAILABLE:
        parts["route_control"] = UNAVAILABLE
    else:
        parts["route_control"] = sha({"order_mode": (route_settings or {}).get("order_mode")})
        version = (route_settings or {}).get("current_version")

    flags = mode_flags(writer, env)
    parts["mode_flags"] = sha(flags)
    parts["prompts"] = (UNAVAILABLE if prompts == UNAVAILABLE or UNAVAILABLE in (prompts or {}).values()
                        else sha(dict(sorted((prompts or {}).items()))))
    parts["memory_items"] = (UNAVAILABLE if memory_items == UNAVAILABLE
                             else None if memory_items is None else sha(memory_items))
    if l0_rules == UNAVAILABLE:
        parts["l0_lexicon"] = UNAVAILABLE
    else:
        l0 = sorted(({"rule_id": str(r.get("rule_id")), "scope": r.get("scope"),
                      "phrase": " ".join(str(r.get("rule_text") or "").split()).lower(),
                      "replacement": r.get("replacement") or None,
                      "applies_to": sorted(r.get("applies_to") or [])} for r in l0_rules),
                    key=lambda r: r["rule_id"])
        parts["l0_lexicon"] = sha(l0) if l0 else None
    parts["brand_config"] = (UNAVAILABLE if brand_rows == UNAVAILABLE
                             else None if brand_rows is None else sha(brand_rows))
    # AA-1047: the writing policy every prompt fragment, rubric, rule table and route row of the path is compiled from
    # (services/quality/writing_policy.yaml): a policy change moves the fingerprint even when no prompt symbol changed.
    parts["policy"] = _policy_sha()

    return {"v": FP_VERSION, "sha": hashlib.sha256(_canon(parts).encode("utf-8")).hexdigest(), "parts": parts,
            "stage_shas": dict(sorted(stage_shas.items())), "mode_flags_set": sorted(flags),
            "route_config_version": version, "service": service or current_service()}


def _policy_sha() -> str:
    try:
        from services.quality import writing_policy
        return writing_policy.policy_sha()
    except Exception as e:  # noqa: BLE001 — a fingerprint never fails on a policy read problem
        logger.warning("wm_config_fp_policy_unreadable", error=f"{type(e).__name__}: {str(e)[:200]}")
        return UNAVAILABLE


def _usable(v: Any) -> bool:
    return v is not None and v != UNAVAILABLE


def mode_flags_reference(stored: Mapping, now: Mapping, references: Iterable[Mapping] = ()) -> Optional[str]:
    """The stored mode_flags part to compare the current one with, or None (not compared): only when the current record
    was computed by the writer service, against the first record of that service among the release's own fingerprint
    and `references` (the release's evidence-run fingerprints, R2 first). A record that names no service is not
    compared (its env is unknown)."""
    new = (now or {}).get("config_fp") or {}
    if new.get("service") != WRITER_SERVICE:
        return None
    for rec in ((stored or {}),) + tuple(references or ()):
        cfp = (rec or {}).get("config_fp") if isinstance(rec, Mapping) else None
        if isinstance(cfp, Mapping) and cfp.get("service") == WRITER_SERVICE:
            part = (cfp.get("parts") or {}).get("mode_flags")
            if _usable(part):
                return part
    return None


def reverify_components(stored: Mapping, now: Mapping, references: Iterable[Mapping] = ()) -> list[str]:
    """The components of `config_fp` that differ between a stored fingerprint and the current one ([] when the stored
    one has no config_fp: drafted before W0, not a change). A component the stored record lacks, or that is
    UNAVAILABLE on either side, is not a change. mode_flags is compared as `mode_flags_reference` says (worker env
    against worker env only)."""
    old = (stored or {}).get("config_fp")
    new = (now or {}).get("config_fp")
    if not isinstance(old, Mapping) or not isinstance(new, Mapping):
        return []
    op, np_ = old.get("parts") or {}, new.get("parts") or {}
    out = []
    for c in COMPONENTS:
        if c not in op:
            continue
        before = mode_flags_reference(stored, now, references) if c == "mode_flags" else op.get(c)
        if c == "mode_flags" and before is None:
            continue
        if UNAVAILABLE in (before, np_.get(c)):
            continue
        if before != np_.get(c):
            out.append(c)
    return out


def changed_stages(stored: Mapping, now: Mapping) -> list[str]:
    """The path stages whose route row or params changed (for the admin: which stage moved the fingerprint)."""
    ocfp, ncfp = (stored or {}).get("config_fp") or {}, (now or {}).get("config_fp") or {}
    old, new = ocfp.get("stage_shas") or {}, ncfp.get("stage_shas") or {}
    if not old:
        return []
    for c in ("routes", "stage_params"):          # a failed read on either side names no stage
        if UNAVAILABLE in ((ocfp.get("parts") or {}).get(c), (ncfp.get("parts") or {}).get(c)):
            return []
    return sorted(s for s in set(old) | set(new) if old.get(s) != new.get(s))


def reasons_of(components: Iterable[str]) -> list[str]:
    """The reverify reasons (CIS 287 vocabulary) the changed components raise, sorted, no duplicate."""
    return sorted({REVERIFY_REASON[c] for c in components if c in REVERIFY_REASON})


# ── reads ──────────────────────────────────────────────────────────────────────────────────────────────────────────

async def _rows(conn, sql: str, *args) -> Any:
    """The rows of one read as dicts, or UNAVAILABLE (missing table, no grant, any error), inside a savepoint so a
    failed read never aborts the caller's transaction."""
    try:
        tx = getattr(conn, "transaction", None)
        if tx is None:
            return [dict(r) for r in await conn.fetch(sql, *args)]
        async with tx():
            return [dict(r) for r in await conn.fetch(sql, *args)]
    except Exception as e:  # noqa: BLE001 — a fingerprint never fails a draft or a scan
        logger.info("wm_config_fp_read_unavailable", error=f"{type(e).__name__}: {str(e)[:160]}")
        return UNAVAILABLE


async def collect(conn, writer: str, *, scope: str = "platform", tenant_id: Optional[str] = None,
                  env: Optional[Mapping[str, str]] = None) -> dict:
    """Read every component's rows and `build` the writer's config_fp (one owner: platform, or a tenant)."""
    routes = await _rows(conn, _ROUTES_SQL)
    policies = await _rows(conn, _POLICY_SQL)
    params = await _rows(conn, _PARAMS_SQL)
    models = [] if routes == UNAVAILABLE else route_models(r for r in routes if on_path(writer, r.get("stage")))
    catalog = await _rows(conn, _CATALOG_SQL, models)
    deployments = await _rows(conn, _DEPLOYMENT_SQL, models)
    settings = await _rows(conn, _ROUTE_SETTINGS_SQL)
    settings = settings if settings == UNAVAILABLE else (settings[0] if settings else {})

    memory_items: Any = None
    if scope == "tenant":
        items = await _rows(conn, _PLATFORM_ITEMS_SQL, writer)
        memory_items = items if items == UNAVAILABLE else {"platform": [r["items_sha256"] for r in items]}

    l0_rules: Any = ()
    if writer.startswith("t9_"):
        l0_rules = await _rows(conn, _L0_SQL, writer, tenant_id)

    owner = AA_MASTER_TENANT_ID if writer == "a1" else (tenant_id if scope == "tenant" else None)
    brand: Any = None
    if owner:
        b = await _rows(conn, _BRAND_SQL, owner)
        s = await _rows(conn, _SEO_SQL, owner)
        brand = (UNAVAILABLE if UNAVAILABLE in (b, s) else
                 {"brand_rules": [_jsonish(r["row"]) for r in b], "seo": [_jsonish(r["row"]) for r in s]})

    return build(writer, routes=routes, policies=policies, params=params, catalog=catalog, deployments=deployments,
                 route_settings=settings, env=env, prompts=prompt_shas(writer), memory_items=memory_items,
                 l0_rules=l0_rules, brand_rows=brand)


__all__ = ["AA_MASTER_TENANT_ID", "COMPONENTS", "EVAL_STAGES", "FP_VERSION", "GATEWAY_FLAGS", "MEMORY_FLAGS",
           "MODE_FLAGS", "MODE_FLAG_PREFIXES", "NOT_OUTPUT_FLAGS", "REVERIFY_REASON", "STAGE_PREFIXES", "UNAVAILABLE",
           "WRITER_SERVICE", "build", "changed_stages", "collect", "current_service", "mode_flags",
           "mode_flags_reference", "on_any_path", "on_path", "path_stages", "prompt_shas", "reasons_of",
           "reverify_components", "route_models", "set_service", "sha"]
