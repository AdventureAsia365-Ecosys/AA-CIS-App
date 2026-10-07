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


def test_build_output_is_the_per_commit_tag_not_full_image():
    # The build job must export the per-commit TAG (dev-<sha>), not the full image. The full image
    # embeds the ECR_REGISTRY secret, which GitHub strips from job outputs, leaving DEPLOY_IMAGE
    # empty downstream. The tag carries no secret and survives.
    assert "echo \"tag=$IMAGE_TAG\" >> $GITHUB_OUTPUT" in DEPLOY_WF, (
        "build-and-push must output the per-commit tag (dev-<sha>), not :latest or the full image "
        "(the full image contains the ECR_REGISTRY secret and gets dropped) — AA-725 part 5."
    )
    assert "echo \"image=$LATEST\"" not in DEPLOY_WF, (
        "build-and-push must not output the moving `:latest` image (AA-725 part 5)."
    )


def test_deploy_image_is_resolved_from_tag_plus_registry_env():
    # The deploy job must rebuild the full image from the tag output + its own ECR env, not take
    # it from a job output (which would be stripped as a secret).
    assert "DEPLOY_IMAGE=${ECR_REGISTRY}/${ECR_REPOSITORY}:${IMAGE_TAG:?tag not set}" in DEPLOY_WF, (
        "The deploy job must resolve DEPLOY_IMAGE from the per-commit tag plus ECR_REGISTRY/"
        "ECR_REPOSITORY env (AA-725 part 5)."
    )
    assert "needs.build-and-push.outputs.tag" in DEPLOY_WF, (
        "The deploy job must consume build-and-push.outputs.tag."
    )


def test_deploy_helper_uses_deploy_image_not_latest():
    # The helper must take the pinned image from DEPLOY_IMAGE, never hardcode `:latest`.
    assert 'NEW_IMAGE="${DEPLOY_IMAGE:?' in DEPLOY_WF, (
        "deploy_family.sh must pin DEPLOY_IMAGE (the per-commit image), not `:latest`."
    )
    assert not re.search(r'NEW_IMAGE=.*:latest"', DEPLOY_WF), (
        "deploy_family.sh must not build NEW_IMAGE from the `:latest` tag (AA-725 part 5)."
    )


def test_post_deploy_sha_verification_step_exists():
    assert "Verify api/worker same image SHA" in DEPLOY_WF, (
        "A post-deploy step must assert api and worker run the same image SHA (AA-725 part 5 DoD)."
    )
