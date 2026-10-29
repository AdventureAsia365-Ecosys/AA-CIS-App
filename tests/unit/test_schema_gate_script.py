"""The Deploy Dev ``schema-gate`` (AA-993 M4b, root ADR 0017 d7): ``.github/scripts/schema_gate.py``.

Pins what the commit needs (readiness keys + every file at or below the newest one), how a readiness body is read
(``registered_keys``, then the pre-M4a fallback), the Infra migration-apply run rule (in-flight apply waits, red
blocks, list-only runs do not count, a private repo skips the check) and the bounded loop (timeout, unreachable API).
The file is identical in AA-CIS-App, AA-Booking and AA-Partner; so is this test."""

from __future__ import annotations

import importlib
import sys
from pathlib import Path

import pytest


def _repo() -> Path:
    for p in Path(__file__).resolve().parents:
        if (p / ".github" / "scripts" / "schema_gate.py").is_file():
            return p
    raise RuntimeError("schema_gate.py not found above this test")


REPO = _repo()
sys.path.insert(0, str(REPO / ".github" / "scripts"))
sg = importlib.import_module("schema_gate")

BOOKING_SRC = 'REQUIRED_SCHEMA_VERSIONS: tuple[str, ...] = ("bk-001", "bk-002", "bk-004")\nOTHER = 1\n'
CIS_SRC = "REQUIRED_CIS_SCHEMA = 266\nLEGACY_CIS_BASELINE = 263\nREQUIRED_CIS_KEYS = tuple(range(1, 2))\n"


# ── what the commit needs ──────────────────────────────────────────────────────────────────────────────────────────


def test_booking_keys_plus_every_file_up_to_the_newest_required() -> None:
    files = ["001_a.sql", "002_b.sql", "003_c.sql", "004_d.sql", "005_e.sql", "README.md"]
    # bk-003 is not listed but sits below bk-004 (strict prefix, critic 2); bk-005 is above: not needed.
    assert sg.required_keys("booking", BOOKING_SRC, files) == ["bk-001", "bk-002", "bk-003", "bk-004"]


def test_cis_keys_are_the_range_above_the_baseline() -> None:
    files = ["010_old.sql", "263_x.sql", "264_a.sql", "267_next.sql"]
    assert sg.required_keys("cis", CIS_SRC, files) == ["264", "265", "266"]


def test_crm_keys_are_normalised() -> None:
    src = "REQUIRED_SCHEMA_VERSIONS = ('crm-001', 'crm-002')\n"
    assert sg.required_keys("crm", src, ["001_a.sql", "002_b.sql"]) == ["crm-001", "crm-002"]


@pytest.mark.parametrize(
    ("app", "src"),
    [("booking", "X = 1\n"), ("booking", "REQUIRED_SCHEMA_VERSIONS = ()\n"), ("cis", "REQUIRED_CIS_SCHEMA = 3\n")],
)
def test_unreadable_readiness_is_an_error(app: str, src: str) -> None:
    with pytest.raises(ValueError):
        sg.readiness_keys(app, src)


# ── what Dev has ────────────────────────────────────────────────────────────────────────────────────────────────────


def test_registered_keys_decide() -> None:
    body = {"status": "ok", "registered_keys": ["bk-001", "bk-002", "bk-004"]}
    assert sg.evaluate("booking", ["bk-001", "bk-002", "bk-003", "bk-004"], body).missing == ["bk-003"]
    cis = {"registered_keys": [264, "265", "266"]}
    assert sg.evaluate("cis", ["264", "265", "266"], cis).missing == []


def test_fallback_without_registered_keys() -> None:
    body = {"status": "not_ready", "schema_version": "bk-004", "missing_schema_versions": ["bk-002"]}
    s = sg.evaluate("booking", ["bk-001", "bk-002", "bk-004", "bk-005"], body)
    assert s.missing == ["bk-002", "bk-005"]
    assert s.how.startswith("fallback")
    assert sg.evaluate("booking", ["bk-001"], {"status": "not_ready", "schema_version": None}).missing == ["bk-001"]


# ── the Infra run rule ──────────────────────────────────────────────────────────────────────────────────────────────


def _run(rid: int, status: str = "completed", conclusion: str | None = "success", attempt: int = 1) -> dict:
    return {
        "id": rid,
        "status": status,
        "conclusion": conclusion,
        "run_attempt": attempt,
        "created_at": f"2026-10-29T10:{rid:02d}:00Z",
        "html_url": f"https://example.invalid/runs/{rid}",
    }


