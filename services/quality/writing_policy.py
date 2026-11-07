"""AA-1047 (root ADR 0020, layer L1) — the writing policy registry: loader, compiler and run-time accessors.

`writing_policy.yaml` (next to this module) is the ONE source of the writing rules frozen from the S271 decisions
D1–D16. This module compiles it into `writing_policy.compiled.json` (the generated artefact every consumer reads) and
stamps `policy_sha` on each artefact:

  python -m services.quality.writing_policy --write    # regenerate the compiled file
  python -m services.quality.writing_policy --check    # exit 1 when it is stale (the CI lint runs the same check)

What is compiled, and who reads it:

- **bands** (D5): the one meta band, the title band and the slug cap — content_generation.seo_meta_utils,
  acp_content_writing.quality_gates / service / generate, the writer prompts (through `fill`), the S1 checks and
  the admin UI (frontend/lib/quality-registry.generated.ts).
- **writer aims**: where each writer aims below the meta band (lesson S233: a model overshoots a character target).
- **label_rubric**: the golden-label rubric of the AA-729 panel (scripts/aa729/labels.RUBRIC) and of the R2 eval
  judge (writing_memory.eval_judge.LABEL_RUBRIC) — one object, so they cannot drift.
- **eval**: the classes the eval judge reports (the others are measured by code).
- **rules_config**: claim_rules.RulesConfig (the F9 v2 rule table's open choices).
- **route_rows**: one row per T10 gate (repair route, style-only) and the F9 class actions — quality_gates
  CHEAP_REPAIR_GATES / STYLE_ONLY_GATES / OWNED_BY_FIX_STEP and f9_policy.HOLD_CLASSES.
- **fragments**: prompt lines rendered with `<<token>>` values.
- **severities**: the class severities the policy owns (taxonomy.yaml must agree; the lint checks).

`policy_sha` = sha256 (16 hex) of the canonical JSON of the YAML without its `examples` / `example_seeds` (synthetic
fixtures change no behaviour). It is part of the release fingerprint (writing_memory.config_fp component `policy`)
and of the eval judge version.

Pure: standard library + PyYAML (the CI Lint job runs the lint with only PyYAML installed). Accessors read the
compiled JSON once per process and never raise on a request path for a known key.
"""
from __future__ import annotations

import argparse
import copy
import hashlib
import json
import re
import sys
from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml

_DIR = Path(__file__).resolve().parent
POLICY_PATH = _DIR / "writing_policy.yaml"
COMPILED_PATH = _DIR / "writing_policy.compiled.json"
COMPILED_REL = "services/quality/writing_policy.compiled.json"
POLICY_REL = "services/quality/writing_policy.yaml"

TOKEN_RE = re.compile(r"<<([a-z0-9_.]+)>>")
SEVERITIES = ("hard", "block", "warn")
ROUTE_REPAIRS = ("cheap", "fix_step", "default")
F9_ACTIONS = ("hold", "warn")
EXAMPLE_EXPECT = ("block", "warn", "pass")
EXAMPLE_FIELDS = ("body", "meta", "title", "slug", "cta", "faq", "itinerary")
EXAMPLE_WRITERS = ("a1", "t2")    # the writers an itinerary example's meal check runs (`writers`, default both)
FIXTURE_KEYS = ("examples", "example_seeds")    # synthetic fixtures: no behaviour, not part of policy_sha
RULES_CONFIG_KEYS = ("hypothetical", "uncited_background")
# S272: `block_until_w2_instrument` = the topic's cases are the verdicts the gates give today (main's, fail closed);
# the decided intent waits in `w2_intent` until the W2 claim instrument is measured
ENFORCEMENTS = ("block_until_w2_instrument",)


class PolicyError(ValueError):
    """The policy file breaks its own schema (the compiler refuses to write)."""


# ------------------------------------------------------------------------------------------------------- loading

def load_source(path: Path = POLICY_PATH) -> dict:
    with open(path, encoding="utf-8") as fh:
        data = yaml.safe_load(fh)
    if not isinstance(data, dict):
        raise PolicyError(f"{path.name} is not a mapping")
    return data


