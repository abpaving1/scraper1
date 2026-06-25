"""
Data contracts between the scraper layer and the Redis queue / processor.
Keeping these typed means a malformed scrape fails fast at the source,
not silently inside the Weighting Engine three steps downstream.
"""

from datetime import datetime, timezone
from decimal import Decimal
from enum import Enum
from pydantic import BaseModel, Field, field_serializer, field_validator


class MarketType(str, Enum):
    MATCH_RESULT = "match_result"
    BTTS = "btts"
    OVER_UNDER = "over_under"
    ASIAN_HANDICAP = "asian_handicap"
    CORRECT_SCORE = "correct_score"
    DOUBLE_CHANCE = "double_chance"
    FIRST_GOALSCORER = "first_goalscorer"
    BET_BUILDER = "bet_builder"
    HALF_TIME_FULL_TIME = "half_time_full_time"


class RawPick(BaseModel):
    """
    Mirrors the `picks` table columns that originate from a scrape.
    `fixture_id` / `tipster_id` are NOT resolved here — that's the
    processor's job (Task 5), since the scraper only knows source-side
    identifiers (team names as strings, tipster profile slugs, etc).
    """

    source_slug: str = Field(..., description="e.g. 'olbg'")
    tipster_external_id: str = Field(..., description="Tipster's id/slug on the source site")
    tipster_name: str

    home_team_name: str
    away_team_name: str
    league_name: str | None = None
    kickoff_utc: datetime | None = None

    market: MarketType
    selection: str
    odds_decimal: Decimal | None = None
    confidence: Decimal | None = None

    raw_text: str
    posted_at: datetime
    scraped_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))

    @field_validator("odds_decimal")
    @classmethod
    def odds_must_be_valid(cls, v: Decimal | None) -> Decimal | None:
        if v is not None and v < Decimal("1.000"):
            raise ValueError("odds_decimal must be >= 1.000")
        return v

    @field_serializer("odds_decimal", "confidence")
    def serialize_decimal_fields(self, value: Decimal | None) -> str | None:
        return str(value) if value is not None else None

    @field_serializer("posted_at", "scraped_at", "kickoff_utc")
    def serialize_datetime_fields(self, value: datetime | None) -> str | None:
        return value.isoformat() if value is not None else None
