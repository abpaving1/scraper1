"""
FreeSuperTips scraper (Task 4).

FreeSuperTips (freesupertips.com) publishes editorial expert tips focused
primarily on the Premier League and EFL. Anti-bot difficulty is LOW — no
Cloudflare challenge, standard HTML rendering, no JS-gated content on the
tips listing page. Standard Playwright + stealth is sufficient; no special
fingerprint tricks required beyond the shared base scraper approach.

Scrape cadence: every 4 hours (tips update throughout the day as editors
publish; running more frequently yields diminishing returns and risks
rate-limiting).

DOM STRUCTURE (verified June 2026):
  Tips are grouped into .Card elements (one per accumulator/tip group).
  Each .Leg inside a .Card represents one individual tip within the group.
  The tip selection is in .Leg__win, the match context in .Leg__lose,
  and the kickoff time in a <time> element within .Leg__teams.
  Odds are NOT available per-leg on the listing page (only the accumulator
  total is shown via .BetGrid); we fall back to None and confidence=0.65.

VERIFICATION CHECKLIST — run with SCRAPE_HEADLESS=false before production:
  [ ] FST_TIPS_URL still resolves to the today's tips listing
  [ ] CSS selector TIP_ROW_SEL matches each individual tip .Leg element
  [ ] SELECTION_SEL matches .Leg__win (e.g. "Home Win", "Over 2.5 Goals")
  [ ] MATCH_SEL matches .Leg__lose (opponent/fixture context)
  [ ] KICKOFF_SEL matches the <time> element inside .Leg__teams
  [ ] TIPSTER_SEL (optional per-card byline)
"""

import asyncio
import re
from datetime import datetime, timezone, date, timedelta
from decimal import Decimal, InvalidOperation
from typing import Optional
from zoneinfo import ZoneInfo

import structlog
from playwright.async_api import Page, TimeoutError as PWTimeout
from playwright_stealth import stealth_async
from tenacity import retry, stop_after_attempt, wait_exponential, retry_if_exception_type

from config import settings
from models.pick import MarketType, RawPick
from queues.redis_publisher import PicksPublisher
from sources.base_scraper import BaseSourceScraper
from utils.logger import get_logger

UK_TZ = ZoneInfo("Europe/London")

logger = get_logger(__name__)

# ── URL ──────────────────────────────────────────────────────────────────────
# The canonical football tips listing URL (confirmed June 2026).
FST_TIPS_URL = "https://www.freesupertips.com/free-football-betting-tips/"

# ── CSS SELECTORS (verified against live DOM, June 2026) ─────────────────────
# FST renders tips inside .Card containers. Each .Leg is one tip within a
# group (accumulator). The .Leg__win div holds the selection text and
# .Leg__lose holds the fixture context ("vs Opponent" or "at Opponent").
# There are no per-tip odds on the listing page — only the accumulator total
# is shown; we therefore omit odds and fall back to the editorial baseline.
TIP_ROW_SEL = ".Leg"                          # one tip per .Leg element
SELECTION_SEL = ".Leg__win"                  # tip text: "Home Win", "BTTS", etc.
MATCH_SEL = ".Leg__lose"                     # match context: "vs Chelsea"
KICKOFF_SEL = ".Leg__teams time"             # <time> element with kickoff text
ODDS_SEL = None                              # no per-tip odds on listing page
TIPSTER_SEL = ".TipHeader h2"               # card-level headline (not per-leg)

# FST's fixed source slug and synthetic tipster identity (one editorial team,
# not individual tipsters with tracked ROI — modelled similarly to Forebet).
SOURCE_SLUG = "freesupertips"
FST_TIPSTER_EXTERNAL_ID = "freesupertips-editorial-v1"
FST_TIPSTER_NAME = "FreeSuperTips Editorial"

# Editorial authority baseline confidence — FST doesn't expose per-tip
# confidence or tipster ROI, so we assign a fixed value reflecting their
# status as a curated editorial source rather than a community tipster.
# 0.65 is intentionally below Forebet's algorithmic output range to reflect
# the qualitative rather than quantitative nature of the picks.
FST_BASE_CONFIDENCE = 0.65


def _parse_match(raw: str) -> tuple[str, str]:
    """
    Splits 'Arsenal v Chelsea' or 'Arsenal vs Chelsea' into (home, away).

    On the FST listing page, .Leg__lose contains a phrase like:
      'vs Germany'  → we only know the opponent, not which is home/away.
      'at Germany'  → away fixture; FST team is the away side.
    In these cases we treat the selection's implied team as home and the
    opponent as away (an approximation — league context is not given per-leg).

    Returns ('', '') if no team pair can be derived.
    """
    for sep in (" v ", " vs ", " vs. ", " - "):
        if sep in raw:
            parts = raw.split(sep, 1)
            return parts[0].strip(), parts[1].strip()
    logger.warning("fst_match_parse_failed", raw=raw)
    return "", ""


