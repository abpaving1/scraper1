-- =============================================================================
-- Scraper Service — Postgres / TimescaleDB schema
-- Run with: psql $DATABASE_URL -f sql/schema.sql
-- Requires: TimescaleDB extension (use the timescale/timescaledb docker image,
-- not plain postgres, or `CREATE EXTENSION timescaledb;` will fail).
-- =============================================================================

CREATE EXTENSION IF NOT EXISTS timescaledb;
CREATE EXTENSION IF NOT EXISTS pgcrypto;  -- for gen_random_uuid()

-- -----------------------------------------------------------------------------
-- tipsters
-- -----------------------------------------------------------------------------
-- One row per (source, external_id) tipster identity.
--
-- IMPORTANT — OLBG-specific caveat: per sources/olbg.py, OLBG's listing is
-- "community-popular selections, not individual tipster cards" — every pick
-- uses tip_hash (unique per TIP, not per person) as tipster_external_id, with
-- tipster_name hardcoded to "OLBG Popular". That means OLBG will mint a new
-- tipsters row per pick rather than accumulating history against a stable
-- identity, so 90-day ROI/win-rate/streak tracking (the Weighting Engine,
-- Phase D) will NOT be meaningful for OLBG rows — there's no real per-tipster
-- identity to track. This is expected, not a bug: treat OLBG as a single
-- "consensus" signal, weighted differently from sources with real tipster
-- identities (Forebet, FreeSuperTips, etc.) once those land. Flagging here so
-- it isn't a surprise when OLBG's tipster count balloons.
CREATE TABLE IF NOT EXISTS tipsters (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    source_slug     TEXT NOT NULL,
    external_id     TEXT NOT NULL,
    name            TEXT NOT NULL,

    -- Populated later by the Weighting Engine (Phase D), nullable until then.
    roi_90d             NUMERIC(8, 4),
    win_rate_90d        NUMERIC(5, 4),
    current_streak      INTEGER,
    stats_updated_at    TIMESTAMPTZ,

    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT now(),

    UNIQUE (source_slug, external_id)
);

CREATE INDEX IF NOT EXISTS idx_tipsters_source ON tipsters (source_slug);

