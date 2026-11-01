"""AA-1026 I6 — since CIS migrations 331 / 332 some judge / validate routes END on an acc2 candidate model of
migration 324. 324's own guard describes the state before any route change ("no stage routes to a candidate") and
refuses a re-run while a route names one, so the tests that re-apply 324 take the candidates off the routes around the
re-run and put the routes back afterwards."""
from contextlib import contextmanager

CANDIDATES = ("nova-lite", "nova-2-lite", "llama4-maverick", "llama4-scout", "kimi-k3")


@contextmanager
def candidates_off_routes(c):
    """``c``: a dict cursor. Fallbacks only (a candidate is never a primary or a shadow: 331 / 332 guard that)."""
    c.execute("SELECT stage, fallback_model_ids AS f FROM shared.llm_role_config "
              "WHERE fallback_model_ids && %s::text[]", (list(CANDIDATES),))
    saved = [(r["stage"], list(r["f"])) for r in c.fetchall()]
    for stage, fallbacks in saved:
        c.execute("UPDATE shared.llm_role_config SET fallback_model_ids = %s WHERE stage = %s",
                  ([k for k in fallbacks if k not in CANDIDATES], stage))
    try:
        yield saved
    finally:
        for stage, fallbacks in saved:
            c.execute("UPDATE shared.llm_role_config SET fallback_model_ids = %s WHERE stage = %s", (fallbacks, stage))
