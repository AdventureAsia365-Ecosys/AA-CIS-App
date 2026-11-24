"""AA-1173 — the sealed evidence reader (api/services/compliance_store.py) and its use by compliance.sync, without AWS:
a fake S3 client holds invented manifests and bundles. The newest manifest of a source wins; a bundle that does not
match the SHA-256 in its manifest, or a previous manifest that does not match the link, is refused (SealError -> state
"broken", nothing ingested); no object = None; a valid bundle is ingested with its object key and hash."""
from __future__ import annotations

import asyncio
import io
import json
from datetime import datetime, timezone
from unittest.mock import AsyncMock, patch

import pytest

from api.services import compliance
from api.services import compliance_store as store

NOW = datetime(2026, 11, 24, 4, 0, tzinfo=timezone.utc)
SRC = "aws_nightly"


class FakeS3:
    def __init__(self, objects: dict[str, bytes]):
        self.objects = objects

    def list_objects_v2(self, Bucket, Prefix, MaxKeys=1000, ContinuationToken=None):   # noqa: N803 - boto3 names
        keys = sorted(k for k in self.objects if k.startswith(Prefix))
        return {"Contents": [{"Key": k} for k in keys], "IsTruncated": False}

    def get_object(self, Bucket, Key):   # noqa: N803
        if Key not in self.objects:
            raise KeyError(Key)
        return {"Body": io.BytesIO(self.objects[Key])}


def bundle_bytes(source=SRC, generated="2026-11-24T03:10:00Z") -> bytes:
    return json.dumps({"format": 1, "source": source, "collector": "invented collector", "collector_version": "abc1234",
                       "run_ref": "compliance-aws #7", "generated_at": generated,
                       "checks": [{"check_id": "aws_cloudtrail_on", "status": "pass", "summary": "1 trail logging",
                                   "detail": {"trails": 1}, "link": "https://github.com/ExampleOrg"}]}).encode()


def sealed_run(objects: dict, stamp: str, previous: dict | None, source=SRC, bundle: bytes | None = None,
               sha_override: str | None = None) -> dict:
    day = f"{stamp[:4]}/{stamp[4:6]}/{stamp[6:8]}"
    bkey = f"evidence/{source}/bundles/{day}/{stamp[9:]}Z-1.json"
    body = bundle if bundle is not None else bundle_bytes(source)
    objects[bkey] = body
    manifest = {"format": 1, "source": source, "created_at": "2026-11-24T03:10:05Z", "run_ref": "run 1",
                "collector_version": "abc1234", "previous": previous,
                "objects": [{"key": bkey, "sha256": sha_override or store.sha256(body), "bytes": len(body)}]}
    mkey = f"evidence/{source}/manifest/{day}/{stamp[9:]}Z-1.json"
    raw = json.dumps(manifest).encode()
    objects[mkey] = raw
    return {"key": mkey, "sha256": store.sha256(raw)}


def test_latest_reads_the_newest_manifest_and_checks_the_chain():
    objs: dict = {}
    first = sealed_run(objs, "20261123T031000", None)
    sealed_run(objs, "20261124T031000", first)
    out = store.latest(FakeS3(objs), "b", SRC, NOW)
    assert out is not None and out.chain == "linked"
    assert out.bundle_key == "evidence/aws_nightly/bundles/2026/11/24/031000Z-1.json"
    assert out.bundle_sha256 == store.sha256(out.bundle)


def test_first_manifest_and_previous_month():
    objs: dict = {}
    sealed_run(objs, "20261030T031000", None)
    out = store.latest(FakeS3(objs), "b", SRC, NOW)
    assert out is not None and out.chain == "first"


def test_nothing_sealed_is_none():
    assert store.latest(FakeS3({}), "b", SRC, NOW) is None


def test_a_bundle_that_does_not_match_its_manifest_is_refused():
    objs: dict = {}
    sealed_run(objs, "20261124T031000", None, sha_override="0" * 64)
    with pytest.raises(store.SealError, match="does not match"):
        store.latest(FakeS3(objs), "b", SRC, NOW)


def test_a_rewritten_previous_manifest_breaks_the_chain():
    objs: dict = {}
    first = sealed_run(objs, "20261123T031000", None)
    sealed_run(objs, "20261124T031000", first)
    objs[first["key"]] = objs[first["key"]] + b" "
    with pytest.raises(store.SealError, match="chain broken"):
        store.latest(FakeS3(objs), "b", SRC, NOW)


@pytest.mark.parametrize("manifest", [b"not json", json.dumps({"format": 2}).encode(),
                                      json.dumps({"format": 1, "source": SRC, "objects": []}).encode(),
                                      json.dumps({"format": 1, "source": SRC, "objects": [
                                          {"key": "elsewhere/x.json", "sha256": "a" * 64}]}).encode()])
