"""
Forebet scraper (Task 3).

Forebet publishes algorithmically-derived probability distributions and
predicted scores for football matches. Unlike OLBG (community tipsters),
there is no per-tipster identity here — the "tipster" is Forebet's model
itself, represented as a single synthetic tipster row in the DB.

Anti-bot profile:
  - Forebet serves JS-rendered content and returns 403 to plain HTTP requests.
    Playwright + stealth is required.
  - Forebet has periodically restructured its markup; selectors below are
    the best-known values but MUST be re-verified against the live DOM
    (run with SCRAPE_HEADLESS=false) before production.

Verification steps for each selector:
  1. Run: SCRAPE_HEADLESS=false python -m sources.forebet
  2. Open DevTools on https://www.forebet.com/en/football-predictions
  3. Inspect the predictions table and confirm:
     - SEL_PREDICTION_ROW  matches each repeating match row
     - SEL_HOME_TEAM       matches the home team text
     - SEL_AWAY_TEAM       matches the away team text
     - SEL_KICKOFF_TIME    matches the date/time cell
     - SEL_LEAGUE_NAME     matches the league label
     - SEL_PROB_HOME/DRAW/AWAY  match the three probability cells (e.g. "62%")
     - SEL_PREDICTED_SCORE matches the predicted score cell (e.g. "2:1")
     - SEL_AVG_GOALS_HOME/AWAY  match the two avg-goals cells
"""

from __future__ import annotations

import asyncio
import re
from datetime import datetime, timezone
from decimal import Decimal, ROUND_HALF_UP
from typing import AsyncIterator

from playwright.async_api import Browser, BrowserContext, Page, Playwright, async_playwright
from playwright_stealth import stealth_async

from config import settings
from models.forebet import ForebetMatchResult, ForebetPrediction
from models.pick import MarketType, RawPick
from utils.jitter import jitter_delay, human_scroll
from utils.logger import get_logger
from utils.proxy import get_proxy_settings

logger = get_logger(__name__)

# ─── Confidence filter ────────────────────────────────────────────────────────
# Only emit a pick if the winning outcome's probability exceeds this threshold.
# 55 % means we reject anything close to a coin-flip. Tune in .env via
# FOREBET_MIN_CONFIDENCE_PCT if needed (add to Settings).
MIN_CONFIDENCE_PCT: float = 55.0

# Over/Under thresholds — derived from avg-goals figures on the row.
OVER_25_THRESHOLD: float = 2.5   # avg_goals_home + away >= this → Over 2.5
UNDER_25_THRESHOLD: float = 1.8  # sum <= this → Under 2.5
BTTS_THRESHOLD: float = 1.1      # each side must individually >= this → BTTS Yes

# Synthetic tipster identity — Forebet is an algorithm, not a person.
FOREBET_TIPSTER_NAME: str = "Forebet Algorithm"
FOREBET_TIPSTER_EXTERNAL_ID: str = "forebet-algorithm-v1"

# ─── CSS selectors ────────────────────────────────────────────────────────────
# These selectors target Forebet's table-based predictions layout.
# Forebet returns 403 to plain HTTP requests, so selectors cannot be verified
# without a live Playwright session. The values below are the best-known
# patterns based on Forebet's historical markup (mid-2025). Re-verify before
# production by running: SCRAPE_HEADLESS=false python -m sources.forebet
#
# If a selector is wrong the scraper logs a forebet_prediction_rows_not_found
# error or yields 0 rows — run headless=false to inspect and fix.

# Row containing one match prediction (repeats once per match in the table).
SEL_PREDICTION_ROW = "table.schema tr.rcnt"   # UNVERIFIED — verify with DevTools

# Within each row:
SEL_HOME_TEAM     = "td.homeTeam span"         # home team name text node
SEL_AWAY_TEAM     = "td.awayTeam span"         # away team name text node
SEL_KICKOFF_TIME  = "td.date_bah"              # raw "DD/MM HH:MM" or ISO text
SEL_LEAGUE_NAME   = "td.shortTag span"         # league label (e.g. "Premier League")

# 1X2 probability cells — three consecutive <td> elements with % values.
SEL_PROB_HOME     = "td.predict span.forepr"   # e.g. "62%"
SEL_PROB_DRAW     = "td.predict:nth-child(2) span.forepr"
SEL_PROB_AWAY     = "td.predict:nth-child(3) span.forepr"

# Predicted score — typically "2:1" or "1:0" in a single cell.
SEL_PREDICTED_SCORE = "td.lscr_td span.lscrsp"

