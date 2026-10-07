"""AA-725 part 5 — api and worker must roll the SAME image SHA on one deploy.

Root cause (S215): the deploy helper pinned `:latest` for both the api and the worker. `:latest`
is a moving tag — the two services register their task definitions at different moments, so a
second push landing between them would leave api and worker on two different SHAs even though both
task defs read `:latest`.

The fix: `build-and-push` outputs the immutable per-commit image (`:dev-<sha>`), and the deploy
steps pass that exact image to the helper via `DEPLOY_IMAGE` for BOTH services. These are static
facts about the workflow, asserted here so a regression back to `:latest` turns CI red.
"""
import re
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
DEPLOY_WF = (REPO / ".github" / "workflows" / "deploy-dev.yml").read_text(encoding="utf-8")


def test_build_output_is_the_immutable_per_commit_image():
    # The build job must export the per-commit tag (FULL_IMAGE = ...:dev-<sha>), not :latest.
    assert "echo \"image=$FULL_IMAGE\" >> $GITHUB_OUTPUT" in DEPLOY_WF, (
        "build-and-push must output the immutable FULL_IMAGE (dev-<sha>), not $LATEST — "
        "otherwise the deploy pins a moving tag and api/worker can diverge (AA-725 part 5)."
    )


def test_deploy_helper_uses_deploy_image_not_latest():
    # The helper must take the pinned image from DEPLOY_IMAGE, never hardcode `:latest`.
    assert 'NEW_IMAGE="${DEPLOY_IMAGE:?' in DEPLOY_WF, (
        "deploy_family.sh must pin DEPLOY_IMAGE (the per-commit image), not `:latest`."
    )
    assert not re.search(r'NEW_IMAGE=.*:latest"', DEPLOY_WF), (
        "deploy_family.sh must not build NEW_IMAGE from the `:latest` tag (AA-725 part 5)."
    )


def test_both_deploy_steps_pass_the_built_image():
    # Both the api and the worker deploy steps must set DEPLOY_IMAGE from the build output, so a
    # single deploy rolls one SHA across both services.
    passes = re.findall(
        r"DEPLOY_IMAGE:\s*\$\{\{\s*needs\.build-and-push\.outputs\.image\s*\}\}", DEPLOY_WF)
    # api deploy + worker deploy + the post-deploy SHA verification step.
    assert len(passes) >= 3, (
        "Expected the api deploy, worker deploy, and the SHA-verify step to all source "
        f"DEPLOY_IMAGE from build-and-push.outputs.image; found {len(passes)}."
    )


def test_post_deploy_sha_verification_step_exists():
    assert "Verify api/worker same image SHA" in DEPLOY_WF, (
        "A post-deploy step must assert api and worker run the same image SHA (AA-725 part 5 DoD)."
    )