def test_malformed_manifests_are_refused(manifest):
    with pytest.raises(store.SealError):
        store.parse_manifest(manifest, SRC)


def test_bucket_name_from_the_environment(monkeypatch):
    monkeypatch.setenv(store.BUCKET_ENV, "invented-bucket")
    assert store.bucket() == "invented-bucket"


def test_valid_key():
    assert store.valid_key("evidence/root_daily/bundles/2026/11/24/a.json", "root_daily")
    assert not store.valid_key("evidence/root_daily/../x", "root_daily")
    assert not store.valid_key("evidence/other/x.json", "root_daily")


def test_sync_sealed_ingests_with_the_object_and_refuses_a_broken_one():
    objs: dict = {}
    sealed_run(objs, "20261124T031000", None)
    sealed_run(objs, "20261124T021000", None, source="root_daily",
               bundle=bundle_bytes("root_daily", "2026-11-24T02:10:00Z"), sha_override="1" * 64)
    ingested = []

    async def fake_ingest(pool, rows, hours):
        ingested.extend(rows)
        return len(rows)
    with patch.object(compliance, "ingest", fake_ingest), \
            patch.object(compliance, "_validity_hours", AsyncMock(return_value=48)):
        out = asyncio.run(compliance._sync_sealed(object(), lambda: FakeS3(objs), NOW))
    assert out["aws_nightly"]["state"] == "ok" and out["aws_nightly"]["chain"] == "first"
    assert out["root_daily"]["state"] == "broken"
    assert out["prowler_weekly"]["state"] == "missing"
    assert [r["check_id"] for r in ingested] == ["aws_cloudtrail_on"]
    assert ingested[0]["object_key"].startswith("evidence/aws_nightly/bundles/")
    assert len(ingested[0]["object_sha256"]) == 64


def test_sync_sealed_refuses_a_bundle_of_another_source():
    objs: dict = {}
    sealed_run(objs, "20261124T031000", None, bundle=bundle_bytes("root_daily"))

    with patch.object(compliance, "ingest", AsyncMock(return_value=0)) as ing, \
            patch.object(compliance, "_validity_hours", AsyncMock(return_value=48)):
        out = asyncio.run(compliance._sync_sealed(object(), lambda: FakeS3(objs), NOW))
    assert out["aws_nightly"]["state"] == "invalid" and not ing.await_count


def test_sync_sealed_without_storage_is_unavailable():
    def boom():
        raise RuntimeError("no credentials")
    out = asyncio.run(compliance._sync_sealed(object(), boom, NOW))
    assert {v["state"] for v in out.values()} == {"unavailable"} and set(out) == set(store.SOURCES)


# ── the phase 1 docs reader file never overrides sealed evidence (S290 review) ──────────────────────────────────────


def test_sealed_sources_match_the_store():
    assert compliance.SEALED_SOURCES == store.SOURCES
    assert compliance.DOCS_READER_SOURCE in store.SOURCES


def _docs_reader(raw: bytes | None, sealed: dict):
    ingest = AsyncMock(return_value=1)
    with patch.object(compliance, "_read_published", lambda factory: raw), patch.object(compliance, "ingest", ingest), \
            patch.object(compliance, "_validity_hours", AsyncMock(return_value=48)):
        out = asyncio.run(compliance._sync_docs_reader(object(), lambda: None, NOW, sealed))
    return out, ingest


def test_a_docs_reader_file_claiming_a_sealed_aws_source_is_refused():
    # an overwritten docs reader file says aws_nightly passes; nothing of it may reach compliance.evidence
    out, ingest = _docs_reader(bundle_bytes("aws_nightly"), {"aws_nightly": {"state": "ok"}})
    assert out["state"] == "invalid" and "root_daily" in out["detail"]
    assert not ingest.await_count


def test_the_docs_reader_file_is_not_read_once_the_sealed_root_daily_is_ok():
    def never(factory):
        raise AssertionError("the docs reader file must not be read")
    with patch.object(compliance, "_read_published", never):
        out = asyncio.run(compliance._sync_docs_reader(object(), lambda: None, NOW, {"root_daily": {"state": "ok"}}))
    assert out["state"] == "superseded"


@pytest.mark.parametrize("sealed_state", ["missing", "unavailable", "broken"])
def test_the_docs_reader_root_daily_file_is_still_read_while_nothing_sealed_is_ok(sealed_state):
    out, ingest = _docs_reader(bundle_bytes("root_daily"), {"root_daily": {"state": sealed_state}})
    assert out["state"] == "ok" and out["rows"] == 1 and ingest.await_count == 1
    assert all(r.get("object_key") is None for r in ingest.await_args.args[1])


def test_latest_query_prefers_a_sealed_row_for_sealed_sources():
    sql = " ".join(compliance.SQL_LATEST.split())
    assert "ORDER BY check_id, (object_key IS NULL AND source = ANY($1::text[])), collected_at DESC" in sql
