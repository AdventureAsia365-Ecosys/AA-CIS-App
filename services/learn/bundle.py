"""The AA Academy learn bundle, read from S3 (AA-1105, root ADR 0022 decision 1; format: root
tools/docs/build_learn_bundle.py, manifest format 1).

The root repo's Docs sync uploads the bundle under the docs reader prefix at ``learn/`` (``<prefix>/learn/...`` in the
bronze bucket, the prefix the CIS task role reads: ``api/services/docs_reader.py``):

  manifest.json                      paths, modules, lessons (with video keys), cited sections; never an answer
  lessons/<module folder>/<file>.md  lesson bodies (and ``.vi.md`` translations)
  sections/<kind>/<name>/<a>.md      the redacted text of each cited section
  quizzes/<module id>.json           the quiz items WITH their answers: read here, server side only

Objects are only ever read by a key the manifest names, after a shape check, never by a key a caller typed. The
manifest is cached in process for :data:`CACHE_TTL_S`; bodies and quizzes are cached per key (bounded) and keyed by
the manifest's source sha, so a new bundle is read fresh. ``LearnUnavailable`` -> 503 (no bundle yet, storage or
format); a missing object the manifest names is ``LearnUnavailable`` too (the bundle is inconsistent).

Lesson videos (decision D8) are NOT in the bundle: a lesson's ``video`` names object keys under ``learn-media/`` of the
same bucket, which :func:`presign` turns into short-lived GET URLs after the API's access check.
"""
from __future__ import annotations

import asyncio
import json
import re
import time
from collections import OrderedDict
from dataclasses import dataclass, field
from typing import Any, Optional

import structlog

from api.services import docs_reader

logger = structlog.get_logger()

SUPPORTED_FORMAT = 1
LEARN_DIR = "learn"
CACHE_TTL_S = 300  # technical-constant: in-process refresh of the published bundle, like the docs reader
MAX_MANIFEST_BYTES = 5 * 1024 * 1024  # technical-constant: refuse an absurd object (the builder writes ~100 KB)
MAX_BODY_BYTES = 2 * 1024 * 1024  # technical-constant: same bound as a docs page
BODY_CACHE_SIZE = 128  # technical-constant: bodies and quizzes kept in process

_LESSON_KEY = re.compile(r"^lessons/[A-Za-z0-9._-]+/[0-9]{2}-[a-z0-9-]+(?:\.vi)?\.md$")
_SECTION_KEY = re.compile(r"^sections/[a-z]+/[A-Za-z0-9._-]+/[A-Za-z0-9._-]+\.md$")
_QUIZ_KEY = re.compile(r"^quizzes/[A-Z][0-9]{1,2}\.json$")
_MEDIA_KEY = re.compile(r"^learn-media/[A-Z][0-9]{1,2}/[0-9]{2}-[a-z0-9-]+(?:\.(?:mp4|webm)|\.(?:en|vi)\.vtt)$")


class LearnUnavailable(Exception):
    """No bundle published yet, storage failed, or an object is not in the expected shape (HTTP 503)."""


@dataclass
class Bundle:
    """One published learn bundle, indexed."""

    manifest: dict
    paths: dict[str, dict] = field(default_factory=dict)
    modules: dict[str, dict] = field(default_factory=dict)
    lessons: dict[str, tuple[dict, dict]] = field(default_factory=dict)   # lesson id -> (module, lesson)
    sections: dict[str, dict] = field(default_factory=dict)               # ref -> section entry

    @property
    def sha(self) -> Optional[str]:
        source = self.manifest.get("source") or {}
        value = source.get("sha") if isinstance(source, dict) else None
        return value[:64] if isinstance(value, str) and value else None


def index(manifest: Any) -> Bundle:
    """Validate the manifest's shape and index it; LearnUnavailable when it is not format 1."""
    if not isinstance(manifest, dict) or manifest.get("format") != SUPPORTED_FORMAT:
        raise LearnUnavailable("the learn bundle is not in a supported format")
    try:
        bundle = Bundle(manifest=manifest)
        bundle.paths = {p["id"]: p for p in manifest["paths"]}
        bundle.modules = {m["id"]: m for m in manifest["modules"]}
        for module in manifest["modules"]:
            for lesson in module["lessons"]:
                bundle.lessons[lesson["id"]] = (module, lesson)
        bundle.sections = {s["ref"]: s for s in manifest.get("sections", [])}
    except (KeyError, TypeError) as exc:
        raise LearnUnavailable("the learn bundle manifest is malformed") from exc
    return bundle


_cache: dict[str, Any] = {"bundle": None, "at": -1e18}
_bodies: "OrderedDict[tuple[str, str], Any]" = OrderedDict()


