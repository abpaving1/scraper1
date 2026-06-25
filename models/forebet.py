"""
Forebet-specific data models.

Forebet is a statistical/algorithmic source, not a community tipster platform.
It publishes per-match probability distributions (1X2 %) and a predicted
score rather than a single selection. We therefore need two models:

  ForebetPrediction — the raw output of scraping one table row from Forebet,
                      containing the full probability distribution.

  ForebetMatchResult — a resolved "pick" derived from the prediction, used to
                       decide which market/selection to emit as a RawPick.

The ForebetScraper converts ForebetPrediction → one or more RawPick instances
(match result + optionally BTTS and over/under) before publishing.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal


@dataclass(frozen=True, slots=True)
class ForebetPrediction:
    """
    Represents everything scraped from one Forebet table row.

    Probability fields (home_prob, draw_prob, away_prob) are percentages
    expressed as floats (e.g. 62.3 means 62.3 %). They must sum to ~100;
    we validate this loosely in ForebetScraper._validate_prediction() rather
    than raising inside the dataclass to keep error reporting richer.

    implied_odds_* are computed from probabilities: 100 / prob. They exist on
    the model so the publisher can populate RawPick.odds_decimal without
    re-deriving them downstream.

    avg_goals_home / avg_goals_away come from Forebet's statistical model
    and represent the expected goal tally per side — used to drive the
    over/under pick derivation.
    """

    source_slug: str                   # always "forebet"
    home_team: str
    away_team: str
    league_name: str
    kickoff_utc: datetime | None

    # 1X2 probabilities (%)
    home_prob: float
    draw_prob: float
    away_prob: float

    # Forebet's own predicted scoreline (may be None if element missing)
    predicted_score_home: int | None
    predicted_score_away: int | None

    # Computed implied decimal odds (100 / prob), rounded to 3 dp
    implied_odds_home: Decimal
    implied_odds_draw: Decimal
    implied_odds_away: Decimal

    # Forebet's average-goals stats (used for over/under derivation)
    avg_goals_home: float | None = None
    avg_goals_away: float | None = None

    # Weather / pitch condition metadata (scraped if present, else None)
    weather_info: str | None = None

    # Free-text raw row content — kept for debug / reprocessing
    raw_text: str = ""


@dataclass(frozen=True, slots=True)
class ForebetMatchResult:
    """
    The resolved market selection derived from a ForebetPrediction.

    A single ForebetPrediction may produce multiple ForebetMatchResult
    instances — e.g. one for the 1X2 result, one for over/under if the
    expected goals tally qualifies. Each becomes its own RawPick on the queue.
    """

    market: str           # "match_result" | "over_under_25" | "btts"
    selection: str        # "Home Win" | "Draw" | "Away Win" | "Over 2.5" | "Yes" | "No"
    odds_decimal: Decimal
    confidence: float     # 0.0–1.0, derived from probability / 100
    raw_text: str         # echoes parent prediction's raw_text