# Average goals per side — two cells, home then away.
SEL_AVG_GOALS_HOME = "td.avg_sc:nth-child(1)"
SEL_AVG_GOALS_AWAY = "td.avg_sc:nth-child(2)"

# ─── Helpers ──────────────────────────────────────────────────────────────────

def _parse_probability(raw: str) -> float:
    """'62%' → 62.0. Returns 0.0 on parse failure (logged by caller)."""
    cleaned = raw.strip().rstrip("%")
    try:
        return float(cleaned)
    except ValueError:
        return 0.0


def _implied_odds(prob_pct: float) -> Decimal:
    """Convert a probability percentage to decimal odds, capped at 100.0
    (i.e. minimum 1 % probability) to avoid division by zero."""
    if prob_pct <= 0:
        prob_pct = 1.0
    raw = Decimal(str(100.0 / prob_pct))
    return raw.quantize(Decimal("0.001"), rounding=ROUND_HALF_UP)


def _parse_predicted_score(raw: str) -> tuple[int | None, int | None]:
    """'2:1' → (2, 1). Returns (None, None) on any parse failure."""
    match = re.match(r"(\d+)\s*[:\-]\s*(\d+)", raw.strip())
    if match:
        return int(match.group(1)), int(match.group(2))
    return None, None


def _parse_avg_goals(raw: str) -> float | None:
    """'1.45' → 1.45. Returns None on failure."""
    try:
        return float(raw.strip())
    except ValueError:
        return None


def _parse_kickoff(raw: str) -> datetime | None:
    """
    Attempt to parse Forebet's kickoff display into a UTC-aware datetime.
    Forebet typically shows times in CET/CEST without an explicit TZ label.
    We store as UTC; offset is applied as a best-effort (CET = UTC+1).

    Formats tried, in order:
      "15/06 20:45"   (current year assumed)
      "2025-06-15 20:45"
      "20:45"          (today assumed — risky, only used as last resort)
    """
    raw = raw.strip()
    now = datetime.now(timezone.utc)

    patterns = [
        ("%d/%m %H:%M", True),       # DD/MM HH:MM — inject current year
        ("%Y-%m-%d %H:%M", False),   # ISO-ish — full date present
        ("%H:%M", True),             # time only — inject today
    ]

    for fmt, inject_year in patterns:
        try:
            if inject_year and "%Y" not in fmt and "%d/%m" in fmt:
                raw_with_year = f"{now.year}/{raw}"
                parsed = datetime.strptime(raw_with_year, f"%Y/{fmt}")
            elif inject_year and "%H:%M" == fmt:
                date_str = now.strftime("%Y-%m-%d") + " " + raw
                parsed = datetime.strptime(date_str, "%Y-%m-%d %H:%M")
            else:
                parsed = datetime.strptime(raw, fmt)

            # Assume CET (UTC+1) — good enough for pre-match scheduling;
            # a full TZ library (pytz / zoneinfo) would be overkill here
            # given we only need the date to be right for dedup purposes.
            utc_offset_hours = 1
            utc_ts = parsed.replace(tzinfo=timezone.utc) - \
                     __import__("datetime").timedelta(hours=utc_offset_hours)
            return utc_ts
        except ValueError:
            continue

    logger.warning("forebet_kickoff_parse_failed", raw=raw)
    return None


# ─── Main scraper class ───────────────────────────────────────────────────────

