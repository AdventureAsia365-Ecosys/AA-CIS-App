"""Every runtime and dev requirement is pinned exactly (S288 follow-up).

A floating floor (``pkg>=x``) let a new upstream release change the API image between two builds of the same commit
(S288: sentry-sdk and redis broke CI for an afternoon). Exact pins keep builds reproducible; dependabot raises them.
Allowed forms: ``name==version`` (extras allowed) and a direct URL with a sha256 (``name @ https://...#sha256=...``).
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
EXACT = re.compile(r"^[A-Za-z0-9_.\-]+(\[[A-Za-z0-9_,.\-]+\])?==[A-Za-z0-9_.\-+!]+(\s*;.*)?$")
DIRECT_URL = re.compile(r"^[A-Za-z0-9_.\-]+ @ https://\S+#sha256=[0-9a-f]{64}$")


def _requirement_lines(path: Path) -> list[tuple[int, str]]:
    out = []
    for no, raw in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        line = raw.split(" #", 1)[0].strip()
        if not line or line.startswith("#") or line.startswith("-"):
            continue
        out.append((no, line))
    return out


@pytest.mark.parametrize("name", ["requirements.txt", "requirements-dev.txt"])
def test_every_requirement_is_pinned_exactly(name: str) -> None:
    path = ROOT / name
    if not path.exists():
        pytest.skip(f"{name} not present")
    loose = [f"{name}:{no}: {line}" for no, line in _requirement_lines(path)
             if not (EXACT.match(line) or DIRECT_URL.match(line))]
    assert not loose, "pin these exactly (name==version):\n" + "\n".join(loose)