def _canon(value: Any) -> str:
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"))


def sha_of(source: dict) -> str:
    """policy_sha of a loaded policy (the synthetic examples and their seeds left out)."""
    body = {k: v for k, v in source.items() if k not in FIXTURE_KEYS}
    return hashlib.sha256(_canon(body).encode("utf-8")).hexdigest()[:16]


# ------------------------------------------------------------------------------------------------------- tokens

def tokens_of(source: dict) -> dict[str, str]:
    """The `<<key>>` values fragments and consumer templates may use (all numbers rendered as text)."""
    bands = source.get("bands") or {}
    out: dict[str, str] = {}
    for band in ("meta", "title"):
        b = bands.get(band) or {}
        out[f"{band}.min"] = str(b.get("min"))
        out[f"{band}.max"] = str(b.get("max"))
    out["slug.max"] = str((bands.get("slug") or {}).get("max"))
    for field, by_writer in ((source.get("writer_aims") or {}).items()):
        for writer, aim in (by_writer or {}).items():
            out[f"aim.{writer}.{field}.min"] = str(aim.get("min"))
            out[f"aim.{writer}.{field}.max"] = str(aim.get("max"))
    faq = source.get("faq") or {}
    for k, v in (faq.get("pairs") or {}).items():
        out[f"faq.pairs.{k}"] = str(v)
    return out


def render(text: str, tokens: dict[str, str]) -> str:
    """`text` with every `<<key>>` replaced; an unknown key raises PolicyError (a typo never reaches a prompt)."""
    def sub(m: re.Match) -> str:
        key = m.group(1)
        if key not in tokens:
            raise PolicyError(f"unknown policy token <<{key}>>")
        return tokens[key]
    return TOKEN_RE.sub(sub, text)


# ------------------------------------------------------------------------------------------------------- schema

def _need(cond: bool, msg: str, errors: list[str]) -> None:
    if not cond:
        errors.append(msg)


