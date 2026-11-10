"""AA-1063 (S276 lane l8) — the source of a T2 claim in the review view (services/ground_truth/review.py).

The S276 gap: the t2@2 claims of a round-2 addendum carry sample ids (t2-001 ...) the addendum numbered from 1 again,
and the golden items of those ids are other T2 versions; the lookup by sample id found nothing (or, by sample id only,
another tour's source). The T2 path reads the claim's own version (version_id, same tenant) and its Master as the T2
writer reads it. Invented tours, tenants and text only; no DB (fakes answer the SQL the resolver sends)."""
import asyncio
import json
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone

from services.ground_truth import claims as cl
from services.ground_truth import panel as pn
from services.ground_truth import review as rv

TENANT = "71000000-0000-4000-8000-000000000001"
OTHER = "72000000-0000-4000-8000-000000000002"
VERSION = "73000000-0000-4000-8000-000000000003"
BASE_VERSION = "74000000-0000-4000-8000-000000000004"
PT = "75000000-0000-4000-8000-000000000005"
FS = "76000000-0000-4000-8000-000000000006"
T0 = datetime(2026, 11, 1, tzinfo=timezone.utc)

T2_OUT = {"name": "Zz Quellmoor Lakes by Bike", "subtitle": "Zz two days around Quellmoor",
          "summary": "Ride from Varnhold to the Quellmoor shore. Stay one night at the Tessaly Lodge.",
          "highlights": ["Zz ride the Quellmoor shore path", "Zz a night at the Tessaly Lodge"],
          "itineraries": "Day 1 — Varnhold to Quellmoor. Day 2 — Quellmoor to Varnhold.",
          "seo_title": "Zz Quellmoor bike tour", "seo_meta": "Zz two days by bike", "trip_type": "cycling"}
PT_ROW = {"id": PT, "tour_id": "77000000-0000-4000-8000-000000000007", "aa_name": "Zz Quellmoor Lakes Ride",
          "aa_subtitle": "Zz lakes by bike", "aa_summary": "Ride from Varnhold to the Quellmoor shore.",
          "aa_description": "A night at the Tessaly Lodge on the lake.", "aa_highlights": '["Zz shore path"]',
          "aa_itineraries": "Day 1 — Varnhold to Quellmoor\nDay 2 — Quellmoor to Varnhold", "seo_title": "Zz ride",
          "seo_meta": "Zz meta", "seo_keywords_used": None, "country": "Zzland", "duration": "2 days",
          "src_name": "Zz Quellmoor raw ride", "src_subtitle": None, "src_summary": "Zz raw: Varnhold to Quellmoor.",
          "src_description": None, "src_highlights": None,
          "src_itineraries": "Day 1: Varnhold to Quellmoor. Day 2: back to Varnhold.", "inclusions": "Zz bike",
          "activities": None, "price_raw": None, "exclusions": None, "group_size": None, "best_time_to_go": None}


def _version(**over):
    return {"version_id": VERSION, "tenant_id": TENANT, "published_tour_id": PT,
            "rewritten_content": json.dumps({**T2_OUT, "flags": ["kept out of the object"]}),
            "created_at": T0, "fs_id": None, **over}