class ForebetScraper:
    """
    Playwright-based scraper for forebet.com/en/football-predictions.

    Lifecycle:
        async with ForebetScraper() as scraper:
            async for pick in scraper.scrape():
                await publisher.publish(pick)

    The class manages its own Playwright/browser context so teardown is
    guaranteed even if scraping raises partway through.
    """

    SOURCE_SLUG = "forebet"

    def __init__(self) -> None:
        self._playwright: Playwright | None = None
        self._browser: Browser | None = None
        self._context: BrowserContext | None = None

    # ── Context manager ──────────────────────────────────────────────────────

    async def __aenter__(self) -> "ForebetScraper":
        self._playwright = await async_playwright().start()
        proxy_cfg = get_proxy_settings(session_id="forebet-main")

        self._browser = await self._playwright.chromium.launch(
            headless=settings.scrape_headless,
            args=[
                "--no-sandbox",
                "--disable-blink-features=AutomationControlled",
            ],
        )
        self._context = await self._browser.new_context(
            proxy=proxy_cfg,
            locale="en-GB",
            timezone_id="Europe/London",
            viewport={"width": 1366, "height": 768},
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/125.0.0.0 Safari/537.36"
            ),
        )
        return self

    async def __aexit__(self, *_: object) -> None:
        if self._context:
            await self._context.close()
        if self._browser:
            await self._browser.close()
        if self._playwright:
            await self._playwright.stop()

    # ── Public API ───────────────────────────────────────────────────────────

    async def scrape(self) -> list[RawPick]:
        """
        Yields validated RawPick instances — one per market per match.
        Caller is responsible for publishing to Redis.
        """
        assert self._context is not None, "Browser context is not initialized"
        page = await self._context.new_page()
        await stealth_async(page)

        picks: list[RawPick] = []
        try:
            await self._navigate(page)
            async for pick in self._extract_picks(page):
                picks.append(pick)
        finally:
            await page.close()
        return picks

    # ── Navigation ───────────────────────────────────────────────────────────

    async def _navigate(self, page: Page) -> None:
        url = settings.forebet_football_url
        logger.info("forebet_navigating", url=url)

        await page.goto(url, wait_until="domcontentloaded", timeout=settings.scrape_timeout_ms)

        # Wait for at least one prediction row — if this times out, the
        # selector is stale and needs updating (run with SCRAPE_HEADLESS=false).
        try:
            await page.wait_for_selector(SEL_PREDICTION_ROW, timeout=15_000)
        except Exception:
            logger.error(
                "forebet_prediction_rows_not_found",
                selector=SEL_PREDICTION_ROW,
                hint="Run with SCRAPE_HEADLESS=false to inspect live DOM and update selectors.",
            )
            raise

        await jitter_delay()
        logger.info("forebet_page_loaded", url=url)

    # ── Row extraction ────────────────────────────────────────────────────────

    async def _extract_picks(self, page: Page) -> AsyncIterator[RawPick]:
        rows = await page.query_selector_all(SEL_PREDICTION_ROW)
        logger.info("forebet_rows_found", count=len(rows))

        for row in rows:
            await jitter_delay()

            try:
                prediction = await self._parse_row(page, row)
            except Exception as exc:
                logger.warning("forebet_row_parse_error", error=str(exc))
                continue

            if prediction is None:
                continue

            if not self._validate_prediction(prediction):
                continue

            for result in self._derive_picks(prediction):
                yield self._to_raw_pick(prediction, result)

    async def _parse_row(self, page: Page, row) -> ForebetPrediction | None:
        """
        Extracts all fields from a single prediction table row.
        Returns None if essential fields (teams, probabilities) are missing.
        """

        async def text(selector: str) -> str:
            el = await row.query_selector(selector)
            return (await el.inner_text()).strip() if el else ""

        home_team  = await text(SEL_HOME_TEAM)
        away_team  = await text(SEL_AWAY_TEAM)

        if not home_team or not away_team:
            # Likely a league-header row, not a match row — skip silently.
            return None

        raw_kickoff    = await text(SEL_KICKOFF_TIME)
        raw_league     = await text(SEL_LEAGUE_NAME)
        raw_prob_home  = await text(SEL_PROB_HOME)
        raw_prob_draw  = await text(SEL_PROB_DRAW)
        raw_prob_away  = await text(SEL_PROB_AWAY)
        raw_score      = await text(SEL_PREDICTED_SCORE)
        raw_avg_home   = await text(SEL_AVG_GOALS_HOME)
        raw_avg_away   = await text(SEL_AVG_GOALS_AWAY)

        home_prob = _parse_probability(raw_prob_home)
        draw_prob = _parse_probability(raw_prob_draw)
        away_prob = _parse_probability(raw_prob_away)

        score_home, score_away = _parse_predicted_score(raw_score)

        raw_text = (
            f"{home_team} v {away_team} | {raw_league} | {raw_kickoff} | "
            f"{raw_prob_home}/{raw_prob_draw}/{raw_prob_away} | score: {raw_score}"
        )

        return ForebetPrediction(
            source_slug=self.SOURCE_SLUG,
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

    # ── Validation ────────────────────────────────────────────────────────────

    def _validate_prediction(self, p: ForebetPrediction) -> bool:
        """
        Returns False (and logs a warning) if the prediction is malformed.
        We're conservative: a probability sum wildly outside 95–105 % almost
        certainly means the selector grabbed the wrong element.
        """
        total = p.home_prob + p.draw_prob + p.away_prob
        if not (90.0 <= total <= 110.0):
            logger.warning(
                "forebet_probability_sum_invalid",
                home=p.home_team,
                away=p.away_team,
                total=total,
            )
            return False

        if p.home_prob <= 0 or p.draw_prob <= 0 or p.away_prob <= 0:
            logger.warning(
                "forebet_zero_probability",
                home=p.home_team,
                away=p.away_team,
            )
            return False

        return True

    # ── Pick derivation ───────────────────────────────────────────────────────

    def _derive_picks(self, p: ForebetPrediction) -> list[ForebetMatchResult]:
        """
        Converts one ForebetPrediction into 1–3 ForebetMatchResult instances.

        Rules:
          1. Match result — always emitted if winning prob > MIN_CONFIDENCE_PCT.
          2. Over/Under 2.5 — emitted if avg_goals sum qualifies AND the
             implied outcome has prob > MIN_CONFIDENCE_PCT.
          3. BTTS Yes — emitted if both sides' avg_goals >= BTTS_THRESHOLD.
        """
        results: list[ForebetMatchResult] = []

        # 1. Match result
        candidates = [
            ("Home Win",  p.home_prob, p.implied_odds_home),
            ("Draw",      p.draw_prob, p.implied_odds_draw),
            ("Away Win",  p.away_prob, p.implied_odds_away),
        ]
        selection, best_prob, best_odds = max(candidates, key=lambda c: c[1])

        if best_prob >= MIN_CONFIDENCE_PCT:
            results.append(ForebetMatchResult(
                market="match_result",
                selection=selection,
                odds_decimal=best_odds,
                confidence=round(best_prob / 100.0, 4),
                raw_text=p.raw_text,
            ))
        else:
            logger.debug(
                "forebet_match_result_below_threshold",
                home=p.home_team,
                away=p.away_team,
                best_prob=best_prob,
                threshold=MIN_CONFIDENCE_PCT,
            )

        # 2. Over/Under 2.5
        if p.avg_goals_home is not None and p.avg_goals_away is not None:
            avg_total = p.avg_goals_home + p.avg_goals_away

            if avg_total >= OVER_25_THRESHOLD:
                # Implied probability of Over 2.5 from the goals model.
                # Forebet doesn't publish an explicit O/U %, so we derive a
                # rough confidence: linearly scale avg_total vs threshold.
                # e.g. avg 3.0 goals → ~75 % confidence; 2.5 → 55 %.
                over_confidence = min(0.85, 0.55 + (avg_total - OVER_25_THRESHOLD) * 0.15)
                # Implied odds: 1 / confidence
                over_odds = Decimal(str(round(1.0 / over_confidence, 3)))

                if over_confidence * 100 >= MIN_CONFIDENCE_PCT:
                    results.append(ForebetMatchResult(
                        market="over_under",
                        selection="Over 2.5",
                        odds_decimal=over_odds,
                        confidence=round(over_confidence, 4),
                        raw_text=p.raw_text,
                    ))

            elif avg_total <= UNDER_25_THRESHOLD:
                under_confidence = min(0.80, 0.55 + (UNDER_25_THRESHOLD - avg_total) * 0.20)
                under_odds = Decimal(str(round(1.0 / under_confidence, 3)))

                if under_confidence * 100 >= MIN_CONFIDENCE_PCT:
                    results.append(ForebetMatchResult(
                        market="over_under",
                        selection="Under 2.5",
                        odds_decimal=under_odds,
                        confidence=round(under_confidence, 4),
                        raw_text=p.raw_text,
                    ))

        # 3. BTTS Yes
        if (
            p.avg_goals_home is not None
            and p.avg_goals_away is not None
            and p.avg_goals_home >= BTTS_THRESHOLD
            and p.avg_goals_away >= BTTS_THRESHOLD
        ):
            # Confidence proportional to how far both sides exceed the threshold.
            margin = min(p.avg_goals_home, p.avg_goals_away) - BTTS_THRESHOLD
            btts_confidence = min(0.80, 0.55 + margin * 0.25)
            btts_odds = Decimal(str(round(1.0 / btts_confidence, 3)))

            if btts_confidence * 100 >= MIN_CONFIDENCE_PCT:
                results.append(ForebetMatchResult(
                    market="btts",
                    selection="Yes",
                    odds_decimal=btts_odds,
                    confidence=round(btts_confidence, 4),
                    raw_text=p.raw_text,
                ))

        return results

    # ── RawPick conversion ────────────────────────────────────────────────────

    def _to_raw_pick(
        self,
        prediction: ForebetPrediction,
        result: ForebetMatchResult,
    ) -> RawPick:
        """Maps ForebetMatchResult + its parent ForebetPrediction to a RawPick."""
        return RawPick(
            source_slug=self.SOURCE_SLUG,
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
