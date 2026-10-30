"""AA-920 (WM-23 / WM-24 / WM-26) — the one rule builder of the four writers, release 0 byte-identity, the
post-activation check and its automatic rollback.

Every rule text here is invented (no golden-set or tenant text: the golden-set text guard scans this public repo).
"""
from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from scripts.writing_memory import wm20_release0_replay as replay
from services.writing_memory import insertion
from services.writing_memory import post_activation as pa
from services.writing_memory import releases as rel
from tests.unit import aa919_release0_fakes as fakes
from tests.unit.test_aa919_releases import ADMIN, RID, _h, client  # noqa: F401 — `client` is the routes' fixture

ROOT = Path(__file__).resolve().parents[2]
PINS = Path(__file__).parent / "snapshots" / "aa919_release0_prompts.json"
_FLAGS = ("S1_PER_DAY_TARGETS", "S1_STRUCTURED_FACTS", "T2_TARGETED_REPAIR", "T2_BEST_DRAFT", "S1_WRITER_V2",
          "PIPELINE_LESSONS_ENABLED")
P_ID, T_ID = "11111111-1111-4111-8111-111111111111", "22222222-2222-4222-8222-222222222222"
TENANT_A, TENANT_B = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa", "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"
NOW = datetime(2026, 10, 28, 9, 0, tzinfo=timezone.utc)


@pytest.fixture(autouse=True)
def _flags_off(monkeypatch):
    for name in _FLAGS:
        monkeypatch.delenv(name, raising=False)


def _item(rid, kind="writer_instruction", text="Cut every sentence that only restates the title.", **kw):
    return {"rule_id": rid, "kind": kind, "applies_to": [], "gate_class": None, "rule_text": text,
            "replacement": None, **kw}


def _part(rid, items, cls="rules", **kw):
    return {"release_id": rid, "release_no": 1, "content_class": cls, "rule_ids": [i["rule_id"] for i in items],
            "items": items, "items_sha256": rel.items_sha256(items), **kw}


# ── release 0 / no content: the prompt does not change by one byte ────────────────────────────────────────────────

class _FakeDB:
    """`resolve` + `load_items` on canned rows (fetch only)."""

    def __init__(self, release_rows, rule_rows=()):
        self.release_rows, self.rule_rows, self.calls = list(release_rows), list(rule_rows), []

    async def fetch(self, sql, *args):
        self.calls.append(sql)
        if "FROM shared.writing_rule" in sql:
            ids = set(args[0])
            return [r for r in self.rule_rows if r["rule_id"] in ids]
        if "writing_memory_release_exemplar" in sql:      # AA-921: no pinned examples in these releases
            return []
        return self.release_rows


def _release_row(scope="platform", no=0, cls="empty", rule_ids=(), sha=None, tenant=None):
    return {"memory_enabled": True, "release_id": P_ID if scope == "platform" else T_ID, "scope": scope,
            "tenant_id": tenant, "release_no": no, "content_class": cls, "rule_ids": list(rule_ids),
            "items_sha256": sha or rel.items_sha256([])}


def _resolved_release0(writer):
    rows = [_release_row()] + ([] if writer == "a1" else [_release_row("tenant", 0, tenant=TENANT_A)])
    return asyncio.run(insertion.resolve(_FakeDB(rows), writer, TENANT_A))


def _capture_all(view_for):
    out = {}
    for writer, inputs in fakes.all_inputs().items():
        for inp in inputs:
            cap = replay.CAPTURE[writer](inp, view_for(writer))
            out[inp["input_id"]] = {"writer": writer, "system_sha256": replay.sha(cap["system"]),
                                    "user_sha256": replay.sha(cap["user"])}
    return out


def test_release0_resolved_through_the_db_path_is_byte_identical_to_the_pins():
    """Release 0 as `resolve` returns it from the DB rows (items loaded = [], items hash of nothing) gives the pinned
    pre-AA-919 prompts for all 4 writers x 20 inputs (full sha256 of system and user)."""
    pins = json.loads(PINS.read_text())
    captured = _capture_all(_resolved_release0)
    assert set(captured) == set(pins)
    diffs = sorted(k for k in pins if captured[k] != pins[k])
    assert diffs == [], f"{len(diffs)} release-0 prompts differ: {diffs[:5]}"


