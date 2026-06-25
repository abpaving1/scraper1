"""
Abstract base class for all source scrapers (OLBG, Forebet, FreeSuperTips, etc).
Each concrete scraper (sources/olbg.py, sources/forebet.py, ...) implements
`scrape()` and reuses this class's browser lifecycle, stealth setup, proxy
handling, and retry policy — so anti-bot logic lives in exactly one place.
"""

import sys
from abc import ABC, abstractmethod
from pathlib import Path

from playwright.async_api import Browser, BrowserContext, Page, async_playwright
from playwright_stealth import stealth_async
from tenacity import retry, stop_after_attempt, wait_exponential, before_sleep_log
import logging

from config import settings
from models.pick import RawPick
from queues.redis_publisher import PicksPublisher
from utils.jitter import jitter_delay
from utils.logger import get_logger
from utils.proxy import get_proxy_settings

logger = get_logger(__name__)

# Session cookie persistence: each source gets its own storage_state file so
# a scraper run looks like a returning visitor, not a fresh session every time.
STORAGE_STATE_DIR = Path(__file__).parent.parent / ".storage_state"
STORAGE_STATE_DIR.mkdir(exist_ok=True)


class BaseSourceScraper(ABC):
    source_slug: str  # set by subclass, e.g. "olbg"
    base_url: str  # set by subclass

    def __init__(self) -> None:
        self._publisher = PicksPublisher()
        self._browser: Browser | None = None
        self._context: BrowserContext | None = None

    @property
    def _storage_state_path(self) -> Path:
        return STORAGE_STATE_DIR / f"{self.source_slug}.json"

    async def __aenter__(self) -> "BaseSourceScraper":
        try:
            await self._publisher.connect()
            self._playwright = await async_playwright().start()

            proxy = get_proxy_settings()
            self._browser = await self._playwright.chromium.launch(
                headless=settings.scrape_headless,
                proxy=proxy,
            )

            storage_state = str(self._storage_state_path) if self._storage_state_path.exists() else None
            self._context = await self._browser.new_context(
                storage_state=storage_state,
                user_agent=(
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
                ),
                viewport={"width": 1366, "height": 768},
                locale="en-GB",
                timezone_id="Europe/London",
            )
            self._context.set_default_timeout(settings.scrape_timeout_ms)
            logger.info("scraper_session_started", source=self.source_slug)
            return self
        except Exception:
            # If __aenter__ doesn't complete, Python's `async with` never calls
            # __aexit__ — so anything we already started (browser process,
            # Redis connection) would otherwise leak silently. Tear down
            # whatever exists so far, then let the original error propagate.
            logger.error("scraper_session_start_failed", source=self.source_slug, exc_info=True)
            await self.__aexit__(*sys.exc_info())
            raise

    async def __aexit__(self, exc_type, exc_val, exc_tb) -> None:
        # Each step runs independently and only logs on failure, rather than
        # letting one failure (e.g. storage_state() raising because the
        # context already crashed mid-run) abort the function and skip every
        # cleanup step after it — that used to leak the browser process and
        # the Redis connection on any run that failed partway through.
        if self._context is not None:
            try:
                # Persist cookies/local storage so the next run resumes the
                # session rather than presenting as a brand-new visitor.
                await self._context.storage_state(path=str(self._storage_state_path))
            except Exception as exc:  # noqa: BLE001 — best-effort save, must not block further cleanup
                logger.warning("storage_state_save_failed", source=self.source_slug, error=str(exc))
            try:
                await self._context.close()
            except Exception as exc:  # noqa: BLE001
                logger.warning("context_close_failed", source=self.source_slug, error=str(exc))

        if self._browser is not None:
            try:
                await self._browser.close()
            except Exception as exc:  # noqa: BLE001
                logger.warning("browser_close_failed", source=self.source_slug, error=str(exc))

        if hasattr(self, "_playwright"):
            try:
                await self._playwright.stop()
            except Exception as exc:  # noqa: BLE001
                logger.warning("playwright_stop_failed", source=self.source_slug, error=str(exc))

        try:
            await self._publisher.close()
        except Exception as exc:  # noqa: BLE001
            logger.warning("publisher_close_failed", source=self.source_slug, error=str(exc))

        logger.info("scraper_session_closed", source=self.source_slug)

    async def new_stealth_page(self) -> Page:
        """Opens a new page with playwright-stealth patches applied."""
        assert self._context is not None
        page = await self._context.new_page()
        await stealth_async(page)
        return page

    @retry(
        stop=stop_after_attempt(settings.scrape_max_retries),
        wait=wait_exponential(multiplier=2, min=2, max=30),
        before_sleep=before_sleep_log(logger, logging.WARNING),  # type: ignore[arg-type]
        reraise=True,
    )
    async def goto_with_retry(self, page: Page, url: str) -> None:
        await jitter_delay()  # human-like pause before each navigation
        response = await page.goto(url, wait_until="domcontentloaded")
        if response is None or response.status >= 400:
            status = response.status if response else "no response"
            raise RuntimeError(f"Bad response ({status}) loading {url}")

    async def publish_pick(self, pick: RawPick) -> None:
        await self._publisher.publish(pick)

    @abstractmethod
    async def scrape(self) -> list[RawPick]:
        """Implemented by each concrete source scraper. Returns picks scraped
        in this run (also published to Redis as they're found)."""
        raise NotImplementedError
