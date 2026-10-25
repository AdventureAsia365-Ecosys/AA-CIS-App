"""AA-919 (WM-17) — turn an AA-729 golden-set fixture into the rows of shared.eval_golden_set / eval_golden_item
(CIS 284) and the baseline results of shared.eval_item_result. Pure functions: no DB, no network, no model call.

Rules (AA-919 plan §4 with the critic's fixes):
- writer: stratum s1 -> a1, t2 -> t2, t9_blog -> t9_blog, t9_linkedin -> t9_social; an injection sample follows its
  host stratum (injection_s1 -> a1, injection_t9 -> t9_blog / t9_social by its channel). The T9 channel comes from
  the sample and must equal input.context.channel.
- kind (critic 11): s1 / t2 / t9 -> real, injection_s1 / injection_t9 -> synthetic. Anything else is refused.
- scope: a1 is platform (no tenant); T2 / T9 are tenant. The tenant is refs.tenant_id (equal to input.tenant_id and
  input.context.tenant_id when present); an inj-t9 sample takes it from its host sample's refs (critic 11).
- split: injection samples are traps (split 'trap'); the rest is stratified by (writer, label verdict): in each
  stratum the samples are ordered by sha256("<seed>:<sample_id>") and the first round_half_up(pct x n) go to holdout.
- tiers: the fixture tags, plus long_9d (days >= 9; NULL days is not long), post_edit (output_post_edit, a boolean),
  and fact_dense: the top round_half_up(pct x n) non-trap a1 / t2 items by fact density = (numbers + schedule +
  places entries of services.quality.s1_facts.build_fact_table) per 100 source words. The cut per writer is stored
  in tier_rules.
- tenant brand packs (shared_inputs.brand_rules t2:<slug>) are inlined on the items that reference them, and only on
  items of that tenant (brand_rules_ref t2:<slug> must name the item's tenant slug); only s1:* packs stay at set level.
- baseline (critic 10 / 15): one baseline_import run per writer. Without score files the control rows come from the
  fixture's frozen pipeline verdict (mode score_only, arm control); a post_edit item is never a score_only control.
"""
from __future__ import annotations

import hashlib
import json
import re
from collections import Counter
from dataclasses import dataclass, field
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from typing import Any, Optional

FIXTURE_SCHEMA = "aa729-golden-set"
SCORE_SCHEMA = "aa729-score"
WRITERS = ("a1", "t2", "t9_blog", "t9_social")
# The closed tier list of CIS 284 (eval_golden_item_tiers_check), in display order.
TIERS = ("cycle_bike", "trek_hike", "water", "wildlife", "culinary", "long_9d", "long_18d", "poor_raw",
         "known_failure", "held", "fact_dense", "prompt_injection", "post_edit")
KIND_MAP = {"s1": "real", "t2": "real", "t9": "real", "injection_s1": "synthetic", "injection_t9": "synthetic"}
STRATUM_WRITER = {"s1": "a1", "t2": "t2", "t9_blog": "t9_blog", "t9_linkedin": "t9_social"}
LABEL_SOURCES = ("agent_panel", "synthetic", "admin_edit", "tenant_edit", "human_label")
FACT_DENSE_WRITERS = ("a1", "t2")
LONG_DAYS = 9
DEFAULT_HOLDOUT_PCT = Decimal("0.300")
_SAMPLE_ID_RE = re.compile(r"^[a-z0-9-]{1,40}$")
_UUID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")
_SEMVER_RE = re.compile(r"^[0-9]{1,4}\.[0-9]{1,4}\.[0-9]{1,4}$")
_WORD_RE = re.compile(r"\w+")


class FixtureError(ValueError):
    """The fixture breaks a rule of the import: nothing is written."""


def canonical_json(obj: Any) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=str)


def sha256_json(obj: Any) -> str:
    return hashlib.sha256(canonical_json(obj).encode("utf-8")).hexdigest()


def round_half_up(pct: Decimal, n: int) -> int:
    """round(pct x n) with halves rounded up, in Decimal (0.3 x 5 = 1.5 -> 2; a float would give 1.4999… -> 1)."""
    return int((Decimal(pct) * n).quantize(Decimal(1), rounding=ROUND_HALF_UP))


def order_key(seed: int, sample_id: str) -> str:
    return hashlib.sha256(f"{seed}:{sample_id}".encode("utf-8")).hexdigest()


# ── per-sample mapping ─────────────────────────────────────────────────────────────────────────────────────────