def clear_cache() -> None:
    _cache.update(bundle=None, at=-1e18)
    _bodies.clear()


def install(bundle: Optional[Bundle]) -> None:
    """Tests: serve this bundle (bodies come from :func:`install_body`) without S3."""
    _cache.update(bundle=bundle, at=time.monotonic() + 1e9 if bundle else -1e18)


def install_body(bundle: Bundle, key: str, value: Any) -> None:
    _bodies[(bundle.sha or "", key)] = value


def _get(key: str, limit: int) -> Optional[bytes]:
    client = docs_reader._s3_client()
    return docs_reader._read_bytes(client, docs_reader._bucket(), f"{docs_reader.s3_prefix()}/{LEARN_DIR}/{key}",
                                   limit)


async def load() -> Bundle:
    """The published bundle (cached CACHE_TTL_S)."""
    now = time.monotonic()
    if _cache["bundle"] is not None and now - _cache["at"] < CACHE_TTL_S:
        return _cache["bundle"]
    try:
        raw = await asyncio.to_thread(_get, "manifest.json", MAX_MANIFEST_BYTES)
    except Exception as exc:  # noqa: BLE001 - storage problems are 503, never a 500
        logger.error("learn_bundle_read_failed", error=type(exc).__name__)
        raise LearnUnavailable("the learn bundle cannot be read") from exc
    if raw is None:
        raise LearnUnavailable("no learn bundle is published yet")
    try:
        bundle = index(json.loads(raw))
    except ValueError as exc:
        raise LearnUnavailable("the learn bundle manifest is not JSON") from exc
    _cache.update(bundle=bundle, at=now)
    return bundle


async def _body(bundle: Bundle, key: str, pattern: re.Pattern, as_json: bool) -> Any:
    if not isinstance(key, str) or not pattern.match(key):
        raise LearnUnavailable("the learn bundle names an object of an unexpected shape")
    cache_key = (bundle.sha or "", key)
    if cache_key in _bodies:
        _bodies.move_to_end(cache_key)
        return _bodies[cache_key]
    try:
        raw = await asyncio.to_thread(_get, key, MAX_BODY_BYTES)
    except Exception as exc:  # noqa: BLE001
        logger.error("learn_object_read_failed", key=key, error=type(exc).__name__)
        raise LearnUnavailable("a learn bundle object cannot be read") from exc
    if raw is None:
        raise LearnUnavailable("a learn bundle object is missing")
    try:
        value = json.loads(raw) if as_json else raw.decode("utf-8")
    except ValueError as exc:
        raise LearnUnavailable("a learn bundle object is malformed") from exc
    _bodies[cache_key] = value
    while len(_bodies) > BODY_CACHE_SIZE:
        _bodies.popitem(last=False)
    return value


async def lesson_body(bundle: Bundle, key: str) -> str:
    return await _body(bundle, key, _LESSON_KEY, as_json=False)


async def section_body(bundle: Bundle, key: str) -> str:
    return await _body(bundle, key, _SECTION_KEY, as_json=False)


async def quiz(bundle: Bundle, module: dict) -> list[dict]:
    """The module's quiz items WITH answers (server side only); [] when the module has no quiz."""
    entry = module.get("quiz")
    if not entry:
        return []
    data = await _body(bundle, entry.get("key"), _QUIZ_KEY, as_json=True)
    items = data.get("items") if isinstance(data, dict) else None
    if not isinstance(items, list):
        raise LearnUnavailable("a learn quiz is malformed")
    return [i for i in items if isinstance(i, dict) and isinstance(i.get("id"), str)]


def media_key_ok(key: Any) -> bool:
    return isinstance(key, str) and bool(_MEDIA_KEY.match(key))


def _presign_sync(key: str, ttl_s: int, check: bool) -> Optional[str]:
    client = docs_reader._s3_client()
    bucket = docs_reader._bucket()
    if check:
        try:
            client.head_object(Bucket=bucket, Key=key)
        except Exception as err:  # noqa: BLE001
            if docs_reader._is_missing(err):
                return None
            raise
    return client.generate_presigned_url("get_object", Params={"Bucket": bucket, "Key": key}, ExpiresIn=ttl_s)


async def presign(key: str, ttl_s: int, *, check: bool = False) -> Optional[str]:
    """A presigned GET URL of a learn-media object (None when ``check`` and it does not exist)."""
    if not media_key_ok(key):
        raise LearnUnavailable("the lesson names a video key of an unexpected shape")
    try:
        return await asyncio.to_thread(_presign_sync, key, ttl_s, check)
    except Exception as exc:  # noqa: BLE001
        logger.error("learn_media_presign_failed", error=type(exc).__name__)
        raise LearnUnavailable("the lesson video cannot be reached") from exc
