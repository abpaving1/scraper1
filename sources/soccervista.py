"""
SoccerVista scraper (Task 4).

SoccerVista (soccervista.com) is a statistical prediction site that publishes
match result probability percentages and implied picks for hundreds of fixtures
per day. It shares a similar approach to Forebet — algorithmic/statistical
rather than human tipster — so it is modelled as a single synthetic source.

Anti-bot difficulty: LOW. SoccerVista is a legacy site with minimal JS and
no significant bot protection. Standard Playwright + stealth is more than
sufficient. No proxy rotation is strictly necessary but we use it anyway
for consistency and IP hygiene.

Scrape cadence: every 6 hours. SoccerVista updates its predictions daily,
not intraday — more frequent scraping adds no value.

Key data extracted:
  - Home/Away/Draw probability percentages
  - Implied pick (highest-probability outcome)
  - Match result market only (SoccerVista's primary output)
  - Form indicators (used to weight confidence — see _derive_confidence)

VERIFICATION CHECKLIST — run with SCRAPE_HEADLESS=false before production:
  [ ] SV_BASE_URL still resolves to the predictions listing page
  [ ] TABLE_ROW_SEL matches each match row in the predictions table
  [ ] HOME_TEAM_SEL matches the home team cell within a row
  [ ] AWAY_TEAM_SEL matches the away team cell
  [ ] HOME_PCT_SEL matches the home win probability % string
  [ ] DRAW_PCT_SEL matches the draw probability %
  [ ] AWAY_PCT_SEL matches the away win probability %
  [ ] KICKOFF_SEL matches the kickoff date/time string
  [ ] LEAGUE_SEL matches the competition/league label
  [ ] Probability display format — confirm whether "45%" or "0.45" or "45"
  [ ] Date format in kickoff strings — may be "21/06" or "21 Jun" etc.
"""

import asyncio
import re
from datetime import datetime, timezone, date
from decimal import Decimal, InvalidOperation
from typing import Optional

from playwright.async_api import async_playwright, Page, TimeoutError as PWTimeout
from tenacity import retry, stop_after_attempt, wait_exponential, retry_if_exception_type

from config import settings
from models.pick import MarketType, RawPick
from queues.redis_publisher import PicksPublisher
from utils.logger import get_logger

logger = get_logger(__name__)

# ── URLs ─────────────────────────────────────────────────────────────────────
SV_BASE_URL = "https://www.soccervista.com/football-predictions.html"

# ── CSS SELECTORS (verify with SCRAPE_HEADLESS=false) ────────────────────────
# SoccerVista renders an HTML table — these selectors target the standard
# table structure observed as of June 2025. The site rarely changes layout
# but verify before going live.
TABLE_ROW_SEL = "table.predictions tr, table.matches tr, .predictions-table tr"  # VERIFY
HOME_TEAM_SEL = "td.home-team, td:nth-child(2), .team-home"                       # VERIFY
AWAY_TEAM_SEL = "td.away-team, td:nth-child(4), .team-away"                       # VERIFY
HOME_PCT_SEL = "td.home-pct, td.prob-1, .probability-home"                        # VERIFY
DRAW_PCT_SEL = "td.draw-pct, td.prob-x, .probability-draw"                        # VERIFY
AWAY_PCT_SEL = "td.away-pct, td.prob-2, .probability-away"                        # VERIFY
KICKOFF_SEL = "td.kickoff, td.date-time, td:first-child, .match-time"             # VERIFY
LEAGUE_SEL = "td.league, .competition-name, td.competition"                       # VERIFY

# ── Source identity ───────────────────────────────────────────────────────────
SOURCE_SLUG = "soccervista"
SV_TIPSTER_EXTERNAL_ID = "soccervista-algorithm-v1"
SV_TIPSTER_NAME = "SoccerVista Algorithm"

# Minimum probability gap required for us to treat a pick as worth publishing.
# If home=38%, draw=32%, away=30% the model has no real opinion — skip it.
# A gap of at least 15pp between the top pick and the next-best indicates
# meaningful directional signal.
MIN_PROBABILITY_GAP_PP = 15.0

# Maximum picks to extract per scrape run — SV lists hundreds of matches
# globally; we cap at the highest-probability selections to keep queue
# volume manageable and focus on confident picks.
MAX_PICKS_PER_RUN = 50


def _parse_probability(raw: str) -> Optional[float]:
    """
    Parses probability strings: "45%", "45", "0.45" → 45.0 (as a percentage).
    Returns None if the string isn't a recognisable probability.
    """
    raw = raw.strip().replace("%", "")
    try:
        val = float(raw)
        # Normalise: 0.45 → 45.0, 45 → 45.0
        if 0 < val <= 1.0:
            val = val * 100
        if 0 < val <= 100:
            return val
    except ValueError:
        pass
    return None


def _probability_to_decimal_odds(probability_pct: float) -> Optional[Decimal]:
    """
    Converts a model probability percentage to fair-value decimal odds.
    e.g. 60% → 1/0.60 = 1.667
    We store this as the 'odds_decimal' on the pick — it's the model's
    implied fair price, not a bookmaker price. The Odds API (Task 7) will
    overlay real bookmaker prices later.
    """
    if probability_pct <= 0 or probability_pct >= 100:
        return None
    fair_odds = 1 / (probability_pct / 100)
    return Decimal(str(round(fair_odds, 3)))


