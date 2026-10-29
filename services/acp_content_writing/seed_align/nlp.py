"""AA-789 tier 1 — the one place that loads spaCy.

The model loads lazily, on the first parse, and only when some seed-alignment class is not `off` (modes.py). It is
pinned (requirements.txt: spacy 3.8.x and the en_core_web_sm 3.8.0 wheel by hash); `NLP_VERSION` is written next to
every finding so a replay can see version drift. Without spaCy (a dev machine, a test run without the package) every
check abstains: `parse_many` returns None and seed alignment finds nothing. It never raises into the gate stack.
"""
from __future__ import annotations

import threading
from typing import Any, Optional

import structlog

logger = structlog.get_logger()

MODEL = "en_core_web_sm"
# The pipes a check reads: tags, dependency parse, lemmas. NER is not used.
_DISABLE = ("ner",)

_lock = threading.Lock()
_nlp: Any = None
_failed = False
NLP_VERSION: Optional[str] = None


def _load() -> Any:
    global _nlp, _failed, NLP_VERSION
    if _nlp is not None or _failed:
        return _nlp
    with _lock:
        if _nlp is not None or _failed:
            return _nlp
        try:
            import spacy  # noqa: PLC0415 — lazy: the API process never pays for it

            _nlp = spacy.load(MODEL, disable=list(_DISABLE))
            NLP_VERSION = f"spacy-{spacy.__version__}/{MODEL}-{_nlp.meta.get('version', '?')}"
        except Exception as exc:  # missing package or model: every check abstains
            _failed = True
            logger.warning("seed_align_nlp_unavailable", error=str(exc)[:200])
    return _nlp


def available() -> bool:
    return _load() is not None


def parse_many(texts: list[str]) -> Optional[list[Any]]:
    """spaCy Docs for `texts`, or None when spaCy is not available."""
    nlp = _load()
    if nlp is None:
        return None
    return list(nlp.pipe(texts, batch_size=64))


def parse(text: str) -> Optional[Any]:
    docs = parse_many([text])
    return docs[0] if docs else None


def reset_for_tests(nlp: Any = None, failed: bool = False) -> None:
    """Tests only: install a stand-in pipeline (or none)."""
    global _nlp, _failed
    _nlp, _failed = nlp, failed
