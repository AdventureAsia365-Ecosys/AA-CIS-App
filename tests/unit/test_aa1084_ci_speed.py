"""AA-1084 (CI speed) — the speed-ups keep their results.

- prompt_registry: a file's module names are parsed once per text (memoised on the text, not the path or mtime), so an
  edited file is parsed again and a stage hash is the same with a cold and a warm cache.
- policy_lint: a comparison without a band literal is skipped before its source text is cut out (the p2 finding of a
  real band comparison still fires; the full fire / no-fire set is in tests/unit/quality/test_aa1047_review2.py).
"""
from __future__ import annotations

import json

from shared.llm_client import prompt_registry as pr


def test_module_names_are_parsed_once_per_text_and_again_after_an_edit(tmp_path):
    src = tmp_path / "mod.py"
    src.write_text("A = 1\nfrom services.quality import writing_policy as _wp\n", encoding="utf-8")
    pr._parse_module_names.cache_clear()
    first = pr._module_names(src, tmp_path)
    again = pr._module_names(src, tmp_path)
    assert first is again and pr._parse_module_names.cache_info().hits == 1
    assert "A" in first[0] and first[1] == {"_wp"}
    # Same path, same size, new text: parsed again (the cache key is the text itself).
    src.write_text("B = 1\nfrom services.quality import writing_policy as _xx\n", encoding="utf-8")
    edited = pr._module_names(src, tmp_path)
    assert "B" in edited[0] and "A" not in edited[0] and edited[1] == {"_xx"}


def test_a_stage_hash_is_the_same_with_a_cold_and_a_warm_cache():
    manifest = pr.load_manifest()
    stage = next(s for s, entries in manifest.items() if entries)
    entry = pr.newest(manifest, stage)
    pr._parse_module_names.cache_clear()
    cold = pr.prompt_hash_of(entry["source_path"], entry["symbols"])
    warm = pr.prompt_hash_of(entry["source_path"], entry["symbols"])
    assert cold == warm


def test_policy_lint_still_flags_a_band_comparison_and_skips_a_plain_one():
    from services.quality import policy_lint as pl
    from services.quality import writing_policy as wp
    compiled = json.loads(wp.render_compiled())
    assert any("(p2)" in f for f in pl.scan_python("services/x.py",
                                                   "def ok(description):\n    return len(description) <= 155\n",
                                                   compiled))
    assert pl.scan_python("services/x.py", "def ok(rows):\n    return len(rows) <= limit\n", compiled) == []
