"""AA-902 — .github/scripts/check_resource_ids.py, the blocking resource-ID check of this public repo.

Fake account IDs are built at run time: a 12-digit literal in this file would fail the check itself.
"""
import importlib.util
from pathlib import Path

import pytest

_PATH = Path(__file__).resolve().parents[2] / ".github" / "scripts" / "check_resource_ids.py"
_spec = importlib.util.spec_from_file_location("check_resource_ids", _PATH)
check = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(check)

FAKE = "4" + "2" * 11       # not a real account, not an AWS documentation example
DOC_EXAMPLE = "1234567890" + "12"


def _kinds(line: str) -> list[str]:
    return [kind for kind, _ in check.scan_line(line)]


@pytest.mark.parametrize("line, kind", [
    (f'ROLE = "arn:aws:iam::{FAKE}:role/AA-Bedrock-Invoker"', "ARN with account ID"),
    (f"arn:aws:bedrock:us-west-1:{FAKE}:inference-profile/global.x", "ARN with account ID"),
    (f'BUCKET = "aa-cis-bronze-{FAKE}"', "account ID in a resource name"),
    (f"s3://aa-cis-dev-e2e-mail-{FAKE}/inbound/", "account ID in a resource name"),
    (f'ACC1_ACCOUNT_ID = "{FAKE}"', "12-digit account ID"),
    (f'  "{FAKE}": "acc3",', "12-digit account ID"),
    (f"AA-623 found acc2/{FAKE} is a member account", "12-digit account ID"),
    ("sg-" + "0123abcd", "VPC resource ID"),                  # split so this file passes the check
    ("https://abcde12345" + ".execute-api.us-west-1.amazonaws.com/dev", "API Gateway host"),
])
def test_each_pattern_is_found(line, kind):
    assert _kinds(line) == [kind]


@pytest.mark.parametrize("line", [
    "arn:aws:iam::<account>:role/AA-Bedrock-Invoker",
    'BUCKET = "aa-cis-bronze-<acc2>"',
    f'role = "arn:aws:iam::{DOC_EXAMPLE}:role/doc"',          # AWS documentation example account
    f'TENANT = "a1b2c3d4-0001-4000-8000-{"0" * 11}1"',         # UUID: last group is 12 digits
    'f"arn:aws:iam::{ACC1_ACCOUNT_ID}:role/x"',                # built at run time
])
def test_placeholders_uuids_and_doc_examples_pass(line):
    assert check.scan_line(line) == []


def test_findings_are_masked():
    [(_, frag)] = check.scan_line(f'x = "{FAKE}"')
    assert FAKE not in frag and frag.startswith(FAKE[:3])


def _repo(tmp_path: Path, allowlist: str) -> Path:
    (tmp_path / ".github").mkdir()
    (tmp_path / ".github" / "resource-id-allowlist.txt").write_text(allowlist, encoding="utf-8")
    (tmp_path / "a.py").write_text(f'ACC = "{FAKE}"\n', encoding="utf-8")
    return tmp_path


def _run(root: Path, today: str, monkeypatch) -> int:
    monkeypatch.setenv("TODAY_OVERRIDE", today)
    monkeypatch.chdir(root)
    return check.main(["--root", str(root), "a.py"])


def test_unlisted_finding_fails(tmp_path, monkeypatch):
    assert _run(_repo(tmp_path, ""), "2026-10-19", monkeypatch) == 1


def test_listed_file_passes_until_expiry(tmp_path, monkeypatch):
    root = _repo(tmp_path, "a.py 1 2026-11-30 pending env var\n")
    assert _run(root, "2026-10-19", monkeypatch) == 0
    assert _run(root, "2026-12-01", monkeypatch) == 1


def test_listed_file_fails_when_it_grows(tmp_path, monkeypatch):
    root = _repo(tmp_path, "a.py 1 2026-11-30 pending env var\n")
    (root / "a.py").write_text(f'ACC = "{FAKE}"\nB = "arn:aws:iam::{FAKE}:role/x"\n', encoding="utf-8")
    assert _run(root, "2026-10-19", monkeypatch) == 1


def test_malformed_allowlist_entry_fails(tmp_path, monkeypatch):
    assert _run(_repo(tmp_path, "a.py forever\n"), "2026-10-19", monkeypatch) == 1
