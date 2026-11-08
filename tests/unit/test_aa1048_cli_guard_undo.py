"""S273: scripts.aa1048.cli.main() refuses model calls for the command only, then restores the gateway.

A unit test that called main() in-process left LLMClient.probe / generate patched by the $0 guard, and every later
gateway test in the same pytest worker failed with LLMCallForbidden (CIS #1382 CI, Unit shards 1/3 and 2/3).
"""
from services.golden_set import llm_guard
from shared.llm_client import client as llm_client


def test_main_restores_every_gateway_entry_point():
    from scripts.aa1048 import cli

    before = {name: getattr(llm_client.LLMClient, name) for name in ("generate", "_generate", "probe")}
    rc = cli.main(["sample-size"])
    assert rc == 0
    after = {name: getattr(llm_client.LLMClient, name) for name in ("generate", "_generate", "probe")}
    assert after == before


def test_guard_holds_while_the_command_runs(monkeypatch):
    from scripts.aa1048 import cli

    seen = {}

    def probe_inside(a):
        try:
            llm_client.LLMClient.probe(None)
        except llm_guard.LLMCallForbidden:
            seen["refused"] = True
        return {}

    monkeypatch.setitem(cli.LOCAL_COMMANDS, "sample-size", probe_inside)
    cli.main(["sample-size"])
    assert seen.get("refused") is True
