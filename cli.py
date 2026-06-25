import asyncio
import argparse

from sources.olbg import OLBGScraper
from sources.forebet import ForebetScraper
from sources.freesupertips import FreeSuperTipsScraper
from sources.soccervista import SoccerVistaScraper

SCRAPERS = {
    "olbg": OLBGScraper,
    "forebet": ForebetScraper,
    "freesupertips": FreeSuperTipsScraper,
    "soccervista": SoccerVistaScraper,
}


async def main():
    parser = argparse.ArgumentParser(description="Unified scraper CLI")
    parser.add_argument("source", choices=SCRAPERS.keys(), help="Which scraper to run")
    args = parser.parse_args()

    scraper_cls = SCRAPERS[args.source]

    async with scraper_cls() as scraper:
        picks = await scraper.run()
        print(f"{args.source}: {len(picks)} picks published")


if __name__ == "__main__":
    asyncio.run(main())