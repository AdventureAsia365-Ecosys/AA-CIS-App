"""S273 CI hotfix (AA-1024 lessons guard): URL-pinned wheels come from a cached, sha256-checked copy.

Three CIS CI runs failed on 08/11/2026 only because every shard downloaded the spaCy model wheel (a URL pin in
requirements.txt) from github.com and the download was refused. These tests hold the fix in place:

- .github/scripts/url_wheels.py: parses the URL pins (a URL pin without a sha256 is refused), keys the cache on the
  pins only, keeps a verified copy, takes a verified seed, downloads with retries and backoff on a miss, never accepts
  bytes that do not match the pin, and rewrites the requirements file to the local copies without touching other lines.
- The workflows: no job installs or audits requirements.txt straight from its URL pins; every job that does goes through
  .github/actions/url-wheels first; every `docker build` gets the verified wheel in the .url-wheels/ seed.
- The Dockerfile installs through the script with the seed bind-mounted, and the action pins actions/cache by SHA.
"""
from __future__ import annotations

import hashlib
import importlib.util
import re
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

REPO = Path(__file__).resolve().parents[2]
SCRIPT = REPO / ".github" / "scripts" / "url_wheels.py"
ACTION = REPO / ".github" / "actions" / "url-wheels" / "action.yml"
WORKFLOWS = REPO / ".github" / "workflows"
DOCKERFILE = REPO / "Dockerfile"
SEED = REPO / ".url-wheels"
ACTION_USES = "./.github/actions/url-wheels"

_spec = importlib.util.spec_from_file_location("url_wheels", SCRIPT)
uw = importlib.util.module_from_spec(_spec)
sys.modules["url_wheels"] = uw
_spec.loader.exec_module(uw)

WHEEL = b"invented wheel bytes for a test, not a real package"
SHA = hashlib.sha256(WHEEL).hexdigest()
URL = "https://downloads.example.test/demo_model-1.0.0/demo_model-1.0.0-py3-none-any.whl"
REQS = f"""# invented requirements
alpha==1.2.3
demo_model @ {URL}#sha256={SHA}  # pinned model
-r extra.txt
beta>=2
"""


def _pin() -> "uw.Pin":
    return uw.Pin("demo_model", URL, SHA)


class FakeNet:
    """download() stand-in: plays a script of outcomes (an exception or the bytes to write)."""

    def __init__(self, *outcomes):
        self.outcomes = list(outcomes)
        self.calls = 0

    def __call__(self, url, dest, timeout):
        self.calls += 1
        out = self.outcomes.pop(0)
        if isinstance(out, Exception):
            raise out
        Path(dest).write_bytes(out)


# --- parsing and the cache key ----------------------------------------------------------------------------------------

def test_the_real_requirements_pin_one_model_wheel_by_sha256():
    pins = uw.parse_pins((REPO / "requirements.txt").read_text())
    assert [p.name for p in pins] == ["en_core_web_sm"]
    assert re.fullmatch(r"[0-9a-f]{64}", pins[0].sha256)
    assert pins[0].filename.endswith("-py3-none-any.whl")
    assert pins[0].url.startswith("https://github.com/explosion/spacy-models/releases/download/")


def test_parse_reads_the_pin_and_skips_comments_and_plain_lines():
    text = REQS + f"# commented @ {URL}#sha256={SHA}\n"
    assert uw.parse_pins(text) == [_pin()]


@pytest.mark.parametrize("line", [f"demo_model @ {URL}", f"demo_model @ {URL}#md5=abc",
                                  f"demo_model @ {URL}#sha256=XYZ"])
def test_a_url_pin_without_a_sha256_is_refused(line):
    with pytest.raises(uw.RequirementsError, match="no #sha256"):
        uw.parse_pins(line + "\n")


def test_a_url_pin_that_is_not_a_wheel_is_refused():
    with pytest.raises(uw.RequirementsError, match="not a wheel"):
        uw.parse_pins(f"demo_model @ https://downloads.example.test/demo.tar.gz#sha256={SHA}\n")


def test_the_cache_key_moves_only_with_a_pin():
    base = uw.cache_key(uw.parse_pins(REQS))
    assert base == uw.cache_key(uw.parse_pins(REQS.replace("alpha==1.2.3", "alpha==9.9.9")))
    other = uw.cache_key(uw.parse_pins(REQS.replace(SHA, "0" * 64)))
    assert other != base
    assert uw.cache_key([]) == "none"


# --- fetch ------------------------------------------------------------------------------------------------------------

def test_a_verified_copy_is_kept_without_a_download(tmp_path):
    (tmp_path / _pin().filename).write_bytes(WHEEL)
    net = FakeNet()
    local = uw.fetch([_pin()], tmp_path, download=net, sleep=lambda s: None, log=lambda m: None)
    assert net.calls == 0
    assert local[URL] == (tmp_path / _pin().filename).resolve()


