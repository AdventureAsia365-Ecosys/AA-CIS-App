"""AA-998 — acc1 (the personal AWS account that was the cross-account LLM fallback and the third Cost Explorer
account) left the ecosystem. LLM runs on acc3 only, infrastructure on acc2. This guard fails when runtime code
names acc1 again: the account key, its account ID, or one of the acc1-only IAM roles / env vars / CLI profile.

Comments and docstrings count on purpose (plain text scan): a comment that says "falls back to acc1" is how a
removed path comes back. Write "the retired fallback account" when history needs a word.

Not scanned: api/migrations/ (immutable history; migration 295 removed the data), tests/ (they assert acc1 is
refused), docs/ (history). The account ID is matched by its sha256 only: this repo is public and carries no
account ID (AA-902, DM-6.10), not even split into parts. .github/scripts/check_resource_ids.py already refuses any
12-digit account ID; the hash check here names the retired one in the failure message.
"""
import hashlib
import re
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
SCANNED_ROOTS = ("api", "services", "shared", "worker", "scripts", "frontend/app", "frontend/lib",
                 "frontend/components")
SKIPPED = (Path("api/migrations"),)
SKIPPED_DIRS = {"node_modules", ".next", "__pycache__", "dist", "build"}
SUFFIXES = {".py", ".ts", ".tsx", ".js", ".mjs", ".cjs", ".sh", ".sql", ".json", ".yml", ".yaml", ".toml"}
ROOT_FILES = ("Dockerfile", "README.md", "CONTEXT.md")

# sha256 of the retired account's 12-digit ID (the ID itself is never written in this public repo).
ACC1_ACCOUNT_ID_SHA256 = "5a1e173cd62aa9f50be3678daa187e66a6aab118166e79def0740a4a9c1cb143"
_TWELVE_DIGITS = re.compile(r"(?<![0-9])[0-9]{12}(?![0-9])")


class _HashedAccountId:
    """re-like `search`: true when a 12-digit run of the line hashes to the retired account's ID."""

    @staticmethod
    def search(line: str) -> bool:
        return any(hashlib.sha256(m.group().encode()).hexdigest() == ACC1_ACCOUNT_ID_SHA256
                   for m in _TWELVE_DIGITS.finditer(line))


FORBIDDEN = [
    ("the acc1 account key", re.compile(r"(?i)(?<![a-z0-9])acc1(?![0-9])")),
    ("the acc1 account ID", _HashedAccountId()),
    ("the acc1 invoker role", re.compile(r"(?<![A-Za-z0-9])AA-Bedrock-Invoker")),
    ("the acc1 Cost Explorer role", re.compile(r"(?<![A-Za-z0-9])AA-CostExplorer-Reader")),
    ("the acc1 batch service role", re.compile(r"(?<![A-Za-z0-9])aa-bedrock-batch-inference-role")),
    ("the acc1 account env var", re.compile(r"BEDROCK_ACCT_FALLBACK")),
    ("the acc1 CLI profile", re.compile(r"pqnghiep-admin")),
]
# Root docs say acc1 is retired; they may name it, but never route to it. Checked separately below.
ROOT_DOC_ALLOWED = {"the acc1 account key"}


def _files():
    for root in SCANNED_ROOTS:
        base = REPO / root
        if not base.exists():
            continue
        for path in base.rglob("*"):
            rel = path.relative_to(REPO)
            if (not path.is_file() or path.suffix not in SUFFIXES or SKIPPED_DIRS & set(rel.parts)
                    or any(rel.is_relative_to(s) for s in SKIPPED)):
                continue
            yield path


def _hits(path: Path, allowed=frozenset()):
    out = []
    for no, line in enumerate(path.read_text(encoding="utf-8", errors="replace").splitlines(), 1):
        for name, pattern in FORBIDDEN:
            if name not in allowed and pattern.search(line):
                out.append(f"{path.relative_to(REPO)}:{no} {name}: {line.strip()[:120]}")
    return out


def test_runtime_code_never_names_acc1():
    found = [hit for path in _files() for hit in _hits(path)]
    assert not found, ("acc1 left the ecosystem (AA-998): LLM runs on acc3 only. Remove these references:\n"
                       + "\n".join(found))


def test_root_docs_name_no_acc1_resource():
    found = [hit for name in ROOT_FILES if (REPO / name).exists()
             for hit in _hits(REPO / name, allowed=ROOT_DOC_ALLOWED)]
    assert not found, "\n".join(found)


def test_the_scan_sees_runtime_code():
    # A path refactor must not turn the guard into a no-op.
    scanned = {p.relative_to(REPO).as_posix() for p in _files()}
    assert "shared/llm_client/bedrock_satellite.py" in scanned
    assert "frontend/app/admin/cost-ops/page.tsx" in scanned
    assert not any(s.startswith("api/migrations/") for s in scanned)


def test_hashed_account_id_matches_a_12_digit_run(monkeypatch):
    # The positive path, without the real ID: point the hash at a fake one.
    fake = "4" + "2" * 11
    monkeypatch.setitem(globals(), "ACC1_ACCOUNT_ID_SHA256", hashlib.sha256(fake.encode()).hexdigest())
    assert _HashedAccountId.search(f"arn:aws:iam::{fake}:role/AA3-Bedrock-Invoker")
    assert not _HashedAccountId.search(f"x{fake}9")   # 13 digits: not an account ID


def test_patterns_catch_the_forms_that_used_to_exist():
    samples = ['account="acc1"', "ACC1_ACCOUNT_ID = x", "acc3 → acc1", "acc1-test-account",
               "role/AA-Bedrock-Invoker", "AA-CostExplorer-Reader",
               "role/aa-bedrock-batch-inference-role", 'os.environ.get("BEDROCK_ACCT_FALLBACK")',
               "AWS_PROFILE=pqnghiep-admin"]
    for s in samples:
        assert any(p.search(s) for _, p in FORBIDDEN), s
    fake = "4" + "2" * 11       # a 12-digit run that is not the retired ID (built at run time, AA-902)
    assert not _HashedAccountId.search(f"arn:aws:iam::{fake}:role/x")
    for s in ["acc3", "AA3-Bedrock-Invoker", "AA3-CostExplorer-Reader", "aa3-bedrock-batch-inference-role",
              "BEDROCK_ACCT_PRIMARY", "acc10", "pqnghiep1354", "aa365-admin"]:
        assert not any(p.search(s) for _, p in FORBIDDEN), s