class SourceConn:
    """The reads of review.resolve_sources and review.raw_facts."""

    def __init__(self, *, golden=(), versions=(), masters=None, master_at=None, sheets=None, strict_tenant=True):
        self.golden, self.versions = list(golden), list(versions)
        self.masters = {PT: PT_ROW} if masters is None else masters
        self.master_at = master_at if master_at is not None else T0 - timedelta(days=3)
        self.sheets = sheets or {}
        self.strict_tenant = strict_tenant
        self.calls: list[str] = []

    async def fetch(self, sql, *args):
        self.calls.append(sql)
        if "shared.eval_golden_item" in sql:
            return [g for g in self.golden if g["sample_id"] in args[0]]
        if "acp_shared.content_piece" in sql:
            return []
        if "gold_aa_internal.tenant_tour_versions" in sql:
            return [v for v in self.versions if v["version_id"] in args[0]
                    and (not self.strict_tenant or v["tenant_id"] in args[1])]
        if "silver_aa_internal.generated_content" in sql:
            return [{"published_tour_id": p, "master_at": self.master_at} for p in args[0] if p in self.masters]
        raise AssertionError(f"unexpected SQL: {sql[:80]}")

    async def fetchrow(self, sql, *args):
        self.calls.append(sql)
        assert "gold_aa_internal.published_tours pt" in sql and "WHERE pt.id = $1::uuid" in sql
        return self.masters.get(args[0])

    async def fetchval(self, sql, *args):
        self.calls.append(sql)
        assert "acp_shared.fact_sheet" in sql
        fs_id, tenant = args
        row = self.sheets.get(fs_id)
        return row["render_text"] if row and row["tenant_id"] == tenant else None

    def transaction(self):
        @asynccontextmanager
        async def tx():
            yield None
        return tx()


class FakePool:
    def __init__(self, conn):
        self.conn = conn

    @asynccontextmanager
    async def acquire(self):
        yield self.conn


def _anchor(**over):
    claims = cl.claims_of(T2_OUT)
    c = next(x for x in claims if x.field == "summary")
    return {"claim_hash": c.claim_hash, "writer": "t2", "tenant_id": TENANT, "piece_id": None, "version_id": VERSION,
            "sample_id": "t2-001", "content_sha256": cl.content_sha(T2_OUT), "field": c.field,
            "span_start": c.span_start, "span_end": c.span_end, "claim_text": c.claim_text, **over}


def _golden(**over):
    """The base fixture's item of the same sample id: another version (another tour) of the same tenant."""
    return {"sample_id": "t2-001", "writer": "t2", "channel": None, "tenant_id": TENANT, "trap": False,
            "input": json.dumps({"tour": {"name": "Zz Ostrava Hills Walk", "summary": "Zz another tour"}}),
            "control_output": json.dumps({"name": "Zz Ostrava Hills Walk", "summary": "Zz other text."}),
            "refs": json.dumps({"version_id": BASE_VERSION, "tenant_id": TENANT}), **over}


def _run(conn, anchors):
    return asyncio.run(rv.resolve_sources(FakePool(conn), anchors))


def test_the_version_object_is_the_frozen_projection():
    """select_samples.freeze_t2 freezes these eight keys of rewritten_content: the claims' content sha is theirs."""
    assert rv.t2_object(json.dumps({**T2_OUT, "flags": ["x"], "extra": 1})) == T2_OUT
    assert rv.t2_object({"name": "Zz"}) == {k: ("Zz" if k == "name" else None) for k in rv.T2_OUTPUT_KEYS}
    assert rv.t2_object("not json") == {k: None for k in rv.T2_OUTPUT_KEYS}


def test_the_t2_source_is_the_writer_source_the_panel_read():
    """The bytes the panel read for a frozen T2 sample: labels._source_text of {"input": {"tour": tour_dict}}, where
    the freeze took tour_dict from the T2 job's _prepare (source_tour_dict)."""
    from scripts.aa729.labels import _source_text
    from services.jobs.t2_rewrite_job import source_tour_dict

    text = rv.t2_source_text(PT_ROW)
    assert text == _source_text({"input": {"tour": dict(source_tour_dict(PT_ROW))}})
    assert "Zz Quellmoor Lakes Ride" in text and "Tessaly Lodge" in text and "Zzland" in text
    assert "Zz Quellmoor raw ride" not in text           # the raw source is not what the writer was given


