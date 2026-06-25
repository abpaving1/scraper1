"""
SoccerVista scraper (unified lifecycle version).

Uses BaseSourceScraper for:
- Playwright/browser/context lifecycle
- stealth page creation
- navigation with retry
- publishing via BaseSourceScraper.run()

This module focuses only on:
- selectors
- page extraction
- probability/odds/confidence logic
"""

from __future__ import annotations

import asyncio
import re
from datetime import datetime, timezone, date
from decimal import Decimal
from typing import Optional, List

from playwright.async_api import Page, TimeoutError as PWTimeout

from config import settings
from models.pick import MarketType, RawPick
from sources.base_scraper import BaseSourceScraper
from utils.logger import get_logger

logger = get_logger(__name__)

# ─────────────────────────────────────────────────────────────────────────────
# URL
# ─────────────────────────────────────────────────────────────────────────────
SV_BASE_URL = "https://www.soccervista.com/predictions/"

# ─────────────────────────────────────────────────────────────────────────────
# CSS SELECTORS
# ─────────────────────────────────────────────────────────────────────────────
TABLE_ROW_SEL = "div.match"
HOME_TEAM_SEL = "div.teams div.home"
AWAY_TEAM_SEL = "div.teams div.away"
HOME_PCT_SEL = "div.pred.home"
DRAW_PCT_SEL = "div.pred.draw"
AWAY_PCT_SEL = "div.pred.away"
KICKOFF_SEL = "div.time"
LEAGUE_SEL = "div.league"

# ─────────────────────────────────────────────────────────────────────────────
# Source identity
# ─────────────────────────────────────────────────────────────────────────────
SOURCE_SLUG = "soccervista"
SV_TIPSTER_EXTERNAL_ID = "soccervista-algorithm-v1"
SV_TIPSTER_NAME = "SoccerVista Algorithm"

MIN_PROBABILITY_GAP_PP = 15.0
MAX_PICKS_PER_RUN = 50


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────

def _parse_probability(raw: str) -> Optional[float]:
    raw = raw.strip().replace("%", "")
    try:
        val = float(raw)
        if 0 < val <= 1.0:
            val *= 100
        if 0 < val <= 100:
            return val
    except ValueError:
        return None
    return None


def _probability_to_decimal_odds(probability_pct: float) -> Optional[Decimal]:
    if probability_pct <= 0 or probability_pct >= 100:
        return None
    fair_odds = 1 / (probability_pct / 100)
    return Decimal(str(round(fair_odds, 3)))


def _derive_confidence(winning_pct: float, gap_pp: float) -> float:
    if winning_pct >= 70:
        base = 0.80
    elif winning_pct >= 60:
        base = 0.70
    elif winning_pct >= 50:
        base = 0.60
    elif winning_pct >= 40:
        base = 0.50
    else:
        base = 0.40

    gap_bonus = min(0.05, gap_pp / 100)
    return round(min(0.85, base + gap_bonus), 3)


def _select_pick(home_pct: float, draw_pct: float, away_pct: float):
    outcomes = [
        ("Home Win", home_pct),
        ("Draw", draw_pct),
        ("Away Win", away_pct),
    ]
    outcomes.sort(key=lambda x: x[1], reverse=True)
    top_name, top_pct = outcomes[0]
    second_pct = outcomes[1][1]
    gap = top_pct - second_pct

    if gap < MIN_PROBABILITY_GAP_PP:
        return None

    return top_name, MarketType.MATCH_RESULT, top_pct, gap


def _parse_kickoff(raw: str, today: date) -> Optional[datetime]:
    raw = raw.strip()

    m = re.search(r"(\d{1,2})/(\d{1,2})\s+(\d{2}):(\d{2})", raw)
    if m:
        day, month, hour, minute = map(int, m.groups())
        try:
            return datetime(today.year, month, day, hour, minute, tzinfo=timezone.utc)
        except ValueError:
            pass

    m = re.search(r"(\d{1,2}):(\d{2})", raw)
    if m:
        hour, minute = map(int, m.groups())
        try:
            return datetime(today.year, today.month, today.day, hour, minute, tzinfo=timezone.utc)
        except ValueError:
            pass

    return None


# ─────────────────────────────────────────────────────────────────────────────
# Page extraction (no navigation here)
# ─────────────────────────────────────────────────────────────────────────────

