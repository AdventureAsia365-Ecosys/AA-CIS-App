"""S279 thresholds lane L2 (AA-1096, CIS 412) — the quality values on the settings registry: cross-key rules, the
admin UI's values and the fingerprint of the values in force.

The values themselves are read where they are used, at call time, through ``registry.threshold`` /
``registry.detector_mode`` / ``writing_policy.band`` / ``settings.get(settings_keys...)`` (the settings reader's 30 s
snapshot, never the database). This module adds what a single definition cannot say:

- ``cross_problem(key, value, get=None)``: the rules between keys (a cap stays under the pass line, a band's minimum
  under its maximum, a writer's aim inside the meta band, the blog length band inside the word band the t9_write prompt
  states, a Channel bar names known fields only) and the keys not editable yet (the SEO bands and meta aims, read at
  process start on the A1 / T2 path: ``START_TIME_REASON``). The settings API calls it before a proposal is stored
  (422, against the reader's snapshot) and again when a proposal is approved (409, ``get`` = the approved rows read
  inside the decision transaction, now and once every future-dated approval is in force), so two proposals that
  each pass alone cannot both be approved into a state the rules forbid.
- ``ui_values()``: the pass line, the score colour bands and the SEO bands the admin pages show (GET
  /admin/quality/thresholds), so the frontend keeps no copy (audit 5.4 check 3).
- ``settings_fp()`` / ``prompt_fp()``: '' while every quality value is its seeded default, else a short sha of the
  values in force. The writing-memory release fingerprint (component policy) and the prompt version on the ledger
  carry it, so a stored piece names the policy that produced it.

Standard library only.
"""
from __future__ import annotations

import hashlib
import json
import math
from typing import Any, Callable, Optional

from services.quality import settings_keys as K
from shared import settings

Q = K.QUALITY
# (key that must stay under, key it stays under): a cap or lower band under the pass line, the pass line under the top
_UNDER: tuple[tuple[str, str], ...] = (
    (Q["a1.missing_field_cap"], Q["pass_line"]),
    (Q["judge.mission_absent_cap"], Q["pass_line"]),
    (Q["review.score_band_low"], Q["pass_line"]),
    (Q["pass_line"], Q["ui.score_band_top"]),
)
_NOT_ABOVE: tuple[tuple[str, str], ...] = (
    (Q["brand_fit.repeat_band_low"], Q["brand_fit.repeat_band_high"]),
)
_LABEL = {Q["pass_line"]: "the pass line", Q["ui.score_band_top"]: "the top colour band"}


def _getter(key: str, value: Any, base: Optional[Callable[[str], Any]] = None) -> Callable[[str], Any]:
    """``base`` (default settings.get, the values in force), with the proposed value of ``key`` in its place."""
    get = base or settings.get
    return lambda k: value if k == key else get(k)


def _name(key: str) -> str:
    return _LABEL.get(key) or settings.definition(key).label


def _num(v: Any) -> float:
    return float(v)


def _under_problems(key: str, get: Callable[[str], Any]) -> Optional[str]:
    for lo, hi in _UNDER:
        if key in (lo, hi) and not _num(get(lo)) < _num(get(hi)):
            return f"{_name(lo)} ({get(lo)}) must stay under {_name(hi)} ({get(hi)})"
    for lo, hi in _NOT_ABOVE:
        if key in (lo, hi) and _num(get(lo)) > _num(get(hi)):
            return f"{_name(lo)} ({get(lo)}) must not be above {_name(hi)} ({get(hi)})"
    return None


def _band_problems(key: str, get: Callable[[str], Any]) -> Optional[str]:
    pairs = [(f"the {n} band minimum", K.SEO[f"{n}_min"], "its maximum", K.SEO[f"{n}_max"]) for n in ("meta", "title")]
    pairs.append(("the blog length floor", K.T9_BLOG_CHARS[0], "the ceiling", K.T9_BLOG_CHARS[1]))
    for lo_name, lo, hi_name, hi in pairs:
        if key in (lo, hi) and not _num(get(lo)) < _num(get(hi)):
            return f"{lo_name} ({get(lo)}) must stay under {hi_name} ({get(hi)})"
    return _blog_words_problem(key, get)