def test_an_addendum_claim_resolves_its_own_version_not_the_colliding_golden_item():
    conn = SourceConn(golden=[_golden()], versions=[_version()])
    a = _anchor()
    found, kinds = _run(conn, [a, {**a, "claim_hash": "e" * 64}])
    assert kinds == {"t2_sha": 1} and set(found) == {a["claim_hash"], "e" * 64}
    f = found[a["claim_hash"]]
    assert f["kind"] == "t2_sha" and f["text"] == rv.t2_source_text(PT_ROW) and "Zz another tour" not in f["text"]
    assert f["output"] == T2_OUT and f["t2"]["version"]["version_id"] == VERSION
    # claim_sources (the pre-filter's view) carries the same text and kind
    got, k2 = asyncio.run(rv.claim_sources(FakePool(conn), [a]))
    assert got == {a["claim_hash"]: (f["text"], "t2_sha")} and k2 == {"t2_sha": 1}
    ctx = rv.context_from(a, f["output"], f["text"])
    assert ctx["found"] and ctx["after"].startswith("Stay one night") and ctx["evidence"]


def test_golden_id_never_names_another_object():
    """Before S276 a T2 claim without a sha match took the same tenant's golden item of its sample id (golden_id):
    the source of another tour. Now a contradicting version id rules it out; an agreeing one is still a candidate."""
    a = _anchor()
    found, kinds = _run(SourceConn(golden=[_golden()]), [a])      # no version row
    assert found == {} and kinds == {"unmatched": 1}
    found, kinds = _run(SourceConn(golden=[_golden(refs=json.dumps({"version_id": VERSION}))]), [a])
    assert kinds == {"golden_id": 1} and "Zz another tour" in found[a["claim_hash"]]["text"]
    found, kinds = _run(SourceConn(golden=[_golden(refs=json.dumps({}))]), [a])   # nothing to contradict
    assert kinds == {"golden_id": 1}


def test_a_matching_golden_item_still_wins_and_an_edited_version_is_t2_id():
    a = _anchor()
    golden = _golden(control_output=json.dumps(T2_OUT), refs=json.dumps({"version_id": VERSION}),
                     input=json.dumps({"tour": {"name": "Zz frozen tour text"}}))
    found, kinds = _run(SourceConn(golden=[golden], versions=[_version()]), [a])
    assert kinds == {"golden_sha": 1} and "Zz frozen tour text" in found[a["claim_hash"]]["text"]
    assert found[a["claim_hash"]]["t2"] is None
    edited = _version(rewritten_content=json.dumps({**T2_OUT, "summary": "Zz edited by the agency."}))
    found, kinds = _run(SourceConn(versions=[edited]), [a])
    assert kinds == {"t2_id": 1} and found[a["claim_hash"]]["text"] == rv.t2_source_text(PT_ROW)


def test_tenant_isolation_and_missing_rows():
    a = _anchor()
    # the SQL names the claim tenants only ...
    conn = SourceConn(versions=[_version(tenant_id=OTHER)])
    assert _run(conn, [a]) == ({}, {"unmatched": 1})
    sql = next(c for c in conn.calls if "tenant_tour_versions" in c)
    assert "v.tenant_id = ANY($2::uuid[])" in sql
    # ... and a row of another tenant that came back anyway is still refused per claim
    conn = SourceConn(versions=[_version(tenant_id=OTHER)], strict_tenant=False)
    assert _run(conn, [a]) == ({}, {"unmatched": 1})
    # another tenant's claim on the same version id never gets this tenant's source
    assert _run(SourceConn(versions=[_version()]), [_anchor(tenant_id=OTHER)]) == ({}, {"unmatched": 1})
    # a version whose Master is gone, a claim without a version id: no source (no tour-only fallback)
    assert _run(SourceConn(versions=[_version()], masters={}), [a]) == ({}, {"unmatched": 1})
    conn = SourceConn(versions=[_version()])
    assert _run(conn, [_anchor(version_id=None)]) == ({}, {"unmatched": 1})
    assert not any("tenant_tour_versions" in c for c in conn.calls)


