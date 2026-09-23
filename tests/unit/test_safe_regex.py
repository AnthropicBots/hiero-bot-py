# tests/unit/test_safe_regex.py — bounded matching of owner-supplied patterns

from time import perf_counter

import pytest

from app.utils.safe_regex import MAX_PATTERN_LENGTH, bounded_match, validate_pattern


def test_bounded_match_behaves_like_re_match():
    assert bounded_match(r"(feat|fix)/", "fix/login")
    assert not bounded_match(r"(feat|fix)/", "docs/readme")
    assert bounded_match(r"fix", "fix/login")  # match() anchors at the start only
    assert not bounded_match(r"login", "fix/login")


def test_catastrophic_pattern_is_cut_off():
    started = perf_counter()

    assert bounded_match(r"(a|aa)+$", "a" * 45 + "!") is False

    assert perf_counter() - started < 1.0


def test_over_long_subject_is_truncated_not_rejected():
    assert bounded_match("a", "a" + "b" * 1000)


def test_validate_pattern_rejects_bad_input():
    with pytest.raises(ValueError):
        validate_pattern("[")
    with pytest.raises(ValueError):
        validate_pattern("a" * (MAX_PATTERN_LENGTH + 1))

    assert validate_pattern(r"^(feat|fix)/") == r"^(feat|fix)/"
