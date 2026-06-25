"""
Forebet scraper entrypoint.

Run once:
    python -m sources.forebet_runner

Run headlessly (for selector debugging):
    SCRAPE_HEADLESS=false python -m sources.forebet_runner

The runner:
  1. Connects to Redis via PicksPublisher.
  2. Instantiates ForebetScraper and iterates picks.
  3. Publishes each pick to the queue as it's scraped (not batched), so a
     mid-run failure loses only the remaining unscraped matches — already-
     published picks are safely on the queue.
  4. Logs a summary (total rows, picks published, picks skipped) at exit.
"""

from __future__ import annotations

import asyncio

from queues.redis_publisher import PicksPublisher
from sources.forebet import ForebetScraper
from utils.logger import configure_logging, get_logger

logger = get_logger(__name__)


async def run() -> None:
    configure_logging()
    publisher = PicksPublisher()
    await publisher.connect()

    total_published = 0
    total_errors = 0

    logger.info("forebet_scrape_starting")

    try:
        async with ForebetScraper() as scraper:
            async for pick in scraper.scrape():
                try:
                    await publisher.publish(pick)
                    total_published += 1
                    logger.debug(
                        "forebet_pick_queued",
                        fixture=f"{pick.home_team_name} v {pick.away_team_name}",
                        market=pick.market.value,
                        selection=pick.selection,
                        confidence=pick.confidence,
                    )
                except Exception as exc:
                    total_errors += 1
                    logger.error(
                        "forebet_pick_publish_failed",
                        fixture=f"{pick.home_team_name} v {pick.away_team_name}",
                        error=str(exc),
                    )
    finally:
        await publisher.close()
        logger.info(
            "forebet_scrape_complete",
            picks_published=total_published,
            publish_errors=total_errors,
        )


if __name__ == "__main__":
    asyncio.run(run())
