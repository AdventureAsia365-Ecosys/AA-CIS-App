"""AA-205: post-repair seo_meta band-guard unit tests (pure, deterministic)."""
from services.content_generation.seo_meta_utils import (
    SEO_META_MIN, SEO_META_MAX,
    meta_complete_sentence, meta_in_band, best_meta_candidate,
)

_LEAD = "Trek valleys meet weavers rest at lodge "  # 7 words + trailing space


def _make(n):
    """Complete sentence of EXACTLY n chars: lead + padded safe last word + period."""
    word_len = n - len(_LEAD) - 1
    assert word_len >= 1, "n too small for _make"
    return _LEAD + ("a" * word_len) + "."


def test_make_helper_is_in_band():
    m = _make(148)
    assert len(m) == 148 and meta_in_band(m)


def test_in_band_true_edges():
    assert meta_in_band(_make(SEO_META_MIN))
    assert meta_in_band(_make(SEO_META_MAX))


def test_under_min_not_in_band():
    assert not meta_in_band(_make(SEO_META_MIN - 1))


def test_over_max_not_in_band():
    assert not meta_in_band(_make(SEO_META_MAX + 1))


def test_in_length_but_no_period_not_in_band():
    m = _make(148)[:-1]
    assert SEO_META_MIN <= len(m) <= SEO_META_MAX
    assert not meta_in_band(m)


def test_post_in_band_returned_unchanged():
    post = _make(148)
    assert best_meta_candidate(post, _make(180)) == post


def test_salvage_from_pre_when_post_under_band():
    post = _make(132)
    pre = _make(148)[:-1] + ". More trailing context here purely to push total over band."
    result = best_meta_candidate(post, pre)
    assert meta_in_band(result) and result != post


def test_stuck_single_long_sentence_returns_post():
    post = _make(132)
    pre = _make(185)
    assert best_meta_candidate(post, pre) == post


def test_complete_sentence_rejects_bad_ending():
    assert not meta_complete_sentence("Trek valleys meet weavers and rest at lodge with.")
