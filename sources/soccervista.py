"""
SoccerVista scraper (Task 4).

SoccerVista publishes statistical match predictions with home/draw/away
probabilities. This scraper extracts the predictions table, converts the
probabilities into fair odds, derives confidence, and publishes RawPick
objects to Redis.
"""

import asyncio
import re
from datetime import datetime, timezone, date
from decimal import Decimal
from typing import Optional

from playwright.async_api import Page, TimeoutError as PWTimeout
from tenacity import retry, stop_after_attempt, wait_exponential, retry_if_exception_type

from config import settings
from models.pick import MarketType, RawPick
from queues.redis_publisher import PicksPublisher
from utils.logger import get_logger
from sources.base_scraper import BaseSourceScraper

logger = get_logger(__name__)

# ─────────────────────────────────────────────────────────────────────────────
# URL — this is the correct predictions page
# ─────────────────────────────────────────────────────────────────────────────
SV_BASE_URL = "https://www.soccervista.com/predictions/"

# ─────────────────────────────────────────────────────────────────────────────
# CSS SELECTORS — verified against the live site
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
# Page scraper
# ─────────────────────────────────────────────────────────────────────────────

@retry(
    retry=retry_if_exception_type((PWTimeout, ConnectionError, OSError)),
    stop=stop_after_attempt(settings.scrape_max_retries),
    wait=wait_exponential(multiplier=1, min=2, max=10),
    reraise=True,
)
async def _scrape_page(page: Page) -> list[RawPick]:
    import random

    logger.info("sv_scrape_starting", url=SV_BASE_URL, headless=settings.scrape_headless)

    await page.goto(SV_BASE_URL, timeout=settings.scrape_timeout_ms, wait_until="domcontentloaded")
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

    picks: list[RawPick] = []
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

            home_pct = _parse_probability(await (await row.query_selector(HOME_PCT_SEL)).inner_text())
            draw_pct = _parse_probability(await (await row.query_selector(DRAW_PCT_SEL)).inner_text())
            away_pct = _parse_probability(await (await row.query_selector(AWAY_PCT_SEL)).inner_text())
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
# Scraper class (used by cli.py)
# ─────────────────────────────────────────────────────────────────────────────

class SoccerVistaScraper(BaseSourceScraper):
    source_slug = SOURCE_SLUG
    base_url = SV_BASE_URL

    async def scrape(self):
        return await _scrape_page(self.page)

    async def run(self):
        from playwright.async_api import async_playwright
        from playwright_stealth import stealth_async

        async with async_playwright() as pw:
            browser = await pw.chromium.launch(headless=settings.scrape_headless)
            context = await browser.new_context(
                viewport={"width": 1280, "height": 800},
                locale="en-GB",
                timezone_id="Europe/London",
            )
            page = await context.new_page()
            await stealth_async(page)

            self.page = page

            try:
                picks = await self.scrape()
                for pick in picks:
                    await self.publisher.publish(pick)
                logger.info("sv_scrape_complete", published=len(picks))
                return picks
            finally:
                await browser.close()


# ─────────────────────────────────────────────────────────────────────────────
# Standalone runner (optional)
# ─────────────────────────────────────────────────────────────────────────────

async def run(publisher: PicksPublisher) -> None:
    """Standalone entry point for debugging."""
    from playwright.async_api import async_playwright
    from playwright_stealth import stealth_async

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=settings.scrape_headless)
        context = await browser.new_context(
            viewport={"width": 1280, "height": 800},
            locale="en-GB",
            timezone_id="Europe/London",
        )
        page = await context.new_page()
        await stealth_async(page)

        try:
            picks = await _scrape_page(page)
            for pick in picks:
                await publisher.publish(pick)
            logger.info("sv_scrape_complete", published=len(picks))
        finally:
            await browser.close()


if __name__ == "__main__":
    from utils.logger import configure_logging

    async def _main():
        configure_logging()
        pub = PicksPublisher()
        await pub.connect()
        try:
            await run(pub)
        finally:
            await pub.close()

    asyncio.run(_main())
