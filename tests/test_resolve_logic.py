"""
Tests the Python-level call sequence in processor/resolve.py using a mocked
asyncpg connection — does NOT validate the SQL itself (no real Postgres in
this environment). Confirms: tipster/fixture resolved before pick upsert,
all three happen inside one transaction, and the no-kickoff branch is taken
when kickoff_utc is None.

You still need to run schema.sql against a real Postgres+TimescaleDB
instance and exercise this against it directly before trusting it in prod —
see the README section this test's docstring points you to.

Run with:
    python tests/test_resolve_logic.py
"""

import asyncio
import os
import sys
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

os.environ.setdefault("PROXY_HOST", "fake-host")
os.environ.setdefault("PROXY_PORT", "1234")
os.environ.setdefault("PROXY_USERNAME", "fake-user")
os.environ.setdefault("PROXY_PASSWORD", "fake-pass")

from models.pick import MarketType, RawPick  # noqa: E402
from processor.resolve import process_pick  # noqa: E402

PICK_WITH_KICKOFF = RawPick(
    source_slug="olbg",
    tipster_external_id="hash123",
    tipster_name="OLBG Popular",
    home_team_name="Arsenal FC",
    away_team_name="Chelsea FC",
    league_name="Premier League",
    kickoff_utc=datetime(2026, 6, 21, 15, 0, tzinfo=timezone.utc),
    market=MarketType.MATCH_RESULT,
    selection="Home Win",
    raw_text="test",
    posted_at=datetime.now(timezone.utc),
)

PICK_WITHOUT_KICKOFF = PICK_WITH_KICKOFF.model_copy(update={"kickoff_utc": None})


def _make_fake_pool(fetchrow_results: list[dict]):
    """Builds a fake asyncpg pool/connection where conn.fetchrow() returns
    each dict in fetchrow_results in order, and conn.transaction() is a
    working async context manager."""
    fake_conn = MagicMock()
    fake_conn.fetchrow = AsyncMock(side_effect=fetchrow_results)

    fake_transaction_cm = MagicMock()
    fake_transaction_cm.__aenter__ = AsyncMock(return_value=None)
    fake_transaction_cm.__aexit__ = AsyncMock(return_value=False)
    fake_conn.transaction = MagicMock(return_value=fake_transaction_cm)

    fake_acquire_cm = MagicMock()
    fake_acquire_cm.__aenter__ = AsyncMock(return_value=fake_conn)
    fake_acquire_cm.__aexit__ = AsyncMock(return_value=False)

    fake_pool = MagicMock()
    fake_pool.acquire = MagicMock(return_value=fake_acquire_cm)
    return fake_pool, fake_conn


async def scenario_a_happy_path_with_kickoff():
    tipster_id, fixture_id, pick_id = uuid4(), uuid4(), uuid4()
    fake_pool, fake_conn = _make_fake_pool(
        [{"id": tipster_id}, {"id": fixture_id}, {"id": pick_id}]
    )

    await process_pick(fake_pool, PICK_WITH_KICKOFF)

    assert fake_conn.fetchrow.await_count == 3, "expected exactly 3 queries: tipster, fixture, pick"
    assert fake_conn.transaction.call_count == 1, "all three queries must run inside one transaction"

    # Confirm the fixture upsert used the kickoff-aware (deduping) query, not
    # the no-kickoff branch — distinguishable by the SQL text containing the
    # ON CONFLICT clause with kickoff_date.
    fixture_call_sql = fake_conn.fetchrow.await_args_list[1].args[0]
    assert "ON CONFLICT" in fixture_call_sql and "kickoff_date" in fixture_call_sql
    print("Scenario A (happy path, kickoff known): PASS — 3 queries, 1 transaction, dedup branch used")


async def scenario_b_no_kickoff_skips_dedup():
    tipster_id, fixture_id, pick_id = uuid4(), uuid4(), uuid4()
    fake_pool, fake_conn = _make_fake_pool(
        [{"id": tipster_id}, {"id": fixture_id}, {"id": pick_id}]
    )

    await process_pick(fake_pool, PICK_WITHOUT_KICKOFF)

    fixture_call_sql = fake_conn.fetchrow.await_args_list[1].args[0]
    assert "ON CONFLICT" not in fixture_call_sql, (
        "pick with no kickoff_utc must use the always-insert branch, not the dedup ON CONFLICT branch"
    )
    print("Scenario B (no kickoff): PASS — always-insert branch used, no false-dedup risk")


async def scenario_c_tipster_resolved_before_fixture_before_pick():
    """Call ORDER matters: pick upsert references fixture_id/tipster_id, so
    they must be resolved first. Confirmed via the order of fetchrow calls'
    SQL text (tipster table first, fixtures second, picks third)."""
    tipster_id, fixture_id, pick_id = uuid4(), uuid4(), uuid4()
    fake_pool, fake_conn = _make_fake_pool(
        [{"id": tipster_id}, {"id": fixture_id}, {"id": pick_id}]
    )

    await process_pick(fake_pool, PICK_WITH_KICKOFF)

    calls = fake_conn.fetchrow.await_args_list
    assert "INSERT INTO tipsters" in calls[0].args[0]
    assert "INSERT INTO fixtures" in calls[1].args[0]
    assert "INSERT INTO picks" in calls[2].args[0]
    print("Scenario C (call order): PASS — tipster, then fixture, then pick")


async def main():
    await scenario_a_happy_path_with_kickoff()
    await scenario_b_no_kickoff_skips_dedup()
    await scenario_c_tipster_resolved_before_fixture_before_pick()
    print("\nAll scenarios passed.")
    print(
        "\nNOTE: this validates Python call sequencing only — it mocks asyncpg "
        "entirely. You still need to run sql/schema.sql against a real "
        "Postgres+TimescaleDB instance and confirm the actual SQL executes "
        "correctly (see README setup steps)."
    )


if __name__ == "__main__":
    asyncio.run(main())
