"""AA-708 — read-only Google Drive client for the CON photo folders (Drive API v3 over httpx).

Credentials come from Secrets Manager (`SECRET_GDRIVE_ID`, default `aa-cis/dev/gdrive-photo-reader`)
or the env var `GDRIVE_API_KEY` for local runs. The secret holds either:
  - `{"api_key": "..."}` — enough because the CON photo folders are shared "anyone with the link"
    (checked 01/10/2026), or
  - a service-account key JSON (`client_email`, `private_key`) — for folders shared only with it.
"""
from __future__ import annotations

import asyncio
import json
import os
import re
import time
from dataclasses import dataclass
from typing import Optional

import httpx

API = "https://www.googleapis.com/drive/v3"
TOKEN_URL = "https://oauth2.googleapis.com/token"
SCOPE = "https://www.googleapis.com/auth/drive.readonly"
FOLDER_MIME = "application/vnd.google-apps.folder"
_FIELDS = "nextPageToken,files(id,name,mimeType,modifiedTime,size,md5Checksum,thumbnailLink)"
THUMB_SIZE = 0   # "=s0" = the original file, byte for byte (checked: 8256x5504, 14.26 MB both ways)
# S207 first full sync: after ~100 downloads in a row Drive answered 403 for every file. Pace the
# downloads and back off on rate-limit answers.
MIN_DOWNLOAD_INTERVAL = 0.5
RETRY_DELAYS = (2, 5, 15, 30)
_RETRY_REASONS = {"rateLimitExceeded", "userRateLimitExceeded", "backendError", "downloadQuotaExceeded"}


class DriveError(Exception):
    """A Drive API failure WITHOUT the request URL (the API key must never reach logs or the DB)."""

    def __init__(self, status: int, reason: str, message: str = ""):
        super().__init__(status, reason, message)
        self.status, self.reason, self.message = status, reason, message

    def __str__(self) -> str:
        return f"Drive API {self.status} {self.reason}: {self.message}"[:300]

    @property
    def retryable(self) -> bool:
        return self.status in (429, 500, 502, 503, 504) or self.reason in _RETRY_REASONS


def _error(r: httpx.Response) -> DriveError:
    reason, message = "", ""
    try:
        err = r.json().get("error", {})
        message = re.sub(r"key=[^&\s'\"]+", "key=***", str(err.get("message", "")))
        reason = (err.get("errors") or [{}])[0].get("reason", "") or str(err.get("status", ""))
    except Exception:
        pass
    return DriveError(r.status_code, reason, message)


@dataclass
class DriveFile:
    id: str
    name: str
    mime_type: str
    modified_time: Optional[str]
    size: Optional[int]
    thumbnail_link: Optional[str] = None

    @property
    def is_folder(self) -> bool:
        return self.mime_type == FOLDER_MIME

    @property
    def is_image(self) -> bool:
        return self.mime_type.startswith("image/")


def load_credentials() -> dict:
    key = os.environ.get("GDRIVE_API_KEY")
    if key:
        return {"api_key": key}
    from shared.secrets import _fetch_secret_sdk, _get_cached
    secret_id = os.environ.get("SECRET_GDRIVE_ID", "aa-cis/dev/gdrive-photo-reader")
    raw = _get_cached("gdrive", lambda: _fetch_secret_sdk(secret_id)).strip()
    return json.loads(raw) if raw.startswith("{") else {"api_key": raw}


