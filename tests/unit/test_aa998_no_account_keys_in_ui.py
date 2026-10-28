"""AA-998 — no admin page prints an internal AWS account key ("acc2", "acc3", or the retired "acc1").

Pages name an account by its 12-digit AWS account ID, which the API serves at run time (GET /admin/aws-accounts,
shared/aws_client/accounts.py) because this repo is public and carries no ID. frontend/lib/awsAccounts.ts is the
one file that spells the keys (as constants, plus accountName() / accountLabel()); every other frontend string
literal, template literal, JSX text or identifier that holds a key fails here. Comments are not scanned (they may
name the keys to explain the code).
"""
import re
from pathlib import Path

from tests.unit.ui_text_scan import strip_ts_comments, ts_hits

REPO = Path(__file__).resolve().parents[2]
ACCOUNT_KEY = re.compile(r"(?<![A-Za-z0-9_])acc[0-9]+(?![0-9])")
ROOTS = ("frontend/app", "frontend/lib", "frontend/components")
ALLOWED = {Path("frontend/lib/awsAccounts.ts")}
SKIPPED_DIRS = {"node_modules", ".next"}


def _files():
    for root in ROOTS:
        for path in sorted((REPO / root).rglob("*")):
            rel = path.relative_to(REPO)
            if (path.is_file() and path.suffix in {".ts", ".tsx"} and ".test." not in path.name
                    and rel not in ALLOWED and not SKIPPED_DIRS & set(rel.parts)):
                yield path


def test_frontend_never_spells_an_account_key():
    found = [hit for path in _files() for hit in ts_hits(path, ACCOUNT_KEY, REPO)]
    assert not found, ("A page would print an internal AWS account key (AA-998). Show the account ID instead: "
                       "accountName() / accountLabel() from frontend/lib/awsAccounts.ts and useAwsAccounts():\n"
                       + "\n".join(found))


def test_the_one_allowed_file_defines_the_keys():
    text = strip_ts_comments((REPO / "frontend/lib/awsAccounts.ts").read_text(encoding="utf-8"))
    assert 'MAIN_ACCOUNT = "acc2"' in text and 'LLM_ACCOUNT = "acc3"' in text
    assert "acc1" not in text


def test_the_scan_sees_the_account_pages():
    scanned = {p.relative_to(REPO).as_posix() for p in _files()}
    for page in ("frontend/app/admin/cost-ops/page.tsx", "frontend/app/admin/platform/cost/CostByApp.tsx",
                 "frontend/app/admin/settings/ModelsTab.tsx", "frontend/app/admin/jobs/JobDrawer.tsx"):
        assert page in scanned, page


def test_the_pattern():
    for s in ['"acc3"', "acc2 (main infra)", "`${x} · acc1`", "{ acc3?: Line }"]:
        assert ACCOUNT_KEY.search(s), s
    for s in ["ai_in_aws_acc2_usd", "billed_in_aws_acc2", "account", "accent", "accept"]:
        assert not ACCOUNT_KEY.search(s), s
