"""AA-1095 (S278) — the route replay tests set aside the deployment rows that later migrations seeded.

tests/integration/test_aa1026_migration_366.py, test_aa1042_migration_376.py and test_aa900_migration_383.py re-run
an applied migration's body after the full replay. Those bodies check their new routes with the chain rule
``attempt_cap``, which counts EVERY deployment row of every model on the route (on or off: the same count as
api/services/llm_route_chains.py, the rule the admin route control applies). On Dev each file ran before the later
migrations seeded more deployments of the same models, so a re-run in the post-replay world counts rows that did not
exist then and refuses its own (correct) routes. Applied migrations are never edited, so each replay test removes the
later seeds inside its own transaction (rolled back at the end), the same way it already resets the rows its own file
writes.

``LATER_DEPLOYMENT_SEEDS``: migration number -> the ``updated_by`` its seeded ``shared.llm_model_deployment`` rows
carry. tests/unit/test_aa1095_later_deployment_seeds.py fails when a migration after the newest route replay (383)
seeds deployments and is not listed here (367's and 376's seeds predate this list; the replay tests pass with them).

``LATER_CATALOG_PROFILE_KEYS``: migration number -> the ``shared.llm_model_catalog.bedrock_profile_ids`` key it adds
for its deployments (the wire-drift invariant: an enabled Bedrock deployment calls ``bedrock_profile_ids ->>
connection_key``, tests/integration/test_aa1002_deployment_wire_drift.py). Tests that pin an older file's exact
profiles (295: no acc1 key after it; 366 / 367: the rows carry their acc3 profile only) set that key aside too:
``set_later_profiles_aside`` (295, 367), and ``set_later_deployments_aside`` does it with the deployments (366, 376,
383). Only the key leaves; updated_by / updated_at stay (other replays key on them). The unit guard fails when a
migration after the newest profile replay adds a profile key with ``|| jsonb_build_object('<key>'`` and is not
listed here.
"""
from __future__ import annotations

# migration number -> the updated_by of the deployment rows it seeds
LATER_DEPLOYMENT_SEEDS: dict[int, str] = {
    406: "migration-406-aa1095",   # AA-1095: connection acc1's 13 chat-model deployments (seeded off)
}

# the replay tests that call set_later_deployments_aside, with the migration each one re-runs
ROUTE_REPLAY_TESTS: dict[int, str] = {
    366: "tests/integration/test_aa1026_migration_366.py",
    376: "tests/integration/test_aa1042_migration_376.py",
    383: "tests/integration/test_aa900_migration_383.py",
}

# migration number -> the bedrock_profile_ids key it adds to the catalog rows of its deployments
LATER_CATALOG_PROFILE_KEYS: dict[int, str] = {
    406: "acc1",                   # AA-1095: the acc1 profile = the wire id of the model's acc1 deployment
}

# the replay tests that call set_later_profiles_aside, with the migration whose exact profiles each one pins
PROFILE_REPLAY_TESTS: dict[int, str] = {
    295: "tests/integration/test_aa998_migration_295.py",
    367: "tests/integration/test_aa1026_migration_367.py",
}


def later_seed_actors(replayed: int) -> list[str]:
    """The ``updated_by`` of every deployment seed of a migration numbered above ``replayed``."""
    return [actor for number, actor in sorted(LATER_DEPLOYMENT_SEEDS.items()) if number > replayed]


def later_profile_keys(replayed: int) -> list[str]:
    """The ``bedrock_profile_ids`` key of every catalog profile a migration numbered above ``replayed`` added."""
    return sorted({key for number, key in LATER_CATALOG_PROFILE_KEYS.items() if number > replayed})


def set_later_profiles_aside(c, replayed: int) -> None:
    """``c``: a cursor. Removes the catalog profile keys added after ``replayed`` (only the key; updated_by and
    updated_at stay). Inside the test's transaction, or followed by a re-run of the adding migration."""
    for key in later_profile_keys(replayed):
        c.execute("UPDATE shared.llm_model_catalog SET bedrock_profile_ids = bedrock_profile_ids - %s "
                  "WHERE bedrock_profile_ids ? %s", (key, key))


def set_later_deployments_aside(c, replayed: int) -> None:
    """``c``: a cursor inside the test's transaction. Deletes the deployment rows seeded after ``replayed`` and the
    catalog profile keys added with them."""
    actors = later_seed_actors(replayed)
    if actors:
        c.execute("DELETE FROM shared.llm_model_deployment WHERE updated_by = ANY (%s)", (actors,))
    set_later_profiles_aside(c, replayed)
