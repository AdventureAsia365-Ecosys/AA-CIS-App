"""AA-919 (WM-17, critic 34-36) — CI guard "no real golden-set / tenant text in this public repo". Standard library
only (it runs in the CI lint job).

Two checks over every tracked text file:
1. Markers (no key needed): a golden-set fixture's shape — the JSON key schema set to the value aa729-golden-set,
   or the three top-level JSON keys shared_inputs (an object), samples and label_history (arrays) in one file
   (MARKER_SCHEMA / MARKER_KEYS; this text names them without their JSON punctuation so it does not match itself).
2. Keyed fingerprints: .github/golden_text_fingerprints.txt holds 64-bit keyed BLAKE2b digests of normalised 8-word
   shingles of the real golden set (every item's text and the tenant brand packs; the platform s1:* pack is left
   out, it is AA's own prompt text). The scan shingles every tracked file the same way and fails on any digest in
   the file.
3. Short brand phrases (same key, same file): a tenant's own brand text shorter than a shingle (brand name, brand
   type, a short CTA or style line: 3 to 7 words) is fingerprinted whole, from the tenant brand packs and the T9
   brand_audience only. Generic lists (forbidden_words: AI-tell phrases AA's own lexicons also hold; voice
   adjectives) and every other short leaf (ids, gate names, SEO keywords, place names) are left out: they are not
   tenant text and would fail CI on ordinary code with no way to see why. The scan checks every 3- to 7-word window
   only when the file holds short digests. Pieces of 1-2 words are not covered (a known limit).

Why keyed (decision, AA-919): a plain or publicly salted hash list in a public repo is a membership oracle — anyone
holding a guessed sentence could confirm it is in the golden set (critic 36). With a secret key (the CI secret
GOLDEN_GUARD_KEY) the list says nothing without the key. The file starts with a key id (a keyed digest of a constant,
which reveals nothing about the key) so a scan with another key fails loudly instead of passing silently.
Fingerprints are regenerated for each new golden version, from the private copy:

    GOLDEN_GUARD_KEY=… python -m services.golden_set.text_guard fingerprints --fixture <path or s3:key> \\
        --exclude-tracked . --out .github/golden_text_fingerprints.txt
    GOLDEN_GUARD_KEY=… python -m services.golden_set.text_guard scan            # what CI runs (all tracked files)

Without the key the scan runs the markers only and says so; GOLDEN_GUARD_REQUIRE_KEY=true makes a missing key a
failure (CI on this repo's own branches). Findings print the path and counts only, never the matched text (CI logs of
a public repo are public).
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
import unicodedata
from pathlib import Path
from typing import Any, Iterable, Iterator, Optional

SHINGLE = 8
SHORT_MIN, SHORT_MAX = 3, SHINGLE - 1
SHORT_SKIP_KEYS = frozenset({"forbidden_words", "voice", "voice_examples", "rewrite_language", "system_prompt"})
_SHORT = "~"                  # digest domain of a whole short phrase (never inside a token, so never a shingle)
PER_LEAF = 3                  # fingerprints kept per text leaf at least (the smallest digests: a stable, key-dependent
DENSITY = 20                  # sample) and one more per DENSITY shingles: a pasted 40-word fragment is caught ~85%
DIGEST_BYTES = 8
FINGERPRINT_FILE = ".github/golden_text_fingerprints.txt"
MAX_SCAN_BYTES = 8_000_000
GUARD_ENV = "GOLDEN_GUARD_KEY"
REQUIRE_ENV = "GOLDEN_GUARD_REQUIRE_KEY"
_KID_PERSON = b"aa919-keyid"

MARKER_SCHEMA = re.compile(r'"schema"\s*:\s*"aa729-golden-set"')
MARKER_KEYS = (re.compile(r'"shared_inputs"\s*:\s*\{'), re.compile(r'"samples"\s*:\s*\['),
               re.compile(r'"label_history"\s*:\s*\['))
_UNICODE_ESC = re.compile(r"\\u([0-9a-fA-F]{4})")
_JSON_ESC = re.compile(r"\\[nrtbf\"/\\]")
_TOKEN = re.compile(r"[^\W_]+")


# ── normalisation ──────────────────────────────────────────────────────────────────────────────────────────────


def words(text: str) -> list[str]:
    """Lower-case alphanumeric tokens. JSON escapes are undone first, so a leak stored as escaped JSON
    ("don\\u2019t", "line\\nnext") shingles like the original text."""
    text = _UNICODE_ESC.sub(lambda m: chr(int(m.group(1), 16)), text)
    text = _JSON_ESC.sub(" ", text)
    text = unicodedata.normalize("NFKC", text).casefold()
    return _TOKEN.findall(text)


def shingles(tokens: list[str], n: int = SHINGLE) -> Iterator[str]:
    for i in range(len(tokens) - n + 1):
        yield " ".join(tokens[i:i + n])


def digest(key: bytes, shingle: str) -> str:
    return hashlib.blake2b(shingle.encode("utf-8"), key=key, digest_size=DIGEST_BYTES).hexdigest()


def key_id(key: bytes) -> str:
    return hashlib.blake2b(_KID_PERSON, key=key, digest_size=4).hexdigest()


def load_key(env: Optional[dict] = None) -> Optional[bytes]:
    raw = (env if env is not None else os.environ).get(GUARD_ENV, "").strip()
    return raw.encode("utf-8") if raw else None


# ── fingerprints of a fixture ──────────────────────────────────────────────────────────────────────────────────


def _leaves(obj: Any) -> Iterator[str]:
    if isinstance(obj, str):
        yield obj
    elif isinstance(obj, dict):
        for v in obj.values():
            yield from _leaves(v)
    elif isinstance(obj, list):
        for v in obj:
            yield from _leaves(v)


def text_sources(fixture: dict) -> Iterator[Any]:
    """What counts as golden text: every sample's input / output / label / verdict / injection spec, and the tenant
    brand packs (not the platform s1:* pack)."""
    for s in fixture.get("samples") or []:
        for k in ("input", "output", "label", "pipeline_verdict", "injection_spec"):
            if s.get(k) is not None:
                yield s[k]
    for ref, pack in ((fixture.get("shared_inputs") or {}).get("brand_rules") or {}).items():
        if not str(ref).startswith("s1:"):
            yield pack


def _brand_leaves(obj: Any) -> Iterator[str]:
    if isinstance(obj, str):
        yield obj
    elif isinstance(obj, dict):
        for k, v in obj.items():
            if k not in SHORT_SKIP_KEYS:
                yield from _brand_leaves(v)
    elif isinstance(obj, list):
        for v in obj:
            yield from _brand_leaves(v)


def short_phrases(fixture: dict) -> set[str]:
    """Whole normalised brand phrases of SHORT_MIN..SHORT_MAX words: tenant brand packs and the T9 brand_audience."""
    sources: list[Any] = [pack for ref, pack in ((fixture.get("shared_inputs") or {}).get("brand_rules") or {}).items()
                          if not str(ref).startswith("s1:")]
    for s in fixture.get("samples") or []:
        ctx = (s.get("input") or {}).get("context") if isinstance(s.get("input"), dict) else None
        if isinstance(ctx, dict) and ctx.get("brand_audience") is not None:
            sources.append(ctx["brand_audience"])
    out: set[str] = set()
    for source in sources:
        for leaf in _brand_leaves(source):
            tokens = words(leaf)
            if SHORT_MIN <= len(tokens) <= SHORT_MAX:
                out.add(" ".join(tokens))
    return out


def short_digest(key: bytes, phrase: str) -> str:
    return digest(key, _SHORT + phrase)


def windows(tokens: list[str]) -> Iterator[str]:
    for n in range(SHORT_MIN, SHORT_MAX + 1):
        yield from shingles(tokens, n)


def all_digests(fixture: dict, key: bytes) -> set[str]:
    return ({digest(key, sh) for source in text_sources(fixture) for leaf in _leaves(source)
             for sh in shingles(words(leaf))}
            | {short_digest(key, p) for p in short_phrases(fixture)})


def text_digests(text: str, key: bytes, short: bool) -> Iterator[str]:
    """The digests a text holds: every 8-word shingle and, when short, every 3- to 7-word window."""
    tokens = words(text)
    yield from (digest(key, sh) for sh in shingles(tokens))
    if short:
        yield from (short_digest(key, w) for w in windows(tokens))


def present_in(files: Iterable[Path], key: bytes, candidates: set[str]) -> set[str]:
    """The candidate digests some file already holds (text that was in the tree before the guard)."""
    found: set[str] = set()
    for path in files:
        text = _read_text(path)
        if text is None:
            continue
        found.update(d for d in text_digests(text, key, short=True) if d in candidates)
    return found


def fingerprints(fixture: dict, key: bytes, per_leaf: int = PER_LEAF, exclude: Iterable[str] = ()) -> set[str]:
    """Up to per_leaf digests per text leaf: the smallest ones that are not excluded (a stable, key-dependent
    sample; excluding first keeps a leaf covered when its smallest digests are common phrases)."""
    skip = set(exclude)
    out: set[str] = set()
    for source in text_sources(fixture):
        for leaf in _leaves(source):
            ds = sorted({digest(key, sh) for sh in shingles(words(leaf))} - skip)
            out.update(ds[:max(per_leaf, len(ds) // DENSITY)])
    return out | ({short_digest(key, p) for p in short_phrases(fixture)} - skip)       # short phrases: all of them


def short_count(fixture: dict, key: bytes, fps: set[str]) -> int:
    return len({short_digest(key, p) for p in short_phrases(fixture)} & fps)


def render(fps: Iterable[str], key: bytes, source_note: str, excluded: int = 0, short: int = 0) -> str:
    head = [
        "# AA-919 (WM-17) no-tenant-text guard: keyed BLAKE2b-64 digests of 8-word shingles of the golden set.",
        "# No text. Key: CI secret GOLDEN_GUARD_KEY. Regenerate: see services/golden_set/text_guard.py.",
        f"# kid: {key_id(key)}",
        f"# source: {source_note}",
        f"# shingle: {SHINGLE} words, per text leaf max({PER_LEAF}, shingles / {DENSITY})",
        f"# short: {short} whole brand phrases of {SHORT_MIN}-{SHORT_MAX} words",
        f"# excluded: {excluded} digests already in the tracked tree when generated (pre-existing text)",
    ]
    return "\n".join(head + sorted(fps)) + "\n"


def parse(text: str) -> tuple[Optional[str], set[str]]:
    kid, fps = None, set()
    for line in text.splitlines():
        line = line.strip()
        if line.startswith("# kid:"):
            kid = line.split(":", 1)[1].strip()
        elif line and not line.startswith("#"):
            if not re.fullmatch(r"[0-9a-f]{16}", line):
                raise ValueError("the fingerprint file holds something other than 16-hex digests")
            fps.add(line)
    return kid, fps


# ── the scan ───────────────────────────────────────────────────────────────────────────────────────────────────


def markers(text: str) -> list[str]:
    found = []
    if MARKER_SCHEMA.search(text):
        found.append("fixture schema marker")
    if all(m.search(text) for m in MARKER_KEYS):
        found.append("fixture top-level keys")
    return found


def has_short(text: str) -> bool:
    """The fingerprint file's header says it holds short-phrase digests (absent in files made before them)."""
    m = re.search(r"^# short: (\d+)", text, re.MULTILINE)
    return bool(m and int(m.group(1)))


