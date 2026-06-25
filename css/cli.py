import asyncio

from sources.olbg import OLBGScraper
from sources.forebet import ForebetScraper
from sources.freesupertips import FreeSuperTipsScraper
from sources.soccervista import SoccerVistaScraper

from ccs.engine import build_consensus, rank_consensus_picks


async def main():
    scrapers = [
        OLBGScraper,
        ForebetScraper,
        FreeSuperTipsScraper,
        SoccerVistaScraper,
    ]

    all_picks = []

    for cls in scrapers:
        async with cls() as scraper:
            all_picks.extend(await scraper.scrape())

    consensus = build_consensus(all_picks)
    ranked = rank_consensus_picks(consensus)

    for cp in ranked[:50]:
        print(
            f"{cp.fixture_key} | {cp.market.value} | {cp.selection} | "
            f"CCS={cp.consensus_score:.3f}"
        )


if __name__ == "__main__":
    asyncio.run(main())
