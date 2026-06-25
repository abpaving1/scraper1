"""
Connection pool for the processor's Postgres access. Kept separate from
consumer.py so it's reusable by the (future) Weighting Engine job and any
API layer code without dragging in Redis-consumer concerns.
"""

import asyncpg

from config import settings
from utils.logger import get_logger

logger = get_logger(__name__)


async def create_pool() -> asyncpg.Pool:
    pool = await asyncpg.create_pool(
        dsn=settings.database_url,
        min_size=settings.db_pool_min_size,
        max_size=settings.db_pool_max_size,
    )
    logger.info("postgres_pool_created", min_size=settings.db_pool_min_size, max_size=settings.db_pool_max_size)
    return pool


async def close_pool(pool: asyncpg.Pool) -> None:
    await pool.close()
    logger.info("postgres_pool_closed")
