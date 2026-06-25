"""
Unit tests for sources/olbg.py's embedded-JSON tip extraction.

Run with:
    pytest tests/test_olbg_parsing.py -v
or standalone:
    python tests/test_olbg_parsing.py
"""

import os
import sys

os.environ.setdefault("PROXY_HOST", "fake-host")
os.environ.setdefault("PROXY_PORT", "1234")
os.environ.setdefault("PROXY_USERNAME", "fake-user")
os.environ.setdefault("PROXY_PASSWORD", "fake-pass")

from sources.olbg import OLBGScraper  # noqa: E402

CANONICAL_TIP = (
    '{id:"99",tip_hash:"abc123",selection:"Home Win",outcome_name:"Home",'
    'market_name:"Match Result",market_alias:"1x2",eventname:"Arsenal v Chelsea",'
    'odds:"2.10",event_start:"2026-06-21 15:00:00",menu_league:"Premier League",'
    'confidence:78,win_tips:12,win_tips_count:15,comments_count:4}'
)

# Same fields, different key order — this is the exact case the old
# position-anchored regex could not survive.
REORDERED_TIP = (
    '{market_alias:"1x2",id:"99",tip_hash:"abc123",selection:"Home Win",'
    'outcome_name:"Home",market_name:"Match Result",eventname:"Arsenal v Chelsea",'
    'odds:"2.10",event_start:"2026-06-21 15:00:00",menu_league:"Premier League",'
    'confidence:78,win_tips:12,win_tips_count:15,comments_count:4}'
)


def test_canonical_order_parses():
    tips = OLBGScraper.parse_embedded_tips(f"junk before {CANONICAL_TIP} junk after")
    assert len(tips) == 1
    assert tips[0]["tip_hash"] == "abc123"
    assert tips[0]["confidence"] == 78
    assert tips[0]["win_tips"] == 12


def test_reordered_fields_still_parse():
    tips = OLBGScraper.parse_embedded_tips(f"junk before {REORDERED_TIP} junk after")
    assert len(tips) == 1
    assert tips[0]["tip_hash"] == "abc123"
    assert tips[0]["eventname"] == "Arsenal v Chelsea"


def test_multiple_tips_in_page():
    second_tip = CANONICAL_TIP.replace("abc123", "def456").replace('"99"', '"100"')
    html = f"{CANONICAL_TIP} some separator text {second_tip}"
    tips = OLBGScraper.parse_embedded_tips(html)
    assert {t["tip_hash"] for t in tips} == {"abc123", "def456"}


def test_tip_missing_required_field_is_skipped_not_fatal():
    # Drop eventname entirely — object is well-formed JSON-ish but incomplete.
    broken = (
        '{id:"1",tip_hash:"missing_eventname",selection:"Home Win",outcome_name:"Home",'
        'market_name:"Match Result",market_alias:"1x2",odds:"2.10",'
        'event_start:"2026-06-21 15:00:00",menu_league:"Premier League",confidence:50}'
    )
    html = f"{broken} {CANONICAL_TIP}"
    tips = OLBGScraper.parse_embedded_tips(html)
    # The malformed tip is skipped, but the good one alongside it still parses —
    # this is the "one bad tip doesn't zero out the page" behaviour.
    assert len(tips) == 1
    assert tips[0]["tip_hash"] == "abc123"


def test_tip_missing_confidence_is_skipped():
    broken = CANONICAL_TIP.replace("confidence:78,", "")
    tips = OLBGScraper.parse_embedded_tips(broken)
    assert tips == []


def test_unterminated_object_is_skipped_not_fatal():
    # tip_hash present but no closing brace anywhere after it.
    html = 'prefix {id:"1",tip_hash:"dangling",selection:"X" no closing brace here'
    tips = OLBGScraper.parse_embedded_tips(html)
    assert tips == []


def test_no_tips_in_page_returns_empty_list():
    assert OLBGScraper.parse_embedded_tips("<html><body>nothing here</body></html>") == []


def test_optional_count_fields_default_to_none_when_absent():
    no_counts = (
        '{id:"1",tip_hash:"nocounts",selection:"Home Win",outcome_name:"Home",'
        'market_name:"Match Result",market_alias:"1x2",eventname:"Arsenal v Chelsea",'
        'odds:"2.10",event_start:"2026-06-21 15:00:00",menu_league:"Premier League",'
        "confidence:60}"
    )
    tips = OLBGScraper.parse_embedded_tips(no_counts)
    assert len(tips) == 1
    assert tips[0]["win_tips"] is None
    assert tips[0]["win_tips_count"] is None
    assert tips[0]["comments_count"] is None


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
