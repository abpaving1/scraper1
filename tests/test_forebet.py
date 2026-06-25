"""
Unit tests for the Forebet scraper's pure-Python logic.

Covers:
  - _parse_probability     — % string → float
  - _implied_odds          — prob % → decimal odds
  - _parse_predicted_score — "2:1" → (2, 1)
  - _parse_avg_goals       — "1.45" → 1.45
  - _parse_kickoff         — date string → UTC datetime (or None)
  - _validate_prediction   — rejects malformed probability distributions
  - _derive_picks          — confirms correct market/selection derivation

Does NOT test Playwright DOM interaction (that requires a live browser and
verified selectors). Run selector verification manually with SCRAPE_HEADLESS=false.

Usage:
    python tests/test_forebet.py
"""

from __future__ import annotations

import sys
from datetime import datetime, timezone
from decimal import Decimal

# Patch env vars so config.py loads without real proxy credentials.
import os
os.environ.setdefault("PROXY_HOST", "fake-host")
os.environ.setdefault("PROXY_PORT", "1234")
os.environ.setdefault("PROXY_USERNAME", "fake-user")
os.environ.setdefault("PROXY_PASSWORD", "fake-pass")

from sources.forebet import (
    ForebetScraper,
    _implied_odds,
    _parse_avg_goals,
    _parse_kickoff,
    _parse_predicted_score,
    _parse_probability,
    MIN_CONFIDENCE_PCT,
    OVER_25_THRESHOLD,
    UNDER_25_THRESHOLD,
    BTTS_THRESHOLD,
)
from models.forebet import ForebetPrediction


# ─── Helper: build a minimal valid prediction ─────────────────────────────────

def _make_prediction(
    home_prob: float = 65.0,
    draw_prob: float = 20.0,
    away_prob: float = 15.0,
    avg_goals_home: float | None = 1.6,
    avg_goals_away: float | None = 1.0,
    kickoff_utc: datetime | None = datetime(2025, 6, 21, 15, 0, tzinfo=timezone.utc),
) -> ForebetPrediction:
    return ForebetPrediction(
        source_slug="forebet",
        home_team="Arsenal",
        away_team="Chelsea",
        league_name="Premier League",
        kickoff_utc=kickoff_utc,
        home_prob=home_prob,
        draw_prob=draw_prob,
        away_prob=away_prob,
        predicted_score_home=2,
        predicted_score_away=1,
        implied_odds_home=_implied_odds(home_prob),
        implied_odds_draw=_implied_odds(draw_prob),
        implied_odds_away=_implied_odds(away_prob),
        avg_goals_home=avg_goals_home,
        avg_goals_away=avg_goals_away,
        raw_text="Arsenal v Chelsea | test",
    )


_scraper = ForebetScraper.__new__(ForebetScraper)  # instantiate without __init__ (no Playwright)


# ─── _parse_probability ───────────────────────────────────────────────────────

def test_parse_probability_standard():
    assert _parse_probability("62%") == 62.0

def test_parse_probability_no_percent_sign():
    assert _parse_probability("62") == 62.0

def test_parse_probability_decimal():
    assert _parse_probability("62.3%") == 62.3

def test_parse_probability_empty_string():
    assert _parse_probability("") == 0.0

def test_parse_probability_non_numeric():
    assert _parse_probability("N/A") == 0.0


# ─── _implied_odds ────────────────────────────────────────────────────────────

def test_implied_odds_50_pct():
    assert _implied_odds(50.0) == Decimal("2.000")

def test_implied_odds_100_pct():
    assert _implied_odds(100.0) == Decimal("1.000")

def test_implied_odds_zero_handled():
    # Zero probability must not raise — clamps to 1 % (odds = 100.000)
    result = _implied_odds(0.0)
    assert result == Decimal("100.000")

def test_implied_odds_rounding():
    # 100 / 62 = 1.6129... → rounds to 1.613
    assert _implied_odds(62.0) == Decimal("1.613")


# ─── _parse_predicted_score ───────────────────────────────────────────────────

def test_parse_predicted_score_colon():
    assert _parse_predicted_score("2:1") == (2, 1)

def test_parse_predicted_score_dash():
    assert _parse_predicted_score("2-1") == (2, 1)

def test_parse_predicted_score_with_spaces():
    assert _parse_predicted_score("  2 : 1  ") == (2, 1)

def test_parse_predicted_score_empty():
    assert _parse_predicted_score("") == (None, None)

def test_parse_predicted_score_invalid():
    assert _parse_predicted_score("N/A") == (None, None)


# ─── _parse_avg_goals ────────────────────────────────────────────────────────

def test_parse_avg_goals_valid():
    assert _parse_avg_goals("1.45") == 1.45

def test_parse_avg_goals_integer():
    assert _parse_avg_goals("2") == 2.0

def test_parse_avg_goals_invalid():
    assert _parse_avg_goals("N/A") is None

