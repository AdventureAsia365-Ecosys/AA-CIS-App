"""The spend decision before a model call (root ADR 0009 decision 6: fail closed).

A caller reads today's spend through its own budget store (``ports.BudgetStore``) and asks ``decide``. When the spend
cannot be read the answer is ``unreadable`` (no call, reason ``spend_unreadable``), never "unlimited". A cap and an
optional stage cap both apply; ``near_cap`` turns on at ``near_ratio`` of either. Pure: no I/O, no clock.
"""

from __future__ import annotations

from dataclasses import dataclass

REASON_CAP = "cap_reached"
REASON_SPEND_UNREADABLE = "spend_unreadable"
NEAR_CAP_RATIO = 0.8

THRESHOLD_NEAR = "near"
THRESHOLD_CAP = "cap"


@dataclass(frozen=True)
class BudgetDecision:
    """``allowed`` False means no model is called; ``reason`` says why (``cap_reached`` / ``spend_unreadable``).
    ``spent_usd`` is None when the spend could not be read."""

    allowed: bool
    reason: str | None
    spent_usd: float | None
    cap_usd: float
    near_cap: bool = False
    stage: str | None = None
    stage_spent_usd: float | None = None
    stage_cap_usd: float | None = None

    @property
    def exhausted(self) -> bool:
        return not self.allowed

    def as_dict(self) -> dict:
        """{"spent_usd", "cap_usd", "exhausted", "reason", "near_cap"} plus "stage", "stage_spent_usd",
        "stage_cap_usd" when a stage cap applied."""
        out: dict = {"spent_usd": self.spent_usd, "cap_usd": self.cap_usd}
        if self.stage_cap_usd is not None:
            out.update(stage=self.stage, stage_spent_usd=self.stage_spent_usd, stage_cap_usd=self.stage_cap_usd)
        out.update(exhausted=self.exhausted, reason=self.reason, near_cap=self.near_cap)
        return out


def threshold(spent: float, cap: float, near_ratio: float = NEAR_CAP_RATIO) -> str | None:
    """``cap`` when ``spent`` reached ``cap``, ``near`` from ``near_ratio`` of it, else None."""
    if spent >= cap:
        return THRESHOLD_CAP
    if spent >= cap * near_ratio:
        return THRESHOLD_NEAR
    return None


def unreadable(cap: float) -> BudgetDecision:
    """The fail-closed answer when the spend (or the counter behind it) cannot be trusted."""
    return BudgetDecision(allowed=False, reason=REASON_SPEND_UNREADABLE, spent_usd=None, cap_usd=float(cap))


def decide(
    spent: float | None,
    cap: float,
    *,
    near_ratio: float = NEAR_CAP_RATIO,
    stage: str | None = None,
    stage_spent: float | None = None,
    stage_cap: float | None = None,
) -> BudgetDecision:
    """Today's ``spent`` against ``cap`` and, when ``stage_cap`` is set, ``stage_spent`` against it. ``spent`` None
    (or ``stage_spent`` None with a stage cap) is unreadable: not allowed."""
    if spent is None or (stage_cap is not None and stage_spent is None):
        return unreadable(cap)
    cap = float(cap)
    exhausted = spent >= cap
    near = spent >= cap * near_ratio
    stage_fields: dict = {}
    if stage_cap is not None:
        stage_cap = float(stage_cap)
        exhausted = exhausted or stage_spent >= stage_cap
        near = near or stage_spent >= stage_cap * near_ratio
        stage_fields = {"stage": stage, "stage_spent_usd": round(stage_spent, 6), "stage_cap_usd": stage_cap}
    return BudgetDecision(
        allowed=not exhausted,
        reason=REASON_CAP if exhausted else None,
        spent_usd=round(spent, 6),
        cap_usd=cap,
        near_cap=near,
        **stage_fields,
    )