def writer_and_channel(sample: dict) -> tuple[str, Optional[str]]:
    kind, stratum = sample.get("kind"), sample.get("stratum")
    if kind not in KIND_MAP:
        raise FixtureError(f"{sample.get('sample_id')}: unknown kind {kind!r}")
    ctx_channel = ((sample.get("input") or {}).get("context") or {}).get("channel")
    channel = sample.get("channel")
    if kind in ("t9", "injection_t9"):
        if not channel or channel != ctx_channel:
            raise FixtureError(f"{sample['sample_id']}: T9 channel {channel!r} differs from input.context.channel "
                               f"{ctx_channel!r}")
        writer = "t9_blog" if channel == "blog" else "t9_social"
        if kind == "t9" and STRATUM_WRITER.get(stratum) != writer:
            raise FixtureError(f"{sample['sample_id']}: stratum {stratum!r} does not match channel {channel!r}")
        if not re.fullmatch(r"[a-z_]{1,32}", channel):
            raise FixtureError(f"{sample['sample_id']}: bad channel {channel!r}")
        return writer, channel
    if channel is not None:
        raise FixtureError(f"{sample['sample_id']}: a {kind} sample has a channel ({channel!r})")
    if kind == "injection_s1":
        return "a1", None
    writer = STRATUM_WRITER.get(stratum)
    if writer is None or writer != {"s1": "a1", "t2": "t2"}[kind]:
        raise FixtureError(f"{sample['sample_id']}: stratum {stratum!r} does not match kind {kind!r}")
    return writer, None


def _tenant_of(sample: dict) -> Optional[str]:
    tid = (sample.get("refs") or {}).get("tenant_id")
    return str(tid) if tid else None


def scope_and_tenant(sample: dict, writer: str, by_id: dict[str, dict]) -> tuple[str, Optional[str]]:
    sid = sample["sample_id"]
    if writer == "a1":
        if _tenant_of(sample) or (sample.get("input") or {}).get("tenant_id"):
            raise FixtureError(f"{sid}: an a1 sample carries a tenant")
        return "platform", None
    refs = sample.get("refs") or {}
    if sample["kind"] == "injection_t9":
        host = by_id.get(str(refs.get("host_sample_id") or ""))
        if host is None:
            raise FixtureError(f"{sid}: host sample {refs.get('host_sample_id')!r} is not in the fixture")
        tenant = _tenant_of(host)
        if not tenant:
            raise FixtureError(f"{sid}: host sample {host['sample_id']} has no refs.tenant_id")
        if refs.get("tenant_slug") != (host.get("refs") or {}).get("tenant_slug"):
            raise FixtureError(f"{sid}: tenant_slug differs from its host sample's")
    else:
        tenant = _tenant_of(sample)
        if not tenant:
            raise FixtureError(f"{sid}: a {writer} sample has no refs.tenant_id")
    if not _UUID_RE.match(tenant):
        raise FixtureError(f"{sid}: refs.tenant_id is not a uuid")
    inp = sample.get("input") or {}
    for where, value in (("input.tenant_id", inp.get("tenant_id")),
                         ("input.context.tenant_id", (inp.get("context") or {}).get("tenant_id"))):
        if value is not None and str(value) != tenant:
            raise FixtureError(f"{sid}: {where} differs from the sample's tenant")
    return "tenant", tenant


def source_words(tour: dict) -> int:
    from services.quality.s1_facts import TEXT_FIELDS, source_fields

    fields = source_fields(tour or {})
    return sum(len(_WORD_RE.findall(fields.get(f, ""))) for f in TEXT_FIELDS)


def fact_density(tour: dict) -> float:
    """Fact entries (numbers + schedule + places of build_fact_table) per 100 source words; 0 for an empty tour."""
    from services.quality.s1_facts import build_fact_table

    words = source_words(tour)
    if not words:
        return 0.0
    facts = build_fact_table(tour or {})
    n = len(facts.get("numbers") or []) + len(facts.get("schedule") or []) + len(facts.get("places") or [])
    return round(100.0 * n / words, 4)


def assign_splits(rows: list[dict], seed: int, pct: Decimal = DEFAULT_HOLDOUT_PCT) -> dict[str, str]:
    """sample_id -> tune | holdout | trap. rows carry sample_id, writer, label_verdict and trap."""
    out: dict[str, str] = {}
    strata: dict[tuple[str, str], list[str]] = {}
    for r in rows:
        if r["trap"]:
            out[r["sample_id"]] = "trap"
        else:
            strata.setdefault((r["writer"], r["label_verdict"]), []).append(r["sample_id"])
    for ids in strata.values():
        ordered = sorted(ids, key=lambda s: order_key(seed, s))
        k = round_half_up(pct, len(ordered))
        for i, sid in enumerate(ordered):
            out[sid] = "holdout" if i < k else "tune"
    return out