def _job(name: str, conclusion: str | None, status: str = "completed") -> dict:
    return {"name": name, "status": status, "conclusion": conclusion}


PLAN = _job("Plan (bytes, classify, forecast; no AWS)", "success")


def _jobs(apply: str | None, probe: str | None = "success", others: str = "success", running: str = "") -> dict:
    """apply/probe: a conclusion, None (in progress), or "absent"; running names the jobs still in progress."""
    jobs = [PLAN]
    if apply != "absent":
        jobs.append(_job("Apply on Dev (dev-db-auto)", apply, "in_progress" if "apply" in running else "completed"))
    if probe != "absent":
        jobs.append(
            _job(
                "Isolation probe (blocking) / Isolation probe",
                probe,
                "in_progress" if "probe" in running else "completed",
            )
        )
    for name in ("Regenerate the schema reference (DB Schema Dump)", "Summary and copy PR notes"):
        jobs.append(
            _job(
                name, others if "others" not in running else None, "in_progress" if "others" in running else "completed"
            )
        )
    return {"jobs": jobs}


LIST_ONLY = {
    "jobs": [PLAN, _job("Apply on Dev (dev-db-auto)", "skipped"), _job("Summary and copy PR notes", "success")]
}


class FakeGitHub:
    """Answers the run-list queries by status / conclusion (and created >=), the jobs reads and the repo read."""

    def __init__(self, runs: list, jobs: dict, codes: dict | None = None, repo_status: int = 200) -> None:
        self.runs, self.jobs, self.codes, self.repo_status, self.calls = runs, jobs, codes or {}, repo_status, []

    def __call__(self, path: str) -> tuple:
        self.calls.append(path)
        for needle, code in self.codes.items():
            if needle in path:
                return code, None
        if path == "/repos/o/infra":
            return self.repo_status, ({"private": False} if self.repo_status == 200 else None)
        if "/workflows/" in path:
            query = dict(kv.split("=", 1) for kv in path.split("?", 1)[1].split("&"))
            want = query.get("status", "")
            since = query.get("created", "").replace("%3E%3D", "")
            hits = [r for r in self.runs if want in (r["status"], r["conclusion"]) and r["created_at"] >= since]
            return 200, {"workflow_runs": sorted(hits, key=lambda r: r["created_at"], reverse=True)}
        rid = int(path.split("/runs/")[1].split("/")[0])
        return 200, self.jobs.get(rid, {"jobs": []})


def _infra(gh: FakeGitHub, **kw) -> sg.Infra:
    return sg.Infra("o/infra", gh, now=lambda: 1_793_000_000.0, **kw)  # late Oct 2026: every fixture run is recent


def _state(runs: list, jobs: dict, **kw) -> sg.RunState:
    return _infra(FakeGitHub(runs, jobs, **kw)).state()


def test_private_infra_skips_the_run_check() -> None:
    s = _state([], {}, codes={"/workflows/": 404}, repo_status=404)
    assert s.ok and s.unreadable


def test_a_404_of_the_workflow_in_a_readable_repo_is_not_a_pass() -> None:
    s = _state([], {}, codes={"/workflows/": 404}, repo_status=200)
    assert not s.ok and not s.unreadable and "renamed" in s.reason


@pytest.mark.parametrize("code", [403, 429, 500, 0])
def test_rate_limit_and_errors_are_not_a_pass(code: int) -> None:
    s = _state([], {}, codes={"/workflows/": code})
    assert not s.ok and not s.unreadable and f"HTTP {code}" in s.reason


def test_a_rate_limited_jobs_read_is_not_a_pass() -> None:
    s = _state([_run(2, conclusion="failure")], {}, codes={"/jobs": 403})
    assert not s.ok and "HTTP 403" in s.reason


def test_no_run_at_all_passes() -> None:
    assert _state([], {}).ok


def test_list_only_runs_do_not_count() -> None:
    runs = [_run(3, conclusion="failure"), _run(2), _run(1, conclusion="cancelled")]
    jobs = {3: LIST_ONLY, 2: LIST_ONLY, 1: {"jobs": []}}
    gh = FakeGitHub(runs, jobs)
    assert _infra(gh).state().ok
    assert not any("/runs/2/jobs" in c for c in gh.calls), "a success run is never read without a red one"


