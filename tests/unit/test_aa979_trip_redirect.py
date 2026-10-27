"""AA-979 P2b (runbook §4.3 step 2): the admin host answers /trip/:path* with a 307 to the CRM client host.

307 (Next `permanent: false`), never 301/308 before the P2b clean-up: browsers cache a 301 with no expiry, so it could
not be rolled back (critic S5). The CIS page stays in the app as the rollback target until then.
"""
import re
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
FRONTEND = REPO / "frontend"
CLIENT_HOST = "https://go-dev.lumiguides.it.com"


def test_trip_page_base_url_is_the_client_host():
    apps = (FRONTEND / "lib" / "apps.ts").read_text()
    assert f'export const TRIP_PAGE_BASE_URL = "{CLIENT_HOST}";' in apps


def test_admin_redirects_trip_pages_with_307():
    cfg = (FRONTEND / "next.config.ts").read_text()
    pattern = (
        r'\{\s*source:\s*"/trip/:path\*",\s*destination:\s*`\$\{TRIP_PAGE_BASE_URL\}/trip/:path\*`,'
        r'\s*permanent:\s*false\s*\}'
    )
    assert re.search(pattern, cfg), "next.config.ts: missing the 307 /trip/:path* -> client host redirect"
    assert len(re.findall(r'source:\s*"/trip', cfg)) == 1, "exactly one /trip redirect"


def test_cis_trip_page_kept_as_rollback_target():
    assert (FRONTEND / "app" / "trip" / "[tourId]" / "page.tsx").exists()