def fact_dense_cuts(rows: list[dict], pct: Decimal = DEFAULT_HOLDOUT_PCT) -> tuple[set[str], dict]:
    """(sample ids tiered fact_dense, the per-writer rule record). rows carry sample_id, writer, trap, density."""
    chosen: set[str] = set()
    per_writer: dict[str, dict] = {}
    for writer in FACT_DENSE_WRITERS:
        pool = sorted((r for r in rows if r["writer"] == writer and not r["trap"]),
                      key=lambda r: (-r["density"], r["sample_id"]))
        if not pool:
            continue
        k = round_half_up(pct, len(pool))
        top = pool[:k]
        chosen.update(r["sample_id"] for r in top)
        per_writer[writer] = {"n": len(pool), "k": k, "cut": top[-1]["density"] if top else None,
                              "max": pool[0]["density"], "min": pool[-1]["density"]}
    return chosen, per_writer


# ── the whole fixture ─────────────────────────────────────────────────────────────────────────────────────────


@dataclass
class GoldenSet:
    set_row: dict
    items: list[dict]
    tenant_ids: list[str]
    dropped_packs: list[str] = field(default_factory=list)


def _clean_refs(sid: str, refs: Any) -> dict:
    if not isinstance(refs, dict):
        raise FixtureError(f"{sid}: refs is not an object")
    for k, v in refs.items():
        if v is None:
            continue
        if not isinstance(v, (str, int)) or isinstance(v, bool) or (isinstance(v, str) and len(v) > 100):
            raise FixtureError(f"{sid}: refs.{k} is not an id")
    return dict(refs)


def _numbers_only(obj: Any) -> Any:
    """Keep the numeric leaves of a nested dict (meta.selection: candidate counts, percentiles)."""
    if isinstance(obj, dict):
        kept = {str(k): _numbers_only(v) for k, v in obj.items()}
        return {k: v for k, v in kept.items() if v is not None and v != {}}
    if isinstance(obj, (int, float)) and not isinstance(obj, bool):
        return obj
    return None


