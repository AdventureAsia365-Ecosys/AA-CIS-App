"""The seams an app plugs into the shared core (root ADR 0009 decision 13; AA-642 S8).

The core never opens a connection, reads a secret or picks an account: each app supplies these four.

- ``ConfigSource``: the route of a stage (models in order, prices, deployments). CIS reads ``shared.llm_*``;
  TripPlanner reads ``catalog.v_llm_gateway_config_v1``; the CRM reads its own settings in ``crm`` (ADR 0008a).
- ``LedgerSink``: writes ``ledger.LedgerRow`` rows. CIS and TripPlanner write ``shared.llm_call_log``; the CRM writes
  ``crm.llm_call`` inside its tenant transaction (it never writes ``shared.*``).
- ``BudgetStore``: today's spend of the app (TripPlanner: ``catalog.v_llm_app_spend_today_v1``).
- ``ClientFactory``: a provider client (``boto3.client``-shaped), so tests inject fakes and the core imports no SDK.

Transactions and connections stay with the caller: a sink or store receives the connection or pool it should use.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any, Protocol, runtime_checkable

from .ledger import LedgerRow


@runtime_checkable
class ConfigSource(Protocol):
    async def load_route(self, db: Any, stage: str) -> Any:
        """The stage's route; raises when it cannot be read (the caller then fails closed)."""
        ...


@runtime_checkable
class LedgerSink(Protocol):
    async def write(self, db: Any, rows: Sequence[LedgerRow]) -> None:
        """Writes the rows; never raises for a lost row (it logs an error instead)."""
        ...


@runtime_checkable
class BudgetStore(Protocol):
    async def spent_today(self, db: Any, stages: Sequence[str]) -> float:
        """Today's (UTC) spend of the app over ``stages``; raises when it cannot be read."""
        ...


@runtime_checkable
class ClientFactory(Protocol):
    def __call__(self, service: str, **kwargs: Any) -> Any: ...