_BLOG_LENGTH_KEYS = frozenset(K.T9_BLOG_CHARS + (Q["t9.blog_length_target_margin"],))


def _blog_words_problem(key: str, get: Callable[[str], Any]) -> Optional[str]:
    """The blog length settings against the word band the t9_write prompt states (prompts._BLOG_WORDS_TOP /
    _BLOG_WORDS_MAX stay prompt text): the word floor rendered from the F4 floor plus the margin must stay at or under
    the band's top, and the F4 ceiling must leave room for the most words the prompt allows and for the aim itself.
    Same arithmetic as length_contract.target_chars() / target_words(), on the proposed values."""
    if key not in _BLOG_LENGTH_KEYS:
        return None
    from services.acp_content_writing import length_contract, prompts  # lazy: prompts imports half the package

    floor, ceiling = _num(get(K.T9_BLOG_CHARS[0])), _num(get(K.T9_BLOG_CHARS[1]))
    aim_chars = round(floor * (1 + _num(get(Q["t9.blog_length_target_margin"]))))
    aim_words = int(math.ceil(min(aim_chars, ceiling) / length_contract.CHARS_PER_WORD / 10.0) * 10)
    if aim_words > prompts._BLOG_WORDS_TOP:
        return (f"the blog length aim (floor {floor:,.0f} chars plus the margin = about {aim_words:,} words) must not "
                f"be above the top of the word band the writer prompt states ({prompts._BLOG_WORDS_TOP:,} words)")
    most_chars = math.ceil(prompts._BLOG_WORDS_MAX * length_contract.CHARS_PER_WORD)
    if ceiling < max(most_chars, aim_chars):
        return (f"the blog length ceiling ({ceiling:,.0f} chars) must leave room for the aim ({aim_chars:,} chars) and "
                f"for the most words the writer prompt allows ({prompts._BLOG_WORDS_MAX:,} words, about "
                f"{most_chars:,} chars)")
    return None


def _aim_problem(writer: str, amin: str, amax: str, get: Callable[[str], Any]) -> Optional[str]:
    meta_min, meta_max = K.SEO["meta_min"], K.SEO["meta_max"]
    if not _num(get(amin)) < _num(get(amax)):
        return f"the {writer} meta aim minimum ({get(amin)}) must stay under its maximum ({get(amax)})"
    if _num(get(amax)) > _num(get(meta_max)):
        return (f"the {writer} meta aim maximum ({get(amax)}) must not be above the meta band maximum "
                f"({get(meta_max)}): lower the aim first")
    # writing_policy.schema_errors: only a writer with a deterministic short fix may aim under the band minimum
    if writer not in _SHORT_FIX_WRITERS and _num(get(amin)) < _num(get(meta_min)):
        return (f"the {writer} meta aim minimum ({get(amin)}) must not be under the meta band minimum "
                f"({get(meta_min)}): this writer has no deterministic short fix")
    return None


def _order_problems(key: str, get: Callable[[str], Any]) -> Optional[str]:
    problem = _under_problems(key, get) or _band_problems(key, get)
    if problem:
        return problem
    for writer, (amin, amax) in K.WRITER_AIMS.items():
        if key in (amin, amax, K.SEO["meta_min"], K.SEO["meta_max"]):
            problem = problem or _aim_problem(writer, amin, amax, get)
    return problem


_SHORT_FIX_WRITERS = ("t9", "s1")   # writing_policy.yaml writer_aims.meta.<writer>.short_fix