def build(fixture: dict, *, name: str, source_uri: str, source_sha256: str, imported_by: str,
          pct: Decimal = DEFAULT_HOLDOUT_PCT) -> GoldenSet:
    if fixture.get("schema") != FIXTURE_SCHEMA:
        raise FixtureError(f"not a golden-set fixture (schema {fixture.get('schema')!r})")
    version = str(fixture.get("fixture_version") or "")
    if not _SEMVER_RE.match(version):
        raise FixtureError(f"fixture_version {version!r} is not x.y.z")
    seed = fixture.get("seed")
    if not isinstance(seed, int) or isinstance(seed, bool):
        raise FixtureError("the fixture has no integer seed")
    samples = fixture.get("samples")
    if not isinstance(samples, list) or not samples:
        raise FixtureError("the fixture has no samples")
    packs = ((fixture.get("shared_inputs") or {}).get("brand_rules") or {})
    if not isinstance(packs, dict):
        raise FixtureError("shared_inputs.brand_rules is not an object")

    by_id: dict[str, dict] = {}
    for s in samples:
        sid = s.get("sample_id")
        if not isinstance(sid, str) or not _SAMPLE_ID_RE.match(sid):
            raise FixtureError(f"bad sample_id {sid!r}")
        if sid in by_id:
            raise FixtureError(f"duplicate sample_id {sid}")
        by_id[sid] = s

    items: list[dict] = []
    used_packs: set[str] = set()
    for s in samples:
        sid = s["sample_id"]
        writer, channel = writer_and_channel(s)
        scope, tenant = scope_and_tenant(s, writer, by_id)
        trap = s["kind"].startswith("injection_")
        if trap != (s.get("stratum") == "injection"):
            raise FixtureError(f"{sid}: kind {s['kind']} and stratum {s.get('stratum')} disagree on injection")
        spec = s.get("injection_spec")
        if trap and not isinstance(spec, dict):
            raise FixtureError(f"{sid}: an injection sample without an injection_spec object")
        if not trap and spec is not None:
            raise FixtureError(f"{sid}: injection_spec on a non-injection sample")
        label = s.get("label")
        if not isinstance(label, dict) or label.get("verdict") not in ("pass", "fail"):
            raise FixtureError(f"{sid}: label.verdict must be pass or fail")
        if s.get("label_source") not in LABEL_SOURCES:
            raise FixtureError(f"{sid}: unknown label_source {s.get('label_source')!r}")
        post_edit = s.get("output_post_edit")
        if not isinstance(post_edit, bool):
            raise FixtureError(f"{sid}: output_post_edit must be a boolean")
        tags = s.get("tags") or []
        bad_tags = [t for t in tags if t not in TIERS]
        if bad_tags:
            raise FixtureError(f"{sid}: unknown tags {bad_tags} (a new tier needs a migration)")
        days = s.get("days")
        if days is not None and (not isinstance(days, int) or isinstance(days, bool) or not 1 <= days <= 60):
            raise FixtureError(f"{sid}: days {days!r} is not 1..60")
        inp = s.get("input")
        if not isinstance(inp, dict):
            raise FixtureError(f"{sid}: input is not an object")
        inp = dict(inp)
        ref = inp.get("brand_rules_ref")
        if ref is not None:
            if ref not in packs:
                raise FixtureError(f"{sid}: brand_rules_ref {ref!r} is not in shared_inputs")
            if not str(ref).startswith("s1:"):
                slug = (s.get("refs") or {}).get("tenant_slug")
                if scope != "tenant" or str(ref) != f"t2:{slug}":
                    raise FixtureError(f"{sid}: brand pack {ref!r} may only be inlined on its own tenant's items")
                inp["brand_rules"] = packs[ref]
                used_packs.add(ref)
        for key, value in (("output", s.get("output")), ("pipeline_verdict", s.get("pipeline_verdict"))):
            if value is not None and not isinstance(value, dict):
                raise FixtureError(f"{sid}: {key} is not an object")
        country = s.get("country")
        if country is not None and len(str(country)) > 60:
            raise FixtureError(f"{sid}: country longer than 60 characters")
        pick = s.get("pick_reason")
        if pick is not None and len(str(pick)) > 80:
            raise FixtureError(f"{sid}: pick_reason longer than 80 characters")
        items.append({
            "sample_id": sid, "writer": writer, "channel": channel, "scope": scope, "tenant_id": tenant,
            "trap": trap, "kind": KIND_MAP[s["kind"]], "country": None if country is None else str(country),
            "days": days, "input": inp, "control_output": s.get("output"), "post_edit": post_edit,
            "pipeline_verdict": s.get("pipeline_verdict"), "label": label, "label_verdict": label["verdict"],
            "label_source": s["label_source"], "injection_spec": spec if trap else None,
            "refs": _clean_refs(sid, s.get("refs") or {}), "input_sha256": sha256_json(inp),
            "pick_reason": None if pick is None else str(pick), "_tags": list(tags),
            "_density": fact_density(inp.get("tour") or {}) if writer in FACT_DENSE_WRITERS and not trap else None,
        })

    splits = assign_splits(items, seed, pct)
    dense, dense_rule = fact_dense_cuts(
        [{"sample_id": i["sample_id"], "writer": i["writer"], "trap": i["trap"], "density": i["_density"]}
         for i in items if i["_density"] is not None], pct)
    for i in items:
        tiers = set(i.pop("_tags"))
        if i["days"] is not None and i["days"] >= LONG_DAYS:
            tiers.add("long_9d")
        if i["post_edit"]:
            tiers.add("post_edit")
        if i["sample_id"] in dense:
            tiers.add("fact_dense")
        i["tiers"] = [t for t in TIERS if t in tiers]
        i["split"] = splits[i["sample_id"]]
        i["density"] = i.pop("_density")

    platform_packs = {k: v for k, v in packs.items() if str(k).startswith("s1:")}
    dropped = sorted(k for k in packs if not str(k).startswith("s1:") and k not in used_packs)
    tier_rules = {
        "long_9d": {"days_gte": LONG_DAYS, "null_days": False},
        "post_edit": {"from": "output_post_edit"},
        "fact_dense": {"writers": list(FACT_DENSE_WRITERS), "top_pct": str(pct), "rounding": "half_up",
                       "metric": "(numbers + schedule + places) of services.quality.s1_facts.build_fact_table per "
                                 "100 source words (TEXT_FIELDS), traps excluded",
                       "per_writer": dense_rule},
        "split": {"stratify": ["writer", "label_verdict"], "order": "sha256('<seed>:<sample_id>')",
                  "holdout_pct": str(pct), "rounding": "half_up", "traps": "injection samples, split 'trap'"},
        "fixture_tags": "kept as tiers",
    }
    meta = {"schema": FIXTURE_SCHEMA, "created_at": fixture.get("created_at"),
            "mix": _numbers_only(fixture.get("mix") or {}), "quotas": fixture.get("quotas") or {},
            "selection": _numbers_only(fixture.get("selection") or {}),
            "tenant_packs_inlined": sorted(used_packs), "tenant_packs_dropped": dropped}
    label_history = fixture.get("label_history") or []
    if not isinstance(label_history, list):
        raise FixtureError("label_history is not a list")
    set_row = {"name": name, "version": version, "source_uri": source_uri, "source_sha256": source_sha256,
               "seed": seed, "holdout_pct": pct, "tier_rules": tier_rules,
               "shared_inputs": {"brand_rules": platform_packs}, "meta": meta, "label_history": label_history,
               "item_count": len(items), "imported_by": imported_by}
    tenants = sorted({i["tenant_id"] for i in items if i["tenant_id"]})
    return GoldenSet(set_row, items, tenants, dropped)