-- -----------------------------------------------------------------------------
-- fixtures
-- -----------------------------------------------------------------------------
-- One row per real-world match, de-duplicated ACROSS sources. Sources spell
-- team names differently ("Arsenal" vs "Arsenal FC" vs "Arsenal Football
-- Club"), so matching is done on normalised names (see processor/normalize.py)
-- + kickoff DATE (not exact timestamp — sources disagree on minute-level
-- kickoff time, and even a few minutes' difference would otherwise mint a
-- duplicate fixture row for the same match).
--
-- normalized_home/normalized_away/kickoff_date form the dedup key.
-- home_team_name/away_team_name retain the first-seen raw spelling for
-- display purposes — not authoritative, just human-readable.
CREATE TABLE IF NOT EXISTS fixtures (
    id                  UUID PRIMARY KEY DEFAULT gen_random_uuid(),

    home_team_name      TEXT NOT NULL,
    away_team_name      TEXT NOT NULL,
    normalized_home      TEXT NOT NULL,
    normalized_away      TEXT NOT NULL,

    league_name         TEXT,
    kickoff_utc         TIMESTAMPTZ,
    kickoff_date        DATE GENERATED ALWAYS AS ((kickoff_utc AT TIME ZONE 'UTC')::date) STORED,

    created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Partial unique index: only enforce the dedup key when kickoff_date is known.
-- Fixtures with no parseable kickoff_utc (kickoff_date NULL) skip dedup
-- entirely and always insert a new row — see processor/consumer.py for why
-- (we'd rather have an occasional duplicate fixture than silently merge two
-- different matches under a NULL-matches-NULL collision).
CREATE UNIQUE INDEX IF NOT EXISTS uq_fixtures_dedup_key
    ON fixtures (normalized_home, normalized_away, kickoff_date)
    WHERE kickoff_date IS NOT NULL;

CREATE INDEX IF NOT EXISTS idx_fixtures_kickoff ON fixtures (kickoff_utc);

-- -----------------------------------------------------------------------------
-- picks
-- -----------------------------------------------------------------------------
-- The resolved, fixture/tipster-linked counterpart of the raw RawPick
-- payloads sitting in Redis. ON CONFLICT target matches the README's
-- documented dedup index: (fixture_id, tipster_id, market, selection) —
-- deliberately WITHOUT posted_at, so a re-scrape of the same pick UPDATES
-- the existing row (fresher odds/confidence/raw_text) instead of duplicating.
CREATE TABLE IF NOT EXISTS picks (
    id                  UUID PRIMARY KEY DEFAULT gen_random_uuid(),

    fixture_id          UUID NOT NULL REFERENCES fixtures (id) ON DELETE CASCADE,
    tipster_id          UUID NOT NULL REFERENCES tipsters (id) ON DELETE CASCADE,
    source_slug         TEXT NOT NULL,

    market              TEXT NOT NULL,
    selection           TEXT NOT NULL,
    odds_decimal        NUMERIC(8, 3),
    confidence          NUMERIC(5, 2),

    raw_text            TEXT NOT NULL,
    posted_at           TIMESTAMPTZ NOT NULL,
    scraped_at           TIMESTAMPTZ NOT NULL,

    created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT now(),

    UNIQUE (fixture_id, tipster_id, market, selection)
);

CREATE INDEX IF NOT EXISTS idx_picks_fixture ON picks (fixture_id);
CREATE INDEX IF NOT EXISTS idx_picks_tipster ON picks (tipster_id);
CREATE INDEX IF NOT EXISTS idx_picks_source ON picks (source_slug);
CREATE INDEX IF NOT EXISTS idx_picks_scraped_at ON picks (scraped_at);

-- -----------------------------------------------------------------------------
-- odds_history (TimescaleDB hypertable)
-- -----------------------------------------------------------------------------
-- Time-series bookmaker odds, fed by The Odds API (Phase C, not built yet —
-- this is the destination table for that integration). Kept separate from
-- `picks` because it tracks odds MOVEMENT over time per bookmaker, not a
-- single tipster's pick.
CREATE TABLE IF NOT EXISTS odds_history (
    "time"              TIMESTAMPTZ NOT NULL,
    fixture_id          UUID NOT NULL REFERENCES fixtures (id) ON DELETE CASCADE,
    bookmaker           TEXT NOT NULL,
    market              TEXT NOT NULL,
    selection           TEXT NOT NULL,
    odds_decimal        NUMERIC(8, 3) NOT NULL,

    PRIMARY KEY ("time", fixture_id, bookmaker, market, selection)
);

SELECT create_hypertable('odds_history', 'time', if_not_exists => TRUE);

CREATE INDEX IF NOT EXISTS idx_odds_history_fixture ON odds_history (fixture_id, "time" DESC);

-- -----------------------------------------------------------------------------
-- updated_at auto-touch trigger (tipsters, fixtures, picks)
-- -----------------------------------------------------------------------------
CREATE OR REPLACE FUNCTION touch_updated_at() RETURNS TRIGGER AS $$
BEGIN
    NEW.updated_at = now();
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS trg_tipsters_updated_at ON tipsters;
CREATE TRIGGER trg_tipsters_updated_at
    BEFORE UPDATE ON tipsters
    FOR EACH ROW EXECUTE FUNCTION touch_updated_at();

DROP TRIGGER IF EXISTS trg_fixtures_updated_at ON fixtures;
CREATE TRIGGER trg_fixtures_updated_at
    BEFORE UPDATE ON fixtures
    FOR EACH ROW EXECUTE FUNCTION touch_updated_at();

DROP TRIGGER IF EXISTS trg_picks_updated_at ON picks;
CREATE TRIGGER trg_picks_updated_at
    BEFORE UPDATE ON picks
    FOR EACH ROW EXECUTE FUNCTION touch_updated_at();