def _parse_odds(raw: str) -> Optional[Decimal]:
    """Extracts a decimal odds value from strings like '1.90', '2/1', '6/4'."""
    raw = raw.strip()
    # Fractional odds (e.g. "2/1") — convert to decimal
    frac = re.match(r"^(\d+)/(\d+)$", raw)
    if frac:
        num, den = int(frac.group(1)), int(frac.group(2))
        if den == 0:
            return None
        return Decimal(str(round(num / den + 1, 3)))
    # Already decimal
    try:
        val = Decimal(re.sub(r"[^\d.]", "", raw))
        return val if val >= Decimal("1.01") else None
    except InvalidOperation:
        return None


def _parse_kickoff(raw: str, today: date) -> Optional[datetime]:
    """
    Attempts to parse FST's kickoff time strings into a UTC datetime.
    FST shows times in UK local time (GMT in winter, BST=UTC+1 in summer).
    We use zoneinfo to get the correct offset automatically rather than
    hardcoding +1 year-round (which would be wrong in winter).

    Formats handled:
      "15:00"       — time only, assume today (UK local)
      "3:00pm"      — 12h format, assume today
      "Sat 15:00"   — day-of-week + time — parse to nearest future day
      "Sat 21 Jun"  — date only (no time) — returns None
    """
    raw = raw.strip().lower()

    # Day-of-week + time: "sat 15:00"
    day_time = re.search(
        r"(mon|tue|wed|thu|fri|sat|sun)\s+(\d{1,2}):(\d{2})\s*(am|pm)?", raw
    )
    if day_time:
        day_abbr = day_time.group(1)
        hour = int(day_time.group(2))
        minute = int(day_time.group(3))
        meridiem = day_time.group(4)
    else:
        # Time-only: "15:00" or "3:00pm"
        day_abbr = None
        time_only = re.search(r"(\d{1,2}):(\d{2})\s*(am|pm)?", raw)
        if time_only:
            hour = int(time_only.group(1))
            minute = int(time_only.group(2))
            meridiem = time_only.group(3)
        else:
            return None

    if meridiem == "pm" and hour < 12:
        hour += 12
    elif meridiem == "am" and hour == 12:
        hour = 0

    try:
        if day_abbr:
            # Map to the nearest upcoming weekday
            days = {"mon": 0, "tue": 1, "wed": 2, "thu": 3, "fri": 4, "sat": 5, "sun": 6}
            target_weekday = days[day_abbr]
            days_ahead = (target_weekday - today.weekday()) % 7
            target_date = today + timedelta(days=days_ahead)
        else:
            target_date = today

        # Use zoneinfo for correct BST/GMT offset — not hardcoded +1
        local_dt = datetime(
            target_date.year, target_date.month, target_date.day,
            hour, minute,
            tzinfo=UK_TZ,
        )
        return local_dt.astimezone(timezone.utc)
    except (ValueError, KeyError):
        return None


def _classify_market(selection_text: str) -> MarketType:
    """
    Maps FST selection text to our canonical MarketType enum.
    FST uses plain English — no code-based market identifiers.
    """
    text = selection_text.lower()
    if any(k in text for k in ("home win", "away win", "draw", "1x2", "home", "away")):
        return MarketType.MATCH_RESULT
    if "btts" in text or "both teams" in text:
        return MarketType.BTTS
    if "over" in text or "under" in text:
        return MarketType.OVER_UNDER
    # Default to match result for unclassified FST picks
    return MarketType.MATCH_RESULT


def _derive_confidence(odds: Optional[Decimal]) -> float:
    """
    FST doesn't publish confidence scores. We derive a proxy from odds:
    shorter-priced selections are treated as higher-confidence editorial
    choices. Scaled between 0.55 (long shots) and 0.75 (strong favourites).
    FST_BASE_CONFIDENCE (0.65) is the midpoint for unpriced or mid-odds picks.
    """
    if odds is None:
        return FST_BASE_CONFIDENCE
    o = float(odds)
    if o <= 1.40:
        return 0.75
    if o <= 1.80:
        return 0.70
    if o <= 2.50:
        return 0.65
    if o <= 4.00:
        return 0.60
    return 0.55