def schema_errors(source: dict) -> list[str]:
    """Every schema problem of a loaded policy ([] = compiles)."""
    e: list[str] = []
    _need(isinstance(source.get("version"), str) and source["version"].startswith("writing-policy-"),
          "version must be 'writing-policy-<n>'", e)
    decisions = source.get("decisions") or {}
    _need(sorted(decisions, key=lambda d: int(str(d)[1:])) == [f"D{i}" for i in range(1, 17)],
          "decisions must be exactly D1..D16", e)
    bands = source.get("bands") or {}
    for band in ("meta", "title"):
        b = bands.get(band) or {}
        _need(isinstance(b.get("min"), int) and isinstance(b.get("max"), int) and b["min"] < b["max"],
              f"bands.{band} needs integer min < max", e)
    _need(isinstance((bands.get("slug") or {}).get("max"), int), "bands.slug.max must be an integer", e)
    meta = bands.get("meta") or {}
    for writer, aim in ((source.get("writer_aims") or {}).get("meta") or {}).items():
        ok = isinstance(aim, dict) and isinstance(aim.get("min"), int) and isinstance(aim.get("max"), int)
        _need(ok and aim["min"] < aim["max"] <= meta.get("max", 0),
              f"writer_aims.meta.{writer} must be min < max <= the meta band max", e)
        # S272 review: an aim under the band floor is a documented exception (lesson S233: models overshoot), never
        # silent: it names the deterministic step that lands a short value in the band (D5 "code composes")
        _need(not ok or aim["min"] >= meta.get("min", 0) or (isinstance(aim.get("short_fix"), str)
                                                             and ":" in aim["short_fix"]),
              f"writer_aims.meta.{writer} aims under the band min: it needs short_fix '<module>:<function>'", e)
    pairs = (source.get("faq") or {}).get("pairs") or {}
    _need(all(isinstance(pairs.get(k), int) for k in ("min", "max", "regenerate"))
          and pairs.get("min", 0) <= pairs.get("regenerate", -1) <= pairs.get("max", -1),
          "faq.pairs needs min <= regenerate <= max", e)
    for cid, sev in (source.get("severities") or {}).items():
        _need(sev in SEVERITIES, f"severities.{cid}: {sev!r} is not one of {SEVERITIES}", e)
    rows = source.get("route_rows") or []
    gates = [r.get("gate") for r in rows if isinstance(r, dict)]
    _need(len(gates) == len(set(gates)) == len(rows), "route_rows: exactly one row per gate", e)
    for r in rows:
        _need(r.get("repair") in ROUTE_REPAIRS, f"route_rows.{r.get('gate')}: repair must be one of {ROUTE_REPAIRS}",
              e)
    f9 = source.get("f9_class_rows") or []
    classes = [r.get("class") for r in f9 if isinstance(r, dict)]
    _need(len(classes) == len(set(classes)) == len(f9), "f9_class_rows: exactly one row per class", e)
    for r in f9:
        _need(r.get("action") in F9_ACTIONS, f"f9_class_rows.{r.get('class')}: action must be one of {F9_ACTIONS}", e)
    rubric = source.get("label_rubric") or {}
    _need(all(isinstance(rubric.get(k), str) and rubric[k] for k in ("pass", "fail"))
          and isinstance(rubric.get("rules"), list) and all(isinstance(r, str) for r in rubric["rules"]),
          "label_rubric needs pass, fail and a list of rules", e)
    rc = source.get("rules_config") or {}
    for k in RULES_CONFIG_KEYS:
        _need(rc.get(k) in ("pass", "warn", "fail"), f"rules_config.{k} must be pass | warn | fail", e)
    # S272: a claim topic held back to main's verdicts until the W2 claim instrument records its decided intent
    for topic, spec in (source.get("claims") or {}).items():
        if not isinstance(spec, dict) or "enforcement" not in spec:
            continue
        _need(spec["enforcement"] in ENFORCEMENTS, f"claims.{topic}.enforcement must be one of {ENFORCEMENTS}", e)
        intent = spec.get("w2_intent")
        _need(isinstance(intent, dict) and bool(intent) and set(intent) <= set(spec) - {"w2_intent"},
              f"claims.{topic}: enforcement {spec['enforcement']!r} needs a w2_intent over its own cases", e)
    tok = tokens_of(source)
    for key, frag in (source.get("fragments") or {}).items():
        if not isinstance(frag, dict) or not isinstance(frag.get("text"), str):
            e.append(f"fragments.{key} needs a text")
            continue
        _need(bool(frag.get("byte_equal")) != bool(frag.get("changed")),
              f"fragments.{key} must say byte_equal: true or changed: <topic>", e)
        try:
            render(frag["text"], tok)
        except PolicyError as ex:
            e.append(f"fragments.{key}: {ex}")
    for w in source.get("retired_wordings") or []:
        _need(isinstance(w, dict) and isinstance(w.get("pattern"), str) and w["pattern"],
              "retired_wordings entries need a pattern", e)
    for i, ex in enumerate(source.get("examples") or []):
        where = f"examples[{i}] ({ex.get('id') if isinstance(ex, dict) else '?'})"
        if not isinstance(ex, dict):
            e.append(f"{where} is not a mapping")
            continue
        _need(isinstance(ex.get("id"), str) and isinstance(ex.get("topic"), str), f"{where} needs id and topic", e)
        _need(ex.get("expect") in EXAMPLE_EXPECT, f"{where}: expect must be one of {EXAMPLE_EXPECT}", e)
        _need(isinstance(ex.get("text"), str), f"{where} needs a text", e)
        _need(ex.get("field", "body") in EXAMPLE_FIELDS, f"{where}: field must be one of {EXAMPLE_FIELDS}", e)
        _need(ex.get("seed", "monastery") in (source.get("example_seeds") or {}), f"{where}: unknown seed", e)
        _need(ex.get("field") != "itinerary" or isinstance(ex.get("source_itinerary"), str),
              f"{where}: an itinerary example needs source_itinerary", e)
        writers = ex.get("writers", list(EXAMPLE_WRITERS))
        _need(isinstance(writers, list) and bool(writers) and set(writers) <= set(EXAMPLE_WRITERS),
              f"{where}: writers must be a non-empty list from {EXAMPLE_WRITERS}", e)
    ids = [ex.get("id") for ex in source.get("examples") or [] if isinstance(ex, dict)]
    _need(len(ids) == len(set(ids)), "examples: ids must be unique", e)
    return e


