"""Keyword and posting-age filtering helpers."""

from __future__ import annotations

import re
from typing import Dict, Iterable, List, Optional

# Matches the relative "Age"/"Date Posted" column used by these READMEs, e.g.
# "0d", "3d", "2w", "1mo", "1yr".
_AGE_RE = re.compile(r"(\d+)\s*(h|d|w|mo|y|yr)", re.IGNORECASE)
_UNIT_TO_DAYS = {"h": 1 / 24, "d": 1, "w": 7, "mo": 30, "y": 365, "yr": 365}


def parse_age_in_days(age_text: str) -> Optional[float]:
    """Parse a relative age string like "3d" or "1mo" into a day count.

    Returns None if the text doesn't look like a relative age (so callers can
    choose to let unparseable postings through rather than dropping them).
    """
    if not age_text:
        return None
    match = _AGE_RE.match(age_text.strip())
    if not match:
        return None
    value, unit = match.groups()
    return float(value) * _UNIT_TO_DAYS[unit.lower()]


def passes_age_filter(internship: Dict, max_age_days: Optional[int]) -> bool:
    """Return True if the posting is within max_age_days old.

    Postings whose age we can't parse are let through — we'd rather show a
    listing with unknown age than silently drop a legitimate one because its
    source used a format we don't recognize.
    """
    if not max_age_days or max_age_days <= 0:
        return True
    age_days = parse_age_in_days(internship.get("uploaded_at", ""))
    if age_days is None:
        return True
    return age_days <= max_age_days


def _contains_any(text: str, keywords: Iterable[str]) -> bool:
    lower = text.lower()
    return any(keyword.strip().lower() in lower for keyword in keywords if keyword.strip())


def internship_to_search_text(internship: Dict) -> str:
    parts: List[str] = [
        internship.get("company", ""),
        internship.get("title", ""),
        internship.get("location", ""),
        internship.get("application_url", ""),
        internship.get("source_url", ""),
        " ".join(internship.get("tags", [])),
    ]
    return " ".join(parts)


def passes_filters(internship: Dict, include_keywords: List[str], exclude_keywords: List[str]) -> bool:
    """Return True if the internship should be stored/posted."""
    text = internship_to_search_text(internship)

    if exclude_keywords and _contains_any(text, exclude_keywords):
        return False

    # Empty include list means include everything.
    if include_keywords and not _contains_any(text, include_keywords):
        return False

    return True
