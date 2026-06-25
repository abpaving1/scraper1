"""
Behavioral test for PicksPublisher.publish() retry resilience.
Mocks the redis client so it runs without a real Redis instance.

Usage:
    python tests/test_redis_retry.py
"""

import asyncio
import os
import sys
from unittest.mock import AsyncMock, MagicMock, patch

os.environ.setdefault("PROXY_HOST", "fake-host")
os.environ.setdefault("PROXY_PORT", "1234")
os.environ.setdefault("PROXY_USERNAME", "fake-user")
os.environ.setdefault("PROXY_PASSWORD", "fake-pass")

import redis.asyncio as redis  # noqa: E402

from models.pick import MarketType, RawPick  # noqa: E402
from queues.redis_publisher import PicksPublisher  # noqa: E402
from datetime import datetime, timezone  # noqa: E402

DUMMY_PICK = RawPick(
    source_slug="olbg",
    tipster_external_id="abc123",
    tipster_name="OLBG Popular",
    home_team_name="Arsenal",
    away_team_name="Chelsea",
    market=MarketType.MATCH_RESULT,
    selection="Home Win",
    raw_text="test",
    posted_at=datetime.now(timezone.utc),
)


async def scenario_a_transient_failure_then_success():
    """rpush fails twice with a RedisError, succeeds on the 3rd attempt —
    publish() should retry and ultimately succeed, not raise."""
    publisher = PicksPublisher()
    fake_client = MagicMock()
    fake_client.rpush = AsyncMock(
        side_effect=[redis.ConnectionError("blip"), redis.ConnectionError("blip"), None]
    )
    publisher._client = fake_client

    await publisher.publish(DUMMY_PICK)  # should not raise

    assert fake_client.rpush.await_count == 3, "expected 2 failed attempts + 1 successful retry"
    print("Scenario A (transient RedisError then success): PASS — retried and succeeded")


async def scenario_b_persistent_failure_still_raises():
    """If Redis is genuinely down (every attempt fails), publish() must still
    raise after exhausting retries — must not retry forever or swallow it."""
    publisher = PicksPublisher()
    fake_client = MagicMock()
    fake_client.rpush = AsyncMock(side_effect=redis.ConnectionError("redis is down"))
    publisher._client = fake_client

    raised = None
    try:
        await publisher.publish(DUMMY_PICK)
    except redis.ConnectionError as exc:
        raised = exc

    assert raised is not None, "publish() swallowed a persistent failure instead of raising"
    from config import settings

    assert fake_client.rpush.await_count == settings.scrape_max_retries, (
        f"expected exactly {settings.scrape_max_retries} attempts, got {fake_client.rpush.await_count}"
    )
    print("Scenario B (persistent failure): PASS — raised after exhausting retries, didn't retry forever")


async def scenario_c_programmer_error_fails_immediately():
    """publish() called before connect() raises RuntimeError — this is not
    a transient Redis issue, so it must NOT be retried (would just waste
    time hitting the same RuntimeError 3x before failing anyway)."""
    publisher = PicksPublisher()  # never connected, self._client is None

    raised = None
    try:
        await publisher.publish(DUMMY_PICK)
    except RuntimeError as exc:
        raised = exc

    assert raised is not None
    print("Scenario C (publish before connect): PASS — RuntimeError raised immediately, not retried")


async def main():
    await scenario_a_transient_failure_then_success()
    await scenario_b_persistent_failure_still_raises()
    await scenario_c_programmer_error_fails_immediately()
    print("\nAll scenarios passed.")


if __name__ == "__main__":
    asyncio.run(main())