def test_red_apply_blocks() -> None:
    red = _state([_run(2, conclusion="failure"), _run(1)], {2: _jobs("failure", "skipped"), 1: _jobs("success")})
    assert not red.ok and red.url.endswith("/2") and "re-run its failed jobs" in red.reason


def test_red_probe_blocks() -> None:
    red = _state([_run(2, conclusion="failure")], {2: _jobs("success", "failure")})
    assert not red.ok and red.url.endswith("/2")


def test_apply_and_probe_green_but_summary_red_passes() -> None:
    assert _state([_run(2, conclusion="failure")], {2: _jobs("success", "success", others="failure")}).ok


def test_a_red_run_superseded_by_a_newer_green_apply_passes() -> None:
    runs = [_run(4), _run(3), _run(2, conclusion="failure")]
    jobs = {4: LIST_ONLY, 3: _jobs("success"), 2: _jobs("failure", "skipped")}
    s = _state(runs, jobs)
    assert s.ok and "newer run 3" in s.reason


def test_a_red_run_followed_only_by_list_only_runs_blocks() -> None:
    runs = [_run(4), _run(3), _run(2, conclusion="failure")]
    assert not _state(runs, {4: LIST_ONLY, 3: LIST_ONLY, 2: _jobs("failure", "skipped")}).ok


def test_old_list_only_failures_do_not_hide_a_red_run() -> None:
    # The old 30-run window: a red applying run behind many newer runs still blocks (finding: window).
    runs = [_run(50 - i, conclusion="failure") for i in range(40)] + [_run(5, conclusion="failure")]
    jobs = {r["id"]: LIST_ONLY for r in runs}
    jobs[5] = _jobs("success", "failure")
    assert not _state(runs, jobs).ok


def test_read_cap_fails_closed() -> None:
    runs = [_run(50 - i, conclusion="failure") for i in range(10)]
    s = _infra(FakeGitHub(runs, {r["id"]: LIST_ONLY for r in runs}), max_jobs=3).state()
    assert not s.ok and "jobs of 3 runs" in s.reason


def test_a_full_page_of_red_runs_without_an_apply_fails_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sg, "PAGE", 3)
    runs = [_run(9 - i, conclusion="cancelled") for i in range(3)]
    s = _state(runs, {r["id"]: {"jobs": []} for r in runs})
    assert not s.ok and "full page" in s.reason


def test_in_flight_apply_or_probe_waits() -> None:
    applying = _state([_run(2, "in_progress", None), _run(1)], {2: _jobs(None, "absent", running="apply")})
    assert not applying.ok and "applying" in applying.reason
    probing = _state([_run(2, "in_progress", None)], {2: _jobs("success", None, running="probe")})
    assert not probing.ok
    gap = _state([_run(2, "in_progress", None)], {2: _jobs("success", "absent")})
    assert not gap.ok, "apply done, probe job not created yet: still waiting"


def test_queued_plan_with_no_apply_job_yet_does_not_wait() -> None:
    assert _state([_run(2, "queued", None)], {2: {"jobs": []}}).ok
    assert _state([_run(2, "in_progress", None)], {2: {"jobs": [_job(PLAN["name"], None, "in_progress")]}}).ok


def test_in_flight_run_with_apply_and_probe_done_does_not_wait_for_dump_or_summary() -> None:
    s = _state([_run(2, "in_progress", None)], {2: _jobs("success", "success", running="others")})
    assert s.ok


def test_a_re_run_in_flight_waits() -> None:
    s = _state([_run(2, "queued", None, attempt=2)], {})
    assert not s.ok and "re-run" in s.reason


def test_completed_jobs_are_cached() -> None:
    gh = FakeGitHub([_run(2, conclusion="failure")], {2: _jobs("success", "failure")})
    infra = _infra(gh)
    infra.state()
    infra.state()
    assert sum("/jobs" in c for c in gh.calls) == 1


# ── the loop ────────────────────────────────────────────────────────────────────────────────────────────────────────


class Clock:
    def __init__(self) -> None:
        self.t = 0.0

    def __call__(self) -> float:
        return self.t

    def sleep(self, s: float) -> None:
        self.t += s


def _gate(answers: list, infra=None, timeout: float = 120, grace: float = 60) -> tuple:
    clock, seen = Clock(), []

    def fetch() -> tuple:
        seen.append(clock.t)
        return answers[min(len(seen) - 1, len(answers) - 1)]

    out = sg.run_gate(
        "booking",
        ["bk-001", "bk-002"],
        fetch,
        infra,
        timeout=timeout,
        interval=30,
        unreachable_grace=grace,
        clock=clock,
        sleep=clock.sleep,
        log=lambda _m: None,
    )
    return out, seen


