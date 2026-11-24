"""AA-1173 (S290, compliance readiness phase 2) — read the sealed evidence of Platform › Compliance from the Object Lock
evidence bucket (AA-CIS-Infra accounts/aa365/compliance_evidence.tf; tech lead S287 Q6 / Q8).

The collectors (the root daily workflow, the Infra nightly AWS checks and the weekly Prowler scan) assume the role
``aa-cis-<env>-compliance-evidence`` and write WRITE-ONCE objects under one prefix (the bucket policy refuses a
PutObject without ``If-None-Match: *``, every delete, and any retention change; the role may not delete either):

  evidence/<source>/bundles/<yyyy>/<mm>/<dd>/<HHMMSS>Z-<run>.json    the evidence bundle (format 1, the shape of the
                                                                     docs reader file: compliance.parse_bundle)
  evidence/<source>/raw/<yyyy>/<mm>/<dd>/<HHMMSS>Z-<run>/<name>      the raw output (Prowler findings, AWS answers):
                                                                     private, never read here
  evidence/<source>/manifest/<yyyy>/<mm>/<dd>/<HHMMSS>Z-<run>.json   the run's manifest: {"format": 1, "source",
                                                                     "created_at", "run_ref", "collector_version",
                                                                     "previous": {"key", "sha256"} | null,
                                                                     "objects": [{"key", "sha256", "bytes"}]}

Each manifest names the previous manifest of the same source with its SHA-256 (a hash chain per source: removing or
rewriting an older manifest breaks the link of the next one), and lists the SHA-256 of every object of its run
(the writer: tools/compliance/seal.py, the same file in the root repo and AA-CIS-Infra).

    latest(client, bucket, source, now) -> Sealed | None    the newest manifest of a source (this month, else the
                                                            month before), its bundle bytes verified against the
                                                            manifest, and the chain link to the previous manifest
                                                            checked; raises SealError when a hash does not match

The reader needs ListBucket on evidence/ and GetObject on evidence/* of the bucket (AA-CIS-Infra, the CIS task role,
read only). The bucket name is never written in this public repo: COMPLIANCE_EVIDENCE_BUCKET when set, else
``aa-cis-dev-compliance-evidence-<the caller's account>`` (the account read from STS, like the bronze bucket,
shared/aws_client/bronze.py). No bucket configured or no object yet = None (the page shows the state, never an error).
"""
from __future__ import annotations

import hashlib
import json
import os
import re
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Optional

PREFIX = "evidence"
BUCKET_ENV = "COMPLIANCE_EVIDENCE_BUCKET"
BUCKET_PREFIX = "aa-cis-dev-compliance-evidence-"
SOURCES = ("root_daily", "aws_nightly", "prowler_weekly")   # the sources the collectors seal
MANIFEST_FORMAT = 1
MAX_MANIFEST_BYTES = 256 * 1024   # technical-constant: a run lists a handful of objects
MAX_BUNDLE_BYTES = 1024 * 1024    # technical-constant: the same bound as the docs reader file (compliance.py)
_SOURCE = re.compile(r"^[a-z][a-z0-9_]{1,40}$")
_KEY = re.compile(r"^evidence/[a-z][a-z0-9_]{1,40}/[A-Za-z0-9._/-]{1,240}$")
_SHA = re.compile(r"^[0-9a-f]{64}$")


class SealError(Exception):
    """A sealed object does not match its manifest, or the chain link to the previous manifest is broken."""


@dataclass(frozen=True)
class Sealed:
    source: str
    manifest_key: str
    manifest_sha256: str
    bundle_key: str
    bundle_sha256: str
    bundle: bytes
    chain: str                      # "linked" (previous manifest found and matching) | "first" (no previous)
    created_at: Optional[str]


def bucket() -> str:
    name = os.environ.get(BUCKET_ENV, "").strip()
    if name:
        return name
    from shared.aws_client.bronze import caller_account_id   # noqa: PLC0415 - STS only when no name is set

    return f"{BUCKET_PREFIX}{caller_account_id()}"


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def valid_key(key: Any, source: Optional[str] = None) -> bool:
    if not isinstance(key, str) or not _KEY.fullmatch(key) or ".." in key:
        return False
    return source is None or key.startswith(f"{PREFIX}/{source}/")


