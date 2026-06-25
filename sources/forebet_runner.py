"""
Forebet scraper runner (unified lifecycle version).

Run:
    python -m sources.forebet_runner
"""

import asyncio
from config import settings
from queues.redis_publisher import PicksPublisher
from sources.forebet import ForebetScraper
from utils.logger import configure_logging, get_logger

logger = get_logger(__name__)


async def run() -> None:
    configure_logging()

    publisher = PicksPublisher()
    await publisher.connect()

    logger.info("forebet_scrape_starting")

    try:
        async with ForebetScraper() as scraper:
            # Unified lifecycle: run() scrapes + publishes
            picks = await scraper.run()

            logger.info(
                "forebet_scrape_complete",
                picks_published=len(picks),
            )

    finally:
        await publisher.close()


if __name__ == "__main__":
    asyncio.run(run())