def test_a_release_whose_items_fail_their_check_renders_nothing():
    """A content release whose items are missing / changed renders "" (fail open): still byte-identical, and the stamp
    says ignored."""
    def view(writer):
        part = _part("p", [_item("r-1")])
        part.pop("items")                     # what part_of builds when load_items refused the rows
        return {"writer": writer, "platform": part, "tenant": None}

    pins = json.loads(PINS.read_text())
    captured = _capture_all(view)
    assert sorted(k for k in pins if captured[k] != pins[k]) == []
    assert insertion.stamp(view("t2"))["ignored"] is True


def test_repair_paths_are_unchanged_without_a_hint():
    fb = ["F1_grounding: x"]
    for v in (None, _resolved_release0("t9_social"),
              {"writer": "t9_social", "platform": _part("p", [_item("r-1")]), "tenant": None}):
        assert insertion.with_repair_hints(v, fb, ["F1_grounding"]) is fb
        assert insertion.repair_hint_block(v, ["FORBIDDEN_WORD"]) == ""


# ── the one builder ───────────────────────────────────────────────────────────────────────────────────────────────

def test_rule_lines_fold_whitespace_and_format_by_kind():
    assert insertion.rule_line(_item("a", text="Cut  filler\nopeners.")) == "- Cut filler openers."
    assert insertion.rule_line(_item("b", "lexicon", "nestled")) == '- Never write "nestled".'
    assert insertion.rule_line(_item("c", "lexicon", "a must-see", replacement="worth seeing")) == \
        '- Write "worth seeing" instead of "a must-see".'
    assert insertion.rule_line(_item("d", "preference_summary", "Short, plain sentences.")) == \
        "- Voice: Short, plain sentences."


def test_slots_render_platform_and_tenant_blocks_and_filter_channels():
    plat = [_item("p-1"), _item("p-2", "lexicon", "hidden gem", applies_to=["linkedin"]),
            _item("p-3", "repair_hint", "Drop the unsupported clause.", gate_class="fact.unsupported_claim")]
    ten = [_item("t-1", "preference_summary", "Warm and direct.")]
    view = {"writer": "t9_social", "platform": _part("p", plat), "tenant": _part("t", ten)}
    fb = insertion.t9_platform_block(view, "facebook")
    assert fb == f"\n\n{insertion.PLATFORM_HEADING}\n- Cut every sentence that only restates the title."
    li = insertion.t9_platform_block(view, "linkedin")
    assert li.endswith('\n- Never write "hidden gem".')
    assert "Drop the unsupported clause" not in fb + li                 # a repair hint never in a writing slot
    assert insertion.t9_tenant_lines(view, "facebook") == [insertion.TENANT_HEADING, "- Voice: Warm and direct."]
    t2 = insertion.t2_block({**view, "writer": "t2"})
    assert t2.index(insertion.PLATFORM_HEADING) < t2.index(insertion.TENANT_HEADING)
    assert insertion.a1_house_rules({"writer": "a1", "platform": _part("p", plat), "tenant": None}).startswith(
        insertion.PLATFORM_HEADING + "\n- Cut every")


def test_repair_hints_only_for_the_failed_gate_class(monkeypatch):
    monkeypatch.setattr(insertion, "_gate_class", lambda code: {"F1_grounding": "fact.unsupported_claim",
                                                                "F4_length": "struct.length"}.get(code))
    items = [_item("h-1", "repair_hint", "Drop the unsupported clause.", gate_class="fact.unsupported_claim"),
             _item("h-2", "repair_hint", "Shorten the longest paragraph.", gate_class="struct.length"),
             _item("w-1")]
    view = {"writer": "t9_blog", "platform": _part("p", items), "tenant": None}
    assert insertion.repair_hints(view, ["F1_grounding"]) == ["- Drop the unsupported clause."]
    assert insertion.with_repair_hints(view, ["F1: x"], ["F4_length"]) == ["F1: x", "- Shorten the longest paragraph."]
    assert insertion.repair_hints(view, ["F99_unknown"]) == []
    assert insertion.repair_hint_block(view, ["F1_grounding"]) == (
        f"\n\n{insertion.REPAIR_HEADING}\n- Drop the unsupported clause.")


