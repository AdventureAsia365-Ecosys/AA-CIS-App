"""AA-982 — the T9 blog chain drops the place_paa questions A3 Jev already rejected for the piece.

The verdicts are the ones A3 (services/acp_contract/atom_ranking.py) stored in shared.decision_log;
the chain reads them back (shared/llm_client/decide.platform_verdicts) and never calls Jev."""
import uuid
from datetime import datetime, timezone
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from services.acp_content_writing import quality_gates as qg
from services.acp_content_writing import service as writing_service
from services.acp_contract import atom_ranking as ar
from services.acp_shared import real_questions as rq
from shared.llm_client import decide as dec
from shared.llm_client.decide import Verdict

ABOUT, FOREIGN, LAND = ar.SCOPE_HERE_Q, ar.SCOPE_FOREIGN_Q, ar.LANDING_Q
MOMENT_LAKE = ar.landing_moment("Lake Khövsgöl", "trek the shore")
MOMENT_CAMP = ar.landing_moment("Tsaatan camp", "visit")
LAKE = rq.A3Segment(MOMENT_LAKE, ("Mongolia",))
CAMP = rq.A3Segment(MOMENT_CAMP, ("Mongolia",))


def _v(key, zone, mode="enforce", p=None):
    return Verdict(key, mode, zone, probability=p)


def _about(scope, q, zone, mode="enforce"):
    return {(ABOUT, ar.scope_subject_key(scope, q)): _v(ABOUT, zone, mode)}


def _land(moment, q, zone, mode="enforce"):
    return {(LAND, ar.landing_subject_key(moment, q)): _v(LAND, zone, mode)}


Q = "What do Buddhists eat?"


# ---------------------------------------------------------------- the rule (pure)

def test_subject_keys_are_the_ones_a3_writes():
    assert ar.scope_subject_key(ar.country_scope(["Thailand", "Laos", None, "Laos"]), Q) == \
        "paa:Laos, Thailand:What do Buddhists eat?"
    assert ar.landing_subject_key(MOMENT_LAKE, Q) == f"land:{MOMENT_LAKE}:{Q}"
    assert ar.scope_subject_key("Laos", "x" * 400).endswith(":" + "x" * 300)


def test_about_here_reject_for_the_piece_countries_drops():
    assert rq.a3_rejections([Q], ["Mongolia"], [LAKE], _about("Mongolia", Q, "reject")) == {Q: "about_here"}


def test_about_here_reject_for_a_larger_segment_set_drops_a_smaller_one_does_not():
    gt = rq.A3Segment("Golden Triangle — boat trip", ("Laos", "Myanmar", "Thailand"))
    laos = rq.A3Segment("Luang Prabang — alms", ("Laos",))
    larger = _about("Laos, Myanmar, Thailand", Q, "reject")
    assert rq.a3_rejections([Q], ["Laos"], [gt], larger) == {Q: "about_here"}
    # a Laos-only reject says nothing about a Laos + Thailand piece
    assert rq.a3_rejections([Q], ["Laos", "Thailand"], [laos], _about("Laos", Q, "reject")) == {}


def test_foreign_accept_drops_like_a3_scope_dropped():
    verdicts = {(FOREIGN, ar.scope_subject_key("Mongolia", Q)): _v(FOREIGN, "accept")}
    assert rq.a3_rejections([Q], ["Mongolia"], [LAKE], verdicts) == {Q: "about_here"}


def test_shadow_grey_and_missing_verdicts_keep_the_question():
    assert rq.a3_rejections([Q], ["Mongolia"], [LAKE], _about("Mongolia", Q, "reject", mode="shadow")) == {}
    assert rq.a3_rejections([Q], ["Mongolia"], [LAKE], _about("Mongolia", Q, "grey")) == {}
    assert rq.a3_rejections([Q], ["Mongolia"], [LAKE], {}) == {}
    assert rq.a3_rejections([Q], [], [], _about("Mongolia", Q, "reject")) == {}  # no country, no moment


def test_landing_drops_only_when_every_moment_rejects():
    both = {**_land(MOMENT_LAKE, Q, "reject"), **_land(MOMENT_CAMP, Q, "reject")}
    assert rq.a3_rejections([Q], ["Mongolia"], [LAKE, CAMP], both) == {Q: "landing"}
    one = {**_land(MOMENT_LAKE, Q, "reject"), **_land(MOMENT_CAMP, Q, "grey")}
    assert rq.a3_rejections([Q], ["Mongolia"], [LAKE, CAMP], one) == {}
    missing = _land(MOMENT_LAKE, Q, "reject")  # the camp moment never judged it
    assert rq.a3_rejections([Q], ["Mongolia"], [LAKE, CAMP], missing) == {}