def test_a_corrupt_copy_is_downloaded_again(tmp_path):
    (tmp_path / _pin().filename).write_bytes(b"truncated")
    net = FakeNet(WHEEL)
    uw.fetch([_pin()], tmp_path, download=net, sleep=lambda s: None, log=lambda m: None)
    assert net.calls == 1
    assert (tmp_path / _pin().filename).read_bytes() == WHEEL


def test_a_verified_seed_is_copied_and_a_bad_seed_is_not(tmp_path):
    seed, dest = tmp_path / "seed", tmp_path / "dest"
    seed.mkdir()
    (seed / _pin().filename).write_bytes(WHEEL)
    net = FakeNet()
    uw.fetch([_pin()], dest, seed=seed, download=net, log=lambda m: None)
    assert net.calls == 0 and (dest / _pin().filename).read_bytes() == WHEEL

    (seed / _pin().filename).write_bytes(b"tampered")
    (dest / _pin().filename).unlink()
    net = FakeNet(WHEEL)
    uw.fetch([_pin()], dest, seed=seed, download=net, sleep=lambda s: None, log=lambda m: None)
    assert net.calls == 1


def test_download_retries_with_growing_backoff_then_succeeds(tmp_path):
    net = FakeNet(OSError("refused stream"), TimeoutError("read timed out"), WHEEL)
    slept, logs = [], []
    uw.fetch([_pin()], tmp_path, download=net, sleep=slept.append, log=logs.append, base_delay=5.0)
    assert net.calls == 3
    assert len(slept) == 2 and 5.0 <= slept[0] < 6.0 and 10.0 <= slept[1] < 11.0
    assert sum("::warning::" in m for m in logs) == 2
    assert (tmp_path / _pin().filename).read_bytes() == WHEEL


def test_bytes_that_never_match_the_pin_fail_and_leave_nothing(tmp_path):
    net = FakeNet(b"other", b"other", b"other")
    with pytest.raises(uw.FetchError, match="does not match the pin"):
        uw.fetch([_pin()], tmp_path, download=net, sleep=lambda s: None, log=lambda m: None, attempts=3)
    assert net.calls == 3
    assert not (tmp_path / _pin().filename).exists()


def test_the_backoff_is_capped(tmp_path):
    net = FakeNet(*[OSError("down")] * 8)
    slept = []
    with pytest.raises(uw.FetchError):
        uw.fetch([_pin()], tmp_path, download=net, sleep=slept.append, log=lambda m: None, attempts=8)
    assert len(slept) == 7 and max(slept) < 61.0


def test_copy_to_receives_the_verified_wheel(tmp_path):
    (tmp_path / "dest").mkdir()
    (tmp_path / "dest" / _pin().filename).write_bytes(WHEEL)
    uw.fetch([_pin()], tmp_path / "dest", copy_to=tmp_path / "ctx", log=lambda m: None)
    assert (tmp_path / "ctx" / _pin().filename).read_bytes() == WHEEL


# --- rewrite ----------------------------------------------------------------------------------------------------------

def test_rewrite_points_the_pin_at_the_local_wheel_and_keeps_every_other_line(tmp_path):
    wheel = tmp_path / "w" / _pin().filename
    out = uw.rewrite(REQS, {URL: wheel}, tmp_path / "src")
    lines, src = out.splitlines(), REQS.splitlines()
    assert lines[2] == f"demo_model @ {wheel.as_uri()}#sha256={SHA}  # pinned model"
    assert lines[3] == f"-r {(tmp_path / 'src' / 'extra.txt').resolve()}"
    assert [lines[i] for i in (0, 1, 4)] == [src[i] for i in (0, 1, 4)]
    assert uw.parse_pins(out) == []  # nothing left that points at the network


# --- CLI --------------------------------------------------------------------------------------------------------------

def _cli(*args, cwd):
    return subprocess.run([sys.executable, "-I", str(SCRIPT), *args], cwd=cwd, capture_output=True, text=True,
                          timeout=60)


def test_cli_key_and_fetch_from_a_seed(tmp_path):
    (tmp_path / "requirements.txt").write_text(REQS)
    (tmp_path / "extra.txt").write_text("gamma==1\n")
    (tmp_path / "seed").mkdir()
    (tmp_path / "seed" / _pin().filename).write_bytes(WHEEL)
    key = _cli("key", "--req", "requirements.txt", cwd=tmp_path)
    assert key.returncode == 0 and key.stdout.strip() == f"key={uw.cache_key([_pin()])}"

    res = _cli("fetch", "--req", "requirements.txt", "--dest", "d", "--seed", "seed", "--out", "o/req.txt",
               cwd=tmp_path)
    assert res.returncode == 0, res.stderr
    text = (tmp_path / "o" / "req.txt").read_text()
    assert (tmp_path / "d" / _pin().filename).resolve().as_uri() in text and "https://" not in text


