import asyncio
import pytest

from sources.olbg import OLBGScraper
from sources.forebet import ForebetScraper
from sources.freesupertips import FreeSuperTipsScraper
from sources.soccervista import SoccerVistaScraper

# Scrapers that should ALWAYS return picks unless the site is down
SCRAPERS_EXPECTING_PICKS = [
    OLBGScraper,
    ForebetScraper,
    SoccerVistaScraper,
]

# Scrapers that MAY return 0 picks depending on the day/time
SCRAPERS_ALLOWING_ZERO = [
    FreeSuperTipsScraper,
]


@pytest.mark.asyncio
@pytest.mark.parametrize("scraper_cls", SCRAPERS_EXPECTING_PICKS)
async def test_scraper_returns_picks(scraper_cls):
    """
    Smoke test: scraper should run end-to-end and return >= 1 pick.
    """
    async with scraper_cls() as scraper:
        picks = await scraper.scrape()

    assert isinstance(picks, list), "Scraper did not return a list"
    assert len(picks) > 0, f"{scraper_cls.__name__} returned 0 picks"
    assert all(hasattr(p, "home_team_name") for p in picks), "Invalid RawPick objects"


@pytest.mark.asyncio
@pytest.mark.parametrize("scraper_cls", SCRAPERS_ALLOWING_ZERO)
async def test_scraper_runs_without_error(scraper_cls):
    """
    Smoke test: scraper should run end-to-end without crashing.
    It may return 0 picks depending on the day.
    """
    async with scraper_cls() as scraper:
        picks = await scraper.scrape()

    assert isinstance(picks, list), "Scraper did not return a list"
    # No assertion on length — FST sometimes has no tips