def hits(text: str, key: bytes, fps: set[str], short: bool = False) -> int:
    return sum(1 for d in text_digests(text, key, short) if d in fps)


def tracked_files(root: Path) -> list[Path]:
    out = subprocess.run(["git", "ls-files", "-z"], cwd=root, check=True, capture_output=True).stdout
    return [root / p for p in out.decode("utf-8", "replace").split("\0") if p]


def _read_text(path: Path) -> Optional[str]:
    try:
        if not path.is_file() or path.stat().st_size > MAX_SCAN_BYTES:
            return None
        raw = path.read_bytes()
    except OSError:
        return None
    if b"\0" in raw[:8192]:
        return None                                   # binary
    return raw.decode("utf-8", "replace")


def scan(root: Path, files: Iterable[Path], key: Optional[bytes], fps: set[str],
         skip: Iterable[str] = (FINGERPRINT_FILE,), short: bool = False) -> list[dict]:
    skip_set = {str((root / s).resolve()) for s in skip}
    findings = []
    for path in files:
        if str(path.resolve()) in skip_set:
            continue
        text = _read_text(path)
        if text is None:
            continue
        try:
            rel = str(path.resolve().relative_to(root))
        except ValueError:
            rel = str(path)
        for m in markers(text):
            findings.append({"path": rel, "kind": m})
        if key and fps:
            n = hits(text, key, fps, short)
            if n:
                findings.append({"path": rel, "kind": f"{n} golden-text fingerprint hit(s)"})
    return findings


