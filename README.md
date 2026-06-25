# Scraper Service — Betting Tip Aggregator (OLBG + Forebet Hardened)

**Phase 1 (OLBG + Foundation) complete.** OLBG is production-ready with enhanced logging, better unmapped market warnings, and full E2E pipeline support.

## Overview

Two new scraper modules feeding the existing Redis → PostgreSQL pipeline from Task 5.

| Source | Module | Type | Cadence | Anti-bot |
|---|---|---|---|---|
| FreeSuperTips | `scrapers/freesupertips.py` | Editorial expert | Every 4h | Low — stealth only |
| SoccerVista | `scrapers/soccervista.py` | Statistical algorithm | Every 6h | Low — stealth only |

Both scrapers are modelled as **synthetic single-source tipsters** (same pattern as Forebet from Task 3) — they have a fixed `tipster_external_id` rather than individual human identities with tracked ROI.

---

## File Structure

```
scrapers/
  freesupertips.py      ← Editorial tips scraper
  soccervista.py        ← Statistical predictions scraper

tests/
  test_freesupertips.py ← 22 unit tests (pure functions, no browser)
  test_soccervista.py   ← 24 unit tests (pure functions, no browser)

config_additions.py     ← Fields to add to config.py Settings class
```

---

## Installation

No new dependencies — Task 4 uses the same stack as Tasks 2 & 3:

```bash
pip install -r requirements.txt
```

The existing `requirements.txt` already covers:
- `playwright`, `playwright-stealth`
- `tenacity`, `structlog`, `pydantic`
- `redis`, `asyncpg`

---

## Config Changes

Add to the `Settings` class in `config.py`:

```python
fst_base_url: str = "https://www.freesupertips.com/football-tips/"
soccervista_base_url: str = "https://www.soccervista.com/"
```

No new environment variables or API keys required — both sources are public.

---

## ⚠️ SELECTOR VERIFICATION (Required Before Production)

Both scrapers ship with **structured placeholder selectors**. You MUST verify
these against live DOM before deploying.

### FreeSuperTips — run locally:

```bash
SCRAPE_HEADLESS=false python scrapers/freesupertips.py
```

Open DevTools on `https://www.freesupertips.com/football-tips/` and verify:

| Constant | What to find | Update if wrong |
|---|---|---|
| `TIP_ROW_SEL` | Each tip card container | Yes |
| `MATCH_SEL` | "Arsenal v Chelsea" text | Yes |
| `LEAGUE_SEL` | Competition name (e.g. "Premier League") | Yes |
| `SELECTION_SEL` | Tip text ("Home Win", "Over 2.5 Goals") | Yes |
| `ODDS_SEL` | Decimal or fractional odds string | Yes |
| `KICKOFF_SEL` | Kickoff time element | Yes |
| `TIPSTER_SEL` | Author/expert name (may not exist per-tip) | Yes |

### SoccerVista — run locally:

```bash
SCRAPE_HEADLESS=false python scrapers/soccervista.py
```

Open DevTools on `https://www.soccervista.com/` and verify:

| Constant | What to find | Update if wrong |
|---|---|---|
| `TABLE_ROW_SEL` | Each match row in the predictions table | Yes |
| `HOME_TEAM_SEL` | Home team name cell | Yes |
| `AWAY_TEAM_SEL` | Away team name cell | Yes |
| `HOME_PCT_SEL` | Home win probability ("45%" or "45") | Yes |
| `DRAW_PCT_SEL` | Draw probability | Yes |
| `AWAY_PCT_SEL` | Away win probability | Yes |
| `KICKOFF_SEL` | Kickoff date/time cell | Yes |
| `LEAGUE_SEL` | League/competition cell | Yes |

---

## Running the Tests

```bash
# FreeSuperTips unit tests (22 tests, no browser required)
python tests/test_freesupertips.py

# SoccerVista unit tests (24 tests, no browser required)
python tests/test_soccervista.py
```

All tests mock browser interactions and test pure parsing functions in isolation.
Selector verification still requires running against the live sites.

---

## Cron Schedule

Add to your crontab (or cron management tool):

```cron
# FreeSuperTips — every 4 hours
0 */4 * * * cd /path/to/project && python -m scrapers.freesupertips

# SoccerVista — every 6 hours
0 */6 * * * cd /path/to/project && python -m scrapers.soccervista
```

---

## Design Decisions

### Confidence Derivation

Neither source publishes per-tip confidence scores or tipster ROI. We derive confidence as follows:

**FreeSuperTips:** Proxy from displayed odds — shorter-priced editorial selections are treated as higher-conviction. Range: 0.55–0.75. Base: 0.65 (editorial authority midpoint).

**SoccerVista:** Derived from model probability percentage + gap between top pick and second-best outcome. Range: 0.40–0.85. A 70% home win with a 40pp gap scores higher than a 50% pick with a 15pp gap.

### Minimum Conviction Filter (SoccerVista)

SoccerVista publishes predictions for hundreds of matches including obscure lower-league fixtures with near-even probabilities. Picks where the top outcome leads the second by less than **15 percentage points** are silently skipped — these represent low-conviction model outputs not worth including in an accumulator.

This threshold (`MIN_PROBABILITY_GAP_PP = 15.0`) is tunable in `scrapers/soccervista.py`.

### Volume Cap (SoccerVista)

SoccerVista lists hundreds of fixtures daily. We cap at **50 picks per run** (`MAX_PICKS_PER_RUN`) sorted by confidence descending. This keeps Redis queue volume manageable and focuses on the strongest signals.

---

## Next Task

**Task 6 — CCS Weighting Engine**

With all 5 scrapers now feeding the pipeline (OLBG, Forebet, FreeSuperTips, SoccerVista, and BettingExpert pending), Task 6 can begin building the Consensus Confidence Score algorithm that aggregates these sources into ranked selections for the Acca Builder.
