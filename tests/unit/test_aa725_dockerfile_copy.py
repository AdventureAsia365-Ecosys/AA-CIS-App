"""AA-725 part 4 — a new top-level app module must be COPY'd into the image and listed in the
deploy path filter, so it cannot silently ship missing.

Regression context:
 - S210: `worker/` was added but had no `COPY worker/` line, so the worker image was missing its
   own entrypoint package.
 - A top-level module missing from the `deploy-dev.yml` push path filter means edits to it do not
   trigger a deploy at all.

Both are static facts about the repo, checked here against the real Dockerfile and workflow.
"""
import re
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
DOCKERFILE = REPO / "Dockerfile"
DEPLOY_WF = REPO / ".github" / "workflows" / "deploy-dev.yml"

# Top-level dirs that are NOT part of the runtime image and must not be required to have a COPY
# line (tests/scripts are dev-only; frontend ships via Vercel, not this image).
_NOT_IN_IMAGE = {"tests", "scripts", "frontend", "migrations", "docs", "infra", "load"}


def _top_level_python_modules() -> set[str]:
    """A top-level dir is an app module if it directly contains any .py file."""
    mods = set()
    for child in REPO.iterdir():
        if not child.is_dir() or child.name.startswith(".") or child.name.startswith("__"):
            continue
        if child.name in _NOT_IN_IMAGE:
            continue
        if any(p.suffix == ".py" for p in child.iterdir() if p.is_file()):
            mods.add(child.name)
    return mods


def _dockerfile_copied_dirs() -> set[str]:
    copied = set()
    for line in DOCKERFILE.read_text(encoding="utf-8").splitlines():
        # Matches:  COPY api/ ./api/
        m = re.match(r"\s*COPY\s+([A-Za-z0-9_]+)/\s+\./", line)
        if m:
            copied.add(m.group(1))
    return copied


def _deploy_path_filter_dirs() -> set[str]:
    """Dirs named in the `paths:` filter of the push trigger, e.g. `- 'api/**'`."""
    dirs = set()
    for line in DEPLOY_WF.read_text(encoding="utf-8").splitlines():
        m = re.match(r"\s*-\s*'([A-Za-z0-9_]+)/\*\*'", line)
        if m:
            dirs.add(m.group(1))
    return dirs


def test_every_runtime_module_is_copied_into_the_image():
    modules = _top_level_python_modules()
    copied = _dockerfile_copied_dirs()
    missing = sorted(modules - copied)
    assert not missing, (
        "Top-level app module(s) with no `COPY <dir>/ ./<dir>/` line in the Dockerfile — the image "
        f"will be missing them at runtime (S210): {missing}\n"
        f"(modules seen: {sorted(modules)}; copied: {sorted(copied)})"
    )


def test_every_runtime_module_is_in_the_deploy_path_filter():
    modules = _top_level_python_modules()
    filtered = _deploy_path_filter_dirs()
    missing = sorted(modules - filtered)
    assert not missing, (
        "Top-level app module(s) not listed in the deploy-dev.yml push `paths:` filter — editing "
        f"them would not trigger a Dev deploy: {missing}\n"
        f"(modules seen: {sorted(modules)}; path filter: {sorted(filtered)})"
    )
