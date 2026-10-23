"""AA-986 L2 guard (ADR 0015 decision 2): services/tour_lifecycle is the only CIS code that changes a tour's served
state. No other module may UPDATE raw_tours.source_status / deleted_at or published_tours.master_status /
deleted_at / master_status_reason, and none may INSERT INTO raw_tours / published_tours (the two repositories the
service drives excepted, and only the service may drive their insert paths).

A failure here means a new writer bypasses the cascade (Masters, reviews, queued tenant jobs, the lifecycle record,
tenant notifications): call the service instead (services/tour_lifecycle/service.py).
"""
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SCANNED = ("api", "services", "shared", "worker", "scripts")
SERVICE_DIR = ROOT / "services" / "tour_lifecycle"
REPOSITORIES = {
    ROOT / "shared" / "repository" / "raw_tour_repository.py",
    ROOT / "shared" / "repository" / "published_catalog_repository.py",
}
STATUS_COLUMNS = ("source_status", "master_status", "deleted_at", "master_status_reason")

_UPDATE_RE = re.compile(
    r"\bUPDATE\s+(?:ONLY\s+)?[\w{}.\"]*?\b(raw_tours|published_tours)\b(?:\s+(?:AS\s+)?\w+)?\s+SET\b(.*?)"
    r"(?=\bWHERE\b|\bFROM\b|\bRETURNING\b|\"\"\"|'''|$)",
    re.IGNORECASE | re.DOTALL)
_INSERT_RE = re.compile(r"\bINSERT\s+INTO\s+[\w{}.\"]*?\b(raw_tours|published_tours)\b", re.IGNORECASE)


def _status_writes(text: str) -> list[str]:
    out = []
    for m in _UPDATE_RE.finditer(text):
        set_clause = m.group(2)
        cols = [c for c in STATUS_COLUMNS if re.search(rf"\b{c}\s*=", set_clause)]
        if cols:
            out.append(f"UPDATE {m.group(1)} SET {', '.join(cols)}")
    return out


def _inserts(text: str) -> list[str]:
    return [f"INSERT INTO {m.group(1)}" for m in _INSERT_RE.finditer(text)]


def _files():
    for top in SCANNED:
        for f in sorted((ROOT / top).rglob("*.py")):
            if SERVICE_DIR in f.parents or "__pycache__" in f.parts:
                continue
            yield f


def test_the_patterns_catch_the_writers_l2_removed():
    # Mutation check: the shapes the routers used before L2 are caught (and harmless writes are not).
    assert _status_writes("UPDATE silver_aa_internal.raw_tours\n SET source_status = 'trashed', deleted_at = NOW()\n"
                          " WHERE tour_id = $1")
    assert _status_writes("UPDATE gold_aa_internal.published_tours SET master_status = $1::x WHERE tour_id = $2")
    assert _status_writes("UPDATE {silver}.raw_tours rt SET deleted_at = NULL WHERE x")
    assert _inserts("INSERT INTO silver_aa_internal.raw_tours (tenant_id) VALUES ($1)")
    assert _inserts('f"INSERT INTO {self.gold}.published_tours (tour_id)"')
    assert not _status_writes("UPDATE silver_aa_internal.raw_tours SET pipeline_status = 'published' WHERE x")
    assert not _status_writes("UPDATE gold_aa_internal.published_tours SET quality_score_id = $1 WHERE x")
    assert not _status_writes("UPDATE silver_aa_internal.raw_tours SET country = $1 WHERE tour_id = $2")


def test_no_status_write_outside_the_lifecycle_service():
    offenders = []
    for f in _files():
        for hit in _status_writes(f.read_text(encoding="utf-8", errors="replace")):
            offenders.append(f"{f.relative_to(ROOT)}: {hit}")
    assert offenders == [], ("these write a tour's served state outside services/tour_lifecycle "
                             "(ADR 0015 decision 2):\n" + "\n".join(offenders))


def test_no_insert_into_raw_or_published_tours_outside_the_service():
    offenders = []
    for f in _files():
        if f in REPOSITORIES:
            continue
        for hit in _inserts(f.read_text(encoding="utf-8", errors="replace")):
            offenders.append(f"{f.relative_to(ROOT)}: {hit}")
    assert offenders == [], ("these insert a tour version / Master outside services/tour_lifecycle:\n"
                             + "\n".join(offenders))


def test_only_the_service_drives_the_repository_inserts():
    """The repositories hold the INSERT text; only the service may call their insert paths."""
    offenders = []
    for f in _files():
        if f in REPOSITORIES or f.name == "__init__.py" and f.parent.name == "repository":
            continue
        text = f.read_text(encoding="utf-8", errors="replace")
        if "PublishedCatalogRepository(" in text:
            offenders.append(f"{f.relative_to(ROOT)}: PublishedCatalogRepository(")
        if re.search(r"\.insert_batch\(", text):
            offenders.append(f"{f.relative_to(ROOT)}: .insert_batch(")
        if re.search(r"\btour_repo\.insert\(", text):
            offenders.append(f"{f.relative_to(ROOT)}: tour_repo.insert(")
    assert offenders == [], "call services.tour_lifecycle (publish / insert_new_sources) instead:\n" + "\n".join(
        offenders)


def test_the_service_itself_is_found_by_the_patterns():
    """The guard is not vacuous: the service holds the writes it forbids elsewhere."""
    text = "".join(p.read_text() for p in SERVICE_DIR.glob("*.py"))
    hits = " ".join(_status_writes(text))
    for col in ("source_status", "master_status", "deleted_at"):
        assert col in hits
    assert "INSERT INTO raw_tours" in " ".join(_inserts(text))
