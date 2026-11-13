"""AA-1093 part 3 — where a tenant backup lives: the CIS gold bucket, prefix exports/tenants/<tenant>/cis/<backup>/.

The bucket's `exports-expire` lifecycle rule deletes everything under `exports/` after 7 days (AA-CIS-Infra
modules/s3), which is the D11 retention of a backup; the bucket encrypts every object at rest (SSE-S3) and the
writes here ask for it explicitly. The CIS task role holds s3:PutObject / GetObject / ListBucket on every bucket of
the account, so the backup needs no new grant.

This repo is public, so no account ID is written here: GOLD_BUCKET when set, else `aa-cis-gold-<caller account>`
(the bucket naming of AA-CIS-Infra, as shared/aws_client/bronze.py does for bronze).
"""
from __future__ import annotations

import asyncio
import os
from typing import Optional, Protocol

GOLD_BUCKET_PREFIX = "aa-cis-gold-"
SSE = "AES256"


def gold_bucket() -> str:
    name = os.environ.get("GOLD_BUCKET", "").strip()
    if name:
        return name
    from shared.aws_client.bronze import caller_account_id

    return f"{GOLD_BUCKET_PREFIX}{caller_account_id()}"


class ObjectMissing(Exception):
    """The object is not there (the 7-day rule took it, or it was never written)."""


class ObjectStore(Protocol):
    async def put_file(self, key: str, path: str, content_type: str) -> None: ...

    async def get_file(self, key: str, path: str) -> None: ...

    async def list_keys(self, prefix: str) -> list[str]: ...


class S3Store:
    """boto3 behind asyncio.to_thread (the worker's event loop must not block on an upload)."""

    def __init__(self, bucket: Optional[str] = None, client=None):
        self.bucket = bucket or gold_bucket()
        if client is None:
            import boto3

            client = boto3.client("s3", region_name=os.environ.get("AWS_REGION", "us-west-1"))
        self.client = client

    async def put_file(self, key: str, path: str, content_type: str) -> None:
        await asyncio.to_thread(self.client.upload_file, path, self.bucket, key,
                                ExtraArgs={"ServerSideEncryption": SSE, "ContentType": content_type})

    async def get_file(self, key: str, path: str) -> None:
        def _get():
            try:
                self.client.download_file(self.bucket, key, path)
            except Exception as e:  # botocore ClientError 404 / NoSuchKey
                code = str(getattr(e, "response", {}).get("Error", {}).get("Code", ""))
                if code in ("404", "NoSuchKey", "NotFound"):
                    raise ObjectMissing(key) from None
                raise
        await asyncio.to_thread(_get)

    async def list_keys(self, prefix: str) -> list[str]:
        def _list():
            keys, token = [], None
            while True:
                kw = {"Bucket": self.bucket, "Prefix": prefix, "MaxKeys": 1000}
                if token:
                    kw["ContinuationToken"] = token
                page = self.client.list_objects_v2(**kw)
                keys.extend(o["Key"] for o in page.get("Contents") or [])
                if not page.get("IsTruncated"):
                    return keys
                token = page.get("NextContinuationToken")
        return await asyncio.to_thread(_list)


class DirStore:
    """A local directory as the store (tests, and a dry look at a backup on a laptop)."""

    def __init__(self, root: str):
        self.root = root

    def _path(self, key: str) -> str:
        return os.path.join(self.root, *key.split("/"))

    async def put_file(self, key: str, path: str, content_type: str) -> None:
        import shutil

        dest = self._path(key)
        os.makedirs(os.path.dirname(dest), exist_ok=True)
        shutil.copyfile(path, dest)

    async def get_file(self, key: str, path: str) -> None:
        import shutil

        src = self._path(key)
        if not os.path.exists(src):
            raise ObjectMissing(key)
        shutil.copyfile(src, path)

    async def list_keys(self, prefix: str) -> list[str]:
        out = []
        for dirpath, _, files in os.walk(self.root):
            for f in files:
                key = os.path.relpath(os.path.join(dirpath, f), self.root).replace(os.sep, "/")
                if key.startswith(prefix):
                    out.append(key)
        return sorted(out)