def test_every_slot_goes_through_the_one_rule_renderer(monkeypatch):
    """No per-writer copy: replacing `rule_line` changes every slot and the repair hints."""
    monkeypatch.setattr(insertion, "rule_line", lambda item: "<ONE>")
    monkeypatch.setattr(insertion, "_gate_class", lambda code: "fact.x")
    items = [_item("w-1"), _item("h-1", "repair_hint", "x", gate_class="fact.x")]
    view = {"writer": "t2", "platform": _part("p", items), "tenant": _part("t", [_item("w-2")])}
    assert insertion.a1_house_rules(view).endswith("<ONE>")
    assert insertion.t2_block(view).count("<ONE>") == 2
    assert insertion.t9_platform_block(view).endswith("<ONE>")
    assert insertion.t9_tenant_lines(view)[-1] == "<ONE>"
    assert insertion.repair_hints(view, ["G"]) == ["<ONE>"]


def test_no_writer_module_formats_rules_itself():
    """The rule headings live only in insertion.py (the writers import the slot functions)."""
    writers = ["services/content_generation/s1_writer_v2.py", "services/content_generation/s1_writer_v2_live.py",
               "services/content_generation/batch_prompt.py", "services/content_generation/flag_fix_node.py",
               "services/acp_content_writing/generate.py", "services/acp_content_writing/prompts.py",
               "services/acp_content_writing/service.py"]
    for f in writers:
        text = (ROOT / f).read_text()
        for marker in ("HOUSE RULES (", "THIS BRAND'S WRITING RULES", "REPAIR HINTS (", "rule_text"):
            assert marker not in text, f"{f} renders writing rules itself ({marker!r})"


# ── the items: loaded once, checked, hashed ───────────────────────────────────────────────────────────────────────

def _rule_row(rid, writer="t9_social", scope="platform", tenant=None, **kw):
    return {"rule_id": rid, "kind": "writer_instruction", "writer": writer, "scope": scope, "tenant_id": tenant,
            "applies_to": [], "gate_class": None, "rule_text": f"Cut the filler of {rid}.", "replacement": None, **kw}


def test_resolve_loads_checked_items_and_stamps_the_fingerprint():
    rules = [_rule_row("r-2"), _rule_row("r-1")]
    sha = rel.items_sha256(rules)
    db = _FakeDB([_release_row(no=1, cls="rules", rule_ids=["r-1", "r-2"], sha=sha)], rules)
    view = asyncio.run(insertion.resolve(db, "t9_social", TENANT_A))
    assert [i["rule_id"] for i in view["platform"]["items"]] == ["r-1", "r-2"]
    assert "Cut the filler of r-1." in insertion.t9_platform_block(view, "facebook")
    st = insertion.stamp(view)
    assert st["platform"] == {"release_id": P_ID, "release_no": 1, "items_sha256": sha} and st["ignored"] is False
    assert "rule_text" not in json.dumps(st) and "Cut the filler" not in json.dumps(st)


@pytest.mark.parametrize("problem", ["missing", "other_writer", "other_tenant", "changed_text"])
def test_items_failing_a_check_render_nothing(problem):
    rules = [_rule_row("r-1", scope="tenant", tenant=TENANT_A)]
    sha = rel.items_sha256(rules)
    if problem == "missing":
        rules = []
    elif problem == "other_writer":
        rules = [{**rules[0], "writer": "t9_blog"}]
    elif problem == "other_tenant":
        rules = [{**rules[0], "tenant_id": TENANT_B}]
    else:
        rules = [{**rules[0], "rule_text": "Edited after approval."}]
    row = _release_row("tenant", 2, "rules", ["r-1"], sha, tenant=TENANT_A)
    view = asyncio.run(insertion.resolve(_FakeDB([_release_row(), row], rules), "t9_social", TENANT_A))
    assert "items" not in view["tenant"]
    assert insertion.t9_tenant_lines(view, "facebook") == [] and insertion.stamp(view)["ignored"] is True


