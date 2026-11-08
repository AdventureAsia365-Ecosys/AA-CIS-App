"""AA-1048 W2 (S273) — the claim-label import through the staff API without a DB: the payload the CLI builds, the line
checks, the idempotency plan, import_labels on a fake connection, the routes (policy, 422 / 409, counts only) and the
CLI `payload` command. Invented names and places only."""
import asyncio
import json
from contextlib import asynccontextmanager

import pytest

from services.ground_truth import importer
from services.ground_truth import panel as pn
from services.ground_truth import store

SET = "ZZ-AA1048-I"
TENANT = "00000000-0000-4000-8000-000000a10484"
PIECE = "00000000-0000-4000-8000-0000000a1049"
TOUR = "00000000-0000-4000-8000-0000000a104a"
OUTPUT = {"content_text": "Arrive in Velmora and rest at the Harbour Lodge. Walk 40 km to Ostrin Valley on day "
                          "two. Breakfast is served at the lodge each morning. The valley is quiet in spring."}
SOURCE = "Day 1: Arrive in Velmora. Harbour Lodge. Dinner included.\nDay 2: Walk 12 km to Ostrin Valley."


def _export():
    return pn.export_records(set_id=SET, writer="t9_blog", scope="tenant", tenant_id=TENANT,
                             refs={"sample_id": "zz-911", "piece_id": PIECE, "tour_id": TOUR},
                             output=OUTPUT, source_text=SOURCE)


def _panel(lines, *, flip=None):
    out = []
    for i, e in enumerate(lines):
        labels = ["violation", "violation", "ok"] if i == 1 else ["ok", "ok", "ok"]
        for lens, lab in zip(pn.LENSES, labels):
            if flip == (i, lens):
                lab = "violation" if lab == "ok" else "ok"
            out.append({"claim_hash": e["claim_hash"], "lens": lens, "class": "fact.number", "label": lab,
                        "reason": f"{lens}: invented reason", "labeller": "panel:zz"})
    return out


def _payload(dry_run=True, **kw):
    lines = _export()
    return importer.build_payload(lines, _panel(lines, **kw), set_id=SET, set_version="1.0.0", dry_run=dry_run)


# ── payload and line checks ─────────────────────────────────────────────────────────────────────────────────────────


def test_payload_keeps_only_judged_claims_and_slim_keys():
    lines = _export()
    js = [j for j in _panel(lines) if j["claim_hash"] != lines[-1]["claim_hash"]]
    body = importer.build_payload(lines, js, set_id=SET, set_version="1.0.0")
    assert body["dry_run"] is True and body["schema"] == importer.PAYLOAD_SCHEMA
    assert len(body["export"]) == len(lines) - 1
    for e in body["export"]:
        assert set(e) <= set(importer.EXPORT_KEYS)
        assert "source_text" not in e and "field_text" not in e and "context" not in e
        assert importer.check_line(e) is None


def test_payload_refuses_a_bad_set_and_too_many_lines(monkeypatch):
    lines = _export()
    with pytest.raises(pn.PanelError):
        importer.build_payload(lines, _panel(lines), set_id="bad set", set_version="1.0.0")
    monkeypatch.setattr(importer, "MAX_JUDGEMENTS", 2)
    with pytest.raises(pn.PanelError, match="too large"):
        importer.build_payload(lines, _panel(lines), set_id=SET, set_version="1.0.0")


def test_a_masked_piece_id_is_dropped_when_the_sample_names_the_object():
    lines = _export()
    lines[0] = {**lines[0], "refs": {**lines[0]["refs"], "piece_id": "37047c45-[phone removed]-7cda75a4106f"}}
    body = importer.build_payload(lines, _panel(lines), set_id=SET, set_version="1.0.0")
    assert body["refs_dropped"] == 1
    assert "piece_id" not in body["export"][0]["refs"] and body["export"][0]["refs"]["tour_id"] == TOUR
    assert all(importer.check_line(e) is None for e in body["export"])
    no_sample = {**lines[0], "refs": {"piece_id": "not-a-uuid"}}
    line, gone = importer.clean_refs(importer.slim_line(no_sample))
    assert gone == [] and importer.check_line(line) == "refs.piece_id is not a uuid"


