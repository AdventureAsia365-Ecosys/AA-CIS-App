"""AA-979: the admin host answers /trip/:path* with a permanent redirect to the CRM client host.

P2b (runbook §4.3) shipped it as a 307 so it could be rolled back (critic S5: browsers cache a 301/308 with no expiry).
The P2b clean-up (runbook §4.5) made it permanent (Next `permanent: true` = 308, method and query kept) and deleted
the CIS page app/trip/[tourId], which only served as the rollback target.
"""
import re
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
FRONTEND = REPO / "frontend"
CLIENT_HOST = "https://go-dev.lumiguides.it.com"


def test_trip_page_base_url_is_the_client_host():
    apps = (FRONTEND / "lib" / "apps.ts").read_text()
    assert f'export const TRIP_PAGE_BASE_URL = "{CLIENT_HOST}";' in apps


def test_admin_redirects_trip_pages_permanently():
    cfg = (FRONTEND / "next.config.ts").read_text()
    pattern = (
        r'\{\s*source:\s*"/trip/:path\*",\s*destination:\s*`\$\{TRIP_PAGE_BASE_URL\}/trip/:path\*`,'
        r'\s*permanent:\s*true\s*\}'
    )
    assert re.search(pattern, cfg), "next.config.ts: missing the permanent /trip/:path* -> client host redirect"
    assert len(re.findall(r'source:\s*"/trip', cfg)) == 1, "exactly one /trip redirect"


def test_cis_trip_page_is_gone():
    assert not (FRONTEND / "app" / "trip").exists(), "the CIS trip page was deleted at the P2b clean-up"
