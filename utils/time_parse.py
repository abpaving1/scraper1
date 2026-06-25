"""
Relative and absolute timestamp parsing for OLBG listing pages.

OLBG shows kickoff times as "Today 20:00", "Tomorrow 02:00", "15 Jun 17:00", etc.
Individual tip post times may appear as "2h ago" on detail pages.
"""

import re
from datetime import datetime, timedelta, timezone

_MONTHS = {
    "jan": 1,
    "feb": 2,
    "mar": 3,
    "apr": 4,
    "may": 5,
    "jun": 6,
    "jul": 7,
    "aug": 8,
    "sep": 9,
    "oct": 10,
    "nov": 11,
    "dec": 12,
}


def parse_olbg_timestamp(text: str, reference: datetime | None = None) -> datetime | None:
    """Parse OLBG absolute or relative timestamp strings into UTC."""
    ref = reference or datetime.now(timezone.utc)
    cleaned = text.strip()
    if not cleaned:
        return None

    iso_attr = cleaned
    if "T" in iso_attr or re.match(r"\d{4}-\d{2}-\d{2}", iso_attr):
        try:
            return datetime.fromisoformat(iso_attr.replace("Z", "+00:00")).astimezone(timezone.utc)
        except ValueError:
            pass

    lowered = cleaned.lower()
    relative = re.match(r"(\d+)\s*(m|min|mins|minute|minutes|h|hr|hrs|hour|hours|d|day|days)\s*ago", lowered)
    if relative:
        amount = int(relative.group(1))
        unit = relative.group(2)[0]
        if unit == "m":
            delta = timedelta(minutes=amount)
        elif unit == "h":
            delta = timedelta(hours=amount)
        else:
            delta = timedelta(days=amount)
        return ref - delta

    clock = re.search(r"(\d{1,2}):(\d{2})", cleaned)
    if not clock:
        return None
    hour, minute = int(clock.group(1)), int(clock.group(2))

    if lowered.startswith("today"):
        base = ref.date()
    elif lowered.startswith("tomorrow"):
        base = (ref + timedelta(days=1)).date()
    else:
        date_match = re.search(r"(\d{1,2})\s+([A-Za-z]{3,9})(?:\s+(\d{4}))?", cleaned)
        if not date_match:
            return None
        day = int(date_match.group(1))
        month_key = date_match.group(2)[:3].lower()
        month = _MONTHS.get(month_key)
        if month is None:
            return None
        year = int(date_match.group(3)) if date_match.group(3) else ref.year
        # Regex captures (\d{1,2} for day, \d{1,2}:\d{2} for hour/minute) are
        # purely shape-matched, not range-validated — "31 Feb", "99:99" etc.
        # all match the pattern but raise ValueError out of datetime().
        # A single malformed timestamp on the listing page must not take
        # down the whole scrape run, so treat construction failure the same
        # as "couldn't parse" rather than letting it propagate.
        try:
            base = datetime(year, month, day).date()
        except ValueError:
            return None

    try:
        local = datetime(base.year, base.month, base.day, hour, minute, tzinfo=timezone.utc)
    except ValueError:
        return None
    return local
