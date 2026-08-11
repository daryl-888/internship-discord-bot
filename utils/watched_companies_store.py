"""Watched-company management for the Workday fast lane.

Unlike sources.json (managed at runtime via /add_source etc.), this file is
meant to be hand-edited on the host and picked up on the next container
restart — same deploy model as config.json. See
docs/superpowers/specs/2026-08-11-nvidia-fast-lane-design.md.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, List

ROOT_DIR = Path(__file__).resolve().parents[1]
WATCHED_COMPANIES_PATH = ROOT_DIR / "watched_companies.json"


def load_watched_companies() -> List[Dict[str, Any]]:
    if not WATCHED_COMPANIES_PATH.exists():
        return []
    with WATCHED_COMPANIES_PATH.open("r", encoding="utf-8") as f:
        data = json.load(f)
    return data if isinstance(data, list) else []


def get_enabled_watched_companies() -> List[Dict[str, Any]]:
    return [company for company in load_watched_companies() if company.get("enabled", True)]