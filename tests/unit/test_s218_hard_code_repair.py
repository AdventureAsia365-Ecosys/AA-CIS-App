"""S218 audit guard: every hard-block code has a declared repair path (lesson in skill/aa-lessons)."""
import importlib

from services.content_generation.graph import HARD_CODE_REPAIR, V2_HARD_BLOCK_CODES, _HARD_BLOCK_CODES

# AA-645: a code that is hard on an A1 writer-v2 draft only (graph.hard_block_codes) needs a repair path too.
ALL_HARD = set(_HARD_BLOCK_CODES) | set(V2_HARD_BLOCK_CODES)


def test_every_hard_code_declares_a_repair_path():
    missing = ALL_HARD - set(HARD_CODE_REPAIR)
    assert not missing, f"hard codes without a repair path: {sorted(missing)}"


def test_repair_registry_names_only_hard_codes():
    assert set(HARD_CODE_REPAIR) <= ALL_HARD


def test_deterministic_repairs_point_at_real_functions():
    for code, how in HARD_CODE_REPAIR.items():
        if not how.startswith("deterministic:"):
            # AA-925: "review:" = no automated repair can clear it (DEGRADED_DETECTOR: a detector outage — the
            # reviewer re-runs the tour once the detector is healthy); it must still say what to do.
            assert how.startswith(("llm:", "review:")), code
            if how.startswith("review:"):
                assert len(how.split(":", 1)[1].strip()) > 10, code
            continue
        target = how.split(":", 1)[1].strip().split(" ")[0]          # e.g. seo_meta_utils.fit_seo_title
        module, func = target.rsplit(".", 1)
        mod = importlib.import_module(f"services.content_generation.{module}")
        assert callable(getattr(mod, func)), f"{code}: {target} not found"
