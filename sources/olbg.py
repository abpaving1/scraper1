"""
OLBG (olbg.com) scraper.

OLBG's football tips listing embeds structured tip objects in the page HTML
(Svelte payload). Extraction uses that embedded JSON as the primary source,
verified against https://www.olbg.com/betting-tips/Football/1 (Jun 2026).

Each listing row is a community-popular selection (not an individual tipster
card). We map these to RawPick using the tip_hash as tipster_external_id and
"OLBG Popular" as tipster_name so downstream dedup remains stable.

Run via: python cli.py olbg  (or python -m sources.olbg)
"""

import asyncio
import re
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Any

from playwright.async_api import Page

from config import settings
from models.pick import MarketType, RawPick
from sources.base_scraper import BaseSourceScraper
from utils.jitter import human_scroll, jitter_delay
from utils.logger import get_logger
from utils.time_parse import parse_olbg_timestamp

logger = get_logger(__name__)

# Verified against live OLBG DOM (betting-tips listing table, Jun 2026)
SEL_TIP_ROW = "div.tips-table div.rw"
SEL_FIXTURE_LINK = "a[href*='event_id=']"
SEL_LEAGUE = ".rw.event .text-xs.text-olbg-grey, .rw.event span.text-xs"
SEL_KICKOFF = ".rw.event time, .rw.event .text-xs"
SEL_MARKET = ".rw.market, .rw.selection"
SEL_SELECTION = ".rw.selection h4, .rw.selection .font-bold"
SEL_ODDS = "[data-decimal]"
SEL_CONFIDENCE = "[style*='--confidence'] span"
SEL_LOAD_MORE = "button:has-text('Load More Tips')"
SEL_COOKIE_ACCEPT_BUTTON = "button#onetrust-accept-btn-handler"

CONSENSUS_TIPSTER_NAME = "OLBG Popular"

# Anchor on tip_hash rather than a fixed field order — the Svelte payload's
# key order isn't a guaranteed contract, and the old position-anchored regex
# (id -> tip_hash -> selection -> ... -> event_start -> ... -> menu_league ->
# ... -> confidence) would silently stop matching entirely if OLBG reordered
# or inserted a single field anywhere in that chain.
TIP_HASH_ANCHOR_RE = re.compile(r'tip_hash:"(?P<tip_hash>[^"]+)"')

# Required fields for a tip to be usable downstream. Pulled out of the
# enclosing object individually (order-independent) once we've located it.
_REQUIRED_STRING_FIELDS = (
    "id",
    "selection",
    "outcome_name",
    "market_name",
    "market_alias",
    "eventname",
    "odds",
    "event_start",
    "menu_league",
)
_FIELD_RE_CACHE = {field: re.compile(rf'{field}:"([^"]*)"') for field in _REQUIRED_STRING_FIELDS}
_CONFIDENCE_RE = re.compile(r"confidence:(\d+)")
_WIN_TIPS_RE = re.compile(r"win_tips:(\d+)")
_WIN_TIPS_COUNT_RE = re.compile(r"win_tips_count:(\d+)")
_COMMENTS_RE = re.compile(r"comments_count:(\d+)")


def _find_enclosing_object(html: str, anchor_start: int) -> str | None:
    """
    Given the start index of a `tip_hash:"..."` match, walk left to the
    nearest unmatched `{` and right to its balanced `}`, returning the full
    object literal text. Balanced-brace scanning is robust to field
    reordering/insertion; a fixed-order regex is not.
    """
    open_idx = html.rfind("{", 0, anchor_start)
    if open_idx == -1:
        return None

    depth = 0
    for i in range(open_idx, len(html)):
        ch = html[i]
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return html[open_idx : i + 1]
    return None  # unterminated — malformed/truncated HTML

