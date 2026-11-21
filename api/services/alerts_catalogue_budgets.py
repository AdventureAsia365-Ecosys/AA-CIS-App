"""AA-1171 (S287 alerts catalogue P0, decision D7) — the budget and cost anomaly rows of Platform › Alerts › Catalogue.

acc2 budgets come from the Platform › Infra read of the Terraform docs output (:func:`main_budget_rows`); the budgets
and anomaly subscriptions of acc1, acc3 and the management account are static from their Terraform roots
(:data:`EXTERNAL_BUDGETS`, no cross-account read role), with each account's ID inserted at run time only
(``shared.aws_client.accounts``, env ``AWS_MGMT_ACCOUNT_ID``); no account ID is written in this public repo. Split out
of api/services/alerts_catalogue.py (file size limit).
"""
from __future__ import annotations

import os
import re
from typing import Callable, Optional

from api.services.alerts_catalogue_rows import (
    EDIT_TERRAFORM, INFRA_REPO, TYPE_ANOMALY, TYPE_BUDGET, row as _row,
)

# ──────────────────────────────────────────────────────────────────────────────────────

# D7: the budgets and anomaly subscriptions of the other accounts, read from their Terraform roots (accounts/
# acc1-bedrock/cost_guard.tf, accounts/acc3-bedrock/cost_guard.tf + cost_anomaly_immediate.tf, accounts/management/
# budgets.tf + cost_anomaly_app_tag.tf). Account keys resolve to IDs at run time (no ID in this public repo).
# A change of one of these values in Terraform needs the matching change here (tests/unit/test_aa1171_alerts_catalogue
# checks the names against the generated alarm page when it is published; the root PR keeps them in step).
EXTERNAL_BUDGETS: tuple[dict, ...] = (
    {"account": "acc1", "name": "aa1-llm-monthly-total", "type": TYPE_BUDGET,
     "condition": "monthly cost of the LLM serving account", "threshold": "ACTUAL > 50 %, 80 %, 100 %; "
     "FORECASTED > 100 % of 100 USD", "file": "accounts/acc1-bedrock/cost_guard.tf", "channel": ["e-mail (direct)"]},
    {"account": "acc1", "name": "aa1-cost-anomaly-daily", "type": TYPE_ANOMALY,
     "condition": "cost anomaly of the LLM serving account (daily digest)", "threshold": "impact >= 10 USD",
     "file": "accounts/acc1-bedrock/cost_guard.tf", "channel": ["e-mail (direct)"]},
    {"account": "acc3", "name": "aa3-llm-monthly-total", "type": TYPE_BUDGET,
     "condition": "monthly cost of the LLM fallback account", "threshold": "ACTUAL > 80 %, 100 %; FORECASTED > 100 % "
     "of 100 USD", "file": "accounts/acc3-bedrock/cost_guard.tf", "channel": ["e-mail (direct)"]},
    {"account": "acc3", "name": "aa3-cost-anomaly-daily", "type": TYPE_ANOMALY,
     "condition": "cost anomaly of the LLM fallback account (daily digest)", "threshold": "impact >= 10 USD",
     "file": "accounts/acc3-bedrock/cost_guard.tf", "channel": ["e-mail (direct)"]},
    {"account": "acc3", "name": "aa3-cost-anomaly-immediate", "type": TYPE_ANOMALY,
     "condition": "cost anomaly of the LLM fallback account (immediate)", "threshold": "impact >= 15 USD",
     "file": "accounts/acc3-bedrock/cost_anomaly_immediate.tf", "channel": ["SNS topic aa3-cost-alerts (e-mail)"]},
    {"account": "mgmt", "name": "aa-org-monthly", "type": TYPE_BUDGET,
     "condition": "monthly cost of the whole company organization", "threshold": "ACTUAL > 50 %, 80 %, 100 %; "
     "FORECASTED > 100 % of the organization budget", "file": "accounts/management/budgets.tf",
     "channel": ["e-mail (direct)"]},
    {"account": "mgmt", "name": "<account>-monthly", "type": TYPE_BUDGET,
     "condition": "monthly cost of each member account and of the management account",
     "threshold": "ACTUAL > 80 %, 100 %; FORECASTED > 100 % of each account's budget",
     "file": "accounts/management/budgets.tf", "channel": ["e-mail (direct)"]},
    {"account": "mgmt", "name": "<account>-anomaly-daily", "type": TYPE_ANOMALY,
     "condition": "cost anomaly of each account of the organization (daily digest)", "threshold": "impact >= 15 USD",
     "file": "accounts/management/budgets.tf", "channel": ["e-mail (direct)"]},
)
EXTERNAL_ROLE = {"acc1": "LLM serving account", "acc3": "LLM fallback account", "mgmt": "management account"}


