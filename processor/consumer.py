"""
Processor (Task 5): drains the `queue:picks:raw` Redis list that the
scrapers publish to, resolves each RawPick's loose source-side identifiers
into stable fixture_id/tipster_id foreign keys, and upserts into Postgres.

Run via: python -m processor.consumer

Dead-letter queue is now implemented:
- Failed picks (invalid payload or processing errors after retries)
  are moved to `queue:picks:failed` (configurable via REDIS_FAILED_QUEUE).
- This allows manual inspection/reprocessing without data loss.
- The original scraper will still re-publish on next run due to dedup logic.
"""

import asyncio
import json
import signal

import asyncpg
import redis.asyncio as redis
from tenacity import retry, retry_if_exception_type, stop_after_attempt, wait_exponential
import logging

from config import settings
from models.pick import RawPick
from processor.db import close_pool, create_pool
from processor.resolve import process_pick
from utils.logger import configure_logging, get_logger

logger = get_logger(__name__)

# Errors worth retrying: transient connection issues. A malformed payload
# (pydantic ValidationError) or a schema/constraint violation is not
# transient and retrying it would just waste time hitting the same error.
_RETRYABLE_EXCEPTIONS = (asyncpg.PostgresConnectionError, ConnectionError, TimeoutError, OSError)


async def _push_to_dead_letter(redis_client: redis.Redis, raw_payload: str, reason: str) -> None:
    """Push failed payload to dead-letter queue for later inspection/reprocessing."""
    try:
        await redis_client.rpush(settings.redis_failed_queue, raw_payload)
        logger.warning(
            "pick_moved_to_dead_letter",
            reason=reason,
            queue=settings.redis_failed_queue,
            payload_preview=raw_payload[:200],
        )
    except Exception as exc:  # noqa: BLE001
        logger.error(
            "dead_letter_push_failed",
            error=str(exc),
            reason=reason,
            raw_payload=raw_payload[:500],
        )


@retry(
    retry=retry_if_exception_type(_RETRYABLE_EXCEPTIONS),
    stop=stop_after_attempt(settings.processor_max_retries),
    wait=wait_exponential(multiplier=1, min=1, max=10),
    reraise=True,
)
async def _process_with_retry(pool: asyncpg.Pool, pick: RawPick) -> None:
    await process_pick(pool, pick)


async def _handle_payload(pool: asyncpg.Pool, redis_client: redis.Redis, raw_payload: str) -> None:
    try:
        pick = RawPick.model_validate_json(raw_payload)
    except Exception as exc:  # noqa: BLE001 — malformed payload, not a connection issue
        logger.error("pick_payload_invalid", error=str(exc), raw_payload=raw_payload[:500])
        await _push_to_dead_letter(redis_client, raw_payload, "invalid_payload")
        return

    try:
        await _process_with_retry(pool, pick)
    except Exception as exc:  # noqa: BLE001 — must not kill the consumer loop over one bad pick
        logger.error(
            "pick_processing_failed",
            error=str(exc),
            error_type=type(exc).__name__,
            source=pick.source_slug,
            tipster_external_id=pick.tipster_external_id,
            fixture=f"{pick.home_team_name} v {pick.away_team_name}",
        )
        await _push_to_dead_letter(redis_client, raw_payload, "processing_failed")


async def run(stop_event: asyncio.Event) -> None:
    pool = await create_pool()
    redis_client = redis.from_url(settings.redis_url, decode_responses=True)
    await redis_client.ping()
    logger.info(
        "processor_started",
        raw_queue=settings.redis_picks_queue,
        failed_queue=settings.redis_failed_queue,
    )

    try:
        while not stop_event.is_set():
            try:
                # BLPOP blocks server-side up to `timeout` seconds; using a
                # short timeout (not 0/infinite) so the loop wakes up
                # periodically to check stop_event for graceful shutdown.
                result = await redis_client.blpop(settings.redis_picks_queue, timeout=2)
            except (ConnectionError, OSError) as exc:
                logger.warning("redis_blpop_failed", error=str(exc))
                await asyncio.sleep(2)
                continue

            if result is None:
                continue  # timeout, no pick available — loop and recheck stop_event

            _, raw_payload = result
            await _handle_payload(pool, redis_client, raw_payload)
    finally:
        await redis_client.aclose()
        await close_pool(pool)
        logger.info("processor_stopped")


async def main() -> None:
    configure_logging()
    stop_event = asyncio.Event()

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, stop_event.set)
        except NotImplementedError:
            # add_signal_handler isn't available on Windows' default event
            # loop policy — fall back to relying on KeyboardInterrupt for
            # Ctrl+C; SIGTERM handling won't work on Windows either way.
            pass

    await run(stop_event)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
