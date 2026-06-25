#!/usr/bin/env python3
"""
Simple DLQ replayer for queue:picks:failed

Usage:
    python scripts/replay_dlq.py
    # or with limit:
    python scripts/replay_dlq.py --limit 10
"""

import asyncio
import sys
import argparse

import redis.asyncio as redis
from pydantic import ValidationError

from config import settings
from models.pick import RawPick
from processor.resolve import process_pick
from processor.db import create_pool, close_pool
from utils.logger import configure_logging, get_logger

logger = get_logger(__name__)


async def replay_dlq(limit: int = 50) -> None:
    configure_logging()
    redis_client = redis.from_url(settings.redis_url, decode_responses=True)
    pool = await create_pool()

    moved_back = 0
    processed = 0
    failed_again = 0

    try:
        while (limit is None or moved_back < limit):
            raw_payload = await redis_client.lpop(settings.redis_failed_queue)  # type: ignore[attr-defined]
            if not raw_payload:
                break

            moved_back += 1
            logger.info("dlq_item_retrieved", count=moved_back)

            try:
                pick = RawPick.model_validate_json(raw_payload)
                await process_pick(pool, pick)
                processed += 1
                logger.info("dlq_pick_replayed_success", fixture=f"{pick.home_team_name} v {pick.away_team_name}")
            except ValidationError as e:
                logger.error("dlq_invalid_payload", error=str(e))
                # Push back to failed if still bad
                await redis_client.rpush(settings.redis_failed_queue, raw_payload)
                failed_again += 1
            except Exception as e:
                logger.error("dlq_replay_failed", error=str(e))
                await redis_client.rpush(settings.redis_failed_queue, raw_payload)
                failed_again += 1

    finally:
        await redis_client.aclose()
        await close_pool(pool)

    logger.info(
        "dlq_replay_complete",
        moved_back=moved_back,
        successfully_processed=processed,
        failed_again=failed_again,
    )


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--limit", type=int, default=50, help="Max items to replay")
    args = parser.parse_args()

    asyncio.run(replay_dlq(args.limit))