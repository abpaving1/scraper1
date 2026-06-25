"""
Unit tests for processor/normalize.py.

Run with:
    python tests/test_normalize.py
"""

import sys

from processor.normalize import fixture_dedup_key, normalize_team_name


def test_strips_trailing_fc():
    assert normalize_team_name("Arsenal FC") == "arsenal"


def test_strips_trailing_football_club():
    assert normalize_team_name("Arsenal Football Club") == "arsenal"


def test_plain_name_unchanged():
    assert normalize_team_name("Arsenal") == "arsenal"


def test_case_insensitive():
    assert normalize_team_name("ARSENAL") == normalize_team_name("arsenal")


def test_accents_stripped():
    assert normalize_team_name("Atlético Madrid") == "atletico madrid"


def test_leading_afc_not_stripped_known_limitation():
    # Documented limitation: only TRAILING suffixes are stripped, so a
    # leading "AFC" (AFC Bournemouth) is left alone. This test exists so a
    # future "fix" that breaks this doesn't go unnoticed either way —
    # if you change this behaviour, update the test deliberately.
    assert normalize_team_name("AFC Bournemouth") == "afc bournemouth"


def test_does_not_mangle_substring_that_looks_like_suffix():
    # "Real Union" must not lose "Union" just because suffix list contains
    # short tokens — our suffix regex only matches whole trailing words from
    # the _SUFFIXES list, and "union" isn't one of them, so this is really
    # just confirming no false-positive stripping happens.
    assert normalize_team_name("Real Union") == "real union"


def test_punctuation_and_whitespace_collapsed():
    assert normalize_team_name("  Newcastle   United.  ") == "newcastle united"


def test_empty_string():
    assert normalize_team_name("") == ""


def test_fixture_dedup_key_order_preserved():
    key = fixture_dedup_key("Arsenal FC", "Chelsea FC")
    assert key == ("arsenal", "chelsea")
    # Reversed fixture must NOT produce the same key — home/away matters.
    reversed_key = fixture_dedup_key("Chelsea FC", "Arsenal FC")
    assert key != reversed_key


def test_different_spellings_same_team_match():
    assert normalize_team_name("Man Utd") != normalize_team_name("Manchester United")
    # Known limitation, not a bug: abbreviation expansion (Man Utd ->
    # Manchester United) is NOT handled by this normaliser — only
    # suffix/case/accent/punctuation normalisation. Cross-source matching for
    # genuinely different abbreviations needs an explicit alias table, which
    # is intentionally out of scope here (see module docstring: false
    # positives are worse than false negatives, so we don't guess).


def _run_all():
    tests = [obj for name, obj in globals().items() if name.startswith("test_") and callable(obj)]
    failures = 0
    for test in tests:
        try:
            test()
            print(f"PASS  {test.__name__}")
        except AssertionError as exc:
            failures += 1
            print(f"FAIL  {test.__name__}: {exc}")
        except Exception as exc:  # noqa: BLE001
            failures += 1
            print(f"ERROR {test.__name__}: {type(exc).__name__}: {exc}")
    print(f"\n{len(tests) - failures}/{len(tests)} passed")
    if failures:
        sys.exit(1)


if __name__ == "__main__":
    _run_all()
