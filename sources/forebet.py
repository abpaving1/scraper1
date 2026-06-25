"""
Forebet scraper (unified lifecycle version).

This version uses BaseSourceScraper for:
- Playwright/browser/context lifecycle
- stealth page creation
- navigation with retry
- publishing via BaseSourceScraper.run()

Forebet-specific logic is limited to:
- selectors
- row parsing
- probability/odds/confidence derivation
"""

from __future__ import annotations

import logging
import re
from datetime import datetime, timezone, timedelta
from decimal import Decimal, ROUND_HALF_UP
from typing import List, Optional

from playwright.async_api import Page

from config import settings
from models.forebet import ForebetMatchResult, ForebetPrediction
from models.pick import MarketType, RawPick
from sources.base_scraper import BaseSourceScraper

logger = logging.getLogger(__name__)

# ─── Confidence filter ────────────────────────────────────────────────────────
MIN_CONFIDENCE_PCT: float = 55.0

OVER_25_THRESHOLD: float = 2.5
UNDER_25_THRESHOLD: float = 1.8
BTTS_THRESHOLD: float = 1.1

FOREBET_TIPSTER_NAME: str = "Forebet Algorithm"
FOREBET_TIPSTER_EXTERNAL_ID: str = "forebet-algorithm-v1"

# ─── CSS selectors ────────────────────────────────────────────────────────────
SEL_PREDICTION_ROW = "table.schema tr.rcnt"

SEL_HOME_TEAM = "td.homeTeam span"
SEL_AWAY_TEAM = "td.awayTeam span"
SEL_KICKOFF_TIME = "td.date_bah"
SEL_LEAGUE_NAME = "td.shortTag span"

SEL_PROB_HOME = "td.predict span.forepr"
SEL_PROB_DRAW = "td.predict:nth-child(2) span.forepr"
SEL_PROB_AWAY = "td.predict:nth-child(3) span.forepr"

SEL_PREDICTED_SCORE = "td.lscr_td span.lscrsp"

SEL_AVG_GOALS_HOME = "td.avg_sc:nth-child(1)"
SEL_AVG_GOALS_AWAY = "td.avg_sc:nth-child(2)"


# ─── Helpers ──────────────────────────────────────────────────────────────────

def _parse_probability(raw: str) -> float:
    cleaned = raw.strip().rstrip("%")
    try:
        return float(cleaned)
    except ValueError:
        return 0.0


def _implied_odds(prob_pct: float) -> Decimal:
    if prob_pct <= 0:
        prob_pct = 1.0
    raw = Decimal(str(100.0 / prob_pct))
    return raw.quantize(Decimal("0.001"), rounding=ROUND_HALF_UP)


def _parse_predicted_score(raw: str) -> tuple[Optional[int], Optional[int]]:
    match = re.match(r"(\d+)\s*[:\-]\s*(\d+)", raw.strip())
    if match:
        return int(match.group(1)), int(match.group(2))
    return None, None


def _parse_avg_goals(raw: str) -> Optional[float]:
    try:
        return float(raw.strip())
    except ValueError:
        return None


def _parse_kickoff(raw: str) -> Optional[datetime]:
    raw = raw.strip()
    now = datetime.now(timezone.utc)

    patterns = [
        ("%d/%m %H:%M", True),
        ("%Y-%m-%d %H:%M", False),
        ("%H:%M", True),
    ]

    for fmt, inject_year in patterns:
        try:
            if inject_year and "%d/%m" in fmt:
                raw_with_year = f"{now.year}/{raw}"
                parsed = datetime.strptime(raw_with_year, f"%Y/{fmt}")
            elif inject_year and fmt == "%H:%M":
                date_str = now.strftime("%Y-%m-%d") + " " + raw
                parsed = datetime.strptime(date_str, "%Y-%m-%d %H:%M")
            else:
                parsed = datetime.strptime(raw, fmt)

            # Assume CET (UTC+1), store as UTC
            return parsed.replace(tzinfo=timezone.utc) - timedelta(hours=1)
        except ValueError:
            continue

    logger.warning("forebet_kickoff_parse_failed", extra={"raw": raw})
    return None


