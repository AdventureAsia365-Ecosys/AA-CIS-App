"""AA-1142 follow-up (S293) — the external checks run by the CIS worker (job kind ``ops_external_check``, source
``worker``): what a customer outside the VPC meets, measured from the worker's internet egress (the NAT, then the
public internet), every ``cis.ops.external_check_interval_min`` minutes.

The same checks as the root repo's ``tools/ops/external_checks.py`` (the GitHub Actions producer, source ``github``),
ported here so both vantage points measure alike. For every target of ``cis.ops.external_check_targets``:

  dns   the host resolves (getaddrinfo, port 443)
  tls   a verified TLS handshake (the system trust store, the host name checked, TLS 1.2 or newer) and the
        certificate's end date
  http  (mode ``health``) one GET of the target's path without following redirects: the status and the time; the
        status must be one of the target's ``expect`` list (a sign-in redirect is an answer, not a failure)

A failed step leaves the later ones unchecked. Each step waits at most ``cis.ops.external_check_timeout_s``; the
targets run in parallel (one thread each), so a run lasts at most about three times that. Public hosts only, names
and numbers only: no secret, no cookie, no request body. Standard library only, $0.

The result rows have the shape :func:`api.services.ops_status.external.parse` returns, so the state is derived and the
rows stored by the same code for both sources (:func:`api.services.ops_status.external.store_run`).
"""
from __future__ import annotations

import asyncio
import http.client
import socket
import ssl
import time
from datetime import datetime, timezone
from typing import Any, Callable, Optional

USER_AGENT = "aa-external-checks/1 (cis-worker)"
READ_BYTES = 65536  # technical-constant: read at most this much of a health answer (the status is what counts)


def _short(e: BaseException) -> str:
    return f"{type(e).__name__}: {e}"[:300]


def cert_end(cert: Optional[dict]) -> Optional[datetime]:
    """The certificate's notAfter as an UTC time (None when absent). Pure."""
    raw = (cert or {}).get("notAfter")
    if not raw:
        return None
    return datetime.fromtimestamp(ssl.cert_time_to_seconds(raw), tz=timezone.utc)


def check_dns(host: str, timeout: float, resolve: Callable[..., Any] = socket.getaddrinfo
              ) -> tuple[bool, Optional[str]]:
    """(resolved, error). getaddrinfo has no time limit of its own: the run's overall limit bounds it."""
    try:
        resolve(host, 443, proto=socket.IPPROTO_TCP)
        return True, None
    except OSError as e:
        return False, _short(e)


def _context() -> ssl.SSLContext:
    ctx = ssl.create_default_context()
    ctx.minimum_version = ssl.TLSVersion.TLSv1_2   # a host that only offers TLS 1.0/1.1 counts as a TLS failure
    return ctx


def check_tls(host: str, timeout: float) -> tuple[Optional[bool], Optional[datetime], Optional[str]]:
    """(tls_ok, cert_not_after, error): False on a refused certificate, None when no connection was made."""
    try:
        with socket.create_connection((host, 443), timeout=timeout) as raw:
            with _context().wrap_socket(raw, server_hostname=host) as s:
                return True, cert_end(s.getpeercert()), None
    except ssl.SSLError as e:   # SSLCertVerificationError included
        return False, None, _short(e)
    except OSError as e:
        return None, None, _short(e)


def check_http(host: str, path: str, timeout: float) -> tuple[Optional[int], Optional[int], Optional[str]]:
    """(status, latency_ms, error) of one GET without following redirects."""
    start = time.monotonic()
    conn = http.client.HTTPSConnection(host, 443, timeout=timeout, context=_context())
    try:
        conn.request("GET", path, headers={"User-Agent": USER_AGENT, "Accept": "application/json, text/html"})
        resp = conn.getresponse()
        resp.read(READ_BYTES)
        return resp.status, int((time.monotonic() - start) * 1000), None
    except (OSError, http.client.HTTPException) as e:
        return None, int((time.monotonic() - start) * 1000), _short(e)
    finally:
        conn.close()


def check_one(t: dict, timeout: float, *, dns: Callable = check_dns, tls: Callable = check_tls,
              get: Callable = check_http) -> dict:
    """One target's result, in the row shape of ``external.parse``. Sync (run it in a thread)."""
    out = {"target": t["id"], "app": t["app"], "host": t["host"], "mode": t["mode"], "dns_ok": None, "tls_ok": None,
           "cert_not_after": None, "http_status": None, "http_ok": None, "latency_ms": None, "error": None}
    ok, err = dns(t["host"], timeout)
    out["dns_ok"] = ok
    if not ok:
        out["error"] = err
        return out
    tls_ok, end, err = tls(t["host"], timeout)
    out.update(tls_ok=tls_ok, cert_not_after=end)
    if not tls_ok:
        out["error"] = err
        return out
    if t["mode"] == "health":
        status, ms, err = get(t["host"], t["path"], timeout)
        out.update(http_status=status, latency_ms=ms, error=err)
        out["http_ok"] = status in t["expect"] if status is not None else None
    return out


def _gave_up(t: dict, timeout: float) -> dict:
    """The result of a target whose check outlived the run's limit (a resolver that never answered)."""
    return {"target": t["id"], "app": t["app"], "host": t["host"], "mode": t["mode"], "dns_ok": None, "tls_ok": None,
            "cert_not_after": None, "http_status": None, "http_ok": None, "latency_ms": None,
            "error": f"TimeoutError: no answer within {3 * timeout:g} s"}


async def run(targets: list[dict], timeout: float, **checks: Callable) -> list[dict]:
    """Every target checked at once, one thread each; a target that outlives three times the step limit is reported
    with nothing checked and its error (a health target then reads down: a customer meets the same hang) and the run
    goes on."""
    async def one(t: dict) -> dict:
        try:
            return await asyncio.wait_for(asyncio.to_thread(check_one, t, timeout, **checks), timeout=3 * timeout)
        except asyncio.TimeoutError:
            return _gave_up(t, timeout)

    return list(await asyncio.gather(*(one(t) for t in targets)))
