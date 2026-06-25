"""
Unit tests for scrapers/soccervista.py pure functions.
No browser, no network — all probability parsing and pick selection logic
is tested in isolation.

Run with:
    python tests/test_soccervista.py
"""

import sys
from datetime import date, datetime, timezone
from decimal import Decimal

import os
os.environ.setdefault("PROXY_HOST", "fake")
os.environ.setdefault("PROXY_PORT", "1234")
os.environ.setdefault("PROXY_USERNAME", "u")
os.environ.setdefault("PROXY_PASSWORD", "p")

from scrapers.soccervista import (
    _parse_probability,
    _probability_to_decimal_odds,
    _derive_confidence,
    _select_pick,
    _parse_kickoff,
    MIN_PROBABILITY_GAP_PP,
)
from models.pick import MarketType


# ── _parse_probability ────────────────────────────────────────────────────────

def test_parse_pct_with_symbol():
    assert _parse_probability("45%") == 45.0

def test_parse_pct_without_symbol():
    assert _parse_probability("45") == 45.0

def test_parse_decimal_fraction():
    # 0.45 should be treated as 45%
    assert _parse_probability("0.45") == 45.0

def test_parse_probability_100_invalid():
    # 100% implies certainty — not a valid model output
    assert _parse_probability("100") is None

def test_parse_probability_0_invalid():
    assert _parse_probability("0") is None

def test_parse_probability_text_returns_none():
    assert _parse_probability("N/A") is None

def test_parse_probability_empty_returns_none():
    assert _parse_probability("") is None


# ── _probability_to_decimal_odds ──────────────────────────────────────────────

def test_60pct_to_odds():
    result = _probability_to_decimal_odds(60.0)
    assert result is not None
    assert abs(float(result) - 1.667) < 0.01

def test_50pct_to_odds():
    result = _probability_to_decimal_odds(50.0)
    assert result is not None
    assert abs(float(result) - 2.0) < 0.01

def test_zero_pct_returns_none():
    assert _probability_to_decimal_odds(0.0) is None

def test_100pct_returns_none():
    assert _probability_to_decimal_odds(100.0) is None


# ── _select_pick ──────────────────────────────────────────────────────────────

def test_clear_home_favourite():
    result = _select_pick(65.0, 20.0, 15.0)
    assert result is not None
    selection, market, pct, gap = result
    assert selection == "Home Win"
    assert market == MarketType.MATCH_RESULT
    assert pct == 65.0
    assert gap == 45.0  # 65 - 20

def test_clear_away_favourite():
    result = _select_pick(15.0, 20.0, 65.0)
    assert result is not None
    selection, _, _, _ = result
    assert selection == "Away Win"

def test_draw_favourite():
    result = _select_pick(25.0, 55.0, 20.0)
    assert result is not None
    selection, _, _, _ = result
    assert selection == "Draw"

def test_too_close_returns_none():
    # Gap = 38 - 33 = 5pp < MIN_PROBABILITY_GAP_PP (15)
    result = _select_pick(38.0, 33.0, 29.0)
    assert result is None

def test_exact_minimum_gap_accepted():
    # Gap = 45 - 30 = 15pp == MIN_PROBABILITY_GAP_PP — should pass
    result = _select_pick(45.0, 30.0, 25.0)
    assert result is not None

def test_just_below_minimum_gap_rejected():
    # Gap = 44 - 30 = 14pp < 15 — should be rejected
    result = _select_pick(44.0, 30.0, 26.0)
    assert result is None


# ── _derive_confidence ────────────────────────────────────────────────────────

def test_high_probability_high_gap():
    c = _derive_confidence(75.0, 40.0)
    assert c >= 0.80

def test_moderate_probability_moderate_gap():
    c = _derive_confidence(55.0, 20.0)
    assert 0.55 <= c <= 0.70

def test_low_probability_low_gap():
    c = _derive_confidence(42.0, 15.0)
    assert c <= 0.55

def test_confidence_capped_at_0_85():
    c = _derive_confidence(99.0, 80.0)
    assert c <= 0.85

def test_confidence_in_valid_range():
    for pct, gap in [(40, 15), (50, 20), (60, 25), (70, 35), (80, 50)]:
        c = _derive_confidence(pct, gap)
        assert 0.40 <= c <= 0.85, f"Out of range: {c} for pct={pct} gap={gap}"


# ── _parse_kickoff ────────────────────────────────────────────────────────────

def test_parse_date_time_slash_format():
    today = date(2025, 6, 21)
    result = _parse_kickoff("21/06 15:00", today)
    assert result == datetime(2025, 6, 21, 15, 0, tzinfo=timezone.utc)

def test_parse_time_only():
    today = date(2025, 6, 21)
    result = _parse_kickoff("20:00", today)
    assert result == datetime(2025, 6, 21, 20, 0, tzinfo=timezone.utc)

def test_parse_date_with_year():
    today = date(2025, 6, 21)
    result = _parse_kickoff("21/06/2025 19:45", today)
    assert result is not None
    assert result.hour == 19 and result.minute == 45

def test_parse_unparseable_returns_none():
    today = date(2025, 6, 21)
    result = _parse_kickoff("TBD", today)
    assert result is None


# ── Runner ────────────────────────────────────────────────────────────────────

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
