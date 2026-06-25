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

VERIFICATION CHECKLIST — run with SCRAPE_HEADLESS=false before production:
  [ ] FST_TIPS_URL still resolves to the today's tips listing
  [ ] CSS selector TIP_ROW_SEL matches each individual tip card/row
  [ ] MATCH_SEL matches the "Team A v Team B" text within a row
  [ ] LEAGUE_SEL matches the competition label (e.g. "Premier League")
  [ ] SELECTION_SEL matches the tip text (e.g. "Home Win", "Over 2.5 Goals")
  [ ] ODDS_SEL matches the displayed decimal odds string
  [ ] KICKOFF_SEL matches the kickoff time string (format varies — see parser)
  [ ] TIPSTER_SEL matches the expert author name (if shown per-tip)
  [ ] Confidence extraction: FST doesn't publish explicit confidence scores —
      we derive a fixed confidence of 0.65 for all FST picks, representing
      editorial-expert baseline authority (see _derive_confidence docstring)
"""

import asyncio
import re
from datetime import datetime, timezone, date, timedelta
from decimal import Decimal, InvalidOperation
from typing import Optional

import structlog
from playwright.async_api import async_playwright, Page, TimeoutError as PWTimeout
from playwright_stealth import stealth_async
from tenacity import retry, stop_after_attempt, wait_exponential, retry_if_exception_type

from config import settings
from models.pick import MarketType, RawPick
from queues.redis_publisher import PicksPublisher
from utils.logger import get_logger

logger = get_logger(__name__)

# ── URL ──────────────────────────────────────────────────────────────────────
FST_TIPS_URL = "https://www.freesupertips.com/football-tips/"

# ── CSS SELECTORS (verify with SCRAPE_HEADLESS=false) ────────────────────────
# These are structured placeholders derived from FST's known DOM pattern as of
# June 2025. Verify each before going live — see checklist in module docstring.
TIP_ROW_SEL = ".tip-card, .tips-list__item, article.tip"           # VERIFY
MATCH_SEL = ".tip-card__fixture, .tip__match, .fixture-name"        # VERIFY
LEAGUE_SEL = ".tip-card__competition, .tip__league, .competition"   # VERIFY
SELECTION_SEL = ".tip-card__selection, .tip__pick, .selection-text" # VERIFY
ODDS_SEL = ".tip-card__odds, .tip__odds, .odds-value"               # VERIFY
KICKOFF_SEL = ".tip-card__time, .tip__kickoff, time"                # VERIFY
TIPSTER_SEL = ".tip-card__expert, .tip__author, .expert-name"       # VERIFY

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
    Returns ('', '') if the format isn't recognised — these picks are
    dropped upstream rather than stored with empty team names.
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
    FST typically shows times in UK local time (GMT/BST) without an
    explicit date — we assume today unless the string includes a date fragment.

    Formats observed:
      "15:00"      — time only, assume today (UK local)
      "3:00pm"     — 12h format, assume today
      "Sat 21 Jun" — date only (no time) — returns None (no time component)
      "Sat 15:00"  — day + time — map to nearest upcoming day
    """
    raw = raw.strip().lower()

    # Time-only: "15:00" or "3:00pm"
    time_only = re.search(r"(\d{1,2}):(\d{2})\s*(am|pm)?", raw)
    if time_only:
        hour = int(time_only.group(1))
        minute = int(time_only.group(2))
        meridiem = time_only.group(3)
        if meridiem == "pm" and hour < 12:
            hour += 12
        elif meridiem == "am" and hour == 12:
            hour = 0
        # Assume UK local = UTC+1 (BST) during football season; UTC otherwise.
        # Conservative: store as UTC-naive, flag for timezone-aware backfill.
        # TODO: use zoneinfo to localise properly once deployed.
        try:
            return datetime(today.year, today.month, today.day, hour, minute, tzinfo=timezone.utc)
        except ValueError:
            return None

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


@retry(
    retry=retry_if_exception_type((PWTimeout, ConnectionError, OSError)),
    stop=stop_after_attempt(settings.scrape_max_retries),
    wait=wait_exponential(multiplier=1, min=2, max=10),
    reraise=True,
)
async def _scrape_page(page: Page) -> list[RawPick]:
    """Navigates to the FST tips page and extracts all tip rows."""
    import random

    await page.goto(FST_TIPS_URL, timeout=settings.scrape_timeout_ms, wait_until="domcontentloaded")

    # Human-like pause before interacting
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
            # ── Match teams ──────────────────────────────────────────────
            match_el = await row.query_selector(MATCH_SEL)
            if not match_el:
                continue
            match_text = (await match_el.inner_text()).strip()
            home, away = _parse_match(match_text)
            if not home or not away:
                continue

            # ── League ───────────────────────────────────────────────────
            league_el = await row.query_selector(LEAGUE_SEL)
            league = (await league_el.inner_text()).strip() if league_el else None

            # ── Selection ────────────────────────────────────────────────
            sel_el = await row.query_selector(SELECTION_SEL)
            if not sel_el:
                continue
            selection_text = (await sel_el.inner_text()).strip()
            if not selection_text:
                continue

            # ── Odds ─────────────────────────────────────────────────────
            odds_el = await row.query_selector(ODDS_SEL)
            odds_raw = (await odds_el.inner_text()).strip() if odds_el else ""
            odds = _parse_odds(odds_raw)

            # ── Kickoff ──────────────────────────────────────────────────
            ko_el = await row.query_selector(KICKOFF_SEL)
            ko_raw = (await ko_el.inner_text()).strip() if ko_el else ""
            kickoff = _parse_kickoff(ko_raw, today)

            # ── Tipster name (optional per-tip byline) ───────────────────
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
                league_name=league,
                kickoff_utc=kickoff,
                market=market,
                selection=selection_text,
                odds_decimal=odds,
                confidence=confidence,
                raw_text=match_text + " | " + selection_text,
                posted_at=scraped_at,
                scraped_at=scraped_at,
            )
            picks.append(pick)

        except Exception as exc:  # noqa: BLE001
            logger.warning("fst_row_parse_error", error=str(exc))
            continue

    logger.info("fst_picks_extracted", count=len(picks))
    return picks


async def run(publisher: PicksPublisher) -> None:
    """Entry point: launches browser, scrapes FST, publishes to Redis."""
    async with async_playwright() as pw:
        proxy = {
            "server": f"http://{settings.proxy_host}:{settings.proxy_port}",
            "username": settings.proxy_username,
            "password": settings.proxy_password,
        }
        browser = await pw.chromium.launch(headless=settings.scrape_headless, proxy=proxy)
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
            logger.info("fst_scrape_complete", published=len(picks))
        finally:
            await browser.close()


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
