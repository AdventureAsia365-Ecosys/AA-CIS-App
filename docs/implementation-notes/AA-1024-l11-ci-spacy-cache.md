# AA-1024 lane l11 (S273): CI hotfix, cached spaCy model wheel

## Problem

`requirements.txt` pins `en_core_web_sm` 3.8.0 by URL (github.com release asset) and sha256. Every job that installs
`requirements.txt` downloaded it on its own: 3 unit shards, Unit Tests, 4 integration shards, Security Audit
(pip-audit resolves through pip), the Docker build, and the nightly run. On 08/11/2026 three CIS CI runs failed only on
that download (uv over HTTP/2: "refused stream ... request failed after 3 retries"), which ejected PRs from the merge
queue. setup-uv's cache did not help: it prunes downloaded wheels in CI (`uv cache prune --ci`) and keeps only
locally built ones.

## Decisions

- **One script, one composite action.** `.github/scripts/url_wheels.py` (stdlib) parses the `name @ https://...whl
  #sha256=` pins, fetches each into a directory with a sha256 check, and writes a rewritten requirements file whose pins
  point at `file://` copies (hash fragment kept). `.github/actions/url-wheels` wraps it with `actions/cache` keyed on a
  hash of the URL+sha256 pins only (`url_wheels.py key`), so a bump of any other package keeps the cache.
- **Same install, different source.** Jobs install from the action's `requirements` output. Every other line is
  byte-for-byte the same; the wheel is the same bytes (sha256 checked on every run, cache hit or not; pip also checks
  the kept fragment). A URL pin without a sha256 is refused, so the pin can never be relaxed.
- **Downloads on a miss use HTTP/1.1 (urllib) with retries:** 6 attempts, backoff 5/10/20/40/60 s + jitter, 60 s
  timeout each, a `::warning::` per retry; bytes that do not match the pin are discarded and count as a failed attempt.
- **Cache warming on main.** Caches saved on PR refs and merge-queue refs (`gh-readonly-queue/...`) are not readable by
  other refs, and CI no longer runs on push to main (S271). Deploy Dev (push to main) and the nightly integration run
  (schedule = default branch) both use the action, so the entry lives on main and every PR / queue run can restore it.
  A run that misses still downloads once per job, with retries, and saves its own ref-scoped copy.
- **One cache path everywhere** (`${{ runner.temp }}/url-wheels`): actions/cache hashes the path into the cache
  version, so differing paths would mean separate entries.
- **Docker:** the Dockerfile bind-mounts the script and the `.url-wheels/` seed directory (tracked with a README and a
  `.gitignore` for `*.whl`) and installs from the rewritten file. CI's Docker Build Check and Deploy Dev copy the
  verified wheel into the seed first (`copy-to: .url-wheels`), so the image build does not touch github.com; a local
  build with an empty seed downloads with the same retries and hash check. Bind mounts keep the script and the wheel
  out of the image layers.
- `actions/cache` pinned by SHA (v6.1.0, released 26/06/2026).
- `.url-wheels/` added to the CI scope "docker" area (a change there runs Docker Build Check on a PR).

## Changed

- `.github/workflows/ci.yml`: Security Audit, Unit shard i/3, Unit Tests, Integration shard i/4, Docker Build Check use
  the action; job names unchanged (required checks).
- `.github/workflows/nightly-integration.yml`, `.github/workflows/deploy-dev.yml`: same.
- `Dockerfile`: the install step (`RUN --mount=type=bind,...`).
- `scripts/pre-push.sh`: the printed CI install line for Unit Tests.
- `CONTEXT.md`: one paragraph in the CI section.
- Tests: `tests/unit/test_aa1024_url_wheels.py` (script behaviour with a fake network, CLI, and workflow / action /
  Dockerfile guards); `tests/unit/test_s271_ci_scope.py` (docker area list).

## Tradeoffs

- The installed dist-info's `direct_url.json` now records the local `file://` path instead of the github.com URL
  (the package files and version are identical; `nlp.NLP_VERSION` reads spaCy's model meta, not this). Accepted:
  provenance is still the pinned sha256.
- uv does not check a `#sha256=` fragment on a `file://` requirement (tested locally, uv 0.11.2); the script's own
  check before rewriting is what guarantees the bytes. pip does check it (Docker).
- A miss on a non-main ref still downloads once per job. Pre-fetching once per run in a dedicated job and passing an
  artifact would add a job to every run's critical path and another failure point; the main-scoped cache plus retries
  covers the failure seen.

## Should know

- Not tested locally: the Docker build itself (no Docker daemon on the lane machine). `RUN --mount=type=bind` needs
  BuildKit, which the `# syntax=docker/dockerfile:1` line and the runners' default `docker build` already use.
  Docker Build Check runs on this PR because it changes `.github/`.
- The first runs after merge miss the cache until Deploy Dev (this merge touches `Dockerfile`, so Deploy Dev runs) or
  the nightly run saves it on main.