def test_cli_exit_codes(tmp_path):
    (tmp_path / "bad.txt").write_text(f"demo_model @ {URL}\n")
    assert _cli("key", "--req", "bad.txt", cwd=tmp_path).returncode == 2
    # Port 9 (discard) on loopback refuses at once: one attempt, no sleep, exit 1 with an ::error:: line.
    (tmp_path / "down.txt").write_text(f"demo_model @ http://127.0.0.1:9/demo_model-1.0-py3-none-any.whl"
                                       f"#sha256={SHA}\n")
    res = _cli("fetch", "--req", "down.txt", "--dest", "d", "--attempts", "1", cwd=tmp_path)
    assert res.returncode == 1 and "::error::" in res.stderr


# --- workflows, action, Dockerfile ------------------------------------------------------------------------------------

def _workflows():
    for path in sorted(WORKFLOWS.glob("*.yml")):
        yield path.name, yaml.safe_load(path.read_text()) or {}


def _code(run: str) -> str:
    return "\n".join(line for line in run.splitlines() if not line.lstrip().startswith("#"))


ROOT_REQ_INSTALL = re.compile(r"(?:-r\s+|pip_audit_gate\.sh\s+)\"?requirements\.txt\b")


def test_no_root_level_step_installs_requirements_txt_straight_from_its_url_pins():
    """A `-r requirements.txt` at the repo root downloads the URL pin itself; install from the action's output."""
    offenders = []
    for name, wf in _workflows():
        for job_id, job in (wf.get("jobs") or {}).items():
            for step in job.get("steps") or []:
                if "run" not in step or step.get("working-directory"):
                    continue  # another checkout's app (llm-core-consumers), not this repo's requirements.txt
                if ROOT_REQ_INSTALL.search(_code(step["run"])):
                    offenders.append(f"{name}:{job_id}:{step.get('name')}")
    assert not offenders, offenders


def test_every_install_from_req_reads_the_url_wheels_output_of_an_earlier_step():
    seen_jobs = []
    for name, wf in _workflows():
        for job_id, job in (wf.get("jobs") or {}).items():
            steps = job.get("steps") or []
            wheel_at = next((i for i, s in enumerate(steps) if s.get("uses") == ACTION_USES), None)
            for i, step in enumerate(steps):
                env = step.get("env") or {}
                if "steps.wheels.outputs.requirements" not in str(env.get("REQ", "")):
                    continue
                seen_jobs.append(f"{name}:{job_id}")
                assert wheel_at is not None and wheel_at < i, f"{name}:{job_id}: REQ read before the url-wheels step"
                assert steps[wheel_at].get("id") == "wheels", f"{name}:{job_id}: the url-wheels step needs id: wheels"
                assert '"$REQ"' in step["run"], f"{name}:{job_id}:{step.get('name')}: REQ set but not used"
    # Security Audit, the unit shards, Unit Tests, the integration shards, the nightly run.
    for job in ("ci.yml:security-audit", "ci.yml:test-unit-shard", "ci.yml:test-unit",
                "ci.yml:test-integration-shard", "nightly-integration.yml:random-order"):
        assert job in seen_jobs, job


def _builds_the_root_image(run: str) -> bool:
    """A `docker build` whose context is the repo root (the API image); the admin image builds frontend/."""
    joined = re.sub(r"\\\n", " ", _code(run))
    return any(line.split()[-1] == "." for line in joined.splitlines() if re.search(r"\bdocker build\b", line))


def test_every_docker_build_gets_the_verified_wheel_in_the_seed_first():
    builds = 0
    for name, wf in _workflows():
        for job_id, job in (wf.get("jobs") or {}).items():
            steps = job.get("steps") or []
            for i, step in enumerate(steps):
                if "run" in step and _builds_the_root_image(step["run"]):
                    builds += 1
                    seeded = [s for s in steps[:i] if s.get("uses") == ACTION_USES
                              and (s.get("with") or {}).get("copy-to") == ".url-wheels"]
                    assert seeded, f"{name}:{job_id}: docker build without the url-wheels seed step before it"
    assert builds >= 2  # Docker Build Check and Deploy Dev