OK = (200, {"status": "ok", "registered_keys": ["bk-001", "bk-002"]})
BEHIND = (503, {"status": "not_ready", "registered_keys": ["bk-001"]})


def test_passes_at_once_when_everything_is_there() -> None:
    out, seen = _gate([OK])
    assert out.code == 0 and len(seen) == 1


def test_waits_for_the_key_then_passes() -> None:
    out, seen = _gate([BEHIND, BEHIND, OK])
    assert out.code == 0 and seen == [0, 30, 60]


def test_times_out_red_with_the_missing_key() -> None:
    out, seen = _gate([BEHIND], timeout=120)
    assert out.code == 1 and "bk-002" in out.lines[0] and seen[-1] <= 120


def test_unreachable_api_fails_after_the_grace() -> None:
    out, _ = _gate([(0, None)], timeout=1200, grace=60)
    assert out.code == 1 and "no JSON" in out.lines[0]


def test_keys_there_but_a_red_apply_run_blocks() -> None:
    infra = _infra(FakeGitHub([_run(1, conclusion="failure")], {1: _jobs("success", "failure")}))
    out, _ = _gate([OK], infra=infra, timeout=90)
    assert out.code == 1 and "red apply or Isolation Probe" in out.lines[0]


def test_a_not_ok_run_check_is_re_read_only_every_infra_interval() -> None:
    gh = FakeGitHub([_run(1, conclusion="failure")], {}, codes={"/workflows/": 429})
    clock = Clock()
    out = sg.run_gate(
        "booking",
        ["bk-001", "bk-002"],
        lambda: OK,
        _infra(gh),
        timeout=600,
        interval=30,
        unreachable_grace=60,
        infra_interval=120,
        clock=clock,
        sleep=clock.sleep,
        log=lambda _m: None,
    )
    assert out.code == 1 and "HTTP 429" in out.lines[0]
    checks = sum("status=in_progress" in c for c in gh.calls)
    assert 4 <= checks <= 6, checks  # ~600 s / 120 s, not one per 30 s poll


def test_the_pass_reports_the_timing() -> None:
    out, _ = _gate([BEHIND, OK])
    assert out.code == 0 and "keys registered after 30 s" in out.lines[-1]


def test_timeout_comes_from_the_repo_variable() -> None:
    assert sg.timeout_from({}) == sg.DEFAULT_TIMEOUT == 2400
    assert sg.timeout_from({"SCHEMA_GATE_TIMEOUT_S": "1800"}) == 1800
    assert sg.timeout_from({"SCHEMA_GATE_TIMEOUT_S": "soon"}) == sg.DEFAULT_TIMEOUT
    assert sg.timeout_from({"SCHEMA_GATE_TIMEOUT_S": ""}) == sg.DEFAULT_TIMEOUT


