"""AA-1039 R6 slice 2 (root ADR 0019 d20, TL-20): the tenant e-mail about a retired or restored tour.

Recipients: the agency's owner and content members (``shared.tenant_members`` rows ``active`` with Content studio
role ``owner`` or ``editor``; a ``viewer`` is not mailed). CIS keeps no address (the table holds the partner-pool
identity ``(cognito_iss, cognito_sub)`` only, AA-978), so the address is read at send time from the partner pool
(``cognito-idp:ListUsers`` with ``sub = "<sub>"``, the read-only grant of AA-CIS-Infra ``identity_partner_read.tf``;
ListUsers does not count as a Cognito MAU). Only a verified address is used; a member of another pool is skipped.
Nothing is stored: the notice row keeps ids and counts only.

Wording is content-style (TL-20): the tour, the stop date, what the system already did, when each WordPress page
goes to draft, which Facebook posts to edit, a replacement tour if any. Never "flag", "QA" or an internal code.
"""
from __future__ import annotations

import asyncio
import html
import os
from dataclasses import dataclass
from typing import Any, Optional
from urllib.parse import urlparse

import structlog

from shared.aws_client.ses_mailer import Message

logger = structlog.get_logger()

MEMBERS_SQL = """
    SELECT m.cognito_iss, m.cognito_sub, m.content_studio_role
      FROM shared.tenant_members m
     WHERE m.tenant_id = $1::uuid AND m.status = 'active' AND m.content_studio_role IN ('owner', 'editor')
     ORDER BY (m.content_studio_role = 'owner') DESC, m.created_at, m.membership_id
"""
TENANT_SQL = "SELECT name, is_active, cancelled_at FROM shared.tenants WHERE tenant_id = $1::uuid"
MAX_RECIPIENTS = 20


class RecipientsUnavailable(Exception):
    """The partner pool could not be read (no grant yet, throttled, unreachable)."""


@dataclass(frozen=True)
class PoolRef:
    region: str
    pool_id: str
    issuer: str


def partner_pool() -> Optional[PoolRef]:
    """The partner pool of the configured issuer (``https://cognito-idp.<region>.amazonaws.com/<pool id>``)."""
    from api.core.partner_identity import partner_issuer

    issuer = partner_issuer()
    parsed = urlparse(issuer)
    host_parts = (parsed.hostname or "").split(".")
    pool_id = parsed.path.strip("/")
    if len(host_parts) < 2 or host_parts[0] != "cognito-idp" or not pool_id or "/" in pool_id:
        return None
    return PoolRef(region=host_parts[1], pool_id=pool_id, issuer=issuer)


def cognito_client(region: str):
    import boto3  # lazy: only the mail job needs it
    from botocore.config import Config

    return boto3.client("cognito-idp", region_name=region, config=Config(
        connect_timeout=3, read_timeout=5, retries={"max_attempts": 2, "mode": "standard"}))


def _verified_email(user: dict) -> Optional[str]:
    attrs = {a.get("Name"): a.get("Value") for a in user.get("Attributes") or []}
    email = (attrs.get("email") or "").strip().lower()
    if email and str(attrs.get("email_verified", "")).lower() == "true" and "@" in email:
        return email
    return None


async def recipients(pool, tenant_id: str, *, client: Any = None) -> list[str]:
    """The verified addresses of the tenant's owner and content members, owner first, deduplicated."""
    ref = partner_pool()
    if ref is None:
        raise RecipientsUnavailable("partner pool issuer is not a Cognito user pool")
    async with pool.acquire() as conn:
        members = await conn.fetch(MEMBERS_SQL, tenant_id)
    client = client or cognito_client(ref.region)
    out: list[str] = []
    for m in members:
        if m["cognito_iss"] != ref.issuer or not m["cognito_sub"]:
            continue
        sub = str(m["cognito_sub"]).replace("\\", "").replace('"', "")
        try:
            resp = await asyncio.to_thread(client.list_users, UserPoolId=ref.pool_id, Filter=f'sub = "{sub}"', Limit=1)
        except Exception as exc:  # noqa: BLE001 — botocore ClientError / network: retried by the next digest
            raise RecipientsUnavailable(type(exc).__name__) from exc
        for user in resp.get("Users") or []:
            email = _verified_email(user)
            if email and email not in out:
                out.append(email)
        if len(out) >= MAX_RECIPIENTS:
            break
    return out


