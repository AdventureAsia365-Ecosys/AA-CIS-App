"""AA-891 — release-please: the release version on /health, and the release / deploy workflow wiring.

- /health returns ``version`` (next to ``git_sha``) and it equals the manifest (.release-please-manifest.json); the
  FastAPI app version is the same value. The only copy in code is api/version.py, marked for release-please.
- release-please-config.json: manifest mode, bump-minor-pre-major, include-v-in-tag, CHANGELOG.md, extra-files
  holding every file that carries the version.
- release-please.yml: App token (aa-ecosys-ci-bot) minted in environment "release", GITHUB_TOKEN read-only, only on
  push to main + workflow_dispatch, actions pinned to a released major.
- deploy-dev.yml: a release PR (CHANGELOG.md + manifest + api/version.py) does not deploy; the smoke checks version.

Static text checks like test_aa725_image_pin.py / test_aa879_ci_gates.py (no YAML parser in requirements.txt).
"""
import json
import re
from pathlib import Path

from fastapi.testclient import TestClient

REPO = Path(__file__).resolve().parents[2]
MANIFEST = json.loads((REPO / ".release-please-manifest.json").read_text(encoding="utf-8"))
CONFIG = json.loads((REPO / "release-please-config.json").read_text(encoding="utf-8"))
RELEASE_WF = (REPO / ".github" / "workflows" / "release-please.yml").read_text(encoding="utf-8")
DEPLOY_WF = (REPO / ".github" / "workflows" / "deploy-dev.yml").read_text(encoding="utf-8")
SEMVER = re.compile(r"^\d+\.\d+\.\d+$")


def _on_block(text: str) -> str:
    return text.split("\non:\n", 1)[1].split("\npermissions:", 1)[0]


def test_health_returns_the_manifest_version_next_to_git_sha(monkeypatch):
    from api import main

    monkeypatch.setenv("APP_GIT_SHA", "abc123def")
    body = TestClient(main.app).get("/health").json()
    assert list(body)[0] == "status" and body["status"] == "ok"
    assert body["git_sha"] == "abc123def"
    assert body["version"] == MANIFEST["."], (body["version"], MANIFEST)
    assert main.app.version == MANIFEST["."]


def test_manifest_is_one_root_package_with_a_semver():
    assert list(MANIFEST) == ["."] and SEMVER.match(MANIFEST["."]), MANIFEST


def test_every_version_file_is_an_extra_file_with_the_manifest_version():
    pkg = CONFIG["packages"]["."]
    extra = pkg["extra-files"]
    assert "api/version.py" in extra, extra
    for rel in extra:
        lines = (REPO / rel).read_text(encoding="utf-8").splitlines()
        marked = [ln for ln in lines if "x-release-please-version" in ln]
        assert marked, f"{rel} has no x-release-please-version line, release-please would not bump it"
        for ln in marked:
            assert f'"{MANIFEST["."]}"' in ln, (rel, ln, MANIFEST)


def test_no_hardcoded_release_version_left_in_main():
    # AA-891: the version used to be written twice in api/main.py (app + /health); both now read api/version.py.
    text = (REPO / "api" / "main.py").read_text(encoding="utf-8")
    assert not re.search(r'version\W+"\d+\.\d+\.\d+"', text), "api/main.py must use api.version.__version__"


def test_release_please_config_options():
    assert CONFIG["bump-minor-pre-major"] is True
    assert CONFIG["include-v-in-tag"] is True
    pkg = CONFIG["packages"]["."]
    assert pkg["changelog-path"] == "CHANGELOG.md"
    assert pkg["include-component-in-tag"] is False  # tags are vX.Y.Z, not aa-cis-app-vX.Y.Z
    assert "release-as" not in pkg and "release-as" not in CONFIG  # first version comes from the manifest + a feat


def test_release_workflow_triggers_and_permissions():
    on = _on_block(RELEASE_WF)
    assert re.search(r"push:\n\s+branches: \[main\]", on), on
    assert "workflow_dispatch" in on
    assert "pull_request" not in on and "schedule" not in on, on
    perms = RELEASE_WF.split("\npermissions:\n", 1)[1].split("\n\n", 1)[0]
    assert perms.strip() == "contents: read", perms
    assert "environment: release" in RELEASE_WF


def test_release_workflow_uses_the_ci_bot_app_token():
    assert re.search(r"uses: actions/create-github-app-token@v\d+\n", RELEASE_WF)
    assert "app-id: ${{ vars.AA_CI_BOT_APP_ID }}" in RELEASE_WF
    assert "private-key: ${{ secrets.AA_CI_BOT_PRIVATE_KEY }}" in RELEASE_WF
    assert re.search(r"uses: googleapis/release-please-action@v4\n", RELEASE_WF)
    assert "token: ${{ steps.app-token.outputs.token }}" in RELEASE_WF
    assert "secrets.GITHUB_TOKEN" not in RELEASE_WF and "github.token" not in RELEASE_WF


def test_release_pr_files_do_not_trigger_the_deploy():
    paths_block = DEPLOY_WF.split("paths:", 1)[1].split("workflow_dispatch", 1)[0]
    listed = [ln.strip()[1:].strip().strip("'\"") for ln in paths_block.splitlines() if ln.strip().startswith("-")]
    assert "!api/version.py" in listed and "api/**" in listed, listed
    # GitHub applies path patterns in order: the negation only wins when it comes after the pattern it narrows.
    assert listed.index("!api/version.py") > listed.index("api/**"), listed
    positives = [p for p in listed if not p.startswith("!")]
    assert not any(p in ("CHANGELOG.md", ".release-please-manifest.json", "**", "*") for p in positives), positives
    assert all("/" in p or p in ("Dockerfile", "requirements.txt") for p in positives), positives


def test_deploy_smoke_checks_the_manifest_version():
    assert "APP_VERSION=$(jq -r '.\".\"' .release-please-manifest.json)" in DEPLOY_WF
    assert '.version == \\"${APP_VERSION}\\"' in DEPLOY_WF