class DriveClient:
    def __init__(self, creds: dict, http: Optional[httpx.AsyncClient] = None):
        self.creds = creds
        self.http = http or httpx.AsyncClient(timeout=60, follow_redirects=True)
        self._token: Optional[str] = None
        self._token_exp = 0.0
        self._last_download = 0.0

    async def aclose(self) -> None:
        await self.http.aclose()

    async def _auth(self) -> tuple[dict, dict]:
        """(params, headers) for one request."""
        if "private_key" not in self.creds:
            # Header, not `?key=`: an httpx error message carries the URL, and it was stored as the
            # photo's error and logged (S207).
            return {}, {"X-Goog-Api-Key": self.creds["api_key"]}
        if not self._token or time.time() > self._token_exp - 60:
            import jwt
            now = int(time.time())
            assertion = jwt.encode({"iss": self.creds["client_email"], "scope": SCOPE, "aud": TOKEN_URL,
                                    "iat": now, "exp": now + 3600}, self.creds["private_key"],
                                   algorithm="RS256")
            r = await self.http.post(TOKEN_URL, data={
                "grant_type": "urn:ietf:params:oauth:grant-type:jwt-bearer", "assertion": assertion})
            r.raise_for_status()
            body = r.json()
            self._token, self._token_exp = body["access_token"], time.time() + body.get("expires_in", 3600)
        return {}, {"Authorization": f"Bearer {self._token}"}

    async def list_children(self, folder_id: str) -> list[DriveFile]:
        out: list[DriveFile] = []
        page: Optional[str] = None
        while True:
            params, headers = await self._auth()
            params.update({"q": f"'{folder_id}' in parents and trashed = false", "fields": _FIELDS,
                           "pageSize": 1000, "supportsAllDrives": "true",
                           "includeItemsFromAllDrives": "true"})
            if page:
                params["pageToken"] = page
            r = await self._get(f"{API}/files", params, headers)
            body = r.json()
            for f in body.get("files", []):
                out.append(DriveFile(f["id"], f["name"], f.get("mimeType", ""), f.get("modifiedTime"),
                                     int(f["size"]) if f.get("size") else None, f.get("thumbnailLink")))
            page = body.get("nextPageToken")
            if not page:
                return out

    async def _get(self, url: str, params: dict, headers: dict) -> httpx.Response:
        """GET with retries on rate-limit / server errors. Raises DriveError (no URL in it)."""
        for delay in (*RETRY_DELAYS, None):
            try:
                r = await self.http.get(url, params=params, headers=headers)
            except httpx.HTTPError as e:   # network: message has no query string, but keep it short
                err = DriveError(0, type(e).__name__, "")
            else:
                if r.status_code < 400:
                    return r
                err = _error(r)
                if not err.retryable:
                    raise err
            if delay is None:
                raise err
            await asyncio.sleep(delay)
        raise AssertionError("unreachable")

    async def download(self, file_id: str, thumbnail_link: Optional[str] = None) -> bytes:
        """The image bytes. S207: Drive blocks `alt=media` from the ECS NAT IP for a while after ~100
        downloads in a row (403, every file). The listing's thumbnailLink is served by the image host
        (lh3.googleusercontent.com, not the Drive API quota); "=s0" returns the ORIGINAL file (same
        size and bytes as alt=media) — use it first, `alt=media` only when there is no link or the
        image host fails."""
        wait = self._last_download + MIN_DOWNLOAD_INTERVAL - time.monotonic()
        if wait > 0:
            await asyncio.sleep(wait)
        self._last_download = time.monotonic()
        if thumbnail_link:
            url = re.sub(r"=s\d+(-c)?$", "", thumbnail_link) + f"=s{THUMB_SIZE}"
            try:
                r = await self.http.get(url)
                if r.status_code == 200 and r.headers.get("content-type", "").startswith("image/"):
                    return r.content
            except httpx.HTTPError:
                pass
        params, headers = await self._auth()
        params["alt"] = "media"
        return (await self._get(f"{API}/files/{file_id}", params, headers)).content


async def walk(client: DriveClient, root_id: str, max_depth: int = 3) -> list[tuple[list[str], DriveFile]]:
    """Every image under `root_id` with the folder names leading to it (root itself excluded)."""
    found: list[tuple[list[str], DriveFile]] = []
    stack: list[tuple[str, list[str]]] = [(root_id, [])]
    while stack:
        folder_id, path = stack.pop()
        for f in await client.list_children(folder_id):
            if f.is_folder and len(path) < max_depth:
                stack.append((f.id, path + [f.name.strip()]))
            elif f.is_image:
                found.append((path, f))
    return found