async def tenant_name(pool, tenant_id: str) -> Optional[str]:
    async with pool.acquire() as conn:
        row = await conn.fetchrow(TENANT_SQL, tenant_id)
    if row is None or not row["is_active"] or row["cancelled_at"] is not None:
        return None
    return row["name"]


def _studio_link() -> Optional[str]:
    url = (os.environ.get("CONTENT_STUDIO_URL") or "").strip().rstrip("/")
    return url if url.startswith("https://") else None


def _retired_lines(n: dict) -> list[str]:
    s = n.get("summary") or {}
    lines = [f"Adventure Asia stopped offering {n['tour_name']} on {s.get('stop_date') or 'this week'}."]
    did = ["new articles and posts about this tour can no longer be published from your Content studio"]
    if s.get("publishes_cancelled"):
        did.append(f"{s['publishes_cancelled']} publish request(s) still waiting were cancelled")
    lines.append("What we already did: " + "; ".join(did) + ". Everything you made stays in your account.")
    for page in s.get("pages") or []:
        lines.append(f"Your page {page.get('url')} will be moved to draft on {page.get('draft_on')}. You can rewrite "
                     "it around another Adventure Asia tour, or remove it, before then.")
    posts = s.get("posts") or []
    if posts:
        lines.append("Please edit or remove these posts (nothing is deleted for you):")
        lines += [f"  - {p.get('url')}" for p in posts]
    if s.get("replacement"):
        lines.append(f"A tour you could feature instead: {s['replacement']}.")
    return lines


def _restored_lines(n: dict) -> list[str]:
    s = n.get("summary") or {}
    lines = [f"Good news: {n['tour_name']} is back on sale with Adventure Asia from {s.get('back_on') or 'today'}.",
             "You can publish new articles and posts about it again from your Content studio."]
    # AA-1039: the pages we moved to draft while the tour was off sale (the restore's republish jobs fill these lists;
    # a page still waiting is never named: the mail goes once none waits, and the digest backstop moves the rest to
    # pages_kept first)
    for page in s.get("pages_back") or []:
        lines.append(f"Your page {page.get('url')}, which we moved to draft while the tour was off sale, is online "
                     "again.")
    for page in s.get("pages_kept") or []:
        lines.append(f"Your page {page.get('url')} is still in draft: we could not put it back online for you. You can "
                     "publish it again from WordPress when it is ready.")
    return lines


def _section(n: dict) -> list[str]:
    return _retired_lines(n) if n["event"] == "retired" else _restored_lines(n)


def render(notices: list[dict], *, to: tuple[str, ...], agency: str) -> Message:
    """One e-mail for one tenant: a single notice (immediate) or the day's digest (several)."""
    if len(notices) == 1:
        n = notices[0]
        subject = (f"{n['tour_name']} is no longer offered by Adventure Asia" if n["event"] == "retired"
                   else f"{n['tour_name']} is back on sale")
    else:
        subject = f"Adventure Asia tour updates for {agency} ({len(notices)})"
    link = _studio_link()
    text = [f"Hello {agency},", ""]
    body = ["<div style=\"font-family:Arial,sans-serif;font-size:14px;color:#222\">",
            f"<p>Hello {html.escape(agency)},</p>"]
    for n in notices:
        section = _section(n)
        text += section + [""]
        body.append("<p>" + "<br>".join(html.escape(line) for line in section) + "</p>")
    if link:
        text.append(f"Open your Content studio: {link}")
        body.append(f"<p><a href=\"{html.escape(link)}\">Open your Content studio</a></p>")
    text += ["", "Adventure Asia"]
    body.append("<p>Adventure Asia</p></div>")
    return Message(to=to, subject=subject[:200], text="\n".join(text), html="".join(body))


__all__ = ["RecipientsUnavailable", "partner_pool", "recipients", "render", "tenant_name"]