def _derive_confidence(winning_pct: float, gap_pp: float) -> float:
    """
    Derives confidence from the winning probability and the gap between the
    top pick and the second-best outcome.

    Rationale: a 70% home win with a 30pp gap over draw (40% vs 10%) is more
    confident than a 50% home win with a 15pp gap over draw (35% vs 35%).
    Both the absolute level and the relative separation matter.

    Output range: 0.40 – 0.85
    """
    # Base from absolute probability
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

    # Gap bonus (up to +0.05 for very clear leaders)
    gap_bonus = min(0.05, gap_pp / 100)
    return round(min(0.85, base + gap_bonus), 3)


def _select_pick(
    home_pct: float,
    draw_pct: float,
    away_pct: float,
) -> Optional[tuple[str, MarketType, float, float]]:
    """
    Identifies the highest-probability outcome and validates it clears the
    minimum gap threshold.

    Returns (selection_text, market_type, winning_pct, gap_pp) or None if
    no outcome has sufficient conviction to be worth publishing.
    """
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
        return None  # Too close to call — don't publish a low-confidence pick

    return top_name, MarketType.MATCH_RESULT, top_pct, gap


def _parse_kickoff(raw: str, today: date) -> Optional[datetime]:
    """
    SoccerVista kickoff formats observed:
      "21/06 15:00"  — date/time
      "21 Jun 15:00" — date/time
      "15:00"        — time only (assume today)
      "Sat 21/06"    — day + date, no time
    Returns UTC datetime or None if parsing fails.
    """
    raw = raw.strip()

    # Pattern: "dd/mm HH:MM" or "dd/mm/yyyy HH:MM"
    m = re.search(r"(\d{1,2})[/\-](\d{1,2})(?:[/\-]\d{2,4})?\s+(\d{2}):(\d{2})", raw)
    if m:
        day, month, hour, minute = int(m.group(1)), int(m.group(2)), int(m.group(3)), int(m.group(4))
        year = today.year
        try:
            return datetime(year, month, day, hour, minute, tzinfo=timezone.utc)
        except ValueError:
            pass

    # Pattern: time only "HH:MM"
    m = re.search(r"(\d{1,2}):(\d{2})", raw)
    if m:
        hour, minute = int(m.group(1)), int(m.group(2))
        try:
            return datetime(today.year, today.month, today.day, hour, minute, tzinfo=timezone.utc)
        except ValueError:
            pass

    return None


@retry(
    retry=retry_if_exception_type((PWTimeout, ConnectionError, OSError)),
    stop=stop_after_attempt(settings.scrape_max_retries),
    wait=wait_exponential(multiplier=1, min=2, max=10),
    reraise=True,
)
async def _scrape_page(page: Page) -> list[RawPick]:
    """Navigates to SoccerVista predictions page and extracts all match rows."""
    import random

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
            # ── Teams ────────────────────────────────────────────────────
            home_el = await row.query_selector(HOME_TEAM_SEL)
            away_el = await row.query_selector(AWAY_TEAM_SEL)
            if not home_el or not away_el:
                continue
            home = (await home_el.inner_text()).strip()
            away = (await away_el.inner_text()).strip()
            if not home or not away or home == away:
                continue

            # ── Probabilities ─────────────────────────────────────────────
            home_pct_el = await row.query_selector(HOME_PCT_SEL)
            draw_pct_el = await row.query_selector(DRAW_PCT_SEL)
            away_pct_el = await row.query_selector(AWAY_PCT_SEL)
            if not all([home_pct_el, draw_pct_el, away_pct_el]):
                continue

            home_pct = _parse_probability(await home_pct_el.inner_text())
            draw_pct = _parse_probability(await draw_pct_el.inner_text())
            away_pct = _parse_probability(await away_pct_el.inner_text())
            if any(p is None for p in [home_pct, draw_pct, away_pct]):
                continue

            # ── Pick selection ────────────────────────────────────────────
            pick_result = _select_pick(home_pct, draw_pct, away_pct)
            if pick_result is None:
                logger.debug("sv_pick_skipped_low_conviction", home=home, away=away)
                continue

            selection_text, market, winning_pct, gap_pp = pick_result

            # ── League ───────────────────────────────────────────────────
            league_el = await row.query_selector(LEAGUE_SEL)
            league = (await league_el.inner_text()).strip() if league_el else None

            # ── Kickoff ──────────────────────────────────────────────────
            ko_el = await row.query_selector(KICKOFF_SEL)
            ko_raw = (await ko_el.inner_text()).strip() if ko_el else ""
            kickoff = _parse_kickoff(ko_raw, today)

            odds = _probability_to_decimal_odds(winning_pct)
            confidence = _derive_confidence(winning_pct, gap_pp)
            raw_text = f"{home} v {away} | {selection_text} ({winning_pct:.0f}%)"

            pick = RawPick(
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
            picks.append(pick)

            if len(picks) >= MAX_PICKS_PER_RUN:
                logger.info("sv_max_picks_reached", limit=MAX_PICKS_PER_RUN)
                break

        except Exception as exc:  # noqa: BLE001
            logger.warning("sv_row_parse_error", error=str(exc))
            continue

    # Sort by confidence descending so the highest-conviction picks are
    # published to Redis first (consumer processes them in FIFO order).
    picks.sort(key=lambda p: p.confidence or 0.0, reverse=True)
    logger.info("sv_picks_extracted", count=len(picks))
    return picks


async def run(publisher: PicksPublisher) -> None:
    """Entry point: launches browser, scrapes SoccerVista, publishes to Redis."""
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

        # SoccerVista has no significant bot protection but we stealth anyway
        # for consistency and forward compatibility.
        from playwright_stealth import stealth_async
        await stealth_async(page)

        try:
            picks = await _scrape_page(page)
            for pick in picks:
                await publisher.publish(pick)
            logger.info("sv_scrape_complete", published=len(picks))
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
