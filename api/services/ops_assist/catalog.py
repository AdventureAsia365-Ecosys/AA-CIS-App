"""AA-1154 (A0) — the action catalogue of the CIS admin assistant: every action a card may propose, as data.

Deny by default (S285 advisory section 6, like the route authz): an admin write that is not listed here is never
proposed. Each row names the API it calls by its ``POLICY`` key (``api/core/staff_authz.py``), so the card's "who
approves" is the capability of that route and nothing else (tech lead AI7: no new approval layer, each action follows
the approval rule of the API it calls). ``tests/unit/test_aa1154_ops_assist.py`` checks every row against ``POLICY``:
the route exists, the capability is the one listed, the way back and the dry run routes exist too.

Fields:

- ``direction``: ``read`` (nothing changes), ``simulate`` (a DRY RUN or a $0 preview), ``tighten`` (stops or narrows
  something, easy to undo), ``reroute`` (sends traffic or work somewhere new: a looser change by rule),
  ``rerun`` (runs paid work again), ``loosen`` (raises a cap, resets a breaker). An action that changes a limit
  computes its direction from the proposed before / after with ``settings_registry.loosened()`` (``limit_kinds``).
- ``scope``: ``job`` < ``wave`` < ``stage`` < ``connection`` < ``app`` < ``all``; a connection or route action that
  touches at least ``cis.ops.assist_wide_scope_stages`` stages counts as wide (the risk rule, ``risk.py``).
- ``money``: ``none``, ``cents`` (a few cents at most: the probe's tiny calls) or ``paid`` (spends what the work
  spends; the card's cost estimate decides against ``cis.ops.assist_small_money_usd``).
- ``copy_only``: outside the admin API (ECS rollback, a start of the worker, an AWS Support case). The card shows the
  runbook and a text to copy; the tech lead runs it with MFA (R4). Never a button.
- ``proposed``: False for an action a playbook names only under "deliberately not proposed" (with its reason).

The catalogue is served read-only by ``GET /admin/ops-assist/catalog`` (admin-visible); it holds no threshold: every
limit the rules use is a setting (``shared/ops_assist_settings.py``).
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Optional

DIRECTIONS = ("read", "simulate", "tighten", "reroute", "rerun", "loosen")
SCOPES = ("job", "wave", "stage", "connection", "app", "all")
MONEY = ("none", "cents", "paid")

RUNBOOK_INCIDENT = "/admin/docs?path=docs/ops/incident-response.md"
RUNBOOK_ROLLBACK = "/admin/docs?path=docs/ops/runbooks/rollback-ecs.md"
POSTMORTEM_ACCESS = "/admin/docs?path=docs/ops/postmortems/2026-10-29-acc3-bedrock-access-refusal.md"


@dataclass(frozen=True)
class Route:
    method: str
    path: str


@dataclass(frozen=True)
class Action:
    id: str
    title: str
    direction: str
    scope: str
    money: str
    reversible: bool
    call: Optional[Route] = None            # the API the card's button calls (None: watch or copy-only)
    capability: tuple[str, ...] = ()        # the POLICY capability of ``call`` (checked by the test)
    dry_run: Optional[Route] = None         # a DRY RUN / preview of the same change ($0), when there is one
    way_back: Optional[Route] = None        # the call that undoes it, when there is one
    way_back_text: str = ""
    runbook: Optional[str] = None
    copy_only: bool = False
    proposed: bool = True
    check_after: tuple[str, ...] = ()       # what A2 reads again at +10 / +30 minutes
    limit_kinds: dict = field(default_factory=dict)   # loosened() field groups for a limit change

    def view(self) -> dict:
        out = asdict(self)
        out["capability"] = list(self.capability)
        out["check_after"] = list(self.check_after)
        return out


def _r(method: str, path: str) -> Route:
    return Route(method, path)


ACTIONS: tuple[Action, ...] = (
    Action("observe", "Do nothing now and watch", "read", "all", "none", True,
           way_back_text="Nothing to undo.", check_after=("signal still open",)),
    Action("llm_probe", "Probe every AI connection (DRY RUN, then live)", "simulate", "all", "cents", True,
           call=_r("POST", "/admin/job-runner/llm-probe/run"), capability=("ai.admin",),
           dry_run=_r("POST", "/admin/job-runner/llm-probe/run"),
           way_back_text="Nothing to undo: a probe changes no setting. A refused probe costs 0 USD.",
           check_after=("refused models per connection",)),
    Action("route_preview", "Preview a route change ($0, no model call)", "simulate", "stage", "none", True,
           call=_r("POST", "/admin/ai-gateway/route-control/preview"), capability=("ai.admin",),
           way_back_text="Nothing to undo: a preview saves nothing."),
    Action("route_save", "Save the previewed route change", "reroute", "stage", "paid", True,
           call=_r("POST", "/admin/ai-gateway/route-control/save"), capability=("ai.admin",),
           dry_run=_r("POST", "/admin/ai-gateway/route-control/preview"),
           way_back=_r("POST", "/admin/ai-gateway/route-control/versions/{version}/revert"),
           way_back_text="Revert to the route version saved before (one click on the Routing tab).",
           check_after=("refused calls of the moved stages", "spend per hour of the fallback connection")),
    Action("route_revert", "Revert the route version saved just before the problem", "tighten", "stage", "none", True,
           call=_r("POST", "/admin/ai-gateway/route-control/versions/{version}/revert"), capability=("ai.admin",),
           way_back=_r("POST", "/admin/ai-gateway/route-control/versions/{version}/revert"),
           way_back_text="Revert again to the version this one replaced.",
           check_after=("refused calls of the changed stages",)),
    Action("wave_pause", "Pause the open wave", "tighten", "wave", "none", True,
           call=_r("POST", "/admin/waves/{wave_id}/pause"), capability=("ops.write", "wave.run"),
           way_back=_r("POST", "/admin/waves/{wave_id}/resume"),
           way_back_text="Resume the wave (one call, 0 USD).",
           check_after=("queued jobs of the wave", "spend per hour")),
    Action("app_bulk_pause", "Pause the app's bulk AI work", "tighten", "app", "none", True,
           call=_r("PATCH", "/admin/ai-gateway/app-profiles/{app}"), capability=("ai.admin",),
           way_back=_r("PATCH", "/admin/ai-gateway/app-profiles/{app}"),
           way_back_text="Set bulk paused back to off on the App profiles card.",
           check_after=("bulk jobs started", "spend per hour of the app")),
    Action("retry_llm_denied", "Re-queue the jobs the refusal failed (DRY RUN first)", "rerun", "job", "paid", False,
           call=_r("POST", "/admin/job-runner/jobs/retry-llm-denied"), capability=("ops.write",),
           dry_run=_r("POST", "/admin/job-runner/jobs/retry-llm-denied"),
           way_back_text="No undo once re-queued jobs have spent; cancel the ones still queued on the Jobs page.",
           check_after=("re-queued jobs succeeded", "refused calls")),
    Action("job_cancel", "Cancel the stuck queued job", "tighten", "job", "none", True,
           call=_r("POST", "/admin/job-runner/jobs/{job_id}/cancel"), capability=("ops.write",),
           way_back_text="Queue the work again from the page that started it (a cancelled job is not resumed).",
           check_after=("oldest queued wait",)),
    Action("ecs_rollback", "Roll the service back to its previous task definition", "reroute", "app", "none", True,
           copy_only=True, runbook=RUNBOOK_ROLLBACK,
           way_back_text="Deploy the newer revision again once fixed (Deploy Dev workflow).",
           check_after=("server errors", "health check")),
    Action("worker_start", "Bring the job worker back (ECS desired count)", "loosen", "app", "paid", True,
           copy_only=True, runbook=RUNBOOK_ROLLBACK,
           way_back_text="Set the desired count back (the tech lead, with MFA).",
           check_after=("live workers", "oldest queued wait")),
    Action("aws_support_case", "Draft an AWS Support case and a reseller message (copy only)", "read", "all", "none",
           True, copy_only=True, runbook=POSTMORTEM_ACCESS, way_back_text="Nothing to undo."),
    # Named only under "deliberately not proposed" (S285 advisory, the 29/10 example):
    Action("connection_disable", "Switch the AI connection off by hand", "tighten", "connection", "none", True,
           call=_r("PATCH", "/admin/llm-connections/{connection_key}"), capability=("ai.admin",),
           way_back=_r("PATCH", "/admin/llm-connections/{connection_key}"),
           way_back_text="Switch it on again on the Providers tab.", proposed=False),
    Action("anomaly_breaker_reset", "Reset the app's spend anomaly breaker", "loosen", "app", "paid", True,
           call=_r("POST", "/admin/ai-gateway/anomaly-breaker/{app}/reset"), capability=("ai.admin",),
           way_back_text="The breaker trips again on the next anomaly.", proposed=False),
)

BY_ID: dict[str, Action] = {a.id: a for a in ACTIONS}


def get(action_id: str) -> Action:
    return BY_ID[action_id]


def view() -> list[dict]:
    """The catalogue as the API serves it (risk and approvers are added by the router, they need the live limits)."""
    return [a.view() for a in ACTIONS]
