"""AA-1048 — claim identity: the object's content sha, the claim hash, the frozen split, and sentence spans.

A claim is a span of one field of one stored object (a golden item's frozen output, a T9 piece, an S1 Master version,
a T2 tenant version). Identity never depends on a label or a set version, so the same claim keeps its hash across
label sets and re-imports:

  content_sha256 = sha256(canonical JSON of the object's output)               (golden_set.derive.sha256_json)
  claim_hash     = sha256("<content_sha256>:<field>:<normalised span text>")
  split          = held_out when sha256("<SPLIT_SEED>:<group key>") falls in the first HELD_OUT_PCT of the hash
                   space, else train. The group key is the object's tour (refs.tour_id), else its item (sample_id,
                   piece_id, version_id), else its content sha: every object of one tour (S1 Master versions, T2
                   tenant versions, the T9 blog and social piece) is on the same side, so sentences they share never
                   leak from train into held_out; a regenerated object keeps its side; adding objects never moves an
                   existing one (frozen, critic F14 / audit "30% frozen held-out split").

Pure: no DB, no network, no model call.
"""
from __future__ import annotations

import hashlib
import re
import unicodedata
from dataclasses import dataclass
from typing import Any, Iterable, Optional

HELD_OUT_PCT = 0.30
SPLIT_SEED = "aa1048-cl"
SPLITS = ("train", "held_out")
STRATA = ("hard", "representative", "mutants", "traps")
WRITERS = ("a1", "t2", "t9_blog", "t9_social")
LABELS = ("violation", "ok", "unsure")
MAX_CLAIM_CHARS = 2000
# Fields of a stored output, in the order a reader meets them. Anything else is read after these, sorted by name.
FIELD_ORDER = ("name", "title", "seo_title", "subtitle", "summary", "highlights", "itineraries", "content_text",
               "body", "faq", "meta_description", "seo_meta", "hashtags", "cta")
_FIELD_RE = re.compile(r"^[a-z][a-z0-9_.]{0,39}$")
_SET_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9@._-]{0,31}$")
_SEMVER_RE = re.compile(r"^[0-9]{1,4}\.[0-9]{1,4}\.[0-9]{1,4}$")
_HEX64_RE = re.compile(r"^[0-9a-f]{64}$")
# A sentence ends at . ! ? (optionally followed by a closing quote / bracket) and white space, or at a line break.
_SENT_END_RE = re.compile(r"(?<=[.!?])[\"')\]’”]*\s+|\n+")
_WS_RE = re.compile(r"\s+")
MIN_CLAIM_WORDS = 3


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def content_sha(output: Any) -> str:
    """The object's identity: sha256 of its canonical JSON (the same function the golden-set importer uses)."""
    from services.golden_set.derive import sha256_json

    return sha256_json(output)


def normalise(text: str) -> str:
    """NFKC, curly quotes straightened, white space collapsed, lower case: what the claim hash reads."""
    t = unicodedata.normalize("NFKC", text or "")
    t = t.replace("’", "'").replace("‘", "'").replace("“", '"').replace("”", '"')
    return _WS_RE.sub(" ", t).strip().lower()


def claim_hash(content_sha256: str, field: str, span_text: str) -> str:
    return sha256_text(f"{content_sha256}:{field}:{normalise(span_text)}")


SPLIT_GROUP_REFS = ("tour_id", "sample_id", "piece_id", "version_id")


def split_group(refs: Optional[dict], content_sha256: str) -> str:
    """The key an object's split is hashed on: "<ref>:<id>" for the first of SPLIT_GROUP_REFS it has (the tour first:
    objects about one tour share sentences and source), else "content:<content sha>"."""
    refs = refs or {}
    for k in SPLIT_GROUP_REFS:
        v = refs.get(k)
        if v is not None and str(v).strip():
            return f"{k}:{str(v).strip()}"
    return f"content:{content_sha256}"


