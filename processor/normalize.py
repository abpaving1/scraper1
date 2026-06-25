"""
Normalises team names so the same real-world fixture scraped from different
sources (which spell team names differently — "Arsenal" vs "Arsenal FC" vs
"Arsenal Football Club") resolves to the same `fixtures` row instead of
silently creating duplicates.

This is intentionally conservative: it strips common suffixes/punctuation and
collapses whitespace/case, but does NOT attempt fuzzy/phonetic matching
(Levenshtein, etc). A false NEGATIVE (two spellings of the same team treated
as different) just creates a duplicate fixture row, which is recoverable
later with a backfill script. A false POSITIVE (two different teams merged
into one fixture) corrupts data silently and is much worse. Bias accordingly
if you extend this.
"""

import re
import unicodedata

# Suffixes stripped only when they appear as a trailing whole word, so we
# don't mangle a team whose actual name contains one of these as substring
# (e.g. a hypothetical "Real Union" should not lose "Union").
_SUFFIXES = (
    "fc",
    "cf",
    "afc",
    "sc",
    "ac",
    "football club",
    "club de futbol",
)

_SUFFIX_RE = re.compile(r"\b(?:" + "|".join(re.escape(s) for s in _SUFFIXES) + r")\b\.?\s*$")
_NON_ALNUM_RE = re.compile(r"[^a-z0-9\s]")
_WHITESPACE_RE = re.compile(r"\s+")


def normalize_team_name(raw_name: str) -> str:
    """
    'Arsenal FC'        -> 'arsenal'
    'Arsenal Football Club' -> 'arsenal'
    'AFC Bournemouth'    -> 'bournemouth' is NOT handled (leading AFC) —
        see note below; trailing-only suffix stripping is deliberate.
    'Atlético Madrid'    -> 'atletico madrid'
    """
    if not raw_name:
        return ""

    # Strip accents (Atlético -> Atletico) before lowercasing/comparison so
    # accented and unaccented spellings of the same name match.
    decomposed = unicodedata.normalize("NFKD", raw_name)
    ascii_only = "".join(ch for ch in decomposed if not unicodedata.combining(ch))

    lowered = ascii_only.lower().strip()
    lowered = _SUFFIX_RE.sub("", lowered).strip()
    no_punct = _NON_ALNUM_RE.sub(" ", lowered)
    collapsed = _WHITESPACE_RE.sub(" ", no_punct).strip()
    return collapsed


def fixture_dedup_key(home_team_name: str, away_team_name: str) -> tuple[str, str]:
    """Returns the (normalized_home, normalized_away) pair used for matching
    against the partial unique index in fixtures. Order matters — home/away
    are not interchangeable, deliberately not sorted/canonicalised, since a
    reversed-fixture false-merge (treating "Arsenal v Chelsea" as the same
    fixture as "Chelsea v Arsenal" on a different date) would be wrong."""
    return normalize_team_name(home_team_name), normalize_team_name(away_team_name)