async def _scrape_page(page: Page) -> List[RawPick]:
    """
    Extracts all prediction rows from an already-loaded SoccerVista page.
    Caller is responsible for navigation (no page.goto() here).
    """
    import random

    logger.info("sv_scrape_starting_dom", url=SV_BASE_URL, headless=settings.scrape_headless)

    # Human-like pause before scraping
    await asyncio.sleep(random.uniform(settings.scrape_jitter_min_seconds, settings.scrape_jitter_max_seconds))

    try:
        await page.wait_for_selector(TABLE_ROW_SEL, timeout=15_000)
    except PWTimeout:
        logger.error(
            "sv_no_rows_found",
            selector=TABLE_ROW_SEL,
            url=SV_BASE_URL,
            hint="DOM may have changed — re-verify selectors with SCRAPE_HEADLESS=false",
        )
        return []

    rows = await page.query_selector_all(TABLE_ROW_SEL)
    logger.info("sv_rows_found", count=len(rows))

    picks: List[RawPick] = []
    today = date.today()
    scraped_at = datetime.now(timezone.utc)

    for row in rows:
        try:
            home_el = await row.query_selector(HOME_TEAM_SEL)
            away_el = await row.query_selector(AWAY_TEAM_SEL)
            if not home_el or not away_el:
                continue

            home = (await home_el.inner_text()).strip()
            away = (await away_el.inner_text()).strip()
            if not home or not away or home == away:
                continue

            home_pct_el = await row.query_selector(HOME_PCT_SEL)
            draw_pct_el = await row.query_selector(DRAW_PCT_SEL)
            away_pct_el = await row.query_selector(AWAY_PCT_SEL)
            if not home_pct_el or not draw_pct_el or not away_pct_el:
                continue

            home_pct = _parse_probability(await home_pct_el.inner_text())
            draw_pct = _parse_probability(await draw_pct_el.inner_text())
            away_pct = _parse_probability(await away_pct_el.inner_text())
            if any(p is None for p in [home_pct, draw_pct, away_pct]):
                continue

            pick_result = _select_pick(home_pct, draw_pct, away_pct)
            if pick_result is None:
                continue

            selection_text, market, winning_pct, gap_pp = pick_result

            league_el = await row.query_selector(LEAGUE_SEL)
            league = (await league_el.inner_text()).strip() if league_el else None

            ko_el = await row.query_selector(KICKOFF_SEL)
            kickoff = _parse_kickoff((await ko_el.inner_text()).strip() if ko_el else "", today)

            odds = _probability_to_decimal_odds(winning_pct)
            confidence = _derive_confidence(winning_pct, gap_pp)
            raw_text = f"{home} v {away} | {selection_text} ({winning_pct:.0f}%)"

            picks.append(
                RawPick(
                    source_slug=SOURCE_SLUG,
                    tipster_external_id=SV_TIPSTER_EXTERNAL_ID,
                    tipster_name=SV_TIPSTER_NAME,
                    home_team_name=home,
                    away_team_name=away,
                    league_name=league,
                    kickoff_utc=kickoff,
                    market=market,
                    selection=selection_text,
                    odds_decimal=odds,
                    confidence=confidence,
                    raw_text=raw_text,
                    posted_at=scraped_at,
                    scraped_at=scraped_at,
                )
            )

            if len(picks) >= MAX_PICKS_PER_RUN:
                logger.info("sv_max_picks_reached", limit=MAX_PICKS_PER_RUN)
                break

        except Exception as exc:
            logger.warning("sv_row_parse_error", error=str(exc))
            continue

    picks.sort(key=lambda p: p.confidence or 0.0, reverse=True)
    logger.info("sv_picks_extracted", count=len(picks))
    return picks


# ─────────────────────────────────────────────────────────────────────────────
# Scraper class (unified lifecycle)
# ─────────────────────────────────────────────────────────────────────────────

class SoccerVistaScraper(BaseSourceScraper):
    source_slug = SOURCE_SLUG
    base_url = SV_BASE_URL

    async def scrape(self) -> List[RawPick]:
        """
        Opens a stealth page, navigates via BaseSourceScraper.goto_with_retry,
        then extracts picks via _scrape_page.
        Publishing is handled by BaseSourceScraper.run().
        """
        page = await self.new_stealth_page()
        picks: List[RawPick] = []

        try:
            logger.info("sv_scrape_starting", url=self.base_url, headless=settings.scrape_headless)
            await self.goto_with_retry(page, self.base_url)
            picks = await _scrape_page(page)
        finally:
            await page.close()

        logger.info("sv_scrape_complete", source=self.source_slug, picks=len(picks))
        return picks