def test_landing_reject_does_not_drop_a_question_about_here_accepts():
    verdicts = {**_land(MOMENT_LAKE, Q, "reject"), **_about("Mongolia", Q, "accept")}
    assert rq.a3_rejections([Q], ["Mongolia"], [LAKE], verdicts) == {}


# ---------------------------------------------------------------- merge

def test_merge_skips_dropped_questions_after_segment_match_and_fills_the_room():
    seg = ["How deep is Lake Khövsgöl?"]
    place = ["Is Ulaanbaatar safe at night?", "Can you swim in Lake Khövsgöl?", "Is Lake Khövsgöl worth visiting?"]
    tour = ["Is Ulaanbaatar safe at night?", "How many days do you need in Mongolia?"]
    drop = {rq.question_key(place[0]): "about_here", rq.question_key(seg[0]): "landing"}
    stats: dict = {}
    out = rq.merge([(rq.SOURCE_SEGMENT, seg), (rq.SOURCE_PLACE, place), (rq.SOURCE_TOUR, tour)],
                   per_source=2, drop=drop, stats=stats)
    assert [(r.question, r.source) for r in out] == [
        ("How deep is Lake Khövsgöl?", "segment_match"),  # passed A3 for its own Segment: kept
        ("Can you swim in Lake Khövsgöl?", "place_paa"),
        ("Is Lake Khövsgöl worth visiting?", "place_paa"),  # takes the dropped one's room
        ("How many days do you need in Mongolia?", "tour_paa"),  # the rejected text cannot come back here
    ]
    assert stats == {"a3_dropped": {"place_paa": 1}}


# ---------------------------------------------------------------- the chain against a fake DB

REJECTED = "What do Buddhists eat?"
ACCEPTED = "How deep is Lake Khövsgöl?"
NO_VERDICT = "Can you swim in Lake Khövsgöl?"
HASHES = {ABOUT: "h-about", FOREIGN: "h-foreign", LAND: "h-land"}


class _Conn:
    """Answers the chain's queries and the decision_log / decision_question reads."""

    def __init__(self, *, place_qs, log, modes=None):
        self.place_qs, self.log, self.modes = place_qs, log, modes or {}
        self.calls = []

    async def fetch(self, sql, *args):
        self.calls.append((sql, args))
        if "FROM shared.decision_question" in sql:
            return [{"question_key": k, "stage": "a3", "kind": "noul", "instructions": "i", "criteria": None,
                     "mode": self.modes.get(k, "enforce"), "accept_floor": None,
                     "reject_ceiling": 0.4 if k == LAND else 0.3, "threshold_version": 2, "question_hash": h}
                    for k, h in HASHES.items()]
        if "jev_tenant_allowlist" in sql:
            return []
        if "FROM shared.decision_log" in sql:
            subjects, keys, hashes = set(args[0]), args[1], args[2]
            want = dict(zip(keys, hashes))
            return [{"subject_key": sk, "question_key": k, "probability": p, "choice": None, "probabilities": None}
                    for (k, sk, h), p in self.log.items() if sk in subjects and want.get(k) == h]
        if "v_active_tour_atoms" in sql:
            return [{"segment_id": "seg-lake", "canonical_place": "Lake Khövsgöl",
                     "canonical_action": "trek the shore", "countries": ["Mongolia"]}]
        if "acp_contract.atom_matches" in sql:
            return []
        if "acp_contract.search_demand" in sql:
            return [{"keyword": "lake khovsgol", "market": "US", "search_volume": 1900,
                     "people_also_ask": list(self.place_qs)}]
        if "acp_contract.atom_segment_alias" in sql:
            return [{"segment_id": "seg-lake", "canonical_place": "Lake Khövsgöl"}]
        if "acp_contract.tour_atoms" in sql:
            return [{"place": "Khövsgöl Lake", "country": "Mongolia"}]
        raise AssertionError(sql)


def _pool(conn):
    pool = MagicMock()

    @asynccontextmanager
    async def acquire():
        yield conn
    pool.acquire = acquire
    return pool