def count_report(gs: GoldenSet) -> dict:
    """Counts only (no text): items per writer / split / tier / scope, traps, the fact_dense cuts."""
    by_split: dict[str, Counter] = {w: Counter() for w in WRITERS}
    by_tier: dict[str, Counter] = {w: Counter() for w in WRITERS}
    for i in gs.items:
        by_split[i["writer"]][i["split"]] += 1
        for t in i["tiers"]:
            by_tier[i["writer"]][t] += 1
    return {
        "items": len(gs.items),
        "by_writer_split": {w: dict(sorted(c.items())) for w, c in by_split.items()},
        "by_writer_tier": {w: dict(sorted(c.items())) for w, c in by_tier.items()},
        "traps": sum(1 for i in gs.items if i["trap"]),
        "scope": dict(Counter(i["scope"] for i in gs.items)),
        "kind": dict(Counter(i["kind"] for i in gs.items)),
        "post_edit": sum(1 for i in gs.items if i["post_edit"]),
        "tenants": len(gs.tenant_ids),
        "fact_dense": gs.set_row["tier_rules"]["fact_dense"]["per_writer"],
        "tenant_packs_inlined": gs.set_row["meta"]["tenant_packs_inlined"],
        "tenant_packs_dropped": gs.dropped_packs,
    }


# ── baseline (one baseline_import run per writer) ──────────────────────────────────────────────────────────────

PHASE_OF_SPLIT = {"tune": "tune", "holdout": "holdout", "trap": "probe"}
_MODE_OF_FILE = {"score-only": "score_only", "regenerate": "regenerate"}


def _num(v: Any) -> Optional[float]:
    """A finite number from an int, a float, a Decimal or a numeric string (the AA-729 fixture stores
    pipeline_verdict.score as "8.00"; score files carry DB numerics as strings). Anything else is None."""
    if isinstance(v, bool) or not isinstance(v, (int, float, Decimal, str)):
        return None
    try:
        d = Decimal(v.strip() if isinstance(v, str) else v)
    except InvalidOperation:
        return None
    return float(d) if d.is_finite() else None


def _codes(v: Any) -> list[str]:
    return [str(c)[:120] for c in (v or []) if isinstance(c, (str, int))][:50] if isinstance(v, list) else []


def _fixture_control(item: dict) -> dict:
    pv = item.get("pipeline_verdict") or {}
    passed = pv.get("passed")
    verdict = "skipped" if not isinstance(passed, bool) else ("pass" if passed else "fail")
    return {"mode": "score_only", "verdict": verdict,
            "score": _num(pv.get("score")), "failure_codes": _codes(pv.get("failure_codes")), "output": None,
            "judge_model": None, "judge_version": "fixture.pipeline_verdict", "cost_usd": 0.0,
            "details": {"source": "fixture.pipeline_verdict"}}


def _score_row(row: dict, mode: str, run_id: str) -> dict:
    verdict = row.get("verdict") if isinstance(row.get("verdict"), dict) else {}
    error = row.get("error")
    if error:
        v = "error"
    elif "passed" not in verdict:
        v = "skipped"
    else:
        v = "pass" if verdict.get("passed") else "fail"
    score = _num(verdict.get("quality_score"))
    if score is None:
        score = _num(verdict.get("judge_score"))
    output = row.get("output") if mode == "regenerate" and isinstance(row.get("output"), dict) else None
    details: dict = {"source": "score_file", "source_run_id": run_id}
    if error:
        details["error_kind"] = str(error).split(":", 1)[0][:80]
    if verdict.get("degraded_hard"):
        details["degraded_hard"] = True
    return {"mode": mode, "verdict": v, "score": score,
            "failure_codes": _codes(verdict.get("failure_codes") or verdict.get("blocking_failures")),
            "output": output, "output_sha256": sha256_json(output) if output is not None else None,
            "judge_model": None, "judge_version": f"aa729-score:{run_id}"[:128],
            "cost_usd": max(_num(row.get("cost_usd")) or 0.0, 0.0), "details": details}


