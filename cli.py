"""
CLI entrypoint for Tippster Scraper.

Usage:
    python cli.py olbg
    python cli.py forebet
"""

import asyncio
import sys

from sources.olbg import OLBGScraper
from sources.forebet import ForebetScraper
from sources.freesupertips import FreeSuperTipsScraper
from sources.soccervista import SoccerVistaScraper

from utils.logger import configure_logging, get_logger

logger = get_logger(__name__)

SCRAPERS = {
    "olbg": OLBGScraper,
    "forebet": ForebetScraper,
    "freesupertips": FreeSuperTipsScraper,
    "soccervista": SoccerVistaScraper,
}


async def run(source_slug: str) -> None:
    scraper_cls = SCRAPERS.get(source_slug)
    if scraper_cls is None:
        logger.error("unknown_source", source=source_slug, available=list(SCRAPERS.keys()))
        print(f"Unknown source: {source_slug}")
        print(f"Available sources: {list(SCRAPERS.keys())}")
        sys.exit(1)

    async with scraper_cls() as scraper:
        picks = await scraper.run()
        logger.info("run_complete", source=source_slug, picks_published=len(picks))


if __name__ == "__main__":
    configure_logging()
    
    if len(sys.argv) != 2:
        print("Usage: python cli.py <source_slug>")
        print("Available:", list(SCRAPERS.keys()))
        sys.exit(1)

    asyncio.run(run(sys.argv[1]))