@pytest.fixture(autouse=True)
def _fresh_decide_config(monkeypatch):
    monkeypatch.setattr(dec, "_questions_loaded_at", 0.0)
    monkeypatch.setattr(dec, "_allowlist_loaded_at", 0.0)


def _log(**over):
    log = {
        (ABOUT, ar.scope_subject_key("Mongolia", REJECTED), HASHES[ABOUT]): 0.05,   # reject (≤ 0.3)
        (ABOUT, ar.scope_subject_key("Mongolia", ACCEPTED), HASHES[ABOUT]): 0.97,   # grey (no accept floor)
        (LAND, ar.landing_subject_key(MOMENT_LAKE, ACCEPTED), HASHES[LAND]): 0.80,  # grey
    }
    log.update(over)
    return log


async def _chain(conn, **kw):
    return await rq.collect_real_questions(_pool(conn), atom_ids=["atom-1"], tour_paa=[], **kw)


@pytest.mark.asyncio
async def test_chain_drops_the_rejected_place_question_and_keeps_the_rest():
    conn = _Conn(place_qs=[REJECTED, ACCEPTED, NO_VERDICT], log=_log())
    stats: dict = {}
    out = await _chain(conn, a3_filter=True, stats=stats)
    assert [(r.question, r.source) for r in out] == [(ACCEPTED, "place_paa"), (NO_VERDICT, "place_paa")]
    assert stats["a3_dropped"] == {"place_paa": 1}
    assert stats["a3_rejected"] == {"about_here": 1}
    assert (stats["a3_looked_up"], stats["a3_with_verdict"], stats["a3_scopes"], stats["a3_moments"]) == (3, 2, 1, 1)
    assert (stats["a3_countries"], stats["a3_segment_ids"]) == (["Mongolia"], ["seg-lake"])
    log_sql, log_args = next(c for c in conn.calls if "FROM shared.decision_log" in c[0])
    assert "NOT l.cached" in log_sql and "l.tenant_id IS NULL" in log_sql
    assert log_args[3] == dec.PLATFORM_TENANT_ID and log_args[4] == dec.CACHE_MAX_AGE_DAYS


@pytest.mark.asyncio
async def test_chain_without_the_filter_is_unchanged():
    conn = _Conn(place_qs=[REJECTED, ACCEPTED], log=_log())
    out = await _chain(conn)
    assert [r.question for r in out] == [REJECTED, ACCEPTED]
    assert not any("decision_log" in sql for sql, _ in conn.calls)


@pytest.mark.asyncio
async def test_a_verdict_under_old_wording_or_a_question_switched_off_keeps_the_question():
    old_wording = _Conn(place_qs=[REJECTED], log={(ABOUT, ar.scope_subject_key("Mongolia", REJECTED), "old"): 0.05})
    assert [r.question for r in await _chain(old_wording, a3_filter=True)] == [REJECTED]
    dec._questions_loaded_at = 0.0
    off = _Conn(place_qs=[REJECTED], log=_log(), modes={ABOUT: "off", FOREIGN: "off"})
    assert [r.question for r in await _chain(off, a3_filter=True)] == [REJECTED]
    dec._questions_loaded_at = 0.0
    shadow = _Conn(place_qs=[REJECTED], log=_log(), modes={ABOUT: "shadow"})
    assert [r.question for r in await _chain(shadow, a3_filter=True)] == [REJECTED]


@pytest.mark.asyncio
async def test_a_failed_verdict_read_keeps_every_question():
    conn = _Conn(place_qs=[REJECTED, ACCEPTED], log=_log())
    stats: dict = {}
    with patch.object(rq, "platform_verdicts", new=AsyncMock(side_effect=RuntimeError("db down"))):
        out = await _chain(conn, a3_filter=True, stats=stats)
    assert [r.question for r in out] == [REJECTED, ACCEPTED]
    assert stats["a3_error"] == "db down" and stats["a3_dropped"] == {}


def test_kill_switch(monkeypatch):
    monkeypatch.delenv(rq.A3_FILTER_ENV, raising=False)
    assert rq.a3_filter_enabled()
    for off in ("off", "0", "false", "NO"):
        monkeypatch.setenv(rq.A3_FILTER_ENV, off)
        assert not rq.a3_filter_enabled()


# ---------------------------------------------------------------- the writer prompt (T9 job)