def split_of(group_key: str, pct: float = HELD_OUT_PCT) -> str:
    """train | held_out of a group key (split_group): a fixed share of the hash space, frozen per key."""
    bucket = int(sha256_text(f"{SPLIT_SEED}:{group_key}")[:8], 16) / 0xFFFFFFFF
    return "held_out" if bucket < pct else "train"


def field_texts(output: Any) -> list[tuple[str, str]]:
    """(field, text) of a stored output: string fields as they are, a list of strings joined by new lines (one item per
    line), nested objects flattened as "<field>.<key>". Empty fields are skipped."""
    if isinstance(output, str):
        return [("body", output)] if output.strip() else []
    if not isinstance(output, dict):
        return []
    out: list[tuple[str, str]] = []

    def add(name: str, value: Any) -> None:
        if isinstance(value, str):
            if value.strip():
                out.append((name, value))
        elif isinstance(value, list):
            items = [v if isinstance(v, str) else " ".join(str(x) for x in v.values()) if isinstance(v, dict)
                     else str(v) for v in value]
            text = "\n".join(i for i in items if i and i.strip())
            if text:
                out.append((name, text))
        elif isinstance(value, dict):
            for k in sorted(value):
                add(f"{name}.{k}", value[k])

    keys = [k for k in FIELD_ORDER if k in output] + sorted(k for k in output if k not in FIELD_ORDER)
    for k in keys:
        if _FIELD_RE.match(str(k)):
            add(str(k), output[k])
    return [(f, t) for f, t in out if _FIELD_RE.match(f)]


def sentence_spans(text: str, min_words: int = MIN_CLAIM_WORDS) -> list[tuple[int, int]]:
    """(start, end) of each sentence of `text` (end exclusive, trimmed of white space). A sentence of fewer than
    `min_words` words (a heading such as "Day 2") is skipped; a sentence longer than MAX_CLAIM_CHARS is cut there."""
    spans: list[tuple[int, int]] = []
    start = 0
    for m in list(_SENT_END_RE.finditer(text)) + [None]:
        end = m.start() if m is not None else len(text)
        if m is not None and text[m.start():m.end()].strip():
            end = m.start() + len(text[m.start():m.end()].rstrip())     # keep a closing quote with its sentence
        seg = text[start:end]
        lead = len(seg) - len(seg.lstrip())
        s, e = start + lead, start + len(seg.rstrip())
        if e > s and len(text[s:e].split()) >= min_words:
            spans.append((s, min(e, s + MAX_CLAIM_CHARS)))
        start = m.end() if m is not None else len(text)
    return spans


@dataclass(frozen=True)
class Claim:
    """One claim candidate of one object (what the panel export carries, before any label)."""
    claim_hash: str
    content_sha256: str
    field: str
    span_start: int
    span_end: int
    claim_text: str

    def as_dict(self) -> dict:
        return {"claim_hash": self.claim_hash, "content_sha256": self.content_sha256, "field": self.field,
                "span": [self.span_start, self.span_end], "claim_text": self.claim_text}


def claims_of(output: Any, *, fields: Optional[Iterable[str]] = None) -> list[Claim]:
    """Every sentence claim of a stored output, in reading order. The same sentence twice in one field is one claim
    (one hash), at its first span."""
    sha = content_sha(output)
    wanted = set(fields) if fields is not None else None
    seen: set[str] = set()
    out: list[Claim] = []
    for field, text in field_texts(output):
        if wanted is not None and field not in wanted and field.split(".")[0] not in wanted:
            continue
        for s, e in sentence_spans(text):
            span = text[s:e]
            h = claim_hash(sha, field, span)
            if h in seen:
                continue
            seen.add(h)
            out.append(Claim(h, sha, field, s, e, span))
    return out


_WORD_RE = re.compile(r"[a-z0-9][a-z0-9'-]+")
_STOP = frozenset({"the", "and", "for", "with", "from", "into", "this", "that", "you", "your", "are", "was", "will",
                   "its", "their", "our", "has", "have", "can", "all", "day", "trip", "tour"})


def _words(text: str) -> set[str]:
    return {w for w in _WORD_RE.findall(normalise(text)) if len(w) >= 3 and w not in _STOP}


