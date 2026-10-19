"""shared/aws_client/bronze.py — the CIS bronze bucket name and the caller's AWS account (AA-902).

This repo is public, so no AWS account ID is written into the code. The bronze bucket's name embeds
the account ID by convention (`aa-cis-bronze-<account>`); the code gets it at run time instead:

  1. BRONZE_BUCKET — set on the api and worker task definitions (AA-CIS-Infra modules/ecs). This is
     the path every ECS caller takes, including scripts run in the container by ECS exec.
  2. Otherwise `aa-cis-bronze-<caller account>`, the account read once from STS GetCallerIdentity
     (no IAM permission needed). This covers runs outside ECS with the admin profile or a GitHub
     Actions OIDC role in the same account (the eval regression workflow).

Tests set BRONZE_BUCKET in tests/unit/conftest.py and tests/integration/conftest.py, so no test
calls STS.
"""
from __future__ import annotations

import os
from functools import lru_cache

import boto3

BRONZE_BUCKET_PREFIX = "aa-cis-bronze-"


@lru_cache(maxsize=1)
def caller_account_id() -> str:
    """AWS account of the current credentials (the ECS task role's account on ECS)."""
    region = os.environ.get("AWS_REGION", "us-west-1")
    return boto3.client("sts", region_name=region).get_caller_identity()["Account"]


def bronze_bucket() -> str:
    """Name of the CIS bronze bucket: BRONZE_BUCKET, else derived from the caller's account."""
    name = os.environ.get("BRONZE_BUCKET", "").strip()
    if name:
        return name
    return f"{BRONZE_BUCKET_PREFIX}{caller_account_id()}"
