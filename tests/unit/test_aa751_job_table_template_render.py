"""AA-751 (X6) — job table template rendering and the comparator's normalisation, without Postgres.

The comparison against a live table runs in tests/integration/test_aa751_job_table_template.py.
"""
import re

import pytest

from shared.jobs import job_table_shape as shape

CIS = dict(schema="shared", kind_pattern=shape.CIS_KIND_PATTERN, extra_statuses=", 'stopped_budget'",
           max_attempts_default=3)
BOOKING = dict(schema="booking", kind_pattern=shape.BOOKING_KIND_PATTERN, extra_statuses="",
               max_attempts_default=5)


def _render(**kw):
    return shape.render(shape.load_template(), **kw)


def test_template_lists_every_placeholder_it_uses():
    text = shape.load_template()
    body = "\n".join(line for line in text.splitlines() if not line.lstrip().startswith("--"))
    used = set(re.findall(r"\{\{\s*([a-z_]+)\s*\}\}", body))
    assert used == set(shape.PLACEHOLDERS)


@pytest.mark.parametrize("owner", [CIS, BOOKING])
def test_render_fills_every_placeholder_and_drops_comments(owner):
    sql = _render(**owner)
    assert "{{" not in sql and "}}" not in sql
    assert not any(line.lstrip().startswith("--") for line in sql.splitlines())
    assert f"CREATE TABLE {owner['schema']}.job (" in sql
    assert f"CHECK (kind ~ '{owner['kind_pattern']}')" in sql
    assert f"DEFAULT {owner['max_attempts_default']}," in sql
    assert f"REFERENCES {owner['schema']}.job(id)" in sql


def test_render_statuses_per_owner():
    assert "'cancelled', 'stopped_budget'))" in _render(**CIS)
    assert "'cancelled'))" in _render(**BOOKING)


@pytest.mark.parametrize("owner", [CIS, BOOKING])
def test_render_statement_count(owner):
    stmts = shape.split_statements(_render(**owner))
    kinds = [re.match(r"(CREATE (?:UNIQUE )?INDEX|CREATE TABLE|CREATE FUNCTION|CREATE TRIGGER|ALTER TABLE)", s)
             .group(1) for s in stmts]
    assert kinds == ["CREATE TABLE"] + ["CREATE UNIQUE INDEX"] + ["CREATE INDEX"] * 5 + [
        "CREATE FUNCTION", "CREATE TRIGGER", "ALTER TABLE"]
    # The function body ($$ ... $$, with its own ';') stays one statement.
    assert "RETURN NEW;" in stmts[7] and stmts[7].endswith("$$")


@pytest.mark.parametrize("bad", [
    dict(CIS, schema="Shared"), dict(CIS, schema="shared; DROP TABLE x"), dict(CIS, schema=""),
    dict(CIS, kind_pattern="^x'$"), dict(CIS, kind_pattern=""),
    dict(CIS, extra_statuses="'stopped_budget'"), dict(CIS, extra_statuses=", 'x'); DROP TABLE y; --"),
    dict(CIS, max_attempts_default=0), dict(CIS, max_attempts_default=True), dict(CIS, max_attempts_default="3"),
])
def test_render_rejects_unsafe_values(bad):
    with pytest.raises(ValueError):
        _render(**bad)


def test_render_rejects_an_unknown_placeholder():
    with pytest.raises(ValueError, match="unknown template placeholder"):
        shape.render("CREATE TABLE {{schema}}.job ({{oops}} int);", **CIS)


def test_split_statements_respects_quotes_and_dollar_bodies():
    sql = "SELECT 'a;b'; CREATE FUNCTION f() AS $$ BEGIN x; y; END; $$; SELECT 2"
    assert shape.split_statements(sql) == ["SELECT 'a;b'", "CREATE FUNCTION f() AS $$ BEGIN x; y; END; $$",
                                           "SELECT 2"]


def test_body_normaliser_treats_booking_and_template_bodies_as_equal():
    template = "\nBEGIN\n    NEW.updated_at := now();\n    RETURN NEW;\nEND;\n"
    booking = "\nBEGIN\n    NEW.updated_at := now();\n    RETURN NEW;\nEND "
    assert shape.normalise_body(template) == shape.normalise_body(booking) == \
        "BEGIN NEW.updated_at := now(); RETURN NEW; END"
    assert shape.normalise_body(template) != shape.normalise_body(template.replace("now()", "clock_timestamp()"))


