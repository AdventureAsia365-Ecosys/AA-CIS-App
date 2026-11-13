"""S279 thresholds lane L2 (AA-1096, CIS 412) — the quality settings index equals the CIS 412 seed, and every quality
key is evidence-gated and maker-checker (tech lead S279: quality thresholds are admin-editable WITH an evidence link).
No DB."""
from __future__ import annotations

import yaml

from scripts.settings import gen_compiled_defaults as gen
from services.quality import registry
from services.quality import settings_keys as sk


def _seed_409() -> dict[str, dict]:
    text = (gen.MIGRATIONS / "409_quality_settings_registry.sql").read_text(encoding="utf-8")
    import json

    rows = [r for block in gen.BLOCK_RE.findall(text) for r in json.loads(block)]
    return {r["key"]: r for r in rows}


def test_the_index_names_exactly_the_409_seed():
    assert set(sk.ALL_KEYS) == set(_seed_409())
    assert len(sk.ALL_KEYS) == len(set(sk.ALL_KEYS)) == 151


def test_every_quality_key_needs_evidence_and_a_second_person():
    for key, row in _seed_409().items():
        assert row["evidence_required"] is True, key
        assert row["approval"] == "maker_checker", key
        assert row["scope_kind"] == "platform" and row["enforced"] is True, key


def test_every_taxonomy_detector_has_its_mode_setting_seeded_with_todays_mode():
    seed = _seed_409()
    with open(registry.TAXONOMY_PATH, encoding="utf-8") as fh:
        tax = yaml.safe_load(fh)
    seen = set()
    for c in tax["classes"]:
        for d in c.get("detectors") or []:
            key = sk.DETECTOR_MODES[(c["id"], d["ref"])]
            seen.add(key)
            if "mode" in d:   # the YAML loses its modes in the code PR; until then they equal the seed
                assert seed[key]["default_value"] == d["mode"], key
    assert seen == set(sk.DETECTOR_MODES.values())


def test_prompt_keys_are_quality_keys():
    assert set(sk.PROMPT_KEYS) <= set(sk.ALL_KEYS)