SEED = ("Lake Khövsgöl — trekking along the shore\n"
        "Location: Khövsgöl Lake, Khövsgöl Province, Mongolia (do not place it in any other town, region or "
        "country)\nSource itinerary: Trek the lakeshore; Buddhists eat with the herders at the camp.\n"
        "Place background (checked facts about the place itself — you may use these):\n"
        "- Lake Khövsgöl is up to 262 metres deep.\n- Buddhists of the area eat dairy and mutton.\n")
FAQ_TEXT = qg.replace_faq_section(
    "Lake Khövsgöl holds most of Mongolia's fresh water.\n",
    [("How deep is Lake Khövsgöl?", "Lake Khövsgöl is up to 262 metres deep."),
     ("Can you swim in Lake Khövsgöl?", "Only in summer; the water stays cold."),
     ("What should families pack?", "Warm layers for the cold evenings.")])


async def _writer_questions(chain):
    ctx = {"atom_text": SEED, "goal": {"key": "promotion"}, "channel_style": {"channel": "blog"},
           "brand_audience": {}, "chosen": {}, "cta": "Ask Terra to plan this trip.", "destination": None,
           "trip_name": None, "brand_rubric_text": "", "channel": "blog", "atom_id": "a1",
           "people_also_ask": [r.question for r in chain], "real_questions": [r.as_dict() for r in chain],
           "a3_question_filter": {"a3_dropped": {"place_paa": 1}}}
    row = {"piece_id": str(uuid.uuid4()), "attempt_number": 1, "status": "approved"}
    passed = {"passed": True, "gate_ledger": [], "first_failure": None, "flags": []}
    enforce = AsyncMock(side_effect=lambda t, **k: (t, 0.0, []))
    with patch.object(writing_service, "write_content", return_value=(FAQ_TEXT, 0.01, {}, None)) as wc, \
         patch.object(writing_service, "_fit_seo_meta", new=AsyncMock(side_effect=lambda m, *a, **k: (m, 0.0))), \
         patch.object(writing_service, "_enforce_faq_contract", new=enforce), \
         patch.object(writing_service, "run_quality_gates", return_value=passed), \
         patch.object(writing_service, "_finalize_piece", new=AsyncMock(return_value=row)) as fin:
        await writing_service.run_write_background(uuid.uuid4(), uuid.uuid4(), ctx, pool=MagicMock())
    return wc.call_args.kwargs.get("search_questions") or [], fin.call_args.kwargs["writer_input"]


@pytest.mark.asyncio
async def test_rejected_place_question_never_reaches_the_writer_prompt_accepted_still_does():
    # Without the filter the rejected question is in scope for this seed and reaches the writer…
    unfiltered = await _chain(_Conn(place_qs=[REJECTED, ACCEPTED], log=_log()))
    before, _ = await _writer_questions(unfiltered)
    assert REJECTED in before and ACCEPTED in before
    # …with it, only the accepted one does.
    dec._questions_loaded_at = 0.0
    filtered = await _chain(_Conn(place_qs=[REJECTED, ACCEPTED], log=_log()), a3_filter=True)
    after, writer_input = await _writer_questions(filtered)
    assert REJECTED not in after and ACCEPTED in after
    assert writer_input["a3_question_filter"] == {"a3_dropped": {"place_paa": 1}}
    assert all(s["question"] != REJECTED for s in writer_input["search_questions"])


# ---------------------------------------------------------------- start_write wiring

def _start_write_pool():
    conn = AsyncMock()
    placeholder = {"piece_id": uuid.uuid4(), "tenant_id": uuid.uuid4(), "angle_gate_request_id": uuid.uuid4(),
                   "attempt_number": 1, "content_text": "", "status": "processing", "held_reason": None,
                   "gate_ledger": [], "repair_log": [], "created_at": datetime.now(timezone.utc)}
    conn.fetchrow.side_effect = [{"text": "atom text"}, placeholder]
    conn.fetch.return_value = []
    ctx = AsyncMock()
    ctx.__aenter__ = AsyncMock(return_value=conn)
    ctx.__aexit__ = AsyncMock(return_value=False)
    pool = MagicMock()
    pool.acquire = MagicMock(return_value=ctx)
    return pool