def baseline(gs: GoldenSet, score_files: Optional[list[tuple[str, dict]]] = None) -> dict[str, dict]:
    """writer -> {"run": run row fields, "results": [result fields + sample_id]}. score_files: (key, parsed file)."""
    version = gs.set_row["version"]
    items = {i["sample_id"]: i for i in gs.items}
    by_mode: dict[str, tuple[str, dict]] = {}
    for key, f in score_files or []:
        if not isinstance(f, dict) or f.get("schema") != SCORE_SCHEMA:
            raise FixtureError(f"{key}: not an aa729 score file")
        if str(f.get("fixture_version")) != version:
            raise FixtureError(f"{key}: scored fixture {f.get('fixture_version')!r}, importing {version!r}")
        if f.get("final") is not True:
            raise FixtureError(f"{key}: a partial (non-final) score file")
        mode = _MODE_OF_FILE.get(f.get("mode"))
        if mode is None:
            raise FixtureError(f"{key}: unknown mode {f.get('mode')!r}")
        if mode in by_mode:
            raise FixtureError(f"{key}: a second {f.get('mode')} file")
        unknown = [r.get("sample_id") for r in f.get("samples") or [] if r.get("sample_id") not in items]
        if unknown:
            raise FixtureError(f"{key}: sample ids not in the fixture: {unknown[:5]}")
        by_mode[mode] = (key, f)

    rows: dict[str, list[dict]] = {w: [] for w in WRITERS}
    skipped_post_edit: Counter = Counter()
    if "score_only" in by_mode:
        key, f = by_mode["score_only"]
        for r in f.get("samples") or []:
            it = items[r["sample_id"]]
            if it["post_edit"]:
                skipped_post_edit[it["writer"]] += 1
                continue
            rows[it["writer"]].append({"sample_id": it["sample_id"], **_score_row(r, "score_only", f["run_id"])})
    else:
        for it in gs.items:
            if it["control_output"] is None or it["pipeline_verdict"] is None:
                continue                      # nothing frozen to record (the traps have no pipeline verdict)
            if it["post_edit"]:
                skipped_post_edit[it["writer"]] += 1
                continue
            rows[it["writer"]].append({"sample_id": it["sample_id"], **_fixture_control(it)})
    if "regenerate" in by_mode:
        key, f = by_mode["regenerate"]
        for r in f.get("samples") or []:
            it = items[r["sample_id"]]
            rows[it["writer"]].append({"sample_id": it["sample_id"], **_score_row(r, "regenerate", f["run_id"])})

    sources = [{"key": key, "run_id": f.get("run_id"), "mode": f.get("mode")} for key, f in by_mode.values()]
    source_cost = {k: round(sum(r["cost_usd"] for r in rows[k]), 6) for k in WRITERS}
    out: dict[str, dict] = {}
    for w in WRITERS:
        rs = rows[w]
        if not rs:
            continue
        for r in rs:
            it = items[r["sample_id"]]
            r.update({"arm": "control", "phase": PHASE_OF_SPLIT[it["split"]]})
            r.setdefault("output_sha256", None)
        counts = Counter(f"{r['mode']}:{r['verdict']}" for r in rs)
        out[w] = {
            "run": {"prompt_version": f"aa729-frozen@{version}", "tour_count": len({r["sample_id"] for r in rs}),
                    "cost_usd": 0.0, "verdict": "baseline", "stop_reason": "completed", "run_kind": "baseline_import",
                    "fingerprint": {"golden_set": f"{gs.set_row['name']}@{version}",
                                    "baseline_source": "score_files" if by_mode else "fixture"},
                    "details": {"source": "score_files" if by_mode else "fixture.pipeline_verdict",
                                "score_runs": sources, "results": dict(sorted(counts.items())),
                                "skipped_post_edit": skipped_post_edit.get(w, 0),
                                "source_cost_usd": source_cost[w]}},
            "results": rs,
        }
    return out