# ------------------------------------------------------------------------------------------------------- compile

def compile_policy(source: dict) -> dict:
    """The compiled artefacts of a loaded policy (pure). Raises PolicyError on a schema problem."""
    errors = schema_errors(source)
    if errors:
        raise PolicyError("; ".join(errors))
    sha = sha_of(source)
    tok = tokens_of(source)
    stamp = {"policy_sha": sha}
    rubric = source["label_rubric"]
    rows = source.get("route_rows") or []
    return {
        "_about": ("GENERATED by `python -m services.quality.writing_policy --write` from "
                   f"{POLICY_REL} (AA-1047). Do not edit by hand: change the YAML and regenerate."),
        "policy_version": source["version"],
        "policy_sha": sha,
        "decisions": {k: v["text"] for k, v in sorted(source["decisions"].items(), key=lambda kv: int(kv[0][1:]))},
        "bands": {**copy.deepcopy(source["bands"]), **stamp},
        "writer_aims": {**copy.deepcopy(source.get("writer_aims") or {}), **stamp},
        "tokens": dict(sorted(tok.items())),
        "cta": {**copy.deepcopy(source.get("cta") or {}), **stamp},
        "faq": {**copy.deepcopy(source.get("faq") or {}), **stamp},
        "meals": {**copy.deepcopy(source.get("meals") or {}), **stamp},
        "day_count": {**copy.deepcopy(source.get("day_count") or {}), **stamp},
        "claims": {**copy.deepcopy(source.get("claims") or {}), **stamp},
        "rules_config": {**copy.deepcopy(source["rules_config"]), **stamp},
        "severities": {**dict(sorted((source.get("severities") or {}).items())), **stamp},
        "eval": {**copy.deepcopy(source.get("eval") or {}), **stamp},
        "label_rubric": {"pass": rubric["pass"], "fail": rubric["fail"], "rules": list(rubric["rules"]), **stamp},
        "route_rows": {
            "gates": [{"gate": r["gate"], "repair": r["repair"], "style_only": bool(r.get("style_only"))}
                      for r in rows],
            "f9_classes": [{"class": r["class"], "action": r["action"]} for r in source.get("f9_class_rows") or []],
            **stamp,
        },
        "gate_promotions": {"rows": copy.deepcopy(source.get("gate_promotions") or []), **stamp},
        "rule_injection": {**copy.deepcopy(source.get("rule_injection") or {}), **stamp},
        "fragments": {**{k: render(v["text"], tok) for k, v in sorted((source.get("fragments") or {}).items())},
                      **stamp},
        "fragment_status": {k: ("byte_equal" if v.get("byte_equal") else f"changed:{v.get('changed')}")
                            for k, v in sorted((source.get("fragments") or {}).items())},
        "retired_wordings": [w["pattern"] for w in source.get("retired_wordings") or []],
    }


def render_compiled(source: dict | None = None) -> str:
    """The compiled file's exact text."""
    return json.dumps(compile_policy(source if source is not None else load_source()), indent=2,
                      ensure_ascii=False) + "\n"


def stale() -> bool:
    """True when the compiled file on disk is not what the YAML compiles to."""
    return not COMPILED_PATH.exists() or COMPILED_PATH.read_text(encoding="utf-8") != render_compiled()


# ------------------------------------------------------------------------------------------------------- accessors

@lru_cache(maxsize=1)
def compiled() -> dict:
    with open(COMPILED_PATH, encoding="utf-8") as fh:
        return json.load(fh)


def policy_sha() -> str:
    return compiled()["policy_sha"]


def policy_version() -> str:
    return compiled()["policy_version"]


