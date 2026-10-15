"""Keep the E2E test tenants' key copies in Secrets Manager in sync with a key rotation (S237).

A tenant API key is shown once and only its sha256 is stored (shared.tenants.api_key_hash), so nothing
else can learn a new key. The four E2E test tenants (atlas-hearth, terra-family, trail-pulse,
wildkind-travel) also have a hand-made copy at `aa-cis/dev/e2e-tenant/<slug>` that UI verify, paid
test runs and Playwright read to sign in as the tenant. When an admin rotated such a key, the copy went
stale and every test sign-in failed with 401 (S237, atlas-hearth at 07:11Z).

Rule: after a key is issued (generate-key, restore), if a secret `aa-cis/dev/e2e-tenant/<slug>` already
exists, its value is replaced with the new key. The app never creates these secrets, so a real tenant
(no such secret) is never written and its key still exists only on the admin's screen. IAM: the task
role may only DescribeSecret + PutSecretValue under that prefix (AA-CIS-Infra modules/ecs).

Runs only on the real ECS deployment (AWS_EXECUTION_ENV set, the AA-577 signal); local and CI return
"skipped" without touching AWS. Never raises: a failed sync is logged and reported, the key is still
returned to the admin.
"""
from __future__ import annotations

import asyncio
import os
from typing import Optional

import structlog

logger = structlog.get_logger()

SECRET_PREFIX = "aa-cis/dev/e2e-tenant/"
SYNCED, NOT_TEST_TENANT, SKIPPED, FAILED = "synced", "not_test_tenant", "skipped", "failed"


def _client():
    import boto3  # lazy: unit tests and local runs never import it on this path

    return boto3.client("secretsmanager", region_name=os.environ.get("AWS_REGION", "us-west-1"))


def _sync(slug: str, api_key: str) -> str:
    client = _client()
    name = SECRET_PREFIX + slug
    try:
        client.describe_secret(SecretId=name)
    except client.exceptions.ResourceNotFoundException:
        return NOT_TEST_TENANT
    client.put_secret_value(SecretId=name, SecretString=api_key)
    return SYNCED


async def sync_test_tenant_key(slug: Optional[str], api_key: str) -> str:
    """Returns "synced", "not_test_tenant", "skipped" (no slug, or not on ECS) or "failed". Never raises."""
    if not slug or not os.environ.get("AWS_EXECUTION_ENV"):
        return SKIPPED
    try:
        status = await asyncio.to_thread(_sync, slug, api_key)
    except Exception as exc:  # noqa: BLE001 — a sync problem must never lose the key the admin just got
        logger.warning("e2e_tenant_key_sync_failed", slug=slug, error=type(exc).__name__)
        return FAILED
    if status == SYNCED:
        logger.info("e2e_tenant_key_synced", slug=slug, secret=SECRET_PREFIX + slug)
    return status


def sync_note(status: str, slug: Optional[str]) -> str:
    """One sentence for the admin's response message, empty when there is nothing to say."""
    if status == SYNCED:
        return f" The E2E test copy {SECRET_PREFIX}{slug} was updated."
    if status == FAILED:
        return (f" Updating the E2E test copy {SECRET_PREFIX}{slug} failed: put the new key there by hand "
                "or test sign-ins will fail.")
    return ""