def _month_prefixes(source: str, now: datetime) -> list[str]:
    first = now.replace(day=1)
    before = first - timedelta(days=1)
    return [f"{PREFIX}/{source}/manifest/{d:%Y/%m}/" for d in (first, before)]


def _newest_keys(client: Any, bucket_name: str, prefix: str, limit: int = 2) -> list[str]:
    keys: list[str] = []
    token = None
    while True:
        args = {"Bucket": bucket_name, "Prefix": prefix, "MaxKeys": 1000}
        if token:
            args["ContinuationToken"] = token
        page = client.list_objects_v2(**args)
        keys.extend(o["Key"] for o in page.get("Contents") or [] if isinstance(o.get("Key"), str))
        if not page.get("IsTruncated"):
            break
        token = page.get("NextContinuationToken")
    return sorted(keys)[-limit:][::-1]


def _get(client: Any, bucket_name: str, key: str, limit: int) -> bytes:
    obj = client.get_object(Bucket=bucket_name, Key=key)
    body = obj["Body"].read(limit + 1)
    if len(body) > limit:
        raise SealError(f"{key} is over the {limit} byte limit")
    return body


def parse_manifest(raw: bytes, source: str) -> dict:
    """The manifest as a dict, or SealError when it is not a format 1 manifest of ``source``."""
    try:
        m = json.loads(raw)
    except ValueError as e:
        raise SealError("the manifest is not JSON") from e
    if not isinstance(m, dict) or m.get("format") != MANIFEST_FORMAT or m.get("source") != source:
        raise SealError(f"the manifest is not a format {MANIFEST_FORMAT} manifest of {source}")
    objects = m.get("objects")
    if not isinstance(objects, list) or not objects:
        raise SealError("the manifest lists no object")
    for o in objects:
        if not (isinstance(o, dict) and valid_key(o.get("key"), source) and isinstance(o.get("sha256"), str)
                and _SHA.fullmatch(o["sha256"])):
            raise SealError("the manifest lists an object without a valid key and SHA-256")
    prev = m.get("previous")
    if prev is not None and not (isinstance(prev, dict) and valid_key(prev.get("key"), source)
                                 and isinstance(prev.get("sha256"), str) and _SHA.fullmatch(prev["sha256"])):
        raise SealError("the manifest's previous link is malformed")
    return m


def latest(client: Any, bucket_name: str, source: str, now: datetime) -> Optional[Sealed]:
    """The newest sealed bundle of ``source``; None when the source has sealed nothing in this or the last month."""
    if not _SOURCE.fullmatch(source):
        raise ValueError(f"bad source {source!r}")
    keys: list[str] = []
    for prefix in _month_prefixes(source, now):
        keys = _newest_keys(client, bucket_name, prefix)
        if keys:
            break
    if not keys:
        return None
    key = keys[0]
    raw = _get(client, bucket_name, key, MAX_MANIFEST_BYTES)
    m = parse_manifest(raw, source)
    bundles = [o for o in m["objects"] if o["key"].startswith(f"{PREFIX}/{source}/bundles/")]
    if len(bundles) != 1:
        raise SealError(f"the manifest lists {len(bundles)} bundles (one expected)")
    bundle = _get(client, bucket_name, bundles[0]["key"], MAX_BUNDLE_BYTES)
    if sha256(bundle) != bundles[0]["sha256"]:
        raise SealError("the bundle does not match the SHA-256 in its manifest")
    chain = "first"
    prev = m.get("previous")
    if prev is not None:
        prev_raw = _get(client, bucket_name, prev["key"], MAX_MANIFEST_BYTES)
        if sha256(prev_raw) != prev["sha256"]:
            raise SealError("the previous manifest does not match the link of the newest one (chain broken)")
        chain = "linked"
    return Sealed(source=source, manifest_key=key, manifest_sha256=sha256(raw), bundle_key=bundles[0]["key"],
                  bundle_sha256=bundles[0]["sha256"], bundle=bundle, chain=chain,
                  created_at=m.get("created_at") if isinstance(m.get("created_at"), str) else None)