def test_the_required_job_names_are_unchanged():
    jobs = yaml.safe_load((WORKFLOWS / "ci.yml").read_text())["jobs"]
    names = {job.get("name") for job in jobs.values()}
    assert {"Security Audit", "Unit Tests", "Integration Tests", "Docker Build Check"} <= names
    assert jobs["test-unit-shard"]["name"] == "Unit shard ${{ matrix.shard }}/4"
    assert jobs["test-integration-shard"]["name"] == "Integration shard ${{ matrix.shard }}/4"


def test_the_action_caches_by_pin_on_one_path_with_a_sha_pinned_cache_action():
    action = yaml.safe_load(ACTION.read_text())
    steps = action["runs"]["steps"]
    cache = next(s for s in steps if str(s.get("uses", "")).startswith("actions/cache@"))
    assert re.fullmatch(r"actions/cache@[0-9a-f]{40}", cache["uses"])
    assert cache["with"]["path"] == "${{ runner.temp }}/url-wheels"
    assert "steps.key.outputs.key" in cache["with"]["key"]
    fetch = next(s for s in steps if s.get("id") == "fetch")
    assert "url_wheels.py" in fetch["run"] and '--dest "$RUNNER_TEMP/url-wheels"' in fetch["run"]
    assert action["outputs"]["requirements"]["value"] == "${{ steps.fetch.outputs.requirements }}"


def test_the_dockerfile_installs_through_the_script_with_the_seed_mounted():
    text = DOCKERFILE.read_text()
    code = "\n".join(line for line in text.splitlines() if not line.lstrip().startswith("#"))
    assert not re.search(r"pip install[^\n]*-r\s+requirements\.txt", code)
    assert "--mount=type=bind,source=.url-wheels,target=/tmp/url-wheels-seed" in code
    assert "--mount=type=bind,source=.github/scripts/url_wheels.py" in code
    assert "--seed /tmp/url-wheels-seed" in code and "pip install --no-cache-dir -r /tmp/requirements-local.txt" in code
    assert text.splitlines()[0].startswith("# syntax=docker/dockerfile:1")  # RUN --mount needs the BuildKit frontend


def test_the_seed_directory_is_tracked_but_never_holds_a_committed_wheel():
    assert (SEED / "README.md").is_file()
    assert "*.whl" in (SEED / ".gitignore").read_text().split()
    tracked = subprocess.run(["git", "-C", str(REPO), "ls-files", ".url-wheels"], capture_output=True, text=True)
    if tracked.returncode == 0 and tracked.stdout:
        assert not [f for f in tracked.stdout.split() if f.endswith(".whl")]


# S273: GitHub throttled unauthenticated release downloads; a release asset is fetched through `gh` with the job token.
GH_URL = (
    "https://github.com/invented-org/invented-models/releases/download/demo-1.0.0/"
    "demo_model-1.0.0-py3-none-any.whl"
)


def _fake_gh(calls):
    def run(argv, check, timeout, capture_output):
        calls.append(argv)
        out = Path(argv[argv.index("--dir") + 1]) / argv[argv.index("--pattern") + 1]
        out.write_bytes(WHEEL)
    return run


def test_release_asset_goes_through_authenticated_gh(tmp_path, monkeypatch):
    monkeypatch.setenv("GH_TOKEN", "job-token")
    monkeypatch.setattr(uw.shutil, "which", lambda name: "/usr/bin/gh" if name == "gh" else None)
    calls = []
    dest = tmp_path / "demo_model-1.0.0-py3-none-any.whl"
    assert uw._gh_release_download(GH_URL, dest, 30, run=_fake_gh(calls)) is True
    assert dest.read_bytes() == WHEEL
    assert calls[0][:6] == ["gh", "release", "download", "demo-1.0.0", "--repo", "invented-org/invented-models"]
    assert not list(tmp_path.glob(".gh-*"))


def test_no_token_or_no_gh_or_other_host_skips_gh(tmp_path, monkeypatch):
    calls = []
    dest = tmp_path / "x.whl"
    monkeypatch.delenv("GH_TOKEN", raising=False)
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    monkeypatch.setattr(uw.shutil, "which", lambda name: "/usr/bin/gh")
    assert uw._gh_release_download(GH_URL, dest, 30, run=_fake_gh(calls)) is False
    monkeypatch.setenv("GH_TOKEN", "job-token")
    assert uw._gh_release_download(URL, dest, 30, run=_fake_gh(calls)) is False
    monkeypatch.setattr(uw.shutil, "which", lambda name: None)
    assert uw._gh_release_download(GH_URL, dest, 30, run=_fake_gh(calls)) is False
    assert calls == []


def test_action_passes_the_job_token_to_the_fetch_step():
    action = yaml.safe_load(ACTION.read_text())
    fetch = next(s for s in action["runs"]["steps"] if s.get("id") == "fetch")
    assert fetch["env"]["GH_TOKEN"] == "${{ github.token }}"