def test_a_rules_db_error_fails_open():
    class Boom(_FakeDB):
        async def fetch(self, sql, *args):
            if "writing_rule" in sql:
                raise RuntimeError("connection reset")
            return await super().fetch(sql, *args)
    view = asyncio.run(insertion.resolve(Boom([_release_row(no=1, cls="rules", rule_ids=["r-1"], sha="x")]), "a1"))
    assert view["platform"]["release_no"] == 1 and insertion.a1_house_rules(view) == ""


def test_r2_parts_carry_the_items():
    from services.writing_memory.r2 import build_views
    items = [_item("r-1")]
    cand = {"release_id": "c", "release_no": 2, "content_class": "rules", "rule_ids": ["r-1"], "scope": "platform",
            "items": items, "items_sha256": rel.items_sha256(items)}
    _ctl, view = build_views("t9_blog", {"platform": None, "tenant": None}, cand)
    assert insertion.t9_platform_block(view, "blog").endswith("- Cut every sentence that only restates the title.")


# ── the post-activation check ─────────────────────────────────────────────────────────────────────────────────────

def _rel(**kw):
    items = [_item("r-1", text="Never stack three adjectives before a noun.")]
    r = {"release_id": P_ID, "writer": "t9_social", "scope": "platform", "tenant_id": None, "release_no": 1,
         "content_class": "rules", "rule_ids": ["r-1"], "items_sha256": rel.items_sha256(items), "status": "active",
         "activated_at": NOW, "retired_at": None, "previous_release_id": "prev"}
    r.update(kw)
    return r


def _write(ref, text="A calm morning walk along the old harbour wall.", stamp="carried", tenant=TENANT_A, **kw):
    r = _rel()
    stamps = {"carried": {"writer": "t9_social", "platform": {"release_id": P_ID, "release_no": 1,
                                                                 "items_sha256": r["items_sha256"]},
                          "tenant": None, "ignored": False},
              "other": {"writer": "t9_social", "platform": {"release_id": "r0", "release_no": 0}, "tenant": None,
                        "ignored": False},
              "ignored": {"writer": "t9_social", "platform": {"release_id": P_ID, "release_no": 1}, "tenant": None,
                          "ignored": True},
              "wrong": {"writer": "t9_social", "platform": {"release_id": P_ID, "release_no": 1,
                                                               "items_sha256": "0" * 64}, "tenant": None,
                        "ignored": False},
              "none": None}
    return {"ref": ref, "source": "content_piece", "tenant_id": tenant, "created_at": NOW, "stamp": stamps[stamp],
            "text": text, "allowed_text": "", **kw}


FOREIGN = {TENANT_B: {"brand_name": "Zephyrine Voyages", "texts": []}}


def test_carriage_kinds():
    r = _rel()
    assert [pa.carriage(r, _write("x", stamp=s)["stamp"]) for s in ("carried", "other", "ignored", "wrong", "none")] \
        == ["carried", "not_carried", "not_rendered", "fingerprint_mismatch", "not_carried"]


def test_check_write_flags_qa_traces_leaks_and_rule_text():
    texts = [("r-1", "Never stack three adjectives before a noun.")]
    ok = pa.check_write(_rel(), _write("a"), rule_texts=texts, foreign=FOREIGN)
    assert ok["violation"] == [] and ok["carriage"] == "carried"
    trace = pa.check_write(_rel(), _write("b", text="Morning walk. writing_release: 1"), rule_texts=texts,
                           foreign=FOREIGN)
    assert trace["violation"] == ["qa_trace"]
    rule = pa.check_write(_rel(), _write("c", text="Never stack three adjectives before a noun. Then walk."),
                          rule_texts=texts, foreign=FOREIGN)
    assert "rule:r-1" in rule["qa_artefacts"]
    leak = pa.check_write(_rel(), _write("d", text="Travel with Zephyrine Voyages this spring."), rule_texts=texts,
                          foreign=FOREIGN)
    assert leak["violation"] == ["tenant_leak"]
    own = pa.check_write(_rel(), _write("e", text="Travel with Zephyrine Voyages.", tenant=TENANT_B),
                         rule_texts=texts, foreign=FOREIGN)
    assert own["violation"] == []                    # a tenant's own brand is never a leak
    skipped = pa.check_write(_rel(), _write("f", text="gate_ledger", stamp="other"), rule_texts=texts,
                             foreign=FOREIGN)
    assert skipped["violation"] == [] and skipped["carriage"] == "not_carried"


