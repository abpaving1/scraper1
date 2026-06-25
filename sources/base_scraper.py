import asyncio
import json
import logging
import random
from pathlib import Path
from typing import Optional, List

from playwright.async_api import async_playwright, Page, Browser, BrowserContext
from playwright_stealth import stealth_async

from tipster.config import settings
from tipster.models.pick import RawPick
from tipster.queues.redis_publisher import PicksPublisher

logger = logging.getLogger(__name__)


class BaseSourceScraper:
    """
    Unified scraper lifecycle:
    - __aenter__ / __aexit__ manage browser, context, publisher
    - run() calls scrape() and publishes picks
    - scrape() implemented by subclasses
    - new_stealth_page() creates a stealth-enabled page
    - goto_with_retry() handles navigation with retry + jitter
    """

    source_slug: str = "base"
    base_url: Optional[str] = None

    def __init__(self):
        self._pw = None
        self._browser: Optional[Browser] = None
        self._context: Optional[BrowserContext] = None
        self._publisher: Optional[PicksPublisher] = None

    # ------------------------------------------------------------
    # Context Manager Lifecycle
    # ------------------------------------------------------------
    async def __aenter__(self):
        logger.info(f"{self.source_slug}_enter_start")

        self._pw = await async_playwright().start()

        proxy_cfg = None
        if settings.proxy_host and settings.proxy_port:
            proxy_cfg = {
                "server": f"http://{settings.proxy_host}:{settings.proxy_port}",
                "username": settings.proxy_username,
                "password": settings.proxy_password,
            }

        self._browser = await self._pw.chromium.launch(
            headless=settings.scrape_headless,
            proxy=proxy_cfg,
        )

        storage_path = Path(f".storage_state/{self.source_slug}.json")
        storage_path.parent.mkdir(parents=True, exist_ok=True)

        storage_state = None
        if storage_path.exists():
            try:
                storage_state = json.loads(storage_path.read_text())
            except Exception:
                logger.warning(f"{self.source_slug}_invalid_storage_state")

        self._context = await self._browser.new_context(
            viewport={"width": 1280, "height": 800},
            locale="en-GB",
            timezone_id="Europe/London",
            user_agent=settings.scrape_user_agent,
            storage_state=storage_state,
        )

        self._publisher = PicksPublisher()

        logger.info(f"{self.source_slug}_enter_complete")
        return self

    async def __aexit__(self, exc_type, exc, tb):
        logger.info(f"{self.source_slug}_exit_start")

        # Save storage state
        try:
            if self._context:
                state = await self._context.storage_state()
                Path(f".storage_state/{self.source_slug}.json").write_text(json.dumps(state))
        except Exception as e:
            logger.warning(f"{self.source_slug}_storage_state_save_failed: {e}")

        # Cleanup
        for obj, name in [
            (self._context, "context"),
            (self._browser, "browser"),
            (self._pw, "playwright"),
            (self._publisher, "publisher"),
        ]:
            try:
                if obj:
                    close = getattr(obj, "close", None)
                    if close:
                        await close()
            except Exception as e:
                logger.warning(f"{self.source_slug}_cleanup_failed_{name}: {e}")

        logger.info(f"{self.source_slug}_exit_complete")

    # ------------------------------------------------------------
    # Unified run() — scrape + publish
    # ------------------------------------------------------------
    async def run(self) -> List[RawPick]:
        logger.info(f"{self.source_slug}_run_start")

        picks = await self.scrape()

        if not picks:
            logger.warning(f"{self.source_slug}_no_picks_extracted")
            return []

        for pick in picks:
            try:
                await self._publisher.publish(pick)
            except Exception as e:
                logger.error(f"{self.source_slug}_publish_failed: {e}")

        logger.info(f"{self.source_slug}_run_complete", extra={"published": len(picks)})
        return picks

    # ------------------------------------------------------------
    # Subclass contract
    # ------------------------------------------------------------
    async def scrape(self) -> List[RawPick]:
        """
        Subclasses MUST implement this.
        Should:
        - create page via new_stealth_page()
        - navigate via goto_with_retry()
        - extract picks
        - return list[RawPick]
        """
        raise NotImplementedError

    # ------------------------------------------------------------
    # Stealth page creation
    # ------------------------------------------------------------
    async def new_stealth_page(self) -> Page:
        assert self._context is not None, "Context not initialized"
        page = await self._context.new_page()
        await stealth_async(page)
        return page

    # ------------------------------------------------------------
    # Navigation with retry + jitter
    # ------------------------------------------------------------
    async def goto_with_retry(self, page: Page, url: str, attempts: int = 3, wait: int = 2000):
        for attempt in range(1, attempts + 1):
            try:
                jitter = random.uniform(0.2, 1.1)
                await asyncio.sleep(jitter)

                resp = await page.goto(url, timeout=30000, wait_until="domcontentloaded")
                if resp and resp.status < 400:
                    return resp

                logger.warning(
                    f"{self.source_slug}_goto_bad_status",
                    extra={"status": resp.status if resp else None, "attempt": attempt},
                )

            except Exception as e:
                logger.warning(
                    f"{self.source_slug}_goto_exception",
                    extra={"error": str(e), "attempt": attempt},
                )

            await asyncio.sleep(wait / 1000)

        raise RuntimeError(f"{self.source_slug}_goto_failed_all_attempts: {url}")