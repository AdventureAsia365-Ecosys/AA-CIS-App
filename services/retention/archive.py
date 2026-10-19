"""AA-825 — archive a batch of rows to S3 before it is deleted (DM-7.3, C5.4a).

Format: gzipped JSON Lines, one `to_jsonb(row)` per line (every column, Postgres' own JSON for
timestamps / numerics / jsonb). Not Parquet: pyarrow is not a dependency of this image (see
docs/implementation-notes/AA-825.md); a later Athena CTAS can convert the prefix.

Key: archive/<table>/<yyyy>/<mm>/<job_id>-<run>-<part>-b<NNNNN>.jsonl.gz — the runner never puts rows
of two UTC months in one object, so the yyyy/mm partition is exact. <run> is the UTC start time of
this attempt, so a retry of the same job (batch numbers restart) never overwrites an object of an
earlier attempt whose rows are already deleted. Re-running after a failed delete writes a new object
for the same rows; duplicates in the archive are harmless, a lost row is not.

Verified before the DELETE (S239): the PUT carries Content-MD5, so S3 refuses a body that arrived
damaged, and a HEAD of the new key must report the exact byte length and row count; otherwise
`ArchiveNotVerified` is raised inside the batch transaction and nothing of that batch is deleted.
"""
from __future__ import annotations

import asyncio
import base64
import gzip
import hashlib
from datetime import datetime, timezone
from typing import Optional

from services.retention.policy import ARCHIVE_PREFIX
from shared.aws_client.bronze import bronze_bucket


def object_key(archive_name: str, month_of: datetime, job_id: str, run: str, part: str, batch_no: int) -> str:
    m = month_of.astimezone(timezone.utc)
    return f"{ARCHIVE_PREFIX}/{archive_name}/{m:%Y}/{m:%m}/{job_id}-{run}-{part}-b{batch_no:05d}.jsonl.gz"


def encode(docs: list[str]) -> bytes:
    return gzip.compress(("\n".join(docs) + "\n").encode("utf-8"))


def content_md5(body: bytes) -> str:
    """Base64 MD5 for the Content-MD5 header (an integrity check S3 enforces, not security)."""
    return base64.b64encode(hashlib.md5(body, usedforsecurity=False).digest()).decode("ascii")


class ArchiveNotVerified(Exception):
    """The archive object is missing or differs from what was written; the batch is not deleted."""


class S3Archiver:
    """Writes one object per batch. `s3` is a boto3 S3 client (or a test double with put_object)."""

    def __init__(self, s3, job_id: str, bucket: Optional[str] = None, run: Optional[str] = None):
        self.s3 = s3
        self.job_id = job_id
        self.bucket = bucket or bronze_bucket()
        self.run = run or datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")

    async def put(self, archive_name: str, part: str, batch_no: int, month_of: datetime,
                  docs: list[str]) -> str:
        key = object_key(archive_name, month_of, self.job_id, self.run, part, batch_no)
        body = encode(docs)
        await asyncio.to_thread(
            self.s3.put_object, Bucket=self.bucket, Key=key, Body=body, ContentMD5=content_md5(body),
            ContentType="application/x-ndjson", ContentEncoding="gzip",
            ServerSideEncryption="AES256", Metadata={"rows": str(len(docs)), "source": "aa-825"},
        )
        head = await asyncio.to_thread(self.s3.head_object, Bucket=self.bucket, Key=key)
        size = (head or {}).get("ContentLength")
        rows = ((head or {}).get("Metadata") or {}).get("rows")
        if size != len(body) or rows != str(len(docs)):
            raise ArchiveNotVerified(
                f"s3://{self.bucket}/{key}: HEAD says {size} bytes / rows={rows!r}, "
                f"wrote {len(body)} bytes / {len(docs)} rows")
        return key