def test_verdicts():
    r = _rel()
    row = {"carriage": "carried", "violation": [], "ref": "x", "source": "content_piece"}
    assert pa.verdict(r, [row] * 3, n=3)["verdict"] == "pass"
    assert pa.verdict(r, [row] * 2 + [{**row, "carriage": "not_carried"}], n=3)["verdict"] == "pending"
    bad = pa.verdict(r, [row, {**row, "violation": ["qa_trace"]}], n=3)
    assert bad["verdict"] == "violation" and bad["violation_codes"] == {"qa_trace": 1}
    assert bad["violating_refs"] == [{"source": "content_piece", "ref": "x", "codes": ["qa_trace"]}]
    assert pa.verdict(_rel(content_class="empty", rule_ids=[]), [], n=3)["verdict"] == "not_applicable"


class _Tx:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


class _Conn:
    def transaction(self):
        return _Tx()


class _Pool:
    def __init__(self):
        self.conn = _Conn()

    def acquire(self):
        pool = self

        class _A:
            async def __aenter__(self):
                return pool.conn

            async def __aexit__(self, *exc):
                return False
        return _A()


def _wire(monkeypatch, release, writes, *, rollback=None, calls=None):
    calls = calls if calls is not None else []

    async def get_release(conn, rid, for_update=False):
        return release

    async def writes_since(conn, r, limit=200):
        return writes

    async def rule_texts(conn, r):
        return [("r-1", "Never stack three adjectives before a noun.")]

    async def fingerprints(conn):
        return FOREIGN

    async def default_rollback(conn, rid, *, actor, reason):
        calls.append(("rollback", rid, reason))
        return {"release_id": "prev", "release_no": 0}

    async def retire(conn, rid, *, actor, reason):
        calls.append(("retire", rid))
        return {}

    async def memory(conn, writer, enabled, *, actor):
        calls.append(("memory", writer, enabled))
        return {}

    async def audit(conn, **kw):
        calls.append(("audit", kw["action"], kw["details"]))

    async def alert(pool, r, report):
        calls.append(("alert", report["action"]))

    monkeypatch.setattr(rel, "get_release", get_release)
    monkeypatch.setattr(rel, "rollback", rollback or default_rollback)
    monkeypatch.setattr(rel, "retire_without_previous", retire)
    monkeypatch.setattr(rel, "set_memory_enabled", memory)
    monkeypatch.setattr(pa, "writes_since", writes_since)
    monkeypatch.setattr(pa, "release_rule_texts", rule_texts)

    async def no_exemplars(conn, release):       # AA-921: these releases pin no example
        return []
    monkeypatch.setattr(pa, "release_exemplar_texts", no_exemplars)
    monkeypatch.setattr(pa, "tenant_fingerprints", fingerprints)
    monkeypatch.setattr(pa, "_alert", alert)
    import services.acp_shared.audit_log as al
    monkeypatch.setattr(al, "write_audit_log", audit)
    return calls


def test_dry_run_reports_a_violation_and_moves_nothing(monkeypatch):
    calls = _wire(monkeypatch, _rel(), [_write("a"), _write("b", text="F1_grounding failed here")])
    out = asyncio.run(pa.check_release(_Pool(), P_ID, n=20))
    assert out["verdict"] == "violation" and out["action"] == "dry_run" and calls == []
    assert out["violating_refs"][0]["ref"] == "b" and out["cost_usd"] == 0.0


