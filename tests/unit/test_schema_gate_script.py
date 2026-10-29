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


def _run(rid: int, status: str = "completed", conclusion: str | None = "success") -> dict:
    return {
        "id": rid,
        "status": status,
        "conclusion": conclusion,
        "run_attempt": 1,
        "updated_at": "t",
        "html_url": f"https://example.invalid/runs/{rid}",
    }


def _jobs(apply: str | None) -> dict:
    jobs = [{"name": "Plan (bytes, classify, forecast; no AWS)", "conclusion": "success"}]
    if apply != "absent":
        jobs.append({"name": "Apply on Dev (dev-db-auto)", "conclusion": apply})
    return {"jobs": jobs}


class FakeGitHub:
    def __init__(self, runs: list, jobs: dict, status: int = 200) -> None:
        self.runs, self.jobs, self.status, self.calls = runs, jobs, status, []

    def __call__(self, path: str) -> tuple:
        self.calls.append(path)
        if "/workflows/" in path:
            return self.status, ({"workflow_runs": self.runs} if self.status == 200 else None)
        rid = int(path.split("/runs/")[1].split("/")[0])
        return 200, self.jobs[rid]


def _state(runs: list, jobs: dict, status: int = 200) -> sg.RunState:
    return sg.Infra("o/infra", FakeGitHub(runs, jobs, status)).state()


def test_private_infra_skips_the_run_check() -> None:
    s = _state([], {}, status=404)
    assert s.ok and s.unreadable


def test_infra_api_error_is_not_ok() -> None:
    assert not _state([], {}, status=500).ok


def test_list_only_runs_do_not_count() -> None:
    runs = [_run(3, conclusion="failure"), _run(2), _run(1)]
    jobs = {3: _jobs("skipped"), 2: _jobs("skipped"), 1: _jobs("absent")}
    assert _state(runs, jobs).ok


def test_last_applying_run_green_passes_red_blocks() -> None:
    assert _state([_run(2), _run(1, conclusion="failure")], {2: _jobs("success"), 1: _jobs("success")}).ok
    red = _state([_run(2, conclusion="failure"), _run(1)], {2: _jobs("success"), 1: _jobs("success")})
    assert not red.ok and "failure" in red.reason and red.url.endswith("/2")


def test_in_flight_apply_waits_but_a_plan_only_run_does_not() -> None:
    applying = _state([_run(2, "in_progress", None), _run(1)], {2: _jobs(None), 1: _jobs("success")})
    assert not applying.ok and "applying" in applying.reason
    planning = _state([_run(2, "in_progress", None), _run(1)], {2: _jobs("absent"), 1: _jobs("success")})
    assert planning.ok


def test_completed_jobs_are_cached() -> None:
    gh = FakeGitHub([_run(2, conclusion="failure")], {2: _jobs("success")})
    infra = sg.Infra("o/infra", gh)
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
    infra = sg.Infra("o/infra", FakeGitHub([_run(1, conclusion="failure")], {1: _jobs("success")}))
    out, _ = _gate([OK], infra=infra, timeout=90)
    assert out.code == 1 and "failure" in out.lines[0]


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