def test_fk_and_index_placeholdering():
    assert shape.placeholder_table("FOREIGN KEY (parent_job_id) REFERENCES booking.job(id)", "booking") == \
        "FOREIGN KEY (parent_job_id) REFERENCES <schema>.job(id)"
    # Another schema's job table, or a longer name, is not replaced.
    assert "x_booking.job" in shape.placeholder_table("REFERENCES x_booking.job(id)", "booking")
    assert "booking.jobs" in shape.placeholder_table("REFERENCES booking.jobs(id)", "booking")
    assert shape.normalise_index(
        "CREATE UNIQUE INDEX job_idempotency_key_uq ON shared.job USING btree (idempotency_key) "
        "WHERE (idempotency_key IS NOT NULL)", "shared") == \
        "UNIQUE USING btree (idempotency_key) WHERE (idempotency_key IS NOT NULL)"
    # Same definition, different names and schemas: equal after normalisation.
    assert shape.normalise_index("CREATE INDEX job_due_idx ON booking.job USING btree (run_after, created_at) "
                                 "WHERE (status = 'queued'::text)", "booking") == \
        shape.normalise_index("CREATE INDEX job_queued_due_idx ON x6_tmpl.job USING btree (run_after, created_at) "
                              "WHERE (status = 'queued'::text)", "x6_tmpl")


def test_status_set_parse():
    d = ("CHECK ((status = ANY (ARRAY['queued'::text, 'running'::text, 'succeeded'::text, 'failed'::text, "
         "'stopped_budget'::text, 'cancelled'::text])))")
    assert shape.parse_status_set(d) == {"queued", "running", "succeeded", "failed", "stopped_budget", "cancelled"}
    assert shape.parse_status_set("CHECK (((status <> 'running'::text) OR (locked_by IS NOT NULL)))") is None
    assert shape.parse_status_set("CHECK ((kind ~ '^bk_[a-z0-9_]+$'::text))") is None


def _shape(schema, **over):
    base = dict(
        columns={"id": ("uuid", True, "gen_random_uuid()"), "status": ("text", True, "'queued'::text")},
        constraints=[("p", True, "PRIMARY KEY (id)"),
                     ("c", True, "CHECK ((status = ANY (ARRAY['queued'::text, 'running'::text])))"),
                     ("f", True, f"FOREIGN KEY (parent_job_id) REFERENCES {schema}.job(id)")],
        indexes=[f"CREATE UNIQUE INDEX job_pkey ON {schema}.job USING btree (id)"],
        triggers=[shape.Trigger(19, "O", False, False, "\nBEGIN\n RETURN NEW;\nEND;\n", ("search_path=pg_catalog",))],
        reloptions=["autovacuum_vacuum_scale_factor=0.02", "autovacuum_vacuum_threshold=50"],
    )
    base.update(over)
    return shape.Shape(schema, **base)


def test_compare_equal_shapes_in_other_schemas():
    assert shape.compare(_shape("x6_tmpl"), _shape("booking")) == []


def test_compare_status_order_and_status_set():
    t = _shape("x6_tmpl")
    reordered = _shape("booking", constraints=[
        ("p", True, "PRIMARY KEY (id)"),
        ("c", True, "CHECK ((status = ANY (ARRAY['running'::text, 'queued'::text])))"),
        ("f", True, "FOREIGN KEY (parent_job_id) REFERENCES booking.job(id)")])
    assert shape.compare(t, reordered) == []
    extra = _shape("booking", constraints=[
        ("p", True, "PRIMARY KEY (id)"),
        ("c", True, "CHECK ((status = ANY (ARRAY['running'::text, 'queued'::text, 'paused'::text])))"),
        ("f", True, "FOREIGN KEY (parent_job_id) REFERENCES booking.job(id)")])
    assert any("not in template ['paused']" in d for d in shape.compare(t, extra))


def test_compare_trigger_rules():
    t = _shape("x6_tmpl")
    no_config = _shape("booking", triggers=[shape.Trigger(19, "O", False, False, "BEGIN RETURN NEW; END", ())])
    assert any("config missing ['search_path=pg_catalog']" in d for d in shape.compare(t, no_config))
    after = _shape("booking", triggers=[shape.Trigger(17, "O", False, False, "BEGIN RETURN NEW; END",
                                                      ("search_path=pg_catalog",))])
    diffs = shape.compare(t, after)
    assert any("trigger missing: BEFORE UPDATE FOR EACH ROW" in d for d in diffs)
    assert any("trigger not in template: AFTER UPDATE FOR EACH ROW" in d for d in diffs)
    disabled = _shape("booking", triggers=[shape.Trigger(19, "D", False, False, "BEGIN RETURN NEW; END",
                                                         ("search_path=pg_catalog",))])
    assert any("trigger disabled" in d for d in shape.compare(t, disabled))


def test_compare_reloptions_owner_superset_ok():
    t = _shape("x6_tmpl")
    more = _shape("booking", reloptions=t.reloptions + ["fillfactor=90"])
    assert shape.compare(t, more) == []
    other = _shape("booking", reloptions=["autovacuum_vacuum_scale_factor=0.2", "autovacuum_vacuum_threshold=50"])
    assert any("reloption missing: autovacuum_vacuum_scale_factor=0.02" in d for d in shape.compare(t, other))


def test_compare_missing_table():
    assert shape.compare(_shape("x6_tmpl"), _shape("booking", columns={})) == [
        "booking.job: table not found or has no columns"]