@pytest.mark.parametrize("change, why", [
    ({"claim_text": "Walk 12 km to Ostrin Valley on day two."}, "span does not fit"),
    ({"claim_hash": "a" * 64}, "claim_hash is not the hash"),
    ({"writer": "t7"}, "unknown writer"),
    ({"scope": "platform"}, "does not fit writer"),
    ({"tenant_id": "tenant-one"}, "tenant_id is not a uuid"),
    ({"span": [0, "9"]}, "span does not fit"),
    ({"field": "Body Text"}, "bad field"),
    ({"stratum": "loud"}, "unknown stratum"),
])
def test_check_line_refuses_an_edited_line(change, why):
    line = importer.slim_line(_export()[0])
    assert importer.check_line(line) is None
    assert why in (importer.check_line({**line, **change}) or "")


def test_consolidate_payload_refuses_the_whole_file():
    body = _payload()
    body["export"][0] = {**body["export"][0], "claim_hash": "b" * 64}
    with pytest.raises(pn.PanelError, match="export #1"):
        importer.consolidate_payload(body["export"], body["judgements"], set_id=SET, set_version="1.0.0")
    body = _payload()
    body["export"].append(dict(body["export"][0]))
    with pytest.raises(pn.PanelError, match="the same claim twice"):
        importer.consolidate_payload(body["export"], body["judgements"], set_id=SET, set_version="1.0.0")


# ── idempotency plan ────────────────────────────────────────────────────────────────────────────────────────────────


def _rows():
    body = _payload()
    return importer.consolidate_payload(body["export"], body["judgements"], set_id=SET, set_version="1.0.0").rows


def test_plan_rows_new_unchanged_conflict_and_late_panel():
    rows = _rows()
    p = importer.plan_rows(rows, {}, panel_exists=False)
    assert len(p["new"]) == len(rows) and p["unchanged"] == p["conflicts"] == p["late_panel"] == 0
    stored = {importer.judgement_key(r): (r["claim_class"], r["label"]) for r in rows}
    p = importer.plan_rows(rows, stored, panel_exists=True)
    assert p["new"] == [] and p["unchanged"] == len(rows) and p["conflicts"] == 0
    k = importer.judgement_key(rows[0])
    p = importer.plan_rows(rows, {**stored, k: ("fact.name", "violation")}, panel_exists=True)
    assert p["conflicts"] == 1
    some = dict(list(stored.items())[:3])
    p = importer.plan_rows(rows, some, panel_exists=True)
    assert p["late_panel"] == len(rows) - 3


# ── import_labels on a fake connection ──────────────────────────────────────────────────────────────────────────────


class FakeConn:
    def __init__(self):
        self.rows: dict = {}
        self.audit: list = []

    async def fetch(self, sql, *args):
        assert "shared.eval_claim_label" in sql and "claim_text" not in sql
        return [{"claim_hash": k[0], "labeller_kind": k[1], "lens": k[2], "claim_class": v[0], "label": v[1]}
                for k, v in self.rows.items()]

    async def fetchval(self, sql, *args):
        if "count(*)" in sql:
            return sum(1 for k in self.rows if k[1] == "agent_panel")
        cols = dict(zip(store.COLUMNS, args))
        key = (cols["claim_hash"], cols["labeller_kind"], cols["lens"])
        if key in self.rows:
            return None
        self.rows[key] = (cols["claim_class"], cols["label"])
        return f"id-{len(self.rows)}"

    async def execute(self, sql, *args):
        assert "acp_shared.audit_log" in sql
        self.audit.append(args)
        return "INSERT 0 1"

    @asynccontextmanager
    async def transaction(self):
        yield


