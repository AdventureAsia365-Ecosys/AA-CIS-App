"""Migrations the conftest replay runs only in part (moved out of conftest.py, AA-1143 S285: its size ratchet).

Each file below has a part that needs 245's owners and the catalog recipe, which the replay database does not run. The
replay runs only the part the code or the other tests need, in its own transaction and NOT registered, so the file's
own test module can apply the whole file the DB Migrate way.
"""

# file prefix -> (marker that opens the part, marker that closes it). The rest of the opening marker's line is dropped.
PARTS = {
    # AA-645 d06: 273's catalog part needs 253-258 (skipped by the replay) and the catalog recipe. Part 1: the
    # published_tours.updated_at column the publish code writes (test_aa645_migration_273 applies the whole file).
    "273_": ("-- ── 1. The column", "GRANT CREATE ON SCHEMA catalog"),
    # AA-986 L3/L4: 274's part B (the catalog views) needs 253-258 and the catalog recipe. Part A: shared.notifications
    # under RLS, which the tenant-scoped /v1/notifications reads (test_aa986_migration_274).
    "274_": ("-- ── Part A", "-- ── Part B"),
    # AA-835: 278's part B (catalog.v_supply_content) needs 245's owners and the catalog recipe. Part A: the
    # shared.supply_* tables the S6 code writes (test_aa835_migration_278).
    "278_": ("-- ── Part A", "-- ── Part B"),
    # AA-851: 279's part B (catalog.v_country, v_destination.country_iso2) needs 245's owners, 253-258 and the catalog
    # recipe. Part A: shared.countries, the resolver, raw_tours / destinations country_iso2 + trigger, the orphan
    # report (test_aa851_migration_279).
    "279_": ("-- ── Part A", "-- ── Part B"),
    # AA-1143 (S285): 456's seed and catalog.v_llm_connection_cap_v2 need 245's owners and the catalog recipe. Its
    # settings-seed block only: tripplanner.spend.connection_share_enforce, a compiled default the settings tests
    # (test_aa1096_migration_414 / 416, test_s279_migration_407) compare with (test_aa1143_migration_455_456).
    "456_": ("-- settings-seed:begin", "-- settings-seed:end"),
    # AA-1160 (S289): 474's Part B (catalog.v_tenant_tripplanner_v1) needs 379 / 390 / 429 / 438 (skipped by the replay)
    # and the catalog recipe. Part A: shared.tenant_crm_agency, which the CIS code writes (the CRM's snapshot read, the
    # CRM onboarding) and the Trip planner module reads, and the seed of cis.tenant.crm_agency_seen_hours (a compiled
    # default the settings tests compare with); test_aa1160_migration_474 applies the whole file.
    "474_": ("-- ── Part A", "-- ── Part B"),
}


def replay_part(name: str, sql: str) -> str:
    """The SQL the replay runs for migration file ``name``: one part of it for a file in PARTS, else ``sql``."""
    for prefix, (start, end) in PARTS.items():
        if name.startswith(prefix):
            part = sql.split(start)[1].split(end)[0].split("\n", 1)[1]
            return "BEGIN;\n" + part + "COMMIT;\n"
    return sql
