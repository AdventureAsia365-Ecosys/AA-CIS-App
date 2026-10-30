"""AA-919 (critic 7) — a hard guard against model calls inside a $0 process (the golden-set importer, later the
release-0 replay): every entry point of the Model Gateway raises LLMCallForbidden once install() ran, on top of the
llm_call_log before / after count. A process that must cost $0 cannot spend by accident (an import that pulls a
helper which "just" embeds or judges).

Patched by name (setattr) so the AA-927 call-site registry sees no `.generate` load here.
"""
from __future__ import annotations

import importlib
from typing import Callable

# (module, attribute or "Class.method") of every way into a billed model call.
ENTRY_POINTS = (
    ("shared.llm_client.client", "LLMClient.generate"),
    ("shared.llm_client.client", "LLMClient._generate"),
    ("shared.llm_client.bedrock_satellite", "invoke_claude"),
    ("shared.llm_client.bedrock_satellite", "get_satellite_client"),
    ("shared.llm_client.bedrock_satellite", "native_runtime"),      # AA-1002 S3: a task-role (acc2) deployment
    ("shared.llm_client.embed", "_call"),
    ("shared.llm_client.adapters.anthropic_messages", "send"),     # AA-1002 S2: the Anthropic Messages API
)


class LLMCallForbidden(RuntimeError):
    """A model call was attempted in a process that must make none."""


def _refuser(where: str) -> Callable:
    def refuse(*_args, **_kwargs):
        raise LLMCallForbidden(f"{where}: this process makes no model call (AA-919 $0 guard)")
    return refuse


def install(reason: str = "golden_set") -> Callable[[], None]:
    """Patch every entry point; returns an undo (tests). A module that cannot be imported has no way to call."""
    undo: list[tuple[object, str, object]] = []
    for module_name, attr in ENTRY_POINTS:
        try:
            module = importlib.import_module(module_name)
        except Exception:  # noqa: BLE001 — not importable here = not callable here
            continue
        owner: object = module
        name = attr
        if "." in attr:
            cls_name, name = attr.split(".", 1)
            owner = getattr(module, cls_name, None)
            if owner is None:
                continue
        if not hasattr(owner, name):
            continue
        undo.append((owner, name, owner.__dict__.get(name, getattr(owner, name))))
        setattr(owner, name, _refuser(f"{reason}: {module_name}.{attr}"))

    def restore() -> None:
        for owner, name, original in reversed(undo):
            setattr(owner, name, original)
    return restore
