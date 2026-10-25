"""AA-919 (WM-17, critic 34-36) — the no-golden-text CI guard (services/golden_set/text_guard.py), with an INVENTED
fixture and a test key. The real scan of this repo runs in CI (ci.yml, "Golden-set text guard") with the secret key.

Covers: normalisation (JSON escapes), keyed fingerprints catch a pasted fragment and nothing else, a key change
changes every digest (no public oracle), the fixture-shape markers (and this repo's guard sources do not trip them),
short brand phrases (3-7 words) fingerprinted whole, generic lists left out; the key rules of run_scan (absent /
required / another key), findings never print text, and the committed fingerprint file holds digests only.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from services.golden_set import text_guard as tg
from tests.unit.aa919_fake_golden import fake_fixture

ROOT = Path(__file__).resolve().parents[2]
TEST_KEY = ("z" * 32).encode()
OTHER_KEY = ("y" * 32).encode()
FRAGMENT = "Pretend travellers walk to a made-up waterfall and drink invented tea together."


def test_words_undo_json_escapes():
    assert tg.words('don\\u2019t walk\\nthere') == tg.words("don’t walk there")
    assert tg.words("Ünïcode, MIXED case!") == ["ünïcode", "mixed", "case"]
    assert list(tg.shingles(["a"] * 7)) == []
    assert len(list(tg.shingles(list("abcdefghij")))) == 3


def test_fingerprints_catch_a_fragment_and_only_it():
    fps = tg.fingerprints(fake_fixture(), TEST_KEY)
    assert fps and all(len(d) == 16 for d in fps)
    assert tg.hits(f"intro words here then {FRAGMENT} and more", TEST_KEY, fps) >= 1
    assert tg.hits(json.dumps({"x": FRAGMENT}), TEST_KEY, fps) >= 1                    # stored as JSON too
    assert tg.hits("A completely different sentence about real-looking things that nobody wrote before.",
                   TEST_KEY, fps) == 0
    # another key gives other digests: the file is no oracle without the key
    assert not fps & tg.fingerprints(fake_fixture(), OTHER_KEY)
    assert tg.key_id(TEST_KEY) != tg.key_id(OTHER_KEY)


def test_platform_pack_is_not_fingerprinted_tenant_pack_is():
    fx = fake_fixture()
    fps = tg.fingerprints(fx, TEST_KEY)
    assert tg.hits("Invented platform voice for tests.", TEST_KEY, fps) == 0           # fewer than 8 words anyway
    tenant = "Invented tenant style: short pretend sentences about made-up places."
    assert tg.hits(tenant, TEST_KEY, fps) >= 1


def test_short_brand_phrases_are_fingerprinted_whole():
    fx = fake_fixture()
    phrases = tg.short_phrases(fx)
    assert "ask the pretend agency" in phrases                          # tenant pack default_cta (4 words)
    assert "invented pretend trekking outfit" in phrases                # T9 brand_audience.brand_type
    assert "nestled among the pretend hills" not in phrases             # forbidden_words: generic, left out
    assert "invented platform voice for tests" not in phrases           # the platform s1:* pack, left out
    assert all(tg.SHORT_MIN <= len(p.split()) <= tg.SHORT_MAX for p in phrases)
    fps = tg.fingerprints(fx, TEST_KEY)
    assert tg.short_count(fx, TEST_KEY, fps) == len(phrases)
    planted = "CTA_TEXT = 'Ask the pretend agency.'  # copied from a brand pack"
    assert tg.hits(planted, TEST_KEY, fps, short=True) == 1
    assert tg.hits(planted, TEST_KEY, fps, short=False) == 0             # under 8 words: shingles miss it
    assert tg.hits("we never ask the agency anything pretend", TEST_KEY, fps, short=True) == 0
    assert tg.hits("nestled among the pretend hills", TEST_KEY, fps, short=True) == 0


def test_short_phrases_already_in_the_tree_are_excluded():
    fx = fake_fixture()
    cta = tg.short_digest(TEST_KEY, "ask the pretend agency")
    assert cta in tg.all_digests(fx, TEST_KEY)
    assert cta not in tg.fingerprints(fx, TEST_KEY, exclude={cta})


def test_exclusion_keeps_leaves_covered():
    fx = fake_fixture()
    every = tg.all_digests(fx, TEST_KEY)
    some = set(sorted(every)[:50])
    fps = tg.fingerprints(fx, TEST_KEY, exclude=some)
    assert not fps & some and fps


def test_markers_find_a_fixture_and_not_the_guard_sources():
    fixture_json = json.dumps(fake_fixture(), indent=1)
    assert tg.markers(fixture_json) == ["fixture schema marker", "fixture top-level keys"]
    for path in ("services/golden_set/text_guard.py", "services/golden_set/derive.py",
                 "services/golden_set/importer.py", "tests/unit/aa919_fake_golden.py",
                 "docs/implementation-notes/AA-919.md", ".github/workflows/ci.yml"):
        assert tg.markers((ROOT / path).read_text(encoding="utf-8")) == [], path


def _repo(tmp_path, key=TEST_KEY, leak=False):
    (tmp_path / ".github").mkdir()
    fps = tg.fingerprints(fake_fixture(), key)
    (tmp_path / tg.FINGERPRINT_FILE).write_text(
        tg.render(fps, key, "fake 1.0.0", short=tg.short_count(fake_fixture(), key, fps)), encoding="utf-8")
    clean = tmp_path / "clean.py"
    clean.write_text("print('nothing golden here at all, just code and words')\n", encoding="utf-8")
    files = [clean, tmp_path / tg.FINGERPRINT_FILE]
    if leak:
        bad = tmp_path / "leak.md"
        bad.write_text(f"# notes\n{FRAGMENT}\n", encoding="utf-8")
        files.append(bad)
    return files


def test_scan_reports_paths_and_counts_never_text(tmp_path):
    files = _repo(tmp_path, leak=True)
    code, lines = tg.run_scan(tmp_path, env={tg.GUARD_ENV: TEST_KEY.decode()}, files=files)
    assert code == 1
    assert any("leak.md" in line and "fingerprint hit" in line for line in lines)
    assert not any("waterfall" in line or "Pretend" in line for line in lines)
    code, _ = tg.run_scan(tmp_path, env={tg.GUARD_ENV: TEST_KEY.decode()}, files=files[:2])
    assert code == 0                                                     # the fingerprint file itself is skipped


def test_scan_catches_a_short_brand_phrase_when_the_file_holds_short_digests(tmp_path):
    files = _repo(tmp_path)
    short = tmp_path / "cta.py"
    short.write_text("LINES = ['Invented pretend trekking outfit']\n", encoding="utf-8")
    code, lines = tg.run_scan(tmp_path, env={tg.GUARD_ENV: TEST_KEY.decode()}, files=files + [short])
    assert code == 1 and any("cta.py" in line and "1 golden-text fingerprint hit" in line for line in lines)
    assert not any("trekking" in line for line in lines)
    # a fingerprint file made before short phrases (no "# short:" header) scans shingles only
    fp = tmp_path / tg.FINGERPRINT_FILE
    fp.write_text("\n".join(x for x in fp.read_text().splitlines() if not x.startswith("# short:")) + "\n")
    code, _ = tg.run_scan(tmp_path, env={tg.GUARD_ENV: TEST_KEY.decode()}, files=files + [short])
    assert code == 0


def test_scan_key_rules(tmp_path):
    files = _repo(tmp_path)
    code, lines = tg.run_scan(tmp_path, env={}, files=files)
    assert code == 0 and "markers only" in lines[0]                      # fork / Dependabot: markers only
    code, lines = tg.run_scan(tmp_path, env={tg.REQUIRE_ENV: "true"}, files=files)
    assert code == 1 and any("is required" in line for line in lines)
    code, lines = tg.run_scan(tmp_path, env={tg.GUARD_ENV: OTHER_KEY.decode()}, files=files)
    assert code == 1 and any("another key" in line for line in lines)


def test_markers_fail_without_a_key(tmp_path):
    files = _repo(tmp_path)
    leaked = tmp_path / "fixture.json"
    leaked.write_text(json.dumps(fake_fixture()), encoding="utf-8")
    code, lines = tg.run_scan(tmp_path, env={}, files=files + [leaked])
    assert code == 1 and sum("fixture.json" in line for line in lines) == 2


def test_parse_refuses_text_in_the_fingerprint_file():
    with pytest.raises(ValueError, match="other than 16-hex"):
        tg.parse("# kid: 00000000\n0123456789abcdef\nsome words that are not a digest\n")


def test_the_committed_fingerprint_file_holds_digests_only():
    text = (ROOT / tg.FINGERPRINT_FILE).read_text(encoding="utf-8")
    kid, fps = tg.parse(text)
    assert kid and len(kid) == 8 and len(fps) > 1000
    body = [line for line in text.splitlines() if line and not line.startswith("#")]
    assert len(body) == len(fps)                                         # one digest per line, nothing else