def test_main_reads_the_tree_and_writes_the_summary(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    (tmp_path / "db.py").write_text(BOOKING_SRC, encoding="utf-8")
    mig = tmp_path / "migrations"
    mig.mkdir()
    for n in ("001_a.sql", "002_b.sql", "003_c.sql", "004_d.sql"):
        (mig / n).write_text("SELECT 1;\n", encoding="utf-8")
    ready = {"status": "ok", "registered_keys": ["bk-001", "bk-002", "bk-003", "bk-004"]}
    monkeypatch.setattr(sg, "http_json", lambda url, token="", timeout=15.0: (200, ready))
    summary = tmp_path / "summary.md"
    code = sg.main(
        [
            "--app",
            "booking",
            "--readiness-file",
            str(tmp_path / "db.py"),
            "--migrations-dir",
            str(mig),
            "--ready-url",
            "https://example.invalid/ready",
            "--infra-repo",
            "",
            "--summary",
            str(summary),
        ]
    )
    assert code == 0
    assert "Schema gate: passed" in summary.read_text(encoding="utf-8")
    assert (
        sg.main(
            [
                "--app",
                "booking",
                "--readiness-file",
                str(tmp_path / "nope.py"),
                "--migrations-dir",
                str(mig),
                "--ready-url",
                "https://example.invalid/ready",
            ]
        )
        == 2
    )


# ── the workflow wiring ─────────────────────────────────────────────────────────────────────────────────────────────


def test_deploy_dev_runs_the_gate_before_the_rollout() -> None:
    text = (REPO / ".github" / "workflows" / "deploy-dev.yml").read_text(encoding="utf-8")
    assert "  schema-gate:\n" in text and ".github/scripts/schema_gate.py" in text
    deploy = text.split("\n  deploy:\n", 1)[1].split("\n  verify:\n", 1)[0]
    assert "schema-gate" in deploy.split("steps:", 1)[0], "deploy must need schema-gate"
    result = text.split("\n  result:\n", 1)[1]
    assert "schema-gate" in result.split("steps:", 1)[0], "the result job must read schema-gate"


def test_github_get_retries_anonymously_when_the_token_cannot_read() -> None:
    calls = []

    def fetch(url: str, token: str) -> tuple:
        calls.append(token)
        return (404, None) if token else (200, {"ok": True})

    assert sg.github_get("/repos/o/r", "tok", fetch) == (200, {"ok": True})
    assert calls == ["tok", ""]
    for code in (401, 403, 429):
        calls.clear()

        def deny(url: str, token: str, c: int = code) -> tuple:
            calls.append(token)
            return c, None

        # Retried anonymously; the anonymous answer (still 403/429 here) is what the run check judges: not a pass.
        assert sg.github_get("/repos/o/r", "tok", deny) == (code, None)
        assert calls == ["tok", ""]
    calls.clear()
    assert sg.github_get("/repos/o/r", "", fetch) == (200, {"ok": True})
    assert calls == [""]


def test_deploy_dev_gate_job_outlives_the_script_timeout() -> None:
    text = (REPO / ".github" / "workflows" / "deploy-dev.yml").read_text(encoding="utf-8")
    gate = text.split("\n  schema-gate:\n", 1)[1].split("\n  # ", 1)[0].split("\n  preflight:\n", 1)[0]
    assert "timeout-minutes: 45" in gate
    assert "SCHEMA_GATE_TIMEOUT_S: ${{ vars.SCHEMA_GATE_TIMEOUT_S }}" in gate
    assert sg.MAX_TIMEOUT < 45 * 60


def test_main_clamps_the_timeout_to_the_job_limit(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    (tmp_path / "db.py").write_text(BOOKING_SRC, encoding="utf-8")
    (tmp_path / "m").mkdir()
    seen = {}

    def fake_gate(*_a, **kw):
        seen.update(kw)
        return sg.Outcome(0, ["ok"])

    monkeypatch.setattr(sg, "run_gate", fake_gate)
    args = [
        "--app",
        "booking",
        "--readiness-file",
        str(tmp_path / "db.py"),
        "--migrations-dir",
        str(tmp_path / "m"),
        "--ready-url",
        "https://example.invalid/ready",
        "--infra-repo",
        "",
        "--summary",
        "",
        "--timeout",
        "9999",
    ]
    assert sg.main(args) == 0
    assert seen["timeout"] == sg.MAX_TIMEOUT and seen["infra_interval"] == 120


def test_ci_migration_preflight_wiring() -> None:
    """Static check of the CI job: the env the steps read, the step ids the verdict reads, the label read live."""
    text = (REPO / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
    job = text.split("\n  migration-preflight:\n", 1)[1]
    job = job.split("\n  # ", 1)[0] if "\n  # " in job else job
    for env in (
        "OWNER:",
        "MIG_DIR:",
        "READINESS_FILE:",
        "READY_URL:",
        "INFRA_REPO:",
        "INFRA_TOOLS_SHA:",
        "REPLAY_IMAGE:",
    ):
        assert f"      {env}" in job, env
    for step_id in ("id: scope", "id: infra", "id: classify"):
        assert step_id in job, step_id
    for out in (
        "steps.scope.outputs.base",
        "steps.scope.outputs.ooo",
        "steps.infra.outputs.tools",
        "steps.infra.outputs.copies",
        "steps.classify.outputs.status",
    ):
        assert out in job, out
    assert "steps.scope.outputs.relevant == 'true'" in job
    assert "steps.scope.outputs.added != '0'" in job
    assert ".github/scripts/migration_preflight.py" in job and '--ready-url "$READY_URL"' in job
    # Labels are read live (gh pr view) on pull_request too: adding migration:out-of-order + "Re-run jobs" works.
    assert "github.event.pull_request.labels" not in job
    assert "github.event.pull_request.number" in job and "gh pr view" in job
