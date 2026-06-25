"""
Unit tests for scrapers/freesupertips.py pure functions.
No browser, no network, no Redis — all parsing logic is tested in isolation.

Run with:
    python tests/test_freesupertips.py
"""

import sys
from datetime import date, datetime, timezone
from decimal import Decimal

# Minimal env stubs so config.py doesn't blow up on import
import os
os.environ.setdefault("PROXY_HOST", "fake")
os.environ.setdefault("PROXY_PORT", "1234")
os.environ.setdefault("PROXY_USERNAME", "u")
os.environ.setdefault("PROXY_PASSWORD", "p")

from scrapers.freesupertips import (
    _parse_match,
    _parse_odds,
    _parse_kickoff,
    _classify_market,
    _derive_confidence,
    FST_BASE_CONFIDENCE,
)
from models.pick import MarketType


# ── _parse_match ──────────────────────────────────────────────────────────────

def test_parse_match_v_separator():
    assert _parse_match("Arsenal v Chelsea") == ("Arsenal", "Chelsea")

def test_parse_match_vs_separator():
    assert _parse_match("Man Utd vs Liverpool") == ("Man Utd", "Liverpool")

def test_parse_match_vs_dot_separator():
    assert _parse_match("PSG vs. Monaco") == ("PSG", "Monaco")

def test_parse_match_dash_separator():
    assert _parse_match("Wolves - Brighton") == ("Wolves", "Brighton")

def test_parse_match_unknown_separator_returns_empty():
    home, away = _parse_match("Arsenal Chelsea")
    assert home == "" and away == ""

def test_parse_match_strips_whitespace():
    home, away = _parse_match("  Arsenal  v  Chelsea  ")
    assert home == "Arsenal" and away == "Chelsea"


# ── _parse_odds ───────────────────────────────────────────────────────────────

def test_parse_odds_decimal_string():
    assert _parse_odds("1.90") == Decimal("1.90")

def test_parse_odds_fractional_evens():
    assert _parse_odds("1/1") == Decimal("2.000")

def test_parse_odds_fractional_two_to_one():
    assert _parse_odds("2/1") == Decimal("3.000")

def test_parse_odds_fractional_six_to_four():
    result = _parse_odds("6/4")
    assert result is not None
    assert abs(float(result) - 2.5) < 0.01

def test_parse_odds_with_currency_prefix():
    # Some sites prepend odds with symbols — non-digit chars stripped
    assert _parse_odds("£1.90") is not None

def test_parse_odds_below_minimum_returns_none():
    assert _parse_odds("1.00") is None

def test_parse_odds_empty_returns_none():
    assert _parse_odds("") is None

def test_parse_odds_text_returns_none():
    assert _parse_odds("TBC") is None


# ── _parse_kickoff ────────────────────────────────────────────────────────────

def test_parse_kickoff_24h_time_only():
    today = date(2025, 6, 21)
    result = _parse_kickoff("15:00", today)
    assert result == datetime(2025, 6, 21, 15, 0, tzinfo=timezone.utc)

def test_parse_kickoff_12h_pm():
    today = date(2025, 6, 21)
    result = _parse_kickoff("3:00pm", today)
    assert result is not None
    assert result.hour == 15

def test_parse_kickoff_12h_noon():
    today = date(2025, 6, 21)
    result = _parse_kickoff("12:00pm", today)
    assert result is not None
    assert result.hour == 12

def test_parse_kickoff_12h_midnight():
    today = date(2025, 6, 21)
    result = _parse_kickoff("12:00am", today)
    assert result is not None
    assert result.hour == 0

def test_parse_kickoff_date_only_returns_none():
    today = date(2025, 6, 21)
    result = _parse_kickoff("Sat 21 Jun", today)
    assert result is None  # No time component — can't produce a datetime


# ── _classify_market ──────────────────────────────────────────────────────────

def test_classify_home_win():
    assert _classify_market("Home Win") == MarketType.MATCH_RESULT

def test_classify_draw():
    assert _classify_market("Draw") == MarketType.MATCH_RESULT

def test_classify_away_win():
    assert _classify_market("Away Win") == MarketType.MATCH_RESULT

def test_classify_btts_yes():
    assert _classify_market("Both Teams to Score - Yes") == MarketType.BTTS

def test_classify_btts_shorthand():
    assert _classify_market("BTTS") == MarketType.BTTS

def test_classify_over_under():
    assert _classify_market("Over 2.5 Goals") == MarketType.OVER_UNDER

def test_classify_under():
    assert _classify_market("Under 1.5 Goals") == MarketType.OVER_UNDER

def test_classify_unknown_defaults_to_match_result():
    assert _classify_market("Something Unusual") == MarketType.MATCH_RESULT


# ── _derive_confidence ────────────────────────────────────────────────────────

def test_confidence_none_odds_returns_base():
    assert _derive_confidence(None) == FST_BASE_CONFIDENCE

def test_confidence_short_odds_high():
    assert _derive_confidence(Decimal("1.30")) == 0.75

def test_confidence_mid_odds():
    c = _derive_confidence(Decimal("1.75"))
    assert c == 0.70

def test_confidence_long_odds_low():
    c = _derive_confidence(Decimal("5.00"))
    assert c == 0.55

def test_confidence_in_range():
    for odds_str in ["1.20", "1.60", "2.00", "3.00", "6.00"]:
        c = _derive_confidence(Decimal(odds_str))
        assert 0.50 <= c <= 0.80, f"Confidence {c} out of expected range for odds {odds_str}"


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
