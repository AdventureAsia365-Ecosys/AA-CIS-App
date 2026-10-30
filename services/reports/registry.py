"""AA-1003 R1 — the report registry, read from the vendored copy in registry/.

registry/catalog.yaml and registry/kpis.yaml are byte copies of the root repo's docs/reports/*.yaml (the source of
truth, linted there by tools/docs/report_registry.py; compare with `report_registry.py check --copy
<this repo>/services/reports/registry`). Change the root files first, then copy them here in the same session.

Loaded once per process (lru_cache) with yaml.safe_load. Lookups are total: an unknown ID returns None.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from functools import lru_cache
from pathlib import Path
from typing import Any, Optional

import yaml

_DIR = Path(__file__).resolve().parent / "registry"
CATALOG_PATH = _DIR / "catalog.yaml"
KPIS_PATH = _DIR / "kpis.yaml"

# The engine that renders a report here (catalog.yaml `generator`).
CIS_GENERATOR = "cis"


@dataclass(frozen=True)
class Kpi:
    id: str
    name: str
    definition: str
    unit: str
    threshold: str
    sources: tuple[str, ...]
    defined_in: str
    used_by: tuple[str, ...]
    snapshot: Optional[dict] = None
    alert: Optional[dict] = None
    report_thresholds: dict = field(default_factory=dict)
    version: int = 1

    @property
    def snapshotted(self) -> bool:
        return bool(self.snapshot) and self.snapshot.get("job") == "kpi_snapshot_daily"

    def threshold_for(self, report_id: str) -> str:
        return self.report_thresholds.get(report_id, self.threshold)

    def as_dict(self) -> dict:
        return {"id": self.id, "name": self.name, "definition": self.definition, "unit": self.unit,
                "threshold": self.threshold, "sources": list(self.sources), "defined_in": self.defined_in,
                "used_by": list(self.used_by), "snapshotted": self.snapshotted, "alert": self.alert,
                "version": self.version}


@dataclass(frozen=True)
class Report:
    id: str
    family: str
    name_en: str
    name_vi: str
    audience: str
    owner_role: str
    decision: str
    cadence: str
    schedule: str
    channel: str
    automation: str
    status: str
    priority: str
    template: Optional[str]
    generator: Optional[str]
    kpis: tuple[str, ...]
    sections: tuple[str, ...]
    # catalog.yaml `automated_from` (optional): the first snapshot day whose daily chain can create this report's
    # scheduled period (the deploy day of its generator). A scheduled period the chain made before it is not
    # expected, so a report switched to `generator: cis` is not overdue for periods that ended before it existed.
    automated_from: Optional[date] = None

    @property
    def runnable(self) -> bool:
        """The CIS engine renders it (job kind report_generate)."""
        return self.generator == CIS_GENERATOR

    def summary(self) -> dict:
        return {"id": self.id, "family": self.family, "name_en": self.name_en, "name_vi": self.name_vi,
                "audience": self.audience, "owner_role": self.owner_role, "cadence": self.cadence,
                "schedule": self.schedule, "channel": self.channel, "automation": self.automation,
                "status": self.status, "priority": self.priority, "template": self.template,
                "generator": self.generator, "runnable": self.runnable, "kpis": list(self.kpis),
                "automated_from": self.automated_from.isoformat() if self.automated_from else None}

    def detail(self) -> dict:
        return {**self.summary(), "decision": self.decision, "sections": list(self.sections)}


@dataclass(frozen=True)
class Registry:
    version: int
    imported_from: str
    reports: tuple[Report, ...]
    kpis: dict[str, Kpi]

    def report(self, report_id: str) -> Optional[Report]:
        return next((r for r in self.reports if r.id == report_id), None)

    def kpi(self, kpi_id: str) -> Optional[Kpi]:
        return self.kpis.get(kpi_id)

    def snapshot_kpis(self) -> list[Kpi]:
        return [k for k in self.kpis.values() if k.snapshotted]

    def runnable(self) -> list[Report]:
        return [r for r in self.reports if r.runnable]


ALERT_KEYS = frozenset({"amber_gte", "red_gte"})


def _tuple(v: Any) -> tuple:
    return tuple(v or ())


def _date(rid: str, v: Any) -> Optional[date]:
    """yaml.safe_load reads an unquoted YYYY-MM-DD as a date; a quoted one is a string."""
    if v is None:
        return None
    if isinstance(v, date):
        return v
    try:
        return date.fromisoformat(str(v))
    except ValueError:
        raise ValueError(f"report registry: {rid} automated_from {v!r} is not a YYYY-MM-DD date") from None


def parse(catalog: dict, kpis: dict) -> Registry:
    """Builds the registry from the two parsed YAML documents. Raises ValueError on a broken copy (the root lint
    keeps the source valid; this only guards a bad vendoring)."""
    if not isinstance(catalog, dict) or not isinstance(kpis, dict):
        raise ValueError("report registry: both files must be YAML mappings")
    kpi_map: dict[str, Kpi] = {}
    for k in kpis.get("kpis") or []:
        unknown = set(k.get("alert") or {}) - ALERT_KEYS
        if unknown:      # generate.alert_status reads only these: another bound would silently show "ok"
            raise ValueError(f"report registry: {k['id']} has unknown alert bounds {sorted(unknown)}")
        kpi_map[k["id"]] = Kpi(
            id=k["id"], name=k["name"], definition=k["definition"], unit=k["unit"], threshold=str(k["threshold"]),
            sources=_tuple(k.get("sources")), defined_in=k["defined_in"], used_by=_tuple(k.get("used_by")),
            snapshot=k.get("snapshot"), alert=k.get("alert"), report_thresholds=dict(k.get("report_thresholds") or {}),
            version=int(k.get("version") or 1))
    reports = []
    for r in catalog.get("reports") or []:
        missing = [kid for kid in r.get("kpis") or [] if kid not in kpi_map]
        if missing:
            raise ValueError(f"report registry: {r['id']} lists unknown KPIs {missing}")
        reports.append(Report(
            id=r["id"], family=r["family"], name_en=r["name_en"], name_vi=r["name_vi"], audience=r["audience"],
            owner_role=r["owner_role"], decision=r["decision"], cadence=r["cadence"], schedule=r["schedule"],
            channel=r["channel"], automation=r["automation"], status=r["status"], priority=r["priority"],
            template=r.get("template"), generator=r.get("generator"), kpis=_tuple(r.get("kpis")),
            sections=_tuple(r.get("sections")), automated_from=_date(r["id"], r.get("automated_from"))))
    if not reports:
        raise ValueError("report registry: no reports")
    return Registry(version=int(catalog.get("version") or 0), imported_from=str(catalog.get("imported_from") or ""),
                    reports=tuple(reports), kpis=kpi_map)


@lru_cache(maxsize=1)
def load() -> Registry:
    with CATALOG_PATH.open(encoding="utf-8") as f:
        catalog = yaml.safe_load(f)
    with KPIS_PATH.open(encoding="utf-8") as f:
        kpis = yaml.safe_load(f)
    return parse(catalog, kpis)
