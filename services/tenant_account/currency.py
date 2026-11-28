"""AA-1191 (CIS 496) — the enabled currencies: which currencies the platform shows and accepts.

One administrator-edited key, ``cis.billing.enabled_currencies`` (Platform › Settings, maker-checker, audited; tech lead
S295: USD only for now). USD is the base currency and always enabled, even when the stored list leaves it out (every
price has a USD column). A currency that is not enabled:

* is hidden: the admin routes serve ``enabled_currencies`` and leave the VND-only figures out, the frontend hides the
  pickers, VND inputs and columns;
* is refused on write: :func:`problem` names the reason, the routes answer 422 ``currency_not_enabled``;
* is not billed in: a tenant whose stored ``billing_currency`` is not enabled is quoted in USD (:func:`effective`);
  the stored value stays and comes back when the currency is enabled again (no column is dropped, no row converted).

Booking reads the same value through ``catalog.v_billing_currencies_v1`` (CIS 497), the partner CRM through
``GET /catalog/v1/currencies`` (catalog.http.v1 1.5). Pure helpers take the list; :func:`enabled` /
:func:`aenabled` read the value in force (``shared.settings``), never a copied literal.
"""
from __future__ import annotations

from typing import Any, Iterable, Optional

from shared import settings

ENABLED_CURRENCIES = "cis.billing.enabled_currencies"
# technical-constant: the base currency, priced in every plan's USD columns (not a policy value: it is never disabled)
BASE = "USD"
CODE = "currency_not_enabled"


def normalise(values: Optional[Iterable[Any]]) -> tuple[str, ...]:
    """USD first, then every other code of ``values`` in its order, upper-cased, without duplicates. Pure."""
    out = [BASE]
    for v in values or ():
        code = str(v).strip().upper() if isinstance(v, str) else ""
        if code and code not in out:
            out.append(code)
    return tuple(out)


def enabled() -> tuple[str, ...]:
    """The enabled currencies in force (sync: the reader's last snapshot). A build whose compiled defaults do not know
    the key yet (CIS 496 not merged) serves the 496 default, USD only."""
    try:
        return normalise(settings.get(ENABLED_CURRENCIES))
    except KeyError:
        return (BASE,)


async def aenabled() -> tuple[str, ...]:
    try:
        return normalise(await settings.aget(ENABLED_CURRENCIES))
    except KeyError:
        return (BASE,)


def cache_seconds() -> int:
    """How long a reader outside CIS (the partner CRM) may keep the answer: the key's own cache window."""
    try:
        return int(settings.definition(ENABLED_CURRENCIES).cache_s)
    except KeyError:
        return int(settings.Definition.__dataclass_fields__["cache_s"].default)


def effective(stored: Optional[str], allowed: Iterable[str]) -> str:
    """The currency a tenant is quoted and billed in: its stored one while enabled, else USD. Pure."""
    code = (stored or "").strip().upper()
    return code if code and code in tuple(allowed) else BASE


def problem(code: Optional[str], allowed: Iterable[str], *, field: str = "currency") -> Optional[str]:
    """Why a write in ``code`` is refused (None = accepted; None for an empty value too: nothing is written in a
    currency). Pure; the message is the one the API returns."""
    if code is None or code == "":
        return None
    allowed = tuple(allowed)
    if str(code).strip().upper() in allowed:
        return None
    return (f"{field}: {code} is not an enabled currency (enabled: {', '.join(allowed)}). An administrator can "
            "enable it on Platform › Settings › Enabled currencies.")