def _channel_bar_problem(key: str, value: Any) -> Optional[str]:
    if key not in K.CHANNEL_BARS.values() or not isinstance(value, dict):
        return None
    unknown = sorted(set(value) - set(K.CHANNEL_BAR_FIELDS))
    if unknown:
        return f"unknown Channel bar field(s) {unknown}; allowed: {list(K.CHANNEL_BAR_FIELDS)}"
    missing = [f for f in K.CHANNEL_BAR_FIELDS[:3] if f not in value]
    if missing:
        return f"a Channel bar needs {missing}"
    for f, v in value.items():
        if isinstance(v, bool) or not isinstance(v, (int, float)) or float(v) != int(v):
            return f"{f} must be a whole number"
    channel = next(c for c, k in K.CHANNEL_BARS.items() if k == key)
    # an on-demand Channel (slate.CHANNEL_BARS on_demand) is seeded without a hub cap; the structure decides, not
    # the proposal
    on_demand = "most_per_hub" not in settings.definition(key).default_value
    if on_demand and "most_per_hub" in value:
        return f"{channel} is an on-demand Channel: it has no hub cap (leave most_per_hub out)"
    if not on_demand and "most_per_hub" not in value:
        return f"{channel} needs most_per_hub (99 = effectively off)"
    return None


# The SEO bands and the writers' meta aims are stated in prompt text and read by validators that are built when a
# process starts (the A1 / T2 writer prompts, content_generation.seo_meta_utils, the flag-fix and brand-audit prompts,
# the T9 SEO fit and envelope lines): a stored value would reach the T9 gates at once but those only after a rewrite
# of the A1 / T2 writer path to call-time rendering. Until then a change is refused (the seeded values stay in force
# everywhere, so gates and prompts never disagree); the keys are listed so the page shows them and their governance.
_START_TIME_KEYS = frozenset(list(K.SEO.values()) + [k for pair in K.WRITER_AIMS.values() for k in pair])
START_TIME_REASON = ("not editable yet: the A1 / T2 writer prompts and the meta fitters and validators read this value "
                     "when the process starts; a change would reach the T9 gates but not those prompts (follow-up: "
                     "render the SEO bands at call time on the A1 / T2 path). Today's value stays in force")


def governs(key: str) -> bool:
    """Is ``key`` a quality key with rules between keys (the settings API re-checks these on approval)?"""
    return key in _ALL


def cross_problem(key: str, value: Any, get: Optional[Callable[[str], Any]] = None) -> Optional[str]:
    """Why ``value`` for ``key`` breaks a rule between quality keys (None = fine). Only keys of settings_keys.
    ``get`` serves the OTHER keys' values (default: settings.get, the reader's snapshot); the approval passes the
    approved rows it read inside its transaction."""
    if key not in _ALL:
        return None
    if key in _START_TIME_KEYS and value != settings.definition(key).default_value:
        return START_TIME_REASON
    return _channel_bar_problem(key, value) or _order_problems(key, _getter(key, value, get))


_ALL = frozenset(K.ALL_KEYS)


def ui_values() -> dict:
    """What the admin pages show (GET /admin/quality/thresholds): values in force, no copy in the frontend."""
    from services.quality import registry, writing_policy

    meta, title = writing_policy.band("meta"), writing_policy.band("title")
    return {
        "pass_line": float(registry.threshold("review.approve_floor")),
        "score_bands": {"top": float(registry.threshold("ui.score_band_top")),
                        "pass": float(registry.threshold("review.approve_floor")),
                        "low": float(registry.threshold("review.score_band_low"))},
        "seo_bands": {"meta": {"min": meta[0], "max": meta[1]}, "title": {"min": title[0], "max": title[1]},
                      "slug": {"max": writing_policy.slug_max()}},
        "writing_policy_sha": writing_policy.policy_sha_in_force(),
        "settings_fp": settings_fp(),
    }


def _fp(keys: tuple[str, ...]) -> str:
    values = {k: settings.get(k) for k in keys}
    if all(v == settings.definition(k).default_value for k, v in values.items()):
        return ""
    return hashlib.sha256(json.dumps(values, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()[:8]


def settings_fp() -> str:
    """'' while every quality setting is its default, else 8 hex of the values in force (all quality keys)."""
    return _fp(K.ALL_KEYS)


def prompt_fp() -> str:
    """'' while every value a writer prompt renders is its default, else 8 hex of those values (PROMPT_KEYS)."""
    return _fp(K.PROMPT_KEYS)


__all__ = ["START_TIME_REASON", "cross_problem", "governs", "prompt_fp", "settings_fp", "ui_values"]
