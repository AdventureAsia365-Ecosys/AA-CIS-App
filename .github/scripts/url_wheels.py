#!/usr/bin/env python3
"""URL-pinned wheels from a local, hash-checked copy (CI hotfix, S273).

requirements.txt pins one package by URL and sha256 (the spaCy model wheel, AA-789):

    en_core_web_sm @ https://github.com/explosion/spacy-models/releases/download/...whl#sha256=<64 hex>

Every CI job that installed requirements.txt downloaded that wheel from github.com on its own, and on 08/11/2026 three
CIS CI runs failed only on that download (uv: "http2 refused stream ... request failed after 3 retries"), which ejected
PRs from the merge queue. This script keeps what gets installed the same (the same wheel, checked against the same
pinned sha256) and changes only where the bytes come from:

  key    prints `key=<hash of the URL+sha256 pins>` for `$GITHUB_OUTPUT`: the cache key moves only when a pin moves,
         not on every requirements.txt change.
  fetch  puts each pinned wheel in --dest, checked against its sha256: a verified copy already there (a cache hit) is
         kept; else a verified copy in --seed (the Docker build's bind mount) is copied; else it is downloaded, over
         HTTP/1.1, with retries and backoff, and checked. A wheel whose sha256 never matches fails the step: the pin is
         never relaxed. --out writes a copy of the requirements file whose URL pins point at the local files
         (`name @ file:///...#sha256=...`), every other line as is. --copy-to also copies the verified wheels there (the
         Docker build context's .url-wheels/ seed directory).

Used by .github/actions/url-wheels (cached per pin, actions/cache) and the Dockerfile. Stdlib only.
Run: python3 .github/scripts/url_wheels.py fetch --req requirements.txt --dest DIR [--seed DIR] [--copy-to DIR]
       [--out FILE] [--attempts N] [--base-delay S]
Exit 0 = every wheel verified, 1 = a wheel could not be fetched or verified, 2 = a bad requirements file.
"""
from __future__ import annotations

import argparse
import hashlib
import os
import random
import re
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import unquote, urlsplit

PIN = re.compile(
    r"^\s*(?P<name>[A-Za-z0-9][A-Za-z0-9._-]*)(?P<extras>\[[^\]]*\])?\s*@\s*"
    r"(?P<url>https?://[^\s#;]+)(?P<frag>#\S*)?(?P<rest>.*)$"
)
SHA_FRAG = re.compile(r"^#sha256=(?P<sha>[0-9a-f]{64})$")
INCLUDE = re.compile(r"^(?P<flag>-r|-c|--requirement|--constraint)(?P<sep>\s+|=)(?P<path>\S+)(?P<rest>.*)$")
CHUNK = 1 << 16
USER_AGENT = "aa-cis-ci-url-wheels/1"


class RequirementsError(ValueError):
    """The requirements file has a URL pin this script cannot keep pinned."""


class FetchError(RuntimeError):
    """A wheel could not be fetched with the pinned sha256."""


@dataclass(frozen=True)
class Pin:
    name: str
    url: str
    sha256: str

    @property
    def filename(self) -> str:
        return os.path.basename(unquote(urlsplit(self.url).path))


def parse_pins(text: str) -> list[Pin]:
    """The `name @ https://...#sha256=` lines. A URL pin without a sha256 is refused (nothing to check it against)."""
    pins = []
    for lineno, line in enumerate(text.splitlines(), 1):
        if line.lstrip().startswith("#"):
            continue
        m = PIN.match(line)
        if not m:
            continue
        frag = SHA_FRAG.match(m.group("frag") or "")
        if not frag:
            raise RequirementsError(f"line {lineno}: URL requirement {m.group('name')} has no #sha256=<64 hex> pin")
        pin = Pin(m.group("name"), m.group("url"), frag.group("sha"))
        if not pin.filename.endswith(".whl"):
            raise RequirementsError(f"line {lineno}: URL requirement {pin.name} is not a wheel ({pin.filename})")
        pins.append(pin)
    return pins


def cache_key(pins: list[Pin]) -> str:
    if not pins:
        return "none"
    body = "\n".join(sorted(f"{p.url}#sha256={p.sha256}" for p in pins))
    return hashlib.sha256(body.encode()).hexdigest()[:32]