def test_parse_avg_goals_empty():
    assert _parse_avg_goals("") is None


# ─── _parse_kickoff ───────────────────────────────────────────────────────────

def test_parse_kickoff_ddmm_format():
    result = _parse_kickoff("15/06 20:45")
    assert result is not None
    # CET offset applied: 20:45 CET → 19:45 UTC
    assert result.hour == 19
    assert result.minute == 45

def test_parse_kickoff_time_only():
    result = _parse_kickoff("20:45")
    assert result is not None
    assert result.minute == 45

def test_parse_kickoff_garbage():
    assert _parse_kickoff("???") is None


# ─── _validate_prediction ────────────────────────────────────────────────────

def test_validate_valid_prediction():
    p = _make_prediction(65.0, 20.0, 15.0)
    assert _scraper._validate_prediction(p) is True

def test_validate_sum_too_low():
    # 30 + 20 + 10 = 60 — wildly wrong
    p = _make_prediction(30.0, 20.0, 10.0)
    assert _scraper._validate_prediction(p) is False

def test_validate_sum_too_high():
    p = _make_prediction(60.0, 40.0, 30.0)  # 130 %
    assert _scraper._validate_prediction(p) is False

def test_validate_zero_probability():
    p = _make_prediction(0.0, 50.0, 50.0)
    assert _scraper._validate_prediction(p) is False


# ─── _derive_picks ────────────────────────────────────────────────────────────

def test_derive_picks_high_home_confidence():
    p = _make_prediction(70.0, 18.0, 12.0)
    results = _scraper._derive_picks(p)
    match_result = next((r for r in results if r.market == "match_result"), None)
    assert match_result is not None
    assert match_result.selection == "Home Win"
    assert match_result.confidence == round(70.0 / 100.0, 4)

def test_derive_picks_below_confidence_threshold():
    # All three outcomes below MIN_CONFIDENCE_PCT (each ~33 %) — no match result emitted.
    p = _make_prediction(34.0, 33.0, 33.0)
    results = _scraper._derive_picks(p)
    match_results = [r for r in results if r.market == "match_result"]
    assert len(match_results) == 0

def test_derive_picks_over_25_emitted():
    # avg total = 2.0 + 1.5 = 3.5 → Over 2.5
    p = _make_prediction(avg_goals_home=2.0, avg_goals_away=1.5)
    results = _scraper._derive_picks(p)
    over = next((r for r in results if r.market == "over_under_25"), None)
    assert over is not None
    assert over.selection == "Over 2.5"

def test_derive_picks_under_25_emitted():
    # avg total = 0.7 + 0.8 = 1.5 → Under 2.5
    p = _make_prediction(avg_goals_home=0.7, avg_goals_away=0.8)
    results = _scraper._derive_picks(p)
    under = next((r for r in results if r.market == "over_under_25"), None)
    assert under is not None
    assert under.selection == "Under 2.5"

def test_derive_picks_btts_yes_emitted():
    # Both sides >= BTTS_THRESHOLD (1.1)
    p = _make_prediction(avg_goals_home=1.4, avg_goals_away=1.2)
    results = _scraper._derive_picks(p)
    btts = next((r for r in results if r.market == "btts"), None)
    assert btts is not None
    assert btts.selection == "Yes"

def test_derive_picks_btts_not_emitted_when_one_side_low():
    # Away team avg goals too low
    p = _make_prediction(avg_goals_home=1.8, avg_goals_away=0.6)
    results = _scraper._derive_picks(p)
    btts = next((r for r in results if r.market == "btts"), None)
    assert btts is None

def test_derive_picks_no_avg_goals_skips_ou_and_btts():
    p = _make_prediction(avg_goals_home=None, avg_goals_away=None)
    results = _scraper._derive_picks(p)
    markets = {r.market for r in results}
    assert "over_under_25" not in markets
    assert "btts" not in markets

def test_derive_picks_confidence_capped():
    # Very high confidence should cap at 0.85 (Over) and 0.80 (Under/BTTS)
    p = _make_prediction(avg_goals_home=4.0, avg_goals_away=4.0)
    results = _scraper._derive_picks(p)
    over = next((r for r in results if r.market == "over_under_25"), None)
    assert over is not None
    assert over.confidence <= 0.85


# ─── Runner ───────────────────────────────────────────────────────────────────

def _run_all() -> None:
    tests = [(name, obj) for name, obj in globals().items()
             if name.startswith("test_") and callable(obj)]
    failures = 0
    for name, fn in tests:
        try:
            fn()
            print(f"PASS  {name}")
        except AssertionError as exc:
            failures += 1
            print(f"FAIL  {name}: {exc}")
        except Exception as exc:  # noqa: BLE001
            failures += 1
            print(f"ERROR {name}: {type(exc).__name__}: {exc}")

    print(f"\n{len(tests) - failures}/{len(tests)} passed")
    if failures:
        sys.exit(1)


if __name__ == "__main__":
    _run_all()