# ─── Unified ForebetScraper ──────────────────────────────────────────────────

class ForebetScraper(BaseSourceScraper):
    source_slug = "forebet"
    base_url = settings.forebet_football_url

    async def scrape(self) -> List[RawPick]:
        """
        Uses BaseSourceScraper lifecycle:
        - new_stealth_page()
        - goto_with_retry()
        - returns list[RawPick]
        Publishing is handled by BaseSourceScraper.run().
        """
        page = await self.new_stealth_page()
        picks: List[RawPick] = []

        try:
            logger.info("forebet_scrape_starting", extra={"url": self.base_url})
            await self.goto_with_retry(page, self.base_url)
            await page.wait_for_selector(SEL_PREDICTION_ROW, timeout=15_000)
            rows = await page.query_selector_all(SEL_PREDICTION_ROW)
            logger.info("forebet_rows_found", extra={"count": len(rows)})

            for row in rows:
                try:
                    prediction = await self._parse_row(row)
                except Exception as exc:
                    logger.warning("forebet_row_parse_error", extra={"error": str(exc)})
                    continue

                if prediction is None:
                    continue

                if not self._validate_prediction(prediction):
                    continue

                for result in self._derive_picks(prediction):
                    picks.append(self._to_raw_pick(prediction, result))

        except Exception as exc:
            logger.error("forebet_scrape_failed", extra={"error": str(exc)})
        finally:
            await page.close()

        logger.info("forebet_scrape_complete", extra={"picks": len(picks)})
        return picks

    async def _parse_row(self, row) -> Optional[ForebetPrediction]:
        async def text(selector: str) -> str:
            el = await row.query_selector(selector)
            return (await el.inner_text()).strip() if el else ""

        home_team = await text(SEL_HOME_TEAM)
        away_team = await text(SEL_AWAY_TEAM)

        if not home_team or not away_team:
            return None

        raw_kickoff = await text(SEL_KICKOFF_TIME)
        raw_league = await text(SEL_LEAGUE_NAME)
        raw_prob_home = await text(SEL_PROB_HOME)
        raw_prob_draw = await text(SEL_PROB_DRAW)
        raw_prob_away = await text(SEL_PROB_AWAY)
        raw_score = await text(SEL_PREDICTED_SCORE)
        raw_avg_home = await text(SEL_AVG_GOALS_HOME)
        raw_avg_away = await text(SEL_AVG_GOALS_AWAY)

        home_prob = _parse_probability(raw_prob_home)
        draw_prob = _parse_probability(raw_prob_draw)
        away_prob = _parse_probability(raw_prob_away)

        score_home, score_away = _parse_predicted_score(raw_score)

        raw_text = (
            f"{home_team} v {away_team} | {raw_league} | {raw_kickoff} | "
            f"{raw_prob_home}/{raw_prob_draw}/{raw_prob_away} | score: {raw_score}"
        )

        return ForebetPrediction(
            source_slug=self.source_slug,
            home_team=home_team,
            away_team=away_team,
            league_name=raw_league or "Unknown",
            kickoff_utc=_parse_kickoff(raw_kickoff),
            home_prob=home_prob,
            draw_prob=draw_prob,
            away_prob=away_prob,
            predicted_score_home=score_home,
            predicted_score_away=score_away,
            implied_odds_home=_implied_odds(home_prob),
            implied_odds_draw=_implied_odds(draw_prob),
            implied_odds_away=_implied_odds(away_prob),
            avg_goals_home=_parse_avg_goals(raw_avg_home),
            avg_goals_away=_parse_avg_goals(raw_avg_away),
            raw_text=raw_text,
        )

    def _validate_prediction(self, p: ForebetPrediction) -> bool:
        total = p.home_prob + p.draw_prob + p.away_prob
        if not (90.0 <= total <= 110.0):
            logger.warning(
                "forebet_probability_sum_invalid",
                extra={"home": p.home_team, "away": p.away_team, "total": total},
            )
            return False

        if p.home_prob <= 0 or p.draw_prob <= 0 or p.away_prob <= 0:
            logger.warning(
                "forebet_zero_probability",
                extra={"home": p.home_team, "away": p.away_team},
            )
            return False

        return True

    def _derive_picks(self, p: ForebetPrediction) -> List[ForebetMatchResult]:
        results: List[ForebetMatchResult] = []

        # 1. Match result
        candidates = [
            ("Home Win", p.home_prob, p.implied_odds_home),
            ("Draw", p.draw_prob, p.implied_odds_draw),
            ("Away Win", p.away_prob, p.implied_odds_away),
        ]
        selection, best_prob, best_odds = max(candidates, key=lambda c: c[1])

        if best_prob >= MIN_CONFIDENCE_PCT:
            results.append(
                ForebetMatchResult(
                    market="match_result",
                    selection=selection,
                    odds_decimal=best_odds,
                    confidence=round(best_prob / 100.0, 4),
                    raw_text=p.raw_text,
                )
            )

        # 2. Over/Under 2.5
        if p.avg_goals_home is not None and p.avg_goals_away is not None:
            avg_total = p.avg_goals_home + p.avg_goals_away

            if avg_total >= OVER_25_THRESHOLD:
                over_confidence = min(0.85, 0.55 + (avg_total - OVER_25_THRESHOLD) * 0.15)
                over_odds = Decimal(str(round(1.0 / over_confidence, 3)))

                if over_confidence * 100 >= MIN_CONFIDENCE_PCT:
                    results.append(
                        ForebetMatchResult(
                            market="over_under",
                            selection="Over 2.5",
                            odds_decimal=over_odds,
                            confidence=round(over_confidence, 4),
                            raw_text=p.raw_text,
                        )
                    )

            elif avg_total <= UNDER_25_THRESHOLD:
                under_confidence = min(0.80, 0.55 + (UNDER_25_THRESHOLD - avg_total) * 0.20)
                under_odds = Decimal(str(round(1.0 / under_confidence, 3)))

                if under_confidence * 100 >= MIN_CONFIDENCE_PCT:
                    results.append(
                        ForebetMatchResult(
                            market="over_under",
                            selection="Under 2.5",
                            odds_decimal=under_odds,
                            confidence=round(under_confidence, 4),
                            raw_text=p.raw_text,
                        )
                    )

        # 3. BTTS Yes
        if (
            p.avg_goals_home is not None
            and p.avg_goals_away is not None
            and p.avg_goals_home >= BTTS_THRESHOLD
            and p.avg_goals_away >= BTTS_THRESHOLD
        ):
            margin = min(p.avg_goals_home, p.avg_goals_away) - BTTS_THRESHOLD
            btts_confidence = min(0.80, 0.55 + margin * 0.25)
            btts_odds = Decimal(str(round(1.0 / btts_confidence, 3)))

            if btts_confidence * 100 >= MIN_CONFIDENCE_PCT:
                results.append(
                    ForebetMatchResult(
                        market="btts",
                        selection="Yes",
                        odds_decimal=btts_odds,
                        confidence=round(btts_confidence, 4),
                        raw_text=p.raw_text,
                    )
                )

        return results

    def _to_raw_pick(self, prediction: ForebetPrediction, result: ForebetMatchResult) -> RawPick:
        return RawPick(
            source_slug=self.source_slug,
            tipster_external_id=FOREBET_TIPSTER_EXTERNAL_ID,
            tipster_name=FOREBET_TIPSTER_NAME,
            home_team_name=prediction.home_team,
            away_team_name=prediction.away_team,
            league_name=prediction.league_name,
            kickoff_utc=prediction.kickoff_utc,
            market=MarketType(result.market),
            selection=result.selection,
            odds_decimal=result.odds_decimal,
            confidence=Decimal(str(result.confidence)) if result.confidence is not None else None,
            raw_text=result.raw_text,
            posted_at=datetime.now(timezone.utc),
        )