async def _scrape_page(page: Page) -> list[RawPick]:
    """
    Extracts all tip rows from the already-loaded FST tips page.
    The caller is responsible for navigation — this function does NOT
    call page.goto() to avoid double navigation.
    """
    import random

    # Human-like pause before scraping
    await asyncio.sleep(random.uniform(settings.scrape_jitter_min_seconds, settings.scrape_jitter_max_seconds))

    # Wait for at least one tip row — if none appear within timeout, FST has
    # changed their DOM and selectors need re-verification.
    try:
        await page.wait_for_selector(TIP_ROW_SEL, timeout=15_000)
    except PWTimeout:
        logger.error(
            "fst_no_tip_rows_found",
            selector=TIP_ROW_SEL,
            url=FST_TIPS_URL,
            hint="DOM may have changed — re-verify selectors with SCRAPE_HEADLESS=false",
        )
        return []

    tip_rows = await page.query_selector_all(TIP_ROW_SEL)
    logger.info("fst_tip_rows_found", count=len(tip_rows))

    picks: list[RawPick] = []
    today = date.today()
    scraped_at = datetime.now(timezone.utc)

    for row in tip_rows:
        try:
            # ── Selection (tip text) ──────────────────────────────────────
            sel_el = await row.query_selector(SELECTION_SEL)
            if not sel_el:
                continue
            selection_text = (await sel_el.inner_text()).strip()
            if not selection_text:
                continue

            # ── Match context (.Leg__lose: "vs Germany" / "at Paraguay") ──
            match_el = await row.query_selector(MATCH_SEL)
            match_raw = (await match_el.inner_text()).strip() if match_el else ""

            # Build a combined fixture string for _parse_match.
            # .Leg__lose already has the opponent; we synthesise "Team A vs Team B".
            # We don't know the FST team's name from the listing page alone,
            # so we flag unknown home and use the context string as away.
            combined = f"{selection_text} {match_raw}" if match_raw else selection_text
            home, away = _parse_match(combined)
            if not home or not away:
                # Fall back: use selection as home team hint, context as away
                opponent = re.sub(r"^(?:vs\.?|at)\s+", "", match_raw, flags=re.IGNORECASE).strip()
                if not opponent:
                    logger.debug("fst_match_parse_no_opponent", raw=match_raw)
                    continue
                home = "FST Pick"   # placeholder — real name not on listing page
                away = opponent

            # ── Kickoff time ──────────────────────────────────────────────
            ko_el = await row.query_selector(KICKOFF_SEL)
            ko_raw = (await ko_el.inner_text()).strip() if ko_el else ""
            kickoff = _parse_kickoff(ko_raw, today)

            # ── Odds — not available per-leg on listing page ──────────────
            odds: Optional[Decimal] = None  # no per-leg odds on FST listing

            # ── Tipster (card-level headline used as tipster context) ──────
            tipster_el = await row.query_selector(TIPSTER_SEL)
            tipster_name = (
                (await tipster_el.inner_text()).strip() if tipster_el else FST_TIPSTER_NAME
            )

            market = _classify_market(selection_text)
            confidence = _derive_confidence(odds)

            pick = RawPick(
                source_slug=SOURCE_SLUG,
                tipster_external_id=FST_TIPSTER_EXTERNAL_ID,
                tipster_name=tipster_name or FST_TIPSTER_NAME,
                home_team_name=home,
                away_team_name=away,
                league_name=None,  # not available per-leg on listing
                kickoff_utc=kickoff,
                market=market,
                selection=selection_text,
                odds_decimal=odds,
                confidence=confidence,
                raw_text=(selection_text + " | " + match_raw).strip(" | "),
                posted_at=scraped_at,
                scraped_at=scraped_at,
            )
            picks.append(pick)

        except Exception as exc:  # noqa: BLE001
            logger.warning("fst_row_parse_error", error=str(exc))
            continue

    logger.info("fst_picks_extracted", count=len(picks))
    return picks


class FreeSuperTipsScraper(BaseSourceScraper):
    source_slug = SOURCE_SLUG
    base_url = FST_TIPS_URL

    async def scrape(self) -> list[RawPick]:
        page = await self.new_stealth_page()
        picks: list[RawPick] = []
        try:
            logger.info("fst_scrape_starting", url=self.base_url, headless=settings.scrape_headless)
            # goto_with_retry handles navigation; _scrape_page does NOT call
            # page.goto() again — this avoids the double-navigation bug.
            await self.goto_with_retry(page, self.base_url)
            picks = await _scrape_page(page)
        finally:
            await page.close()

        logger.info("fst_scrape_complete", source=self.source_slug, picks=len(picks))
        return picks


async def run(publisher: PicksPublisher) -> None:
    """Entry point: launches browser, scrapes FST, publishes to Redis."""
    async with FreeSuperTipsScraper() as scraper:
        picks = await scraper.scrape()
        for pick in picks:
            await publisher.publish(pick)
        logger.info("fst_scrape_complete", published=len(picks))


if __name__ == "__main__":
    import asyncio
    from utils.logger import configure_logging

    async def _main() -> None:
        configure_logging()
        pub = PicksPublisher()
        await pub.connect()
        try:
            await run(pub)
        finally:
            await pub.close()

    asyncio.run(_main())