def test_apply_rolls_back_audits_and_alerts(monkeypatch):
    calls = _wire(monkeypatch, _rel(), [_write("a", text="Read writing_release before you go.")])
    out = asyncio.run(pa.check_release(_Pool(), P_ID, apply=True, actor="job:x"))
    assert out["action"] == "rolled_back" and out["active_release_id"] == "prev"
    kinds = [c[0] for c in calls]
    assert kinds == ["rollback", "audit", "alert"]
    assert calls[0][2].startswith("post_activation: qa_trace")
    audit = calls[1][2]
    assert audit["refs"] == [{"source": "content_piece", "ref": "a", "codes": ["qa_trace"]}]
    assert "Read writing_release" not in json.dumps(audit)           # ids and codes only, never text


def _no_previous():
    async def rollback(conn, rid, *, actor, reason):
        raise rel.ReleaseError(409, "no_previous", "none")
    return rollback


def test_no_previous_tenant_release_is_retired_alone(monkeypatch):
    w = _write("a", text="Travel with Zephyrine Voyages.")
    w["stamp"] = {**w["stamp"], "tenant": w["stamp"]["platform"], "platform": None}   # written under the tenant part
    calls = _wire(monkeypatch, _rel(scope="tenant", tenant_id=TENANT_A, previous_release_id=None), [w],
                  rollback=_no_previous())
    out = asyncio.run(pa.check_release(_Pool(), P_ID, apply=True))
    assert out["action"] == "retired_alone" and calls[0] == ("retire", P_ID)


def test_no_previous_platform_release_turns_memory_off(monkeypatch):
    calls = _wire(monkeypatch, _rel(previous_release_id=None), [_write("a", text="gate_ledger")],
                  rollback=_no_previous())
    out = asyncio.run(pa.check_release(_Pool(), P_ID, apply=True))
    assert out["action"] == "memory_off" and calls[0] == ("memory", "t9_social", False)


def test_a_refused_rollback_turns_memory_off_instead_of_leaving_the_release_live(monkeypatch):
    # review S255: the previous release failing its own check (or the DB refusing the swap) must not leave the
    # violating release serving; the writer's kill switch goes off
    async def refused(conn, rid, *, actor, reason):
        raise rel.ReleaseError(409, "reverify", "the previous release needs a reverify")
    w = _write("a", text="Travel with Zephyrine Voyages.")
    w["stamp"] = {**w["stamp"], "tenant": w["stamp"]["platform"], "platform": None}
    calls = _wire(monkeypatch, _rel(scope="tenant", tenant_id=TENANT_A, previous_release_id="prev"), [w],
                  rollback=refused)
    out = asyncio.run(pa.check_release(_Pool(), P_ID, apply=True))
    assert out["action"] == "memory_off" and out["rollback_refused"] == "reverify"
    assert calls[0] == ("memory", "t9_social", False) and [c[0] for c in calls] == ["memory", "audit", "alert"]


def test_items_without_a_release_hash_render_nothing():
    row = {"release_id": "r", "scope": "platform", "tenant_id": None, "rule_ids": ["r-1"], "items_sha256": None}

    class _Db:
        async def fetch(self, sql, ids):
            return [{"rule_id": "r-1", "kind": "writer_instruction", "writer": "a1", "scope": "platform",
                     "tenant_id": None, "applies_to": [], "gate_class": None, "rule_text": "Cut it.",
                     "replacement": None}]
    assert asyncio.run(insertion.load_items(_Db(), row, "a1")) is None


def test_pass_and_release0_move_nothing(monkeypatch):
    calls = _wire(monkeypatch, _rel(), [_write(str(i)) for i in range(20)])
    assert asyncio.run(pa.check_release(_Pool(), P_ID, apply=True))["verdict"] == "pass"
    calls_r0 = _wire(monkeypatch, _rel(content_class="empty", rule_ids=[], release_no=0), [_write("a", "gate_ledger")])
    out = asyncio.run(pa.check_release(_Pool(), P_ID, apply=True))
    assert out["verdict"] == "not_applicable" and calls == [] and calls_r0 == []


