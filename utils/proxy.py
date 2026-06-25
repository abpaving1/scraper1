"""
Residential proxy configuration.

We deliberately avoid datacenter IPs (per project security standards) and use
sticky sessions per scrape run so a single tipster-listing crawl looks like one
consistent "visitor" rather than a different IP per request, which is itself
a bot signal on sites with fingerprinting (OLBG sits behind Cloudflare).

Provider notes:
  - Webshare: rotating residential gateway, session pinned via username suffix
    (`-session-<id>`) if your plan supports sticky sessions.
  - Bright Data: session pinned via `-session-<id>` in the zone username.
Adjust `_build_username` if your provider's session syntax differs.
"""

import random
import string

from playwright.async_api import ProxySettings

from config import settings


def _random_session_id(length: int = 8) -> str:
    return "".join(random.choices(string.ascii_lowercase + string.digits, k=length))


def _build_username(session_id: str) -> str:
    """
    Builds the proxy username with an embedded sticky-session id.
    Confirm the exact suffix format against your provider's dashboard —
    Webshare and Bright Data both document this under "Rotating residential".
    """
    prefix = settings.proxy_session_prefix.strip()
    if not prefix or prefix.lower() in ("none", "false"):
        return settings.proxy_username
    return f"{settings.proxy_username}-{prefix}-session-{session_id}"


def get_proxy_settings(
    session_id: str | None = None,
) -> ProxySettings | None:

    if not settings.proxy_enabled:
        return None

    if not settings.proxy_host:
        return None

    session_id = session_id or _random_session_id()

    return {
        "server": f"http://{settings.proxy_host}:{settings.proxy_port}",
        "username": _build_username(session_id),
        "password": settings.proxy_password,
    }