"""AA-926 — `python -m services.quality.lint`: the repo is clean, seeded bad snippets are caught, and the
known false positives (pagination, __all__, word length, docstrings, comments) are not."""
import textwrap

from services.quality import lint, registry

EXACT = {n for n, g in registry.gates().items() if g.get("lint_exact")}
REGISTERED = set(registry.gates())


def _py(src: str) -> lint.Result:
    res = lint.Result()
    lint.scan_python("services/x.py", textwrap.dedent(src), res, EXACT, REGISTERED)
    return res


def _ts(src: str) -> lint.Result:
    res = lint.Result()
    lint.scan_ts("frontend/app/x.tsx", textwrap.dedent(src), res)
    return res


def test_repo_is_clean():
    res = lint.run()
    assert res.errors == [], "\n".join(res.errors[:20])
    assert res.scanned["python"] > 100 and res.scanned["ts"] > 50


def test_report_renders_counts():
    out = lint.render_report(lint.run())
    assert "0 error(s)" in out and "qlint: allow" in out


def test_gate_literal_caught_registered_and_unregistered():
    res = _py('''
        def f():
            return _result("F1_grounding", [])
        X = {"gate": "within_tenant_reuse"}
        Y = "F42_brand_new"
    ''')
    msgs = "\n".join(res.errors)
    assert "'F1_grounding'" in msgs and "'within_tenant_reuse'" in msgs
    assert "'F42_brand_new'" in msgs and "unregistered" in msgs


def test_gate_literal_inside_an_fstring_is_caught():
    assert _py('m = f"F9_cta_fact failed: {x}"\n').errors


def test_docstrings_comments_and_all_are_exempt():
    res = _py('''
        """Module docstring names F1_grounding and promises_an_option."""
        __all__ = ["F9_PROMPT_VERSION", "F2_SUPERLATIVE_GATE", "F14_FLOOR_BLOCKING", "gate_x"]

        def g():
            """F17_trip_fact in a function docstring."""
            # F9_cta_fact in a comment
            return 1
    ''')
    assert res.errors == []


def test_upper_case_identifiers_are_not_gate_names():
    assert _py('X = "F9_PROMPT_VERSION"\nY = "F2_SUPERLATIVE_GATE"\n').errors == []


def test_qlint_allow_is_counted_not_an_error():
    res = _py('ALARM = 7.0  # qlint: allow ACP evaluator alarm, a different metric\n')
    assert res.errors == [] and len(res.allows) == 1 and "different metric" in res.allows[0]


def test_pass_line_literals_in_python():
    res = _py('''
        MIN = 7.0
        def f(score, quality_score):
            if score >= 7:
                pass
            if quality_score < 7:
                pass
        SQL = "rq.score_overall >= 7"
        MSG = "Low quality: score < 7.0"
        H = "WHEN q >= 7 THEN '7-8' ELSE '<7'"
    ''')
    assert len(res.errors) == 6, res.errors


def test_python_false_positives():
    res = _py('''
        def f(t, token, days):
            limit = 2 if len(t) >= 7 else 1
            other = 2 if len(token) >= 7 else 1
            week = days >= 7
            v = "17.05"
            ratio = 0.7
            return limit, other, week, v, ratio
    ''')
    assert res.errors == [], res.errors


def test_pass_line_literals_in_ts():
    res = _ts('''
        const canApprove = item.score >= 7 && failCount === 0;
        const col = s >= 7 ? A.green : A.red;
        const b = gen.score_overall >= 7 ? "amber" : "red";
        const label = <option value="ok">7.0+ (held)</option>;
    ''')
    assert len(res.errors) == 4, res.errors


def test_ts_false_positives_and_comments():
    res = _ts('''
        if (totalPages <= 7) { return range(1, totalPages); }
        // score >= 7 in a comment
        /* 7.0 in a block comment */
        const v = "17.05";
        const ok = s >= SCORE_BANDS.pass;
    ''')
    assert res.errors == [], res.errors


def test_jev_seed_parser_reads_only_decision_question_inserts():
    sql = textwrap.dedent('''
        INSERT INTO shared.llm_role_config (stage, role) VALUES
            ('t2_generate', 'writer');
        INSERT INTO shared.decision_question (question_key, stage, kind, instructions)
        VALUES
            ('z9_new_question', 'x', 'noul', 'Is it true; really?'),
            ('z9_other', 'x', 'noul', 'Second');
        ON CONFLICT (question_key) DO NOTHING;
    ''')
    found = [k for m in lint.SEED_RE.finditer(sql) for k in lint.SEED_KEY_RE.findall(m.group(1))]
    assert found == ["z9_new_question", "z9_other"]


def test_jev_code_constant_must_be_registered():
    keys = lint.jev_code_keys({"services/x.py": 'NEW_Q = "z9_unregistered"\nT8_QUESTIONS_MAX = 12\n'})
    assert keys == {"z9_unregistered": "services/x.py:1"}
    assert "z9_unregistered" not in registry.jev_questions()


def test_a1_code_sites_are_found():
    src = textwrap.dedent('''
        _HARD_BLOCK_CODES = frozenset({"META_TOO_SHORT", "NEW_HARD_CODE"})
        def v(fired):
            fired.append("ANOTHER_CODE")
    ''')
    found = lint.a1_code_sites({"services/content_generation/x.py": src})
    assert {"META_TOO_SHORT", "NEW_HARD_CODE", "ANOTHER_CODE"} <= set(found)
    assert "NEW_HARD_CODE" not in registry.a1_codes()