def align(claim_text: str, source_text: str, k: int = 3) -> list[str]:
    """The k source lines (or sentences of a long line) that share the most content words with the claim: the cheap
    code alignment of a claim to its evidence (critic F8 option one). The panel checks it; the entailment instrument
    reads it. Lines with no shared word are never returned."""
    want = _words(claim_text)
    if not want:
        return []
    units: list[str] = []
    for line in (source_text or "").splitlines():
        line = line.strip()
        if not line:
            continue
        units += [line[s:e] for s, e in sentence_spans(line, min_words=1)] if len(line) > 300 else [line]
    scored = [(len(want & _words(u)), -i, u) for i, u in enumerate(units)]
    return [u for n, _i, u in sorted(scored, reverse=True)[:k] if n > 0]


def check_span(field_text: str, span_start: int, span_end: int, claim_text: str) -> bool:
    """The span still points at the claim text (a re-import against a changed object is refused)."""
    return 0 <= span_start < span_end <= len(field_text) and field_text[span_start:span_end] == claim_text


def valid_set(set_id: str, set_version: str) -> bool:
    return bool(_SET_RE.match(set_id or "")) and bool(_SEMVER_RE.match(set_version or ""))


# The writer surface of each writer, and the label-set families of AA-1048 (Scope; CIS 388 names the set ids
# CL-1 / L2 / t2@2): CL-<n> is the T9 set, L<n> the S1 set, t2@<n> the T2 set. One set holds one surface, so a
# measurement of a T9 set never pools S1 or T2 claims.
SURFACES = {"a1": "s1", "t2": "t2", "t9_blog": "t9", "t9_social": "t9"}
SET_FAMILIES = ((re.compile(r"^CL-[0-9]+$"), "t9"), (re.compile(r"^L[0-9]+$"), "s1"),
                (re.compile(r"^t2@[0-9]+$"), "t2"))


def set_surface(set_id: str) -> Optional[str]:
    """The surface a named label set holds (t9 / s1 / t2), or None for a set id outside the three families (a test or
    exploration set: any one surface)."""
    for rx, surface in SET_FAMILIES:
        if rx.match(set_id or ""):
            return surface
    return None


def set_writers(set_id: str) -> Optional[tuple[str, ...]]:
    """The writers a named label set holds (None: the set id names no family)."""
    surface = set_surface(set_id)
    return None if surface is None else tuple(w for w in WRITERS if SURFACES[w] == surface)


def set_writer_problems(set_id: str, writers: Iterable[str]) -> list[str]:
    """Why these writers cannot share the label set (empty when they can): a writer outside the set's family, or
    writers of more than one surface in one set."""
    ws = sorted({str(w) for w in writers if w in SURFACES})
    allowed = set_writers(set_id)
    if allowed is not None:
        bad = [w for w in ws if w not in allowed]
        if bad:
            return [f"writer(s) {', '.join(bad)} do not belong to set {set_id} (the {set_surface(set_id)} set: "
                    f"{', '.join(allowed)}); export one set per surface (CL-<n> T9, L<n> S1, t2@<n> T2)"]
        return []
    surfaces = sorted({SURFACES[w] for w in ws})
    if len(surfaces) > 1:
        return [f"set {set_id} mixes writer surfaces ({', '.join(surfaces)}): one set holds one surface"]
    return []


def valid_hex64(value: Optional[str]) -> bool:
    return bool(_HEX64_RE.match(value or ""))


def valid_field(value: Optional[str]) -> bool:
    return bool(_FIELD_RE.match(value or ""))


__all__ = ["Claim", "FIELD_ORDER", "HELD_OUT_PCT", "LABELS", "MAX_CLAIM_CHARS", "SPLITS", "SPLIT_GROUP_REFS",
           "SPLIT_SEED", "STRATA", "WRITERS", "align", "check_span", "claim_hash", "claims_of", "content_sha",
           "field_texts", "normalise", "sentence_spans", "sha256_text", "split_group", "split_of", "valid_field",
           "valid_hex64", "valid_set"]
