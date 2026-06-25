"""
Human-timing jitter helpers.

Bots that fetch pages at perfectly regular intervals (or with zero delay
between actions) are trivially fingerprinted. These helpers introduce
randomised pauses modelled loosely on human reading/scrolling pace.
"""

import asyncio
import random

from config import settings


async def jitter_delay(min_seconds: float | None = None, max_seconds: float | None = None) -> None:
    """Async sleep for a random duration within the configured jitter range."""
    lo = min_seconds if min_seconds is not None else settings.scrape_jitter_min_seconds
    hi = max_seconds if max_seconds is not None else settings.scrape_jitter_max_seconds
    await asyncio.sleep(random.uniform(lo, hi))


async def human_scroll(page, distance_px: int = 600, steps: int = 6) -> None:
    """
    Scrolls a page in small increments with small random pauses, rather than
    jumping straight to the bottom — mimics a human reading down the page,
    which also reliably triggers lazy-loaded content some sites rely on.
    """
    step_size = distance_px // steps
    for _ in range(steps):
        await page.mouse.wheel(0, step_size + random.randint(-20, 20))
        await asyncio.sleep(random.uniform(0.15, 0.5))
