"""
Unit tests for utils/time_parse.py.

Run with:
    pytest tests/test_time_parse.py -v
or standalone:
    python tests/test_time_parse.py
"""

import os
import sys
from datetime import datetime, timezone

# Same dummy-env trick as test_cleanup_isolation.py, in case config.py
# (imported transitively) requires these at import time.
os.environ.setdefault("PROXY_HOST", "fake-host")
os.environ.setdefault("PROXY_PORT", "1234")
os.environ.setdefault("PROXY_USERNAME", "fake-user")
os.environ.setdefault("PROXY_PASSWORD", "fake-pass")

from utils.time_parse import parse_olbg_timestamp  # noqa: E402

REF = datetime(2026, 6, 21, 11, 30, tzinfo=timezone.utc)  # fixed reference instant


def test_today_with_clock():
    result = parse_olbg_timestamp("Today 20:00", reference=REF)
    assert result == datetime(2026, 6, 21, 20, 0, tzinfo=timezone.utc)


def test_tomorrow_with_clock():
    result = parse_olbg_timestamp("Tomorrow 02:00", reference=REF)
    assert result == datetime(2026, 6, 22, 2, 0, tzinfo=timezone.utc)


def test_explicit_date_no_year():
    result = parse_olbg_timestamp("15 Jun 17:00", reference=REF)
    assert result == datetime(2026, 6, 15, 17, 0, tzinfo=timezone.utc)


def test_explicit_date_with_year():
    result = parse_olbg_timestamp("15 Jun 2027 17:00", reference=REF)
    assert result == datetime(2027, 6, 15, 17, 0, tzinfo=timezone.utc)


def test_relative_minutes_ago():
    result = parse_olbg_timestamp("45 mins ago", reference=REF)
    assert result == datetime(2026, 6, 21, 10, 45, tzinfo=timezone.utc)


def test_relative_hours_ago():
    result = parse_olbg_timestamp("2h ago", reference=REF)
    assert result == datetime(2026, 6, 21, 9, 30, tzinfo=timezone.utc)


def test_relative_days_ago():
    result = parse_olbg_timestamp("3 days ago", reference=REF)
    assert result == datetime(2026, 6, 18, 11, 30, tzinfo=timezone.utc)


def test_iso_format():
    result = parse_olbg_timestamp("2026-06-21T15:00:00Z", reference=REF)
    assert result == datetime(2026, 6, 21, 15, 0, tzinfo=timezone.utc)


def test_empty_string_returns_none():
    assert parse_olbg_timestamp("", reference=REF) is None


def test_whitespace_only_returns_none():
    assert parse_olbg_timestamp("   ", reference=REF) is None


def test_unparseable_text_returns_none():
    assert parse_olbg_timestamp("garbage text no date here", reference=REF) is None


def test_unknown_month_returns_none():
    assert parse_olbg_timestamp("15 Zzz 17:00", reference=REF) is None


# --- Regression tests for the crash fix: shape-valid but range-invalid ---
# values used to raise ValueError uncaught out of datetime(); they must now
# degrade to None like any other unparseable input.

def test_invalid_day_does_not_raise():
    assert parse_olbg_timestamp("31 Feb 17:00", reference=REF) is None


def test_invalid_hour_does_not_raise():
    assert parse_olbg_timestamp("99:99", reference=REF) is None


def test_invalid_hour_with_explicit_date_does_not_raise():
    assert parse_olbg_timestamp("Today 25:99", reference=REF) is None


def test_day_zero_does_not_raise():
    assert parse_olbg_timestamp("0 Jun 17:00", reference=REF) is None


def test_day_32_does_not_raise():
    assert parse_olbg_timestamp("32 Jun 17:00", reference=REF) is None


def _run_all():
    tests = [obj for name, obj in globals().items() if name.startswith("test_") and callable(obj)]
    failures = 0
    for test in tests:
        try:
            test()
            print(f"PASS  {test.__name__}")
        except AssertionError as exc:
            failures += 1
            print(f"FAIL  {test.__name__}: {exc}")
        except Exception as exc:  # noqa: BLE001
            failures += 1
            print(f"ERROR {test.__name__}: {type(exc).__name__}: {exc}")
    print(f"\n{len(tests) - failures}/{len(tests)} passed")
    if failures:
        sys.exit(1)


if __name__ == "__main__":
    _run_all()