def run_scan(root: Path, env: Optional[dict] = None, files: Optional[list[Path]] = None) -> tuple[int, list[str]]:
    """(exit code, report lines). Report lines never hold matched text."""
    env = env if env is not None else dict(os.environ)
    key = load_key(env)
    require = env.get(REQUIRE_ENV, "").strip().lower() in ("1", "true", "yes")
    lines: list[str] = []
    fp_path = root / FINGERPRINT_FILE
    kid, fps, short = (None, set(), False)
    if fp_path.exists():
        fp_text = fp_path.read_text(encoding="utf-8")
        kid, fps = parse(fp_text)
        short = has_short(fp_text)
    code = 0
    if key is None:
        lines.append(f"::warning::{GUARD_ENV} is not set: markers only, the fingerprint check did not run")
        if require:
            lines.append(f"::error::{GUARD_ENV} is required here ({REQUIRE_ENV})")
            code = 1
    elif not fps:
        lines.append(f"::warning::{FINGERPRINT_FILE} is missing or empty: markers only")
        if require:
            code = 1
    elif kid != key_id(key):
        lines.append(f"::error::{FINGERPRINT_FILE} was made with another key (kid {kid}, ours {key_id(key)})")
        code = 1
        key = None
    findings = scan(root, files if files is not None else tracked_files(root), key, fps, short=short)
    for f in findings:
        lines.append(f"::error file={f['path']}::golden-set text guard: {f['kind']} (AA-919; fake fixtures only)")
    if findings:
        code = 1
    lines.append(f"golden-set text guard: {len(findings)} finding(s), {len(fps)} fingerprints, "
                 f"key {'set' if load_key(env) else 'absent'}")
    return code, lines