async def _start_blog_write(monkeypatch, env=None):
    if env is None:
        monkeypatch.delenv(rq.A3_FILTER_ENV, raising=False)
    else:
        monkeypatch.setenv(rq.A3_FILTER_ENV, env)
    req = {"request_id": str(uuid.uuid4()), "tenant_id": str(uuid.uuid4()), "atom_id": "atom-1", "trip_id": None,
           "channel": "blog", "goal": "promotion", "cta": "Ask us", "status": "approved",
           "angles": [{"idx": 0, "name": "A", "chosen": True}], "route_segment_ids": None,
           "dfs_paa_snapshot": {"people_also_ask": ["How many days do you need in Mongolia?"]}}
    chain = [rq.RealQuestion(ACCEPTED, rq.SOURCE_PLACE), rq.RealQuestion("How many days do you need in Mongolia?",
                                                                        rq.SOURCE_TOUR)]

    async def fake_collect(pool, **kw):
        if kw.get("stats") is not None:
            kw["stats"].update({"a3_looked_up": 2, "a3_rejected": {"about_here": 1}, "a3_dropped": {"place_paa": 1}})
        return chain
    collect = AsyncMock(side_effect=fake_collect)
    goal = {"key": "promotion", "name": "P", "description": "d", "logic": "AIDA", "marketing_term": "AIDA"}
    with patch.object(writing_service.angle_gate_service, "fetch_request", new=AsyncMock(return_value=req)), \
         patch.object(writing_service, "fetch_brand_audience", new=AsyncMock(return_value={})), \
         patch.object(writing_service, "fetch_brand_rubric_text", new=AsyncMock(return_value="rubric")), \
         patch.object(writing_service, "get_goal", return_value=goal), \
         patch.object(writing_service, "collect_real_questions", new=collect), \
         patch.object(writing_service.logger, "info") as log_info:
        result = await writing_service.start_write(uuid.uuid4(), uuid.uuid4(), _start_write_pool())
    return result["context"], collect.call_args.kwargs, log_info


@pytest.mark.asyncio
async def test_start_write_turns_the_filter_on_for_a_blog_and_logs_the_drop(monkeypatch):
    context, kwargs, log_info = await _start_blog_write(monkeypatch)
    assert kwargs["a3_filter"] is True
    assert context["a3_question_filter"] == {"a3_looked_up": 2, "a3_rejected": {"about_here": 1},
                                             "a3_dropped": {"place_paa": 1}, "place_paa_kept": 1}
    event = next(c for c in log_info.call_args_list if c.args and c.args[0] == "t9_place_paa_a3_filter")
    assert event.kwargs["dropped"] == 1 and event.kwargs["place_paa_kept"] == 1 and event.kwargs["piece_id"]


@pytest.mark.asyncio
async def test_start_write_kill_switch_leaves_the_chain_unfiltered(monkeypatch):
    context, kwargs, _ = await _start_blog_write(monkeypatch, env="off")
    assert kwargs["a3_filter"] is False and kwargs["stats"] is None
    assert "a3_question_filter" not in context


# ---------------------------------------------------------------- the Dev check script

@pytest.mark.asyncio
async def test_check_script_flags_a_rejected_question_that_reached_the_writer():
    import importlib.util
    spec = importlib.util.spec_from_file_location("aa982_check", "scripts/aa982_place_paa_check.py")
    script = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(script)
    record = {"a3_countries": ["Mongolia"], "a3_segment_ids": ["seg-lake"], "a3_dropped": {"place_paa": 1},
              "place_paa_kept": 1}
    row = {"piece_id": uuid.uuid4(), "status": "approved", "tour_id": uuid.uuid4(),
           "created_at": datetime.now(timezone.utc), "writer_input": {
               "a3_question_filter": record,
               "real_questions": [{"q": ACCEPTED, "source": "place_paa"}],
               "search_questions": [{"question": ACCEPTED, "source": "place_paa"}]}}
    good = await script.check_piece(_Conn(place_qs=[], log=_log()), row, rq)
    assert good["ok"] and good["dropped"] == 1 and good["rejected_to_writer"] == []
    dec._questions_loaded_at = 0.0
    row["writer_input"]["search_questions"].append({"question": REJECTED, "source": "place_paa"})
    bad = await script.check_piece(_Conn(place_qs=[], log=_log()), row, rq)
    assert not bad["ok"] and bad["rejected_to_writer"] == [REJECTED]
    row["writer_input"].pop("a3_question_filter")
    assert not (await script.check_piece(_Conn(place_qs=[], log=_log()), row, rq))["ok"]
