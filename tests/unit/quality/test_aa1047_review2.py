"""AA-1047 (S272 adversarial review 2) — the counter-examples that are not synthetic policy twins: the prompt
registry's module-alias reads and the policy lint's new forms. The synthetic fire / no-fire twins live in
services/quality/writing_policy.yaml (`examples`) and run in test_aa1047_contradictions.py.

S272 (fail closed until W2): the review-2..4 tests of the T9 short-meta country tail, the number trip frame, F18's
trip-vs-place test and the admin check running F17 / F18 left with the code they tested (reverted to main's gates);
their sentences are kept, not asserted, in tests/unit/quality/fixtures/aa1047_w2_intent_examples.yaml.
"""
from __future__ import annotations

import json

import pytest

from services.quality import writing_policy as wp


def test_reads_policy_follows_a_module_alias(tmp_path):
    from shared.llm_client import prompt_registry as pr
    pkg = tmp_path / "pkg"
    pkg.mkdir()
    (pkg / "__init__.py").write_text("", encoding="utf-8")
    (pkg / "bands.py").write_text("from services.quality import writing_policy as _wp\n"
                                  "SEO_META_MIN, SEO_META_MAX = _wp.band('meta')\nPLAIN = 3\n", encoding="utf-8")
    (pkg / "alias_from.py").write_text("from pkg import bands as smu\n"
                                       "P = f'Write {smu.SEO_META_MIN} characters.'\n", encoding="utf-8")
    (pkg / "alias_import.py").write_text("import pkg.bands as smu2\n"
                                         "P = f'Write up to {smu2.SEO_META_MAX} characters.'\n", encoding="utf-8")
    (pkg / "alias_plain.py").write_text("from pkg import bands as b\nP = f'{b.PLAIN} lines'\n", encoding="utf-8")
    (pkg / "alias_in_helper.py").write_text("from pkg import bands as b\nP = 'x'\n"
                                            "def build():\n    return f'{b.SEO_META_MAX}'\n", encoding="utf-8")
    assert pr.reads_policy("pkg/alias_from.py", tmp_path)
    assert pr.reads_policy("pkg/alias_import.py", tmp_path)
    assert not pr.reads_policy("pkg/alias_plain.py", tmp_path)      # a constant that does not read the policy
    # a helper reading one counts for the stage that hashes that helper, not for the whole file
    assert not pr.reads_policy("pkg/alias_in_helper.py", tmp_path)
    assert pr.symbols_read_policy("pkg/alias_in_helper.py", ["build"], tmp_path)
    assert not pr.symbols_read_policy("pkg/alias_in_helper.py", ["P"], tmp_path)
    compiled = tmp_path / pr.POLICY_COMPILED
    compiled.parent.mkdir(parents=True)
    compiled.write_text(json.dumps({"policy_sha": "a" * 16}), encoding="utf-8")
    before = [pr.prompt_hash_of("pkg/alias_from.py", ["P"], root=tmp_path),
              pr.prompt_hash_of("pkg/alias_in_helper.py", ["build"], root=tmp_path)]
    compiled.write_text(json.dumps({"policy_sha": "b" * 16}), encoding="utf-8")
    after = [pr.prompt_hash_of("pkg/alias_from.py", ["P"], root=tmp_path),
             pr.prompt_hash_of("pkg/alias_in_helper.py", ["build"], root=tmp_path)]
    assert before[0] != after[0] and before[1] != after[1]


# ---------------------------------------------------------------- policy lint: the hand-restatement forms


def _compiled() -> dict:
    return json.loads(wp.render_compiled())


@pytest.mark.parametrize("src", [
    "meta_max = 155\n",
    "def trim(meta_description):\n    return meta_description[:155]\n",
    "def ok(description):\n    return 140 <= len(description) <= 155\n",
    'SEO_LIMITS = {"meta": (140, 155), "title": (45, 60)}\n',
    "def fit(meta, limit=155):\n    return meta\n",
    'x = "meta description up to {} characters".format(155)\n',
    # S272 review 5: the band passed as a call argument
    "import textwrap\ndef cut(meta):\n    return textwrap.shorten(meta, 155)\n",
    "def cut(page):\n    return shorten(page['seo_title'], width=60)\n",
    "x = clip_meta(text, 155)\n",
])
def test_lint_catches_python_band_restatements(src):
    from services.quality import policy_lint as pl
    assert pl.scan_python("services/x.py", src, _compiled())


@pytest.mark.parametrize("src", ["def fetch(timeout=60):\n    pass\n", "items = rows[:60]\n", "PAGE_SIZE = 60\n",
                                 "wait(url, 60)\n", "log.info('title', count=60)\n",
                                 "fetch(DATAFORSEO_BASE, timeout=60)\n", "get(f'{SEO_BASE}/x', timeout=60)\n"])
def test_lint_leaves_unrelated_numbers(src):
    from services.quality import policy_lint as pl
    assert pl.scan_python("services/x.py", src, _compiled()) == []


@pytest.mark.parametrize("src,hit", [("export const META_MAX = 155;", True),
                                     ('<textarea maxLength={155} name="meta" />', True),
                                     ("const BANDS = { meta: [140, 155] }", True),
                                     ("const PAGE = 60;", False)])
def test_lint_catches_ts_band_restatements(src, hit):
    from services.quality import policy_lint as pl
    assert bool(pl.scan_ts("frontend/a.ts", src, _compiled())) is hit


def test_lint_catches_yaml_min_max_under_a_field():
    from services.quality import policy_lint as pl
    found = pl.scan_yaml("services/x.yaml", "meta:\n  min: 140\n  max: 155\nother:\n  max: 155\n", _compiled())
    assert [f.split(":")[1] for f in found] == ["2", "3"]