def test_sampler_queues_the_check_only_while_a_content_release_is_active(monkeypatch):
    from api.core import ops_metrics_sampler as s
    import shared.jobs.registry as reg
    queued = []

    async def enqueue(pool, kind, payload, **kw):
        queued.append((kind, kw["idempotency_key"]))
        return "job-1", True
    monkeypatch.setattr(reg, "enqueue", enqueue)

    class P:
        def __init__(self, exists):
            self.exists = exists

        async def fetchval(self, sql):
            return self.exists
    kinds = {"writing_memory_post_activation": object()}
    out: dict = {}
    asyncio.run(s._writing_memory_post_activation_part(P(False), kinds, NOW, out))
    assert queued == [] and out == {}
    asyncio.run(s._writing_memory_post_activation_part(P(True), kinds, NOW, out))
    asyncio.run(s._writing_memory_post_activation_part(P(True), {}, NOW, out))
    assert len(queued) == 1 and out["writing_memory_post_activation"] == "job-1"
    assert queued[0][1] == f"writing_memory_post_activation:{int(NOW.timestamp()) // 3600}"


def test_rollback_alert_mail_renders_ids_and_codes_only():
    from shared import ops_alerts
    msg = ops_alerts.render_writing_memory_rollback(
        {"writer": "t9_social", "scope": "platform", "release_no": 3, "release_id": P_ID, "action": "rolled_back",
         "codes": {"qa_trace": 2}, "carried": 7, "period": "2026-10-28"}, to="ops@example.com",
        base_url="https://admin.example", environment="dev")
    assert "release 3 rolled back" in msg.subject and "qa_trace: 2" in msg.text
    from services.jobs import ops_alert_email_job as job
    assert job._RENDERERS[ops_alerts.TEMPLATE_WRITING_MEMORY_ROLLBACK] is ops_alerts.render_writing_memory_rollback


def test_the_job_kind_is_registered_as_background_and_free():
    import services.jobs.writing_memory_job  # noqa: F401 — registers the kinds
    from shared.jobs.registry import get_kind
    spec = get_kind("writing_memory_post_activation")
    assert spec is not None and spec.background and spec.concurrency == 1


def test_this_file_holds_no_golden_fixture_marker():
    from services.golden_set import text_guard
    assert text_guard.markers(Path(__file__).read_text()) == []


# ── the route: DRY RUN by default, apply needs a signed-in admin with ai.admin ─────────────────────────────────────

def test_post_activation_route_dry_run_apply_and_roles(client, monkeypatch):
    calls = []

    async def fake(pool, rid, *, n, apply, actor):
        calls.append((rid, n, apply, actor))
        return {"release_id": rid, "verdict": "pending", "apply": apply, "action": "none", "cost_usd": 0.0}

    monkeypatch.setattr(pa, "check_release", fake)
    path = f"/admin/writing-memory/releases/{RID}/post-activation-check"
    r = client.post(path, headers=_h())
    assert r.status_code == 200 and r.json()["verdict"] == "pending"
    assert calls[-1] == (RID, 20, False, "admin:dry_run")
    r = client.post(path, json={"apply": True, "n": 5}, headers=_h())
    assert r.status_code == 200 and calls[-1] == (RID, 5, True, f"admin:{ADMIN}")
    n_calls = len(calls)
    assert client.post(path, json={"apply": True}, headers=_h(user=None)).status_code == 403   # apply needs an id
    for role in ("reviewer", "content"):
        assert client.post(path, headers=_h(role=role)).status_code == 403, role
    assert client.post(path, json={"n": 0}, headers=_h()).status_code == 422
    assert client.post(path, json={"bogus": 1}, headers=_h()).status_code == 422
    assert len(calls) == n_calls

    async def missing(pool, rid, **kw):
        raise rel.ReleaseError(404, "not_found", "Unknown release")

    monkeypatch.setattr(pa, "check_release", missing)
    r = client.post(path, headers=_h())
    assert r.status_code == 404 and r.json()["detail"]["code"] == "not_found"