MARKET_LABEL_MAP: dict[str, MarketType] = {
    "match result": MarketType.MATCH_RESULT,
    "full time result": MarketType.MATCH_RESULT,
    "1x2": MarketType.MATCH_RESULT,
    "both teams to score": MarketType.BTTS,
    "btts": MarketType.BTTS,
    "over/under": MarketType.OVER_UNDER,
    "total goals": MarketType.OVER_UNDER,
    "asian handicap": MarketType.ASIAN_HANDICAP,
    "asian hcap": MarketType.ASIAN_HANDICAP,
    "correct score": MarketType.CORRECT_SCORE,
    "double chance": MarketType.DOUBLE_CHANCE,
    "first goalscorer": MarketType.FIRST_GOALSCORER,
    "bet builder": MarketType.BET_BUILDER,
    "win tournament": MarketType.MATCH_RESULT,
    "half time / full time": MarketType.HALF_TIME_FULL_TIME,
    "half time/full time": MarketType.HALF_TIME_FULL_TIME,    
}


class OLBGScraper(BaseSourceScraper):
    source_slug = "olbg"

    def __init__(self, listing_path: str = "/betting-tips/Football/1") -> None:
        super().__init__()
        self.base_url = settings.olbg_base_url.rstrip("/")
        self.listing_url = f"{self.base_url}{listing_path}"

    async def scrape(self) -> list[RawPick]:
        """Main scrape entrypoint for OLBG. Uses embedded JSON as primary path."""
        page = await self.new_stealth_page()
        picks: list[RawPick] = []
        published_count = 0

        try:
            logger.info("olbg_scrape_starting", url=self.listing_url, headless=settings.scrape_headless)
            await self.goto_with_retry(page, self.listing_url)
            await self._dismiss_cookie_banner(page)
            await self._scroll_and_load(page)

            html = await page.content()
            embedded = self.parse_embedded_tips(html)
            logger.info("embedded_tips_found", count=len(embedded), source=self.source_slug)

            if len(embedded) == 0:
                logger.warning("olbg_no_embedded_tips", hint="checking DOM fallback")

            seen_hashes: set[str] = set()
            for tip in embedded:
                tip_hash = tip["tip_hash"]
                if tip_hash in seen_hashes:
                    continue
                seen_hashes.add(tip_hash)
                pick = self.tip_dict_to_raw_pick(tip)
                if pick is not None:
                    picks.append(pick)
                    await self.publish_pick(pick)

            if not picks:
                logger.warning("embedded_parse_empty_trying_dom", source=self.source_slug)
                dom_picks = await self._parse_dom_rows(page)
                for pick in dom_picks:
                    picks.append(pick)
                    await self.publish_pick(pick)

                if not picks:
                    # Both extraction paths returned nothing — almost certainly
                    # a selector/payload-shape drift rather than "no tips
                    # today". Flag loudly so it doesn't read as a quiet,
                    # successful run with zero results.
                    logger.error(
                        "scrape_yielded_zero_picks",
                        source=self.source_slug,
                        msg="both embedded JSON and DOM fallback parsing found no picks — "
                        "check for selector or payload-shape drift",
                    )

        finally:
            await page.close()

        logger.info("scrape_complete", source=self.source_slug, picks_found=len(picks))
        return picks

    async def _scroll_and_load(self, page: Page, max_rounds: int = 5) -> None:
        """Scroll and click 'Load More' until no new content appears."""
        previous_count = 0
        for _ in range(max_rounds):
            await human_scroll(page, distance_px=1600, steps=10)
            load_more = await page.query_selector(SEL_LOAD_MORE)
            if load_more and await load_more.is_visible():
                await jitter_delay(0.5, 1.0)
                await load_more.click()
                await page.wait_for_timeout(1500)

            html = await page.content()
            current_count = len(self.parse_embedded_tips(html))
            if current_count <= previous_count:
                break
            previous_count = current_count
            await jitter_delay(0.3, 0.8)

    async def _dismiss_cookie_banner(self, page: Page) -> None:
        try:
            button = await page.query_selector(SEL_COOKIE_ACCEPT_BUTTON)
            if button:
                await jitter_delay(0.5, 1.5)
                await button.click()
        except Exception:  # noqa: BLE001 — banner not present or already dismissed, non-fatal
            pass

    async def _parse_dom_rows(self, page: Page) -> list[RawPick]:
        """Fallback DOM parser when embedded JSON is unavailable."""
        picks: list[RawPick] = []
        rows = await page.query_selector_all(SEL_TIP_ROW)
        for row in rows:
            try:
                pick = await self._parse_dom_row(row)
                if pick is not None:
                    picks.append(pick)
            except Exception:
                logger.exception("dom_row_parse_failed", source=self.source_slug)
        return picks

    async def _parse_dom_row(self, row) -> RawPick | None:
        fixture_link = await row.query_selector(SEL_FIXTURE_LINK)
        selection_el = await row.query_selector(SEL_SELECTION)
        odds_el = await row.query_selector(SEL_ODDS)
        if fixture_link is None or selection_el is None:
            return None

        fixture_href = await fixture_link.get_attribute("href") or ""
        fixture_text = (await fixture_link.inner_text()).strip()
        home_team, away_team = self._split_fixture(fixture_text)
        if home_team is None:
            return None

        league_el = await row.query_selector(SEL_LEAGUE)
        league_name = (await league_el.inner_text()).strip() if league_el else None

        market_el = await row.query_selector(SEL_MARKET)
        market_text = (await market_el.inner_text()).strip() if market_el else ""
        selection_text = (await selection_el.inner_text()).strip()
        market = self._map_market(market_text or selection_text)
        if market is None:
            return None

        odds_decimal = await self._parse_odds(odds_el)
        confidence_el = await row.query_selector(SEL_CONFIDENCE)
        confidence = await self._parse_confidence(confidence_el)

        kickoff_el = await row.query_selector(SEL_KICKOFF)
        kickoff_utc = None
        posted_at = datetime.now(timezone.utc)
        if kickoff_el is not None:
            kickoff_text = (await kickoff_el.inner_text()).strip()
            kickoff_attr = await kickoff_el.get_attribute("datetime")
            kickoff_utc = parse_olbg_timestamp(kickoff_attr or kickoff_text) or None
            posted_at = kickoff_utc or posted_at

        raw_text = await row.inner_text()
        tipster_external_id = fixture_href.split("event_id=")[-1].split("&")[0] or fixture_href

        return RawPick(
            source_slug=self.source_slug,
            tipster_external_id=tipster_external_id,
            tipster_name=CONSENSUS_TIPSTER_NAME,
            home_team_name=home_team,
            away_team_name=away_team,
            league_name=league_name,
            kickoff_utc=kickoff_utc,
            market=market,
            selection=selection_text,
            odds_decimal=odds_decimal,
            confidence=confidence,
            raw_text=raw_text.strip(),
            posted_at=posted_at,
        )

    @classmethod
    def parse_embedded_tips(cls, html: str) -> list[dict[str, Any]]:
        """
        Finds each tip object by anchoring on `tip_hash`, then extracts the
        rest of its fields by name from within that object's balanced braces
        — not by position. A tip missing a required field is skipped (and
        logged) individually rather than silently collapsing the whole-page
        match, so one malformed/new tip type doesn't zero out the run.
        """
        tips: list[dict[str, Any]] = []
        seen_spans: set[tuple[int, int]] = set()

        for anchor in TIP_HASH_ANCHOR_RE.finditer(html):
            block = _find_enclosing_object(html, anchor.start())
            if block is None:
                continue

            span = (anchor.start(), anchor.start() + len(block))
            if span in seen_spans:
                continue  # multiple tip_hash-like matches inside one object literal
            seen_spans.add(span)

            confidence_match = _CONFIDENCE_RE.search(block)
            if confidence_match is None:
                logger.warning("tip_object_missing_confidence", tip_hash=anchor.group("tip_hash"))
                continue

            fields: dict[str, str] = {"tip_hash": anchor.group("tip_hash")}
            missing = []
            for field, pattern in _FIELD_RE_CACHE.items():
                field_match = pattern.search(block)
                if field_match is None:
                    missing.append(field)
                else:
                    fields[field] = field_match.group(1)
            if missing:
                logger.warning(
                    "tip_object_missing_fields",
                    tip_hash=anchor.group("tip_hash"),
                    missing_fields=missing,
                )
                continue

            win_tips = _WIN_TIPS_RE.search(block)
            win_tips_count = _WIN_TIPS_COUNT_RE.search(block)
            comments = _COMMENTS_RE.search(block)

            fields["confidence"] = int(confidence_match.group(1))
            fields["win_tips"] = int(win_tips.group(1)) if win_tips else None
            fields["win_tips_count"] = int(win_tips_count.group(1)) if win_tips_count else None
            fields["comments_count"] = int(comments.group(1)) if comments else None
            tips.append(fields)

        return tips

    @classmethod
    def tip_dict_to_raw_pick(cls, tip: dict[str, Any]) -> RawPick | None:
        home_team, away_team = cls._split_fixture(tip["eventname"])
        if home_team is None:
            logger.warning("fixture_unparseable", raw=tip["eventname"], source="olbg")
            return None

        market = cls._map_market(tip["market_name"])
        if market is None:
            logger.warning(
                "unmapped_market_label",
                label=tip["market_name"],
                eventname=tip.get("eventname", "unknown"),
                hint="extend MARKET_LABEL_MAP in sources/olbg.py"
            )
            return None

        kickoff_utc = cls._parse_event_start(tip["event_start"])
        odds_decimal = cls._parse_odds_text(tip["odds"])
        confidence = Decimal(str(tip["confidence"]))

        win_tips = tip.get("win_tips")
        win_tips_count = tip.get("win_tips_count")
        win_summary = (
            f"{win_tips}/{win_tips_count} Win Tips" if win_tips is not None and win_tips_count else ""
        )
        raw_text = " | ".join(
            part
            for part in [
                tip["eventname"],
                tip["market_name"],
                tip["selection"],
                win_summary,
                f"{tip['confidence']}%",
            ]
            if part
        )

        return RawPick(
            source_slug="olbg",
            tipster_external_id=tip["tip_hash"],
            tipster_name=CONSENSUS_TIPSTER_NAME,
            home_team_name=home_team,
            away_team_name=away_team,
            league_name=tip["menu_league"],
            kickoff_utc=kickoff_utc,
            market=market,
            selection=tip["selection"],
            odds_decimal=odds_decimal,
            confidence=confidence,
            raw_text=raw_text,
            posted_at=kickoff_utc or datetime.now(timezone.utc),
        )

    @staticmethod
    def _split_fixture(fixture_text: str) -> tuple[str | None, str | None]:
        match = re.split(r"\s+v(?:s)?\.?\s+", fixture_text, flags=re.IGNORECASE)
        if len(match) != 2:
            return None, None
        return match[0].strip(), match[1].strip()

    @staticmethod
    def _map_market(market_text: str) -> MarketType | None:
        normalised = market_text.lower().strip()
        for label, market in MARKET_LABEL_MAP.items():
            if label in normalised:
                return market
        return None

    @staticmethod
    def _parse_event_start(event_start: str) -> datetime | None:
        try:
            return datetime.strptime(event_start, "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
        except ValueError:
            parsed = parse_olbg_timestamp(event_start)
            return parsed

    @staticmethod
    def _parse_odds_text(text: str) -> Decimal | None:
        try:
            if "/" in text:
                num, denom = text.split("/")
                return (Decimal(num) / Decimal(denom)) + Decimal("1")
            return Decimal(text)
        except (InvalidOperation, ZeroDivisionError, ValueError):
            return None

    @staticmethod
    async def _parse_odds(odds_el) -> Decimal | None:
        if odds_el is None:
            return None
        decimal_attr = await odds_el.get_attribute("data-decimal")
        if decimal_attr:
            return OLBGScraper._parse_odds_text(decimal_attr)
        text = (await odds_el.inner_text()).strip()
        return OLBGScraper._parse_odds_text(text)

    @staticmethod
    async def _parse_confidence(confidence_el) -> Decimal | None:
        if confidence_el is None:
            return None
        text = (await confidence_el.inner_text()).strip().replace("%", "")
        try:
            return Decimal(text)
        except InvalidOperation:
            return None

async def main() -> None:
    from utils.logger import configure_logging

    configure_logging()
    async with OLBGScraper() as scraper:
        await scraper.scrape()


if __name__ == "__main__":
    asyncio.run(main())