def band(name: str) -> tuple[int, int]:
    """(min, max) of the meta or title band."""
    b = compiled()["bands"][name]
    return int(b["min"]), int(b["max"])


def slug_max() -> int:
    return int(compiled()["bands"]["slug"]["max"])


def writer_aim(writer: str, field: str = "meta") -> tuple[int, int]:
    a = compiled()["writer_aims"][field][writer]
    return int(a["min"]), int(a["max"])


def fill(text: str) -> str:
    """A consumer template with its `<<key>>` tokens rendered: a band / aim / FAQ token or a whole fragment
    (`<<t9_repair.background>>`). An unknown key raises PolicyError at import time."""
    c = compiled()
    frags = {k: v for k, v in c["fragments"].items() if k != "policy_sha"}
    return render(text, {**c["tokens"], **frags})


def fragment(key: str) -> str:
    return compiled()["fragments"][key]


def label_rubric() -> dict:
    """{pass, fail, rules (tuple), policy_sha} — a fresh copy (callers may not mutate the shared one)."""
    r = compiled()["label_rubric"]
    return {"pass": r["pass"], "fail": r["fail"], "rules": tuple(r["rules"]), "policy_sha": r["policy_sha"]}


def judge_classes() -> tuple[str, ...]:
    return tuple(compiled()["eval"]["judge_classes"])


def rules_config() -> dict:
    rc = compiled()["rules_config"]
    return {k: rc[k] for k in RULES_CONFIG_KEYS}


def faq_pairs() -> dict:
    return dict(compiled()["faq"]["pairs"])


def faq_topics_need_source() -> tuple[str, ...]:
    return tuple(compiled()["faq"]["topics_need_source"])


def claim(topic: str) -> dict:
    return dict(compiled()["claims"][topic])


def meals() -> dict:
    return dict(compiled()["meals"])


def gates_with_repair(repair: str) -> frozenset:
    return frozenset(r["gate"] for r in compiled()["route_rows"]["gates"] if r["repair"] == repair)


def style_only_gates() -> frozenset:
    return frozenset(r["gate"] for r in compiled()["route_rows"]["gates"] if r["style_only"])


def f9_hold_classes() -> frozenset:
    return frozenset(r["class"] for r in compiled()["route_rows"]["f9_classes"] if r["action"] == "hold")


def severities() -> dict[str, str]:
    return {k: v for k, v in compiled()["severities"].items() if k != "policy_sha"}


def slug_at_boundary(slug: str, cap: int | None = None) -> str:
    """A kebab-case slug cut to at most `cap` (default the policy slug cap) at a hyphen, never mid-word (D5, S271 T9
    contradiction 12). A single word longer than the cap is cut at the cap (nothing better exists)."""
    cap = slug_max() if cap is None else cap
    s = (slug or "").strip("-")
    if len(s) <= cap:
        return s
    cut = s[:cap + 1]
    at = cut.rfind("-")
    return (s[:at] if at > 0 else s[:cap]).strip("-")


def artefact_shas() -> dict[str, str]:
    """The policy_sha each compiled artefact carries (a test: all equal to policy_sha())."""
    c = compiled()
    return {k: v["policy_sha"] for k, v in c.items() if isinstance(v, dict) and "policy_sha" in v}


# ------------------------------------------------------------------------------------------------------- CLI

def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Compile services/quality/writing_policy.yaml (AA-1047)")
    ap.add_argument("--write", action="store_true", help="write writing_policy.compiled.json")
    ap.add_argument("--check", action="store_true", help="exit 1 when the compiled file is stale")
    args = ap.parse_args(argv)
    text = render_compiled()
    if args.write:
        COMPILED_PATH.write_text(text, encoding="utf-8")
        print(f"wrote {COMPILED_PATH} (policy_sha {json.loads(text)['policy_sha']})")
        return 0
    if not COMPILED_PATH.exists() or COMPILED_PATH.read_text(encoding="utf-8") != text:
        print(f"stale: run python -m services.quality.writing_policy --write ({COMPILED_REL})")
        return 1
    print("fresh")
    return 0


if __name__ == "__main__":
    sys.exit(main())