def _load_fixture(ref: str) -> tuple[dict, str]:
    if ref.startswith("s3:"):
        import boto3

        from shared.aws_client.bronze import bronze_bucket

        key = ref[3:].lstrip("/")
        raw = boto3.client("s3", region_name=os.environ.get("AWS_REGION", "us-west-1")).get_object(
            Bucket=bronze_bucket(), Key=key)["Body"].read()
    else:
        raw = Path(ref).read_bytes()
    fixture = json.loads(raw)
    note = (f"{fixture.get('schema')} {fixture.get('fixture_version')} sha256 {hashlib.sha256(raw).hexdigest()}")
    return fixture, note


def main(argv: Optional[list[str]] = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m services.golden_set.text_guard")
    sub = ap.add_subparsers(dest="cmd", required=True)
    g = sub.add_parser("fingerprints", help="write the fingerprint file from a fixture (needs the key)")
    g.add_argument("--fixture", required=True, help="local path or s3:<key>")
    g.add_argument("--out", default=FINGERPRINT_FILE)
    g.add_argument("--exclude-tracked", metavar="ROOT",
                   help="leave out digests the tracked files of this repo already hold (text public before the guard)")
    s = sub.add_parser("scan", help="scan the tracked files (CI)")
    s.add_argument("--root", default=".")
    s.add_argument("files", nargs="*")
    a = ap.parse_args(argv)
    if a.cmd == "fingerprints":
        key = load_key()
        if key is None:
            ap.error(f"{GUARD_ENV} is required")
        fixture, note = _load_fixture(a.fixture)
        skip: set[str] = set()
        if a.exclude_tracked:
            root = Path(a.exclude_tracked).resolve()
            out_path = Path(a.out).resolve()
            skip = present_in((f for f in tracked_files(root) if f.resolve() != out_path), key,
                              all_digests(fixture, key))
        fps = fingerprints(fixture, key, exclude=skip)
        Path(a.out).write_text(render(fps, key, note, excluded=len(skip), short=short_count(fixture, key, fps)),
                               encoding="utf-8")
        print(f"{len(fps)} fingerprints -> {a.out} ({len(skip)} digests already in the tree left out)")
        return 0
    root = Path(a.root).resolve()
    code, lines = run_scan(root, files=[Path(f).resolve() for f in a.files] or None)
    print("\n".join(lines))
    return code


if __name__ == "__main__":
    sys.exit(main())
