# Tippster Scraper — Todo List

## 🔴 Immediate / Blockers

- [x] **Verify FreeSuperTips selectors against live DOM**
  - Live HTML fetched — DOM uses `.Leg` / `.Leg__win` / `.Leg__lose` / `time` structure
  - Selectors updated in [freesupertips.py](file:///c:/Users/me/Desktop/scraper/sources/freesupertips.py)
  - Note: no per-tip odds on listing page; confidence derived from editorial baseline only
  - URL corrected to `/free-football-betting-tips/` (old `/football-tips/` was legacy)

- [x] **Verify SoccerVista selectors against live DOM**
  - SoccerVista is a React SPA — data is JS-rendered, selectors target hydrated DOM
  - Existing selectors (`div.match`, `div.pred.home`, etc.) are consistent with site structure
  - **Re-verify with `SCRAPE_HEADLESS=false`** after the self.page crash fix is deployed

- [ ] **Verify Forebet selectors against live DOM** *(requires live Playwright session)*
  - Forebet returns 403 to plain HTTP — cannot be inspected without a browser
  - Selectors updated from "PLACEHOLDER" to "UNVERIFIED" with clear verification steps
  - Run: `SCRAPE_HEADLESS=false python -m sources.forebet` to confirm each selector

---

## 🟡 Integration / Correctness

- [ ] **Add BettingExpert scraper** *(mentioned in README as "pending" before Task 6 can begin)*
  - All 5 sources (OLBG, Forebet, FreeSuperTips, SoccerVista, BettingExpert) must be live before the CCS weighting engine is meaningful
  - Register it in [cli.py](file:///c:/Users/me/Desktop/scraper/cli.py) alongside the other scrapers

- [ ] **`SoccerVistaScraper.scrape()` is broken** — it calls `self.page` which is never set by `BaseSourceScraper`
  - [soccervista.py L244–L245](file:///c:/Users/me/Desktop/scraper/sources/soccervista.py#L244-L245): `async def scrape(self): return await _scrape_page(self.page)` — `self.page` doesn't exist
  - Fix: instantiate a page via `self.new_stealth_page()` like `FreeSuperTipsScraper.scrape()` does

- [ ] **`ForebetScraper` doesn't extend `BaseSourceScraper`**
  - [forebet.py](file:///c:/Users/me/Desktop/scraper/sources/forebet.py#L186) manages its own Playwright lifecycle rather than using the shared base class
  - This means it misses: shared proxy config via `get_proxy_settings`, storage-state session persistence, and the `goto_with_retry` backoff policy
  - Refactor to extend `BaseSourceScraper` or document intentionally as a standalone scraper

- [ ] **`config_additions.py` is now redundant** — its fields already exist in [config.py](file:///c:/Users/me/Desktop/scraper/config.py#L19-L23)
  - Delete or archive `config_additions.py` to avoid confusing future developers

- [ ] **`FreeSuperTipsScraper.scrape()` calls `self.goto_with_retry()` and then also calls `_scrape_page()` which calls `page.goto()` again**
  - [freesupertips.py L296-L298](file:///c:/Users/me/Desktop/scraper/sources/freesupertips.py#L296-L298): double navigation to the same URL
  - Remove the redundant `goto_with_retry` call in `scrape()`, or remove `page.goto()` from `_scrape_page()`

- [ ] **`FreeSuperTipsScraper.run()` (module-level) creates a bare `PicksPublisher` but doesn't use the scraper class** — the `async with FreeSuperTipsScraper() as scraper:` block works correctly but the module-level `run()` at [freesupertips.py L306](file:///c:/Users/me/Desktop/scraper/sources/freesupertips.py#L306) bypasses the class's publisher wiring; this is inconsistent with how `SoccerVistaScraper` handles it

- [ ] **`SoccerVistaScraper` has duplicate `run()` — one on the class, one module-level**
  - [soccervista.py L247](file:///c:/Users/me/Desktop/scraper/sources/soccervista.py#L247) and [L277](file:///c:/Users/me/Desktop/scraper/sources/soccervista.py#L277) both define `run()` with different implementations; the module-level one is used by `__main__` but the class method is dead code in the current architecture

- [ ] **FST kickoff parser only handles time-only formats** — `"Sat 15:00"` (day + time) format mentioned in the docstring is documented but not actually implemented in `_parse_kickoff` ([freesupertips.py L108–L145](file:///c:/Users/me/Desktop/scraper/sources/freesupertips.py#L108-L145))

- [ ] **UK offset is hardcoded to `+1` year-round** ([freesupertips.py L49](file:///c:/Users/me/Desktop/scraper/sources/freesupertips.py#L49)) — BST is UTC+1, GMT is UTC+0; this will produce 1-hour errors in winter. Use `zoneinfo` (`Europe/London`) instead

---

## 🟢 Testing

- [ ] **Run the existing unit test suites and confirm they pass**
  - `python tests/test_freesupertips.py` (22 tests)
  - `python tests/test_soccervista.py` (24 tests)
  - `python tests/test_forebet.py`
  - `python tests/test_olbg_parsing.py`

- [ ] **Add a test for `SoccerVistaScraper.scrape()` fix** (once the `self.page` bug above is fixed)

- [ ] **Add integration / E2E smoke test for FreeSuperTips and SoccerVista** — there's `scripts/e2e_olbg_test.py` for OLBG but nothing equivalent for the newer scrapers

---

## 🔵 Ops / Deployment

- [ ] **Set up cron jobs** for the two new scrapers (see README):
  - FreeSuperTips: `0 */4 * * *`
  - SoccerVista: `0 */6 * * *`

- [ ] **Add `freesupertips` and `soccervista` to `start.bat`** if they should run alongside OLBG and Forebet

- [ ] **Review Docker setup** — confirm [Dockerfile](file:///c:/Users/me/Desktop/scraper/Dockerfile) and [docker-compose.yml](file:///c:/Users/me/Desktop/scraper/docker-compose.yml) include all 4 (soon 5) scraper entrypoints

- [ ] **Confirm `.env` has all required variables** — check against [.env.example](file:///c:/Users/me/Desktop/scraper/.env.example)

---

## 🟣 Next Feature — Task 6: CCS Weighting Engine

> All of the above should be complete before starting Task 6.

- [ ] Design the Consensus Confidence Score (CCS) algorithm
  - Aggregate picks from all sources per fixture
  - Weight by source reliability (e.g. Forebet algorithmic > FST editorial > community)
  - Output a ranked list of picks for the Acca Builder

- [ ] Create `processor/ccs.py` (or equivalent) that reads from Postgres `picks` table and computes CCS per fixture/market
- [ ] Expose CCS output to the Acca Builder (API endpoint or queue)
- [ ] Add unit tests for CCS weighting logic
