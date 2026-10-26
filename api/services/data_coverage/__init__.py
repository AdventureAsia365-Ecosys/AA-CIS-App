"""AA-992 — the admin "Data coverage" page (/admin/data-coverage): live counts from the raw upload to TripPlanner,
Booking and the partner CRM, numbers only (S252 design, AA-Ecosys docs/audits/2026-10-26-S252-data-coverage/).

- :mod:`.defs` — the shared definitions (live tour, active / place atom, served destination);
- :mod:`.queries` — one SQL statement per source;
- :mod:`.registry` — the sources and the rows, in page order;
- :mod:`.sources` — running one source with its own transaction, timeouts and error mapping;
- :mod:`.build` — tiers, cache, row status, the response.
"""
from .build import build as build_data_coverage  # noqa: F401