def test_import_dry_run_writes_nothing_and_counts():
    conn = FakeConn()
    out = asyncio.run(importer.import_labels(conn, _payload(), actor="admin:unknown"))
    assert out["dry_run"] and out["inserted"] == 0 and out["new"] == out["rows"] > 0
    assert conn.rows == {} and conn.audit == []
    assert out["by_label"]["violation"] == 2 and out["by_lens"] == {lens: out["claims"] for lens in sorted(pn.LENSES)}
    text = json.dumps(out)
    assert "Velmora" not in text and "invented reason" not in text


def test_import_apply_is_idempotent_and_audited():
    conn = FakeConn()
    first = asyncio.run(importer.import_labels(conn, _payload(dry_run=False), actor="admin:unknown"))
    assert first["inserted"] == first["rows"] and len(conn.audit) == 1
    again = asyncio.run(importer.import_labels(conn, _payload(dry_run=False), actor="admin:unknown"))
    assert again["inserted"] == 0 and again["unchanged"] == again["rows"] and len(conn.audit) == 1
    assert "Velmora" not in json.dumps([list(map(str, a)) for a in conn.audit])


def test_import_refuses_a_relabel_into_the_same_version():
    conn = FakeConn()
    asyncio.run(importer.import_labels(conn, _payload(dry_run=False), actor="admin:unknown"))
    with pytest.raises(store.StoreRefused, match="differ"):
        asyncio.run(importer.import_labels(conn, _payload(dry_run=False, flip=(0, "source")), actor="x"))


def test_tech_lead_rows_join_a_version_that_has_panel_rows():
    conn = FakeConn()
    body = _payload(dry_run=False)
    asyncio.run(importer.import_labels(conn, body, actor="admin:unknown"))
    lead = {**body, "judgements": [{"claim_hash": body["export"][1]["claim_hash"], "labeller_kind": "tech_lead",
                                    "class": "fact.number", "label": "violation", "reason": "invented"}]}
    out = asyncio.run(importer.import_labels(conn, lead, actor="admin:unknown"))
    assert out["inserted"] == 1 and out["by_labeller_kind"] == {"tech_lead": 1}


# ── the routes ──────────────────────────────────────────────────────────────────────────────────────────────────────

_SECRET = "aa1048-import-test-admin-secret"


class _Pool:
    def __init__(self, conn):
        self.conn = conn

    @asynccontextmanager
    async def acquire(self):
        yield self.conn


@pytest.fixture
def client(monkeypatch):
    from fastapi.testclient import TestClient

    from api.main import app
    from api.routers import admin, admin_settings
    monkeypatch.setenv("ADMIN_SECRET", _SECRET)
    monkeypatch.setattr(admin, "ADMIN_SECRET", _SECRET)
    monkeypatch.setattr(admin_settings, "ADMIN_SECRET", _SECRET)
    conn = FakeConn()
    monkeypatch.setattr(app.state, "pool", _Pool(conn), raising=False)
    monkeypatch.setattr(app.state, "redis", None, raising=False)
    c = TestClient(app, raise_server_exceptions=True)
    c.fake_conn = conn
    return c


def _h(role="admin"):
    return {"X-Admin-Secret": _SECRET, "x-admin-role": role, "x-admin-user-id": "11111111-1111-1111-1111-111111111111"}


URL = "/admin/writing-memory/claim-labels/import"


def test_route_import_dry_run_then_apply_then_idempotent(client):
    r = client.post(URL, json=_payload(), headers=_h())
    assert r.status_code == 200, r.text
    assert r.json()["dry_run"] and client.fake_conn.rows == {}
    r = client.post(URL, json=_payload(dry_run=False), headers=_h())
    assert r.status_code == 200 and r.json()["inserted"] == r.json()["rows"]
    r = client.post(URL, json=_payload(dry_run=False), headers=_h())
    assert r.status_code == 200 and r.json()["inserted"] == 0 and r.json()["unchanged"] == r.json()["rows"]


