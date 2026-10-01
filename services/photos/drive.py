"""AA-708 — read-only Google Drive client for the CON photo folders (Drive API v3 over httpx).

Credentials come from Secrets Manager (`SECRET_GDRIVE_ID`, default `aa-cis/dev/gdrive-photo-reader`)
or the env var `GDRIVE_API_KEY` for local runs. The secret holds either:
  - `{"api_key": "..."}` — enough because the CON photo folders are shared "anyone with the link"
    (checked 01/10/2026), or
  - a service-account key JSON (`client_email`, `private_key`) — for folders shared only with it.
"""
from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass
from typing import Optional

import httpx

API = "https://www.googleapis.com/drive/v3"
TOKEN_URL = "https://oauth2.googleapis.com/token"
SCOPE = "https://www.googleapis.com/auth/drive.readonly"
FOLDER_MIME = "application/vnd.google-apps.folder"
_FIELDS = "nextPageToken,files(id,name,mimeType,modifiedTime,size,md5Checksum)"


@dataclass
class DriveFile:
    id: str
    name: str
    mime_type: str
    modified_time: Optional[str]
    size: Optional[int]

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

    async def aclose(self) -> None:
        await self.http.aclose()

    async def _auth(self) -> tuple[dict, dict]:
        """(params, headers) for one request."""
        if "private_key" not in self.creds:
            return {"key": self.creds["api_key"]}, {}
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
            r = await self.http.get(f"{API}/files", params=params, headers=headers)
            r.raise_for_status()
            body = r.json()
            for f in body.get("files", []):
                out.append(DriveFile(f["id"], f["name"], f.get("mimeType", ""), f.get("modifiedTime"),
                                     int(f["size"]) if f.get("size") else None))
            page = body.get("nextPageToken")
            if not page:
                return out

    async def download(self, file_id: str) -> bytes:
        params, headers = await self._auth()
        params["alt"] = "media"
        r = await self.http.get(f"{API}/files/{file_id}", params=params, headers=headers)
        r.raise_for_status()
        return r.content


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
