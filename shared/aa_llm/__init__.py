"""aa_llm: the shared, pure core of the AI gateway (AA-642 S8, root ADR 0009 decision 13).

Canonical in AA-CIS-App ``shared/aa_llm/``; copied byte for byte into AA-TripPlanner-Web ``backend/aa_llm/`` and
AA-Partner ``api/app/aa_llm/`` by ``.github/scripts/llm-core-sync.sh`` (each copy pinned to a CIS commit by its
``aa_llm.lock.json`` and checked offline against ``MANIFEST.json`` by ``check_copy.py``). Never edit a copy: change
AA-CIS-App, merge, then sync.

Modules (standard library only, relative imports only, no I/O, no secrets, no provider SDK):
- ``errors``: error classes and ``classify``.
- ``pricing``: ``quote`` of one billed request from catalog prices the caller read.
- ``ledger``: ``LedgerRow`` (ledger v2) and ``insert_sql``.
- ``budget``: ``BudgetDecision`` (fail closed).
- ``ports``: the Protocols an app implements (config source, ledger sink, budget store, client factory).
- ``contract``: names and SELECTs of the CIS contract views for TripPlanner (CIS migration 323).
"""

CORE_VERSION = "1.0.0"