def sha256_of(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for block in iter(lambda: fh.read(CHUNK), b""):
            h.update(block)
    return h.hexdigest()


def verified(path: Path, sha: str) -> bool:
    return path.is_file() and sha256_of(path) == sha


_GH_RELEASE = re.compile(r"^https://github\.com/([^/]+)/([^/]+)/releases/download/([^/]+)/([^/?#]+)$")


def _gh_release_download(url: str, dest: Path, timeout: float, run=subprocess.run) -> bool:
    """Authenticated download of a public GitHub release asset through `gh` (S273: GitHub throttled
    unauthenticated release downloads, 'Increase in timeouts for some unauthenticated users').
    True when gh fetched the file; False when this path does not apply (no gh, no token, not a release URL)."""
    m = _GH_RELEASE.match(url)
    if not m or not shutil.which("gh") or not (os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN")):
        return False
    owner, repo, tag, name = m.groups()
    work = Path(tempfile.mkdtemp(dir=dest.parent, prefix=".gh-"))
    try:
        run(["gh", "release", "download", tag, "--repo", f"{owner}/{repo}", "--pattern", name, "--dir", str(work)],
            check=True, timeout=timeout, capture_output=True)
        got = work / name
        os.chmod(got, 0o644)
        os.replace(got, dest)
        return True
    finally:
        shutil.rmtree(work, ignore_errors=True)


def _download(url: str, dest: Path, timeout: float) -> None:
    try:
        if _gh_release_download(url, dest, timeout):
            return
    except Exception as exc:  # noqa: BLE001 - fall back to the plain HTTP download below
        print(f"::warning::authenticated gh download failed ({type(exc).__name__}); trying plain HTTPS")
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    fd, tmp = tempfile.mkstemp(dir=dest.parent, prefix=".part-")
    try:
        with os.fdopen(fd, "wb") as out, urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310 (https pin)
            shutil.copyfileobj(resp, out, CHUNK)
        os.chmod(tmp, 0o644)  # mkstemp makes 0600; the Docker build reads the seed as another user
        os.replace(tmp, dest)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def download_verified(pin: Pin, dest: Path, *, attempts: int = 6, base_delay: float = 5.0, timeout: float = 60.0,
                      download=_download, sleep=time.sleep, log=print) -> None:
    """Download `pin` to `dest` until its sha256 matches; backoff base_delay * 2**n (max 60 s) plus jitter."""
    last = ""
    for n in range(1, attempts + 1):
        try:
            download(pin.url, dest, timeout)
            got = sha256_of(dest)
            if got == pin.sha256:
                return
            dest.unlink(missing_ok=True)
            last = f"sha256 {got} does not match the pin {pin.sha256}"
        except Exception as exc:  # network errors of any kind: retry
            last = f"{type(exc).__name__}: {exc}"[:300]
        if n < attempts:
            delay = min(60.0, base_delay * 2 ** (n - 1)) + random.uniform(0, 1)
            log(f"::warning::{pin.filename}: attempt {n}/{attempts} failed ({last}); retrying in {delay:.0f} s")
            sleep(delay)
    raise FetchError(f"{pin.filename}: no verified copy after {attempts} attempts (last: {last})")


def fetch(pins: list[Pin], dest: Path, *, seed: Path | None = None, copy_to: Path | None = None,
          log=print, **download_kw) -> dict[str, Path]:
    """Each pin as a verified wheel in `dest`; returns {pin.url: local path}."""
    dest.mkdir(parents=True, exist_ok=True)
    local = {}
    for pin in pins:
        target = dest / pin.filename
        if verified(target, pin.sha256):
            log(f"{pin.filename}: verified local copy (cache hit)")
        elif seed is not None and verified(seed / pin.filename, pin.sha256):
            shutil.copyfile(seed / pin.filename, target)
            log(f"{pin.filename}: verified copy from {seed}")
        else:
            target.unlink(missing_ok=True)
            log(f"{pin.filename}: downloading {pin.url}")
            download_verified(pin, target, log=log, **download_kw)
            log(f"{pin.filename}: downloaded, sha256 verified")
        if copy_to is not None:
            copy_to.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(target, copy_to / pin.filename)
        local[pin.url] = target.resolve()
    return local


def rewrite(text: str, local: dict[str, Path], base_dir: Path) -> str:
    """`text` with each URL pin pointing at its local wheel (hash kept) and relative -r/-c includes made absolute."""
    out = []
    for line in text.splitlines():
        m = PIN.match(line) if not line.lstrip().startswith("#") else None
        if m and m.group("url") in local:
            uri = local[m.group("url")].as_uri()
            line = f"{m.group('name')}{m.group('extras') or ''} @ {uri}{m.group('frag')}{m.group('rest')}"
        else:
            inc = INCLUDE.match(line.strip())
            if inc and not os.path.isabs(inc.group("path")):
                path = (base_dir / inc.group("path")).resolve()
                line = f"{inc.group('flag')}{inc.group('sep')}{path}{inc.group('rest')}"
        out.append(line)
    return "\n".join(out) + "\n"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    k = sub.add_parser("key")
    k.add_argument("--req", required=True, type=Path)
    f = sub.add_parser("fetch")
    f.add_argument("--req", required=True, type=Path)
    f.add_argument("--dest", required=True, type=Path)
    f.add_argument("--seed", type=Path)
    f.add_argument("--copy-to", type=Path)
    f.add_argument("--out", type=Path)
    f.add_argument("--attempts", type=int, default=6)
    f.add_argument("--base-delay", type=float, default=5.0)
    args = ap.parse_args(argv)

    try:
        text = args.req.read_text()
        pins = parse_pins(text)
    except (OSError, RequirementsError) as exc:
        print(f"::error::{args.req}: {exc}", file=sys.stderr)
        return 2
    if args.cmd == "key":
        print(f"key={cache_key(pins)}")
        return 0
    try:
        local = fetch(pins, args.dest, seed=args.seed, copy_to=args.copy_to,
                      attempts=max(1, args.attempts), base_delay=args.base_delay)
    except (FetchError, OSError) as exc:
        print(f"::error::{exc}", file=sys.stderr)
        return 1
    if args.out is not None:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(rewrite(text, local, args.req.resolve().parent))
        print(f"wrote {args.out} ({len(local)} URL pin(s) served from {args.dest})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
