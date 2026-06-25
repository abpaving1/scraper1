"""
Pushes validated RawPick payloads onto the Redis queue that the processor
(Task 5) consumes from. Kept deliberately thin — the scraper's only job is
to scrape and validate; matching fixture_id/tipster_id and writing to
Postgres happens downstream.
"""

import logging

import redis.asyncio as redis
from tenacity import before_sleep_log, retry, retry_if_exception_type, stop_after_attempt, wait_exponential

from config import settings
from models.pick import RawPick
from utils.logger import get_logger

logger = get_logger(__name__)


class PicksPublisher:
    def __init__(self, redis_url: str | None = None, queue_name: str | None = None) -> None:
        self._redis_url = redis_url or settings.redis_url
        self._queue_name = queue_name or settings.redis_picks_queue
        self._client: redis.Redis | None = None

    async def connect(self) -> None:
        self._client = redis.from_url(self._redis_url, decode_responses=True)
        await self._client.ping()
        logger.info("redis_connected", queue=self._queue_name)

    @retry(
        # Only retry on transient Redis/connection failures — a programmer
        # error (e.g. publish() called before connect(), which raises
        # RuntimeError below) should fail immediately, not get masked behind
        # three retries that can never succeed.
        retry=retry_if_exception_type(redis.RedisError),
        stop=stop_after_attempt(settings.scrape_max_retries),
        wait=wait_exponential(multiplier=1, min=1, max=10),
        before_sleep=before_sleep_log(logger, logging.WARNING),  # type: ignore[arg-type]
        reraise=True,
    )
    async def publish(self, pick: RawPick) -> None:
        # A single dropped Redis connection mid-scrape used to raise straight
        # out of rpush() and abort the whole run — losing every pick not yet
        # published, even though the scraper publishes incrementally per-pick
        # specifically so a partial failure doesn't lose everything. Retrying
        # the publish itself (not just the scrape) closes that gap for
        # transient blips without masking a genuinely-down Redis instance
        # (still raises after scrape_max_retries attempts).
        if self._client is None:
            raise RuntimeError("PicksPublisher.connect() must be called before publish()")
        payload = pick.model_dump_json()
        await self._client.rpush(self._queue_name, payload)
        logger.debug(
            "pick_published",
            tipster=pick.tipster_name,
            fixture=f"{pick.home_team_name} v {pick.away_team_name}",
            market=pick.market.value,
        )

    async def close(self) -> None:
        if self._client is not None:
            await self._client.aclose()