def test_route_import_refusals(client):
    bad = _payload()
    bad["judgements"][0] = {**bad["judgements"][0], "class": "fact.not_a_class"}
    r = client.post(URL, json=bad, headers=_h())
    assert r.status_code == 422 and r.json()["detail"]["code"] == "bad_payload"
    assert client.post(URL, json={**_payload(), "extra": 1}, headers=_h()).status_code == 422
    client.post(URL, json=_payload(dry_run=False), headers=_h())
    r = client.post(URL, json=_payload(dry_run=False, flip=(1, "skeptic")), headers=_h())
    assert r.status_code == 409 and r.json()["detail"]["code"] == "set_version_holds_other_labels"


@pytest.mark.parametrize("role", ["content", "tenant_qa", "viewer"])
def test_route_import_is_admin_only(client, role):
    assert client.post(URL, json=_payload(), headers=_h(role)).status_code == 403


def test_route_report_and_finals(client):
    client.post(URL, json=_payload(dry_run=False), headers=_h())
    stored = client.fake_conn.rows
    body = _payload()
    by_hash = {e["claim_hash"]: e for e in body["export"]}

    async def fetch(sql, *args):
        if "SELECT label_id" not in sql:
            return []
        rows = []
        cons = importer.consolidate_payload(body["export"], body["judgements"], set_id=SET, set_version="1.0.0")
        for r in cons.rows:
            assert importer.judgement_key(r) in stored
            rows.append({**r, "label_id": "00000000-0000-4000-8000-00000000000" + str(len(rows) % 10)})
        assert by_hash
        return rows

    client.fake_conn.fetch = fetch
    r = client.get(f"/admin/writing-memory/claim-labels/{SET}/1.0.0/report", headers=_h())
    assert r.status_code == 200, r.text
    rep = r.json()
    assert rep["by_label"]["violation"] == 2 and rep["contradictions"] == [] and rep["manifest"]["digest"]
    assert "Velmora" not in r.text and "invented reason" not in r.text
    r = client.get(f"/admin/writing-memory/claim-labels/{SET}/1.0.0/finals?audit_ok=true", headers=_h())
    assert r.status_code == 200 and {f["source"] for f in r.json()["finals"]} == {"panel_agreed"}
    assert client.get(f"/admin/writing-memory/claim-labels/{SET}/1.0.0/finals", headers=_h()).json()["finals"] == []
    assert client.get("/admin/writing-memory/claim-labels/bad%20set/1.0.0/report", headers=_h()).status_code == 404


def test_route_split_plan_returns_ids_and_counts_only(client):
    async def fetch(sql, *args):
        if "content_piece" in sql:
            return [{"id": PIECE, "channel": "blog", "status": "approved", "tour_id": TOUR}]
        return [{"id": "00000000-0000-4000-8000-0000000a104b", "status": "approved", "tour_id": None}]

    client.fake_conn.fetch = fetch
    r = client.get("/admin/writing-memory/claim-labels/split-plan", headers=_h())
    assert r.status_code == 200, r.text
    out = r.json()
    assert out["target_held_out_claims_per_writer"] == 784
    assert {c["writer"] for c in out["candidates"]} == {"t9_blog", "t2"}
    assert out["writers"]["t2"]["without_tour"] == 1
    assert client.get("/admin/writing-memory/claim-labels/split-plan", headers=_h("finance")).status_code == 403


# ── the CLI payload command ─────────────────────────────────────────────────────────────────────────────────────────


def test_cli_payload_writes_the_route_body(tmp_path):
    from scripts.aa1048 import cli

    lines = _export()
    (tmp_path / "claims.jsonl").write_text("".join(json.dumps(x) + "\n" for x in lines))
    (tmp_path / "panel.jsonl").write_text("".join(json.dumps(x) + "\n" for x in _panel(lines)))
    out = tmp_path / "payload.json"
    rc = cli.main(["payload", str(tmp_path / "panel.jsonl"), "--export", str(tmp_path / "claims.jsonl"),
                   "--set", SET, "--version", "1.0.0", "--out", str(out)])
    assert rc == 0
    body = json.loads(out.read_text())
    assert body["dry_run"] is True and len(body["judgements"]) == 3 * len(lines)
    from api.routers.admin_claim_labels import ImportBody

    ImportBody.model_validate(body)
