"""
Resolves a scraped RawPick's loose source-side identifiers (team name
strings, tipster external ids) into stable `tipsters.id` / `fixtures.id`
foreign keys, then upserts the pick itself.

All three operations use INSERT ... ON CONFLICT ... RETURNING id rather than
SELECT-then-INSERT, so concurrent consumers (if you ever run more than one)
can't race each other into creating duplicate rows between the SELECT and
the INSERT.
"""

from datetime import datetime
from decimal import Decimal
from uuid import UUID

import asyncpg

from models.pick import RawPick
from processor.normalize import fixture_dedup_key
from utils.logger import get_logger

logger = get_logger(__name__)


async def resolve_tipster(conn: asyncpg.Connection, pick: RawPick) -> UUID:
    row = await conn.fetchrow(
        """
        INSERT INTO tipsters (source_slug, external_id, name)
        VALUES ($1, $2, $3)
        ON CONFLICT (source_slug, external_id)
        DO UPDATE SET name = EXCLUDED.name
        RETURNING id
        """,
        pick.source_slug,
        pick.tipster_external_id,
        pick.tipster_name,
    )
    return row["id"]


async def resolve_fixture(conn: asyncpg.Connection, pick: RawPick) -> UUID:
    normalized_home, normalized_away = fixture_dedup_key(pick.home_team_name, pick.away_team_name)

    if pick.kickoff_utc is None:
        # No kickoff time to dedup on — see schema.sql: the partial unique
        # index only covers rows with a non-null kickoff_date, so this
        # always inserts a new row rather than risking a false merge under
        # a NULL-matches-NULL collision. Logged at warning since it means
        # this fixture can't be deduped against the same match scraped from
        # another source.
        logger.warning(
            "fixture_resolved_without_kickoff",
            home=pick.home_team_name,
            away=pick.away_team_name,
            source=pick.source_slug,
        )
        row = await conn.fetchrow(
            """
            INSERT INTO fixtures (home_team_name, away_team_name, normalized_home, normalized_away, league_name, kickoff_utc)
            VALUES ($1, $2, $3, $4, $5, NULL)
            RETURNING id
            """,
            pick.home_team_name,
            pick.away_team_name,
            normalized_home,
            normalized_away,
            pick.league_name,
        )
        return row["id"]

    row = await conn.fetchrow(
        """
        INSERT INTO fixtures (home_team_name, away_team_name, normalized_home, normalized_away, league_name, kickoff_utc)
        VALUES ($1, $2, $3, $4, $5, $6)
        ON CONFLICT (normalized_home, normalized_away, kickoff_date) WHERE kickoff_date IS NOT NULL
        DO UPDATE SET
            -- Keep the first-seen raw spelling rather than overwriting it on
            -- every re-scrape; only league_name backfills if it was missing.
            league_name = COALESCE(fixtures.league_name, EXCLUDED.league_name)
        RETURNING id
        """,
        pick.home_team_name,
        pick.away_team_name,
        normalized_home,
        normalized_away,
        pick.league_name,
        pick.kickoff_utc,
    )
    return row["id"]


async def upsert_pick(
    conn: asyncpg.Connection,
    pick: RawPick,
    fixture_id: UUID,
    tipster_id: UUID,
) -> UUID:
    row = await conn.fetchrow(
        """
        INSERT INTO picks (
            fixture_id, tipster_id, source_slug, market, selection,
            odds_decimal, confidence, raw_text, posted_at, scraped_at
        )
        VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10)
        ON CONFLICT (fixture_id, tipster_id, market, selection)
        DO UPDATE SET
            odds_decimal = EXCLUDED.odds_decimal,
            confidence = EXCLUDED.confidence,
            raw_text = EXCLUDED.raw_text,
            posted_at = EXCLUDED.posted_at,
            scraped_at = EXCLUDED.scraped_at
        RETURNING id
        """,
        fixture_id,
        tipster_id,
        pick.source_slug,
        pick.market.value,
        pick.selection,
        pick.odds_decimal,
        pick.confidence,
        pick.raw_text,
        pick.posted_at,
        pick.scraped_at,
    )
    return row["id"]


async def process_pick(pool: asyncpg.Pool, pick: RawPick) -> None:
    """Resolves and upserts one pick inside a single transaction, so a
    failure partway through (e.g. pick insert fails after fixture/tipster
    were created) doesn't leave orphaned fixture/tipster rows with nothing
    pointing at them."""
    async with pool.acquire() as conn:
        async with conn.transaction():
            tipster_id = await resolve_tipster(conn, pick)
            fixture_id = await resolve_fixture(conn, pick)
            pick_id = await upsert_pick(conn, pick, fixture_id, tipster_id)

    logger.debug(
        "pick_processed",
        pick_id=str(pick_id),
        fixture_id=str(fixture_id),
        tipster_id=str(tipster_id),
        source=pick.source_slug,
    )
