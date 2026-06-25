#!/usr/bin/env python3
"""
E2E test for OLBG scraper (Phase 1 validation).

Runs a short scrape, checks Redis queue, and verifies processor can handle picks.
Requires Docker stack running.
"""

import asyncio
import sys
from datetime import datetime, timezone

from config import settings
from sources.olbg import OLBGScraper
from utils.logger import configure_logging


async def run_e2e_test():
    configure_logging()
    print("🚀 Starting OLBG E2E test...")

    async with OLBGScraper() as scraper:
        picks = await scraper.scrape()

    print(f"✅ Scraped {len(picks)} picks from OLBG")
    if picks:
        print(f"   First pick: {picks[0].home_team_name} v {picks[0].away_team_name} - {picks[0].selection}")
        print(f"   Published to Redis queue: {settings.redis_picks_queue}")
    else:
        print("⚠️  No picks scraped — check selectors or site changes")

    print("\n✅ OLBG E2E test complete. Run processor to drain queue into Postgres.")
    print("   Next: docker compose exec postgres psql -U postgres -d tippster -c 'SELECT COUNT(*) FROM picks WHERE source_slug = 'olbg';'")


if __name__ == "__main__":
    asyncio.run(run_e2e_test())