def external_account_id(key: str) -> Optional[str]:
    """The 12-digit ID of an external account at run time, or None (never from this repo)."""
    if key == "mgmt":
        value = (os.environ.get("AWS_MGMT_ACCOUNT_ID") or "").strip()
        return value if re.fullmatch(r"[0-9]{12}", value) else None
    try:
        from shared.aws_client import accounts

        return accounts.account_id(key)
    except Exception:  # noqa: BLE001 — a label never fails the page
        return None


def external_budget_rows(as_of: str, ids: Callable[[str], Optional[str]] = external_account_id) -> list[dict]:
    rows = []
    for b in EXTERNAL_BUDGETS:
        acct = ids(b["account"])
        rows.append(_row(
            id=f"budget:{b['account']}:{b['name']}", name=b["name"], app="ecosystem", component="billing",
            type=b["type"], source="budgets", severity="ticket", mutable=False, condition=b["condition"],
            threshold=b["threshold"], threshold_source=f"Terraform {b['file']} (AA-CIS-Infra)",
            editable_via=EDIT_TERRAFORM,
            edit_href=f"https://github.com/{INFRA_REPO}/blob/main/{b['file']}", channel=list(b["channel"]),
            state="not_live", account_id=acct, freshness=as_of,
            coverage_gap=("Shown from Terraform: its live state is only in that account's console "
                          f"({EXTERNAL_ROLE[b['account']]})."),
            description=f"{EXTERNAL_ROLE[b['account']]}: {b['condition']}."))
    return rows


def main_budget_rows(infra: dict, as_of: str, account_id: Optional[str]) -> list[dict]:
    """acc2 budgets from the Platform › Infra read (the Terraform docs output): the total budget with its alert lines,
    one row per app budget, the immediate cost anomaly subscription. Pure."""
    group = next((g for g in infra.get("groups") or [] if g.get("group") == "budgets"), None)
    if not group:
        return []
    items = {re.sub(r"^acc\d+_", "", str(i.get("key", ""))): i for i in group.get("items") or []}
    src = "Terraform accounts/aa365/observability.tf (AA-CIS-Infra)"
    href = f"https://github.com/{INFRA_REPO}/blob/main/accounts/aa365/observability.tf"
    rows = []
    total = items.get("total_monthly_usd")
    if total is not None:
        alerts = items.get("total_alerts", {}).get("value") or "ACTUAL > 80 %, 100 %; FORECASTED > 100 %"
        rows.append(_row(id="budget:acc2:monthly-total", name="monthly-total", app="shared", component="billing",
                         type=TYPE_BUDGET, source="budgets", severity="ticket", mutable=False,
                         condition="monthly cost of the main account (every app together)",
                         threshold=f"{alerts} of {total.get('value')} USD", threshold_source=src,
                         editable_via=EDIT_TERRAFORM, edit_href=href, channel=["alerts topic"], state="not_live",
                         account_id=account_id, freshness=as_of))
    for key, item in sorted(items.items()):
        if not key.startswith("per_app_monthly_usd."):
            continue
        app = key.split(".", 1)[1]
        rows.append(_row(id=f"budget:acc2:monthly-app-{app}", name=f"monthly-app-{app}", app=app, component="billing",
                         type=TYPE_BUDGET, source="budgets", severity="ticket", mutable=False,
                         condition=f"monthly cost of the resources tagged App = {app}",
                         threshold=f"ACTUAL > 80 %; FORECASTED > 100 % of {item.get('value')} USD",
                         threshold_source=src, editable_via=EDIT_TERRAFORM, edit_href=href,
                         channel=["alerts topic"], state="not_live", account_id=account_id, freshness=as_of))
    anomaly = items.get("cost_anomaly_alert_usd")
    if anomaly is not None:
        rows.append(_row(id="budget:acc2:cost-anomaly", name="cost-anomaly-immediate", app="shared",
                         component="billing", type=TYPE_ANOMALY, source="budgets", severity="ticket", mutable=False,
                         condition="cost anomaly of the main account (immediate)",
                         threshold=f"impact >= {anomaly.get('value')} USD",
                         threshold_source="Terraform accounts/aa365/cost_anomaly.tf (AA-CIS-Infra)",
                         editable_via=EDIT_TERRAFORM,
                         edit_href=f"https://github.com/{INFRA_REPO}/blob/main/accounts/aa365/cost_anomaly.tf",
                         channel=["alerts topic"], state="not_live", account_id=account_id, freshness=as_of))
    for r in rows:
        r["coverage_gap"] = "Live spend against the budget is on Spend › AWS bill, not here."
    return rows
