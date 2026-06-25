"""
Behavioral test for the __aenter__/__aexit__ cleanup-isolation fix.
Mocks playwright/redis pieces so it runs without a real browser or Redis —
just proves the cleanup-isolation logic itself, fast, no network needed.

Usage:
    1. Drop this file into the root of the `scraper` repo (next to cli.py),
       AFTER replacing sources/base_scraper.py with the fixed version.
    2. Run:  python test_cleanup_isolation.py
       (set dummy proxy env vars first if you don't have a .env yet — see below)
"""

import asyncio
import os
import sys
from unittest.mock import AsyncMock, MagicMock, patch

# Settings requires these at import time — fill in dummy values if you don't
# have a real .env yet. Skip this block if you already have a working .env.
os.environ.setdefault("PROXY_HOST", "fake-host")
os.environ.setdefault("PROXY_PORT", "1234")
os.environ.setdefault("PROXY_USERNAME", "fake-user")
os.environ.setdefault("PROXY_PASSWORD", "fake-pass")

from sources.base_scraper import BaseSourceScraper  # noqa: E402


class DummyScraper(BaseSourceScraper):
    source_slug = "dummy"
    base_url = "https://example.invalid"

    async def scrape(self):
        return []


async def scenario_a_storage_state_raises_doesnt_skip_rest():
    """If context.storage_state() raises during __aexit__, browser.close(),
    playwright.stop(), and publisher.close() must still all run."""
    scraper = DummyScraper()

    fake_context = MagicMock()
    fake_context.storage_state = AsyncMock(side_effect=RuntimeError("context crashed"))
    fake_context.close = AsyncMock()

    fake_browser = MagicMock()
    fake_browser.close = AsyncMock()

    fake_playwright = MagicMock()
    fake_playwright.stop = AsyncMock()

    scraper._context = fake_context
    scraper._browser = fake_browser
    scraper._playwright = fake_playwright
    scraper._publisher.close = AsyncMock()

    await scraper.__aexit__(None, None, None)

    assert fake_context.storage_state.await_count == 1
    assert fake_context.close.await_count == 1, "context.close() was skipped after storage_state() raised"
    assert fake_browser.close.await_count == 1, "browser.close() was skipped — this is the leak"
    assert fake_playwright.stop.await_count == 1, "playwright.stop() was skipped — this is the leak"
    assert scraper._publisher.close.await_count == 1, "publisher.close() was skipped — this is the leak"
    print("Scenario A (storage_state raises mid-cleanup): PASS — all cleanup steps still ran")


async def scenario_b_partial_aenter_failure_tears_down_browser():
    """If new_context() raises after the browser already launched,
    the browser process must still get closed even though __aexit__
    is never invoked by `async with` itself."""
    scraper = DummyScraper()

    fake_browser = MagicMock()
    fake_browser.close = AsyncMock()
    fake_browser.new_context = AsyncMock(side_effect=RuntimeError("new_context blew up"))

    fake_playwright = MagicMock()
    fake_playwright.chromium.launch = AsyncMock(return_value=fake_browser)
    fake_playwright.stop = AsyncMock()

    with patch("sources.base_scraper.async_playwright") as mock_ap:
        mock_ap.return_value.start = AsyncMock(return_value=fake_playwright)
        scraper._publisher.connect = AsyncMock()
        scraper._publisher.close = AsyncMock()

        raised = None
        try:
            await scraper.__aenter__()
        except RuntimeError as exc:
            raised = exc

    assert raised is not None and "new_context blew up" in str(raised), (
        "original __aenter__ exception was not propagated correctly"
    )
    assert fake_browser.close.await_count == 1, "browser launched in __aenter__ was never closed — leak"
    assert fake_playwright.stop.await_count == 1, "playwright was never stopped — leak"
    print("Scenario B (partial __aenter__ failure): PASS — browser/playwright torn down, original error propagated")


async def main():
    await scenario_a_storage_state_raises_doesnt_skip_rest()
    await scenario_b_partial_aenter_failure_tears_down_browser()
    print("\nAll scenarios passed.")


if __name__ == "__main__":
    asyncio.run(main())