def test_non_t2_claims_never_read_the_t2_tables():
    """The CL-1 (T9) and L2 (A1) paths are unchanged: a T9 or A1 claim with a version id still reads no T2 row."""
    tour = {"name": "Zz Velmora Walk", "itinerary": ["Day 1: Arrive in Velmora."]}
    out = {"summary": "Arrive in Velmora."}
    golden = {"sample_id": "s1-001", "writer": "a1", "channel": None, "tenant_id": None, "trap": False,
              "input": json.dumps({"tour": tour}), "control_output": json.dumps(out), "refs": json.dumps({})}
    c = cl.claims_of(out)[0]
    a1 = {"claim_hash": c.claim_hash, "writer": "a1", "tenant_id": None, "piece_id": None, "version_id": VERSION,
          "sample_id": "s1-001", "content_sha256": cl.content_sha(out)}
    conn = SourceConn(golden=[golden], versions=[_version()])
    found, kinds = _run(conn, [a1, _anchor(writer="t9_blog", sample_id="zz-none")])
    assert kinds == {"golden_sha": 1, "unmatched": 1} and found[c.claim_hash]["kind"] == "golden_sha"
    assert not any("tenant_tour_versions" in s or "published_tours" in s for s in conn.calls)


def test_item_context_names_the_kind_flags_a_changed_master_and_shows_raw_facts():
    a = _anchor()

    class ItemConn(SourceConn):
        async def fetchrow(self, sql, *args):
            if "shared.writing_memory_disagreement" in sql:
                assert "l.version_id::text AS version_id" in sql and "l.tenant_id::text AS tenant_id" in sql
                return {k: a[k] for k in ("claim_hash", "claim_text", "field", "span_start", "span_end", "sample_id",
                                          "content_sha256", "writer", "tenant_id", "piece_id", "version_id")}
            return await super().fetchrow(sql, *args)

    item = "78000000-0000-4000-8000-000000000008"
    sheets = {FS: {"tenant_id": TENANT, "render_text": "Zz FACTS: 2 days; Varnhold; Quellmoor."}}
    conn = ItemConn(versions=[_version(fs_id=FS)], sheets=sheets)
    ctx = asyncio.run(rv.item_context(conn, item))
    assert ctx["item_id"] == item and ctx["found"] and ctx["source_kind"] == "t2_sha"
    assert ctx["source_text"] == rv.t2_source_text(PT_ROW) and ctx["master_changed"] is False
    assert ctx["raw_facts"] == sheets[FS]["render_text"] and ctx["raw_facts_kind"] == "stored"
    # a sheet of another tenant is never read; the sheet is rebuilt from the raw tour row; the Master changed later
    conn = ItemConn(versions=[_version(fs_id=FS)], sheets={FS: {**sheets[FS], "tenant_id": OTHER}},
                    master_at=T0 + timedelta(days=1))
    ctx = asyncio.run(rv.item_context(conn, item))
    assert ctx["raw_facts_kind"] == "rebuilt" and "Zz FACTS" not in ctx["raw_facts"] and "Varnhold" in ctx["raw_facts"]
    assert ctx["master_changed"] is True
    # an unresolved claim keeps the old shape (found false) with no kind
    conn = ItemConn()
    ctx = asyncio.run(rv.item_context(conn, item))
    assert ctx == {"item_id": item, "found": False, "before": None, "after": None, "evidence": [],
                   "source_text": None, "source_kind": None}


def test_the_export_line_and_the_resolved_context_agree():
    """What the panel got (panel.export_records on the frozen sample) and what the page shows for the same claim."""
    from services.jobs.t2_rewrite_job import source_tour_dict

    src = rv.source_text("t2", None, {"tour": dict(source_tour_dict(PT_ROW))})
    lines = pn.export_records(set_id="t2@2", writer="t2", scope="tenant", tenant_id=TENANT,
                              refs={"sample_id": "t2-001", "version_id": VERSION, "tour_id": PT_ROW["tour_id"]},
                              output=T2_OUT, source_text=src)
    found, _ = _run(SourceConn(versions=[_version()]), [_anchor()])
    for e in lines:
        claim = {"field": e["field"], "span_start": e["span"][0], "span_end": e["span"][1],
                 "claim_text": e["claim_text"]}
        f = found[_anchor()["claim_hash"]]
        ctx = rv.context_from(claim, f["output"], f["text"])
        assert ctx["source_text"] == e["source_text"] and ctx["evidence"] == e["evidence"]
        assert ctx["before"] == e["context"]["before"] and ctx["after"] == e["context"]["after"]
