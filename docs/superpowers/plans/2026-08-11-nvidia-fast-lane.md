# NVIDIA / Watched-Company Fast Lane Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a fast (10-minute), Workday-based scraper for watched companies (NVIDIA first) that DMs an admin-managed explicit list of members the instant a new internship posting appears — separate from the existing 6-hour GitHub-README scan and the existing premium-role personal digest.

**Architecture:** A generic `scraper/workday_scraper.py` hits the same public JSON search API a Workday careers site's own page uses, filtered to a company's "intern" facet. A new `watched_companies.json` (hand-edited, mirrors `config.json`'s bind-mount-and-restart deploy model) lists companies to watch. `scanner.py` gains a second entry point, `run_watched_company_scan()`, sharing its filter/dedupe/LLM-scoring logic with the existing `run_scan()` via an extracted helper. `bot.py` runs this on its own 10-minute task loop (vs. the existing 6-hour one), and DMs everyone in a new `fast_lane_subscribers` DB table — populated by admin-only `/fast_lane_add` / `/fast_lane_remove` commands, not a Discord role.

**Tech Stack:** Python 3.12, discord.py (`discord.ext.tasks`, `discord.app_commands`), `requests`, SQLite (stdlib `sqlite3`), pytest + `unittest.mock`.

## Global Constraints

- Spec: `docs/superpowers/specs/2026-08-11-nvidia-fast-lane-design.md` — read it for full rationale.
- Admin-only commands are gated exactly like the existing `/set_premium_role`: `@app_commands.default_permissions(manage_guild=True)` + `@app_commands.guild_only()`. Do not invent a different permission check.
- The fast lane has **no LLM scoring and no `/set_profile` dependency** — plain DMs to an explicit subscriber list. Do not route it through `score_personal_match`/`classify_relevance`'s personalization path.
- Do **not** trust Workday's `total` response field for pagination termination — it was verified live (2026-08-11) to be unreliable/inconsistent at higher offsets on NVIDIA's tenant (returned `0` at one offset and non-empty postings, and never returned an empty page even at absurd offsets on another facet). Terminate pagination on an empty or short (`< PAGE_SIZE`) page, with a hard `MAX_PAGES` cap as a circuit breaker.
- Every new SQL-backed helper mirrors the existing `member_profiles` function style in `database/db.py` (`init_db()` call first, context-managed `_connect()`, plain str/list returns).
- `watched_companies.json` (like `config.json` and `sources.json`) is gitignored; only `watched_companies.example.json` is committed.
- Run `pytest` from `~/docker/internship-discord-bot` (repo root) for every test step below — `pytest.ini` already points it at `tests/`.

---

### Task 1: Generic Workday scraper

**Files:**
- Create: `scraper/workday_scraper.py`
- Test: `tests/test_workday_scraper.py`

**Interfaces:**
- Consumes: `scraper.github_scraper.infer_tags(text: str) -> List[str]` (existing), `utils.tags.add_company_classification_tag(tags, company) -> List[str]` (existing).
- Produces: `scrape_workday_intern_jobs(company_config: Dict) -> ScrapeResult` where `ScrapeResult` is a dataclass with field `internships: List[Dict]`. Also produces `normalize_posted_on(posted_on: str) -> str` (public, used directly by tests). `company_config` keys consumed: `name`, `tenant_host`, `site`, `intern_facet_id`.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_workday_scraper.py
"""Tests for the generic Workday intern-job scraper."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from scraper import workday_scraper as ws

COMPANY = {
    "id": "nvidia",
    "name": "NVIDIA",
    "ats": "workday",
    "tenant_host": "nvidia.wd5.myworkdayjobs.com",
    "site": "NVIDIAExternalCareerSite",
    "intern_facet_id": "0c40f6bd1d8f10adf6dae42e46d44a17",
    "enabled": True,
}


def _posting(title="SWE Intern, Fall 2026", posted_on="Posted Today", external_path="/job/US-CA/SWE-Intern_JR1"):
    return {
        "title": title,
        "externalPath": external_path,
        "locationsText": "US, CA, Santa Clara",
        "postedOn": posted_on,
        "bulletFields": ["JR1"],
    }


def _response(job_postings, total=None):
    resp = MagicMock()
    resp.json.return_value = {
        "total": total if total is not None else len(job_postings),
        "jobPostings": job_postings,
    }
    resp.raise_for_status = MagicMock()
    return resp


def test_normalize_posted_on_today():
    assert ws.normalize_posted_on("Posted Today") == "0d"


def test_normalize_posted_on_yesterday():
    assert ws.normalize_posted_on("Posted Yesterday") == "1d"


def test_normalize_posted_on_n_days_ago():
    assert ws.normalize_posted_on("Posted 5 Days Ago") == "5d"


def test_normalize_posted_on_thirty_plus_days_ago():
    assert ws.normalize_posted_on("Posted 30+ Days Ago") == "30d"


def test_normalize_posted_on_passes_through_unrecognized_text():
    assert ws.normalize_posted_on("Some Other Format") == "Some Other Format"


def test_scrape_workday_intern_jobs_builds_normalized_internships():
    posting = _posting()
    with patch.object(ws.requests, "post", return_value=_response([posting], total=1)):
        result = ws.scrape_workday_intern_jobs(COMPANY)

    assert len(result.internships) == 1
    job = result.internships[0]
    assert job["company"] == "NVIDIA"
    assert job["title"] == "SWE Intern, Fall 2026"
    assert job["location"] == "US, CA, Santa Clara"
    assert job["application_url"] == (
        "https://nvidia.wd5.myworkdayjobs.com/NVIDIAExternalCareerSite/job/US-CA/SWE-Intern_JR1"
    )
    assert job["source_url"] == "https://nvidia.wd5.myworkdayjobs.com/NVIDIAExternalCareerSite"
    assert job["source_type"] == "workday"
    assert job["uploaded_at"] == "0d"
    assert "internship" in job["tags"]
    assert ("faang" in job["tags"]) != ("non-faang" in job["tags"])
    assert job["status"] == "unknown"


def test_scrape_workday_intern_jobs_sends_intern_facet_and_empty_search_text():
    with patch.object(ws.requests, "post", return_value=_response([])) as mock_post:
        ws.scrape_workday_intern_jobs(COMPANY)

    call = mock_post.call_args
    assert call.args[0] == "https://nvidia.wd5.myworkdayjobs.com/wday/cxs/nvidia/NVIDIAExternalCareerSite/jobs"
    assert call.kwargs["json"]["appliedFacets"] == {"workerSubType": ["0c40f6bd1d8f10adf6dae42e46d44a17"]}
    assert call.kwargs["json"]["searchText"] == ""
    assert call.kwargs["json"]["offset"] == 0


def test_scrape_workday_intern_jobs_paginates_until_a_short_page():
    full_page = [_posting(external_path=f"/job/JR{i}") for i in range(ws.PAGE_SIZE)]
    short_page = [_posting(external_path="/job/JR-last")]

    with patch.object(ws.requests, "post", side_effect=[_response(full_page), _response(short_page)]) as mock_post:
        result = ws.scrape_workday_intern_jobs(COMPANY)

    assert mock_post.call_count == 2
    assert mock_post.call_args_list[1].kwargs["json"]["offset"] == ws.PAGE_SIZE
    assert len(result.internships) == ws.PAGE_SIZE + 1


def test_scrape_workday_intern_jobs_stops_after_max_pages_regardless_of_full_pages():
    # Guards against the observed Workday quirk where a page can come back
    # full (== PAGE_SIZE) forever even past the real result count.
    full_page = [_posting(external_path="/job/JR-repeat")] * ws.PAGE_SIZE

    with patch.object(ws.requests, "post", return_value=_response(full_page)) as mock_post:
        result = ws.scrape_workday_intern_jobs(COMPANY)

    assert mock_post.call_count == ws.MAX_PAGES
    assert len(result.internships) == ws.MAX_PAGES * ws.PAGE_SIZE


def test_scrape_workday_intern_jobs_empty_first_page_returns_no_internships():
    with patch.object(ws.requests, "post", return_value=_response([])) as mock_post:
        result = ws.scrape_workday_intern_jobs(COMPANY)

    assert result.internships == []
    assert mock_post.call_count == 1


def test_scrape_workday_intern_jobs_raises_on_http_error():
    error_response = MagicMock()
    error_response.raise_for_status.side_effect = ws.requests.HTTPError("500")

    with patch.object(ws.requests, "post", return_value=error_response):
        try:
            ws.scrape_workday_intern_jobs(COMPANY)
            assert False, "expected HTTPError to propagate"
        except ws.requests.HTTPError:
            pass
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd ~/docker/internship-discord-bot && pytest tests/test_workday_scraper.py -v`
Expected: FAIL (or collection error) — `scraper/workday_scraper.py` does not exist yet.

- [ ] **Step 3: Write the implementation**

```python
# scraper/workday_scraper.py
"""Generic Workday internship scraper.

Hits the same public JSON search endpoint a Workday-hosted careers site's own
page JS calls (no login, no bypassing rate limits — the same request class as
scraper/github_scraper.py's raw README fetch), filtered to a company's
"intern" workerSubType facet, and normalizes results into the same shape
github_scraper.py produces so they flow through the rest of the pipeline
unchanged.

Verified live against NVIDIA's tenant on 2026-08-11: the "total" field in the
response is NOT trustworthy for pagination — it returned 0 at one offset with
a non-empty jobPostings page, and a different (non-intern) facet kept
returning full PAGE_SIZE pages indefinitely even at offsets far beyond any
real result count. Pagination here terminates on an empty/short page with a
MAX_PAGES hard cap as a circuit breaker, and never reads "total" at all.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Dict, List

import requests

from scraper.github_scraper import infer_tags
from utils.tags import add_company_classification_tag

REQUEST_HEADERS = {
    "User-Agent": "local-discord-internship-bot/1.0 (+https://github.com/)",
    "Content-Type": "application/json",
}
PAGE_SIZE = 20
MAX_PAGES = 10


@dataclass
class ScrapeResult:
    internships: List[Dict]


def scrape_workday_intern_jobs(company_config: Dict) -> ScrapeResult:
    """Fetch every posting matching company_config['intern_facet_id'] from a
    Workday tenant's public job-search API, across up to MAX_PAGES pages."""
    tenant_host = company_config["tenant_host"]
    site = company_config["site"]
    facet_id = company_config["intern_facet_id"]
    company_name = company_config["name"]
    careers_url = f"https://{tenant_host}/{site}"
    tenant_slug = tenant_host.split(".")[0]
    endpoint = f"https://{tenant_host}/wday/cxs/{tenant_slug}/{site}/jobs"

    internships: List[Dict] = []
    offset = 0
    for _ in range(MAX_PAGES):
        payload = {
            "appliedFacets": {"workerSubType": [facet_id]},
            "limit": PAGE_SIZE,
            "offset": offset,
            "searchText": "",
        }
        response = requests.post(endpoint, json=payload, headers=REQUEST_HEADERS, timeout=20)
        response.raise_for_status()
        postings = response.json().get("jobPostings", [])

        for posting in postings:
            internships.append(_build_internship(posting, company_name, careers_url))

        if len(postings) < PAGE_SIZE:
            break
        offset += PAGE_SIZE

    return ScrapeResult(internships=internships)


def _build_internship(posting: Dict, company_name: str, careers_url: str) -> Dict:
    title = (posting.get("title") or "").strip()
    location = (posting.get("locationsText") or "").strip() or "Unknown"
    application_url = careers_url + (posting.get("externalPath") or "")

    return {
        "company": company_name,
        "title": title,
        "location": location,
        "application_url": application_url,
        "source_url": careers_url,
        "source_type": "workday",
        "uploaded_at": normalize_posted_on(posting.get("postedOn", "")),
        "tags": add_company_classification_tag(infer_tags(f"{company_name} {title}"), company_name),
        "status": "unknown",
    }


_TODAY_RE = re.compile(r"posted\s+today", re.IGNORECASE)
_YESTERDAY_RE = re.compile(r"posted\s+yesterday", re.IGNORECASE)
_N_DAYS_RE = re.compile(r"posted\s+(\d+)\+?\s+days?\s+ago", re.IGNORECASE)


def normalize_posted_on(posted_on: str) -> str:
    """Convert Workday's bucketed relative-date text into the compact "Nd"
    format utils.filters.parse_age_in_days() already understands.

    Unrecognized text passes through unchanged — parse_age_in_days() already
    treats unparseable age text as "let it through" rather than dropping the
    posting, so there's no need to special-case that here too.
    """
    text = (posted_on or "").strip()
    if _TODAY_RE.search(text):
        return "0d"
    if _YESTERDAY_RE.search(text):
        return "1d"
    match = _N_DAYS_RE.search(text)
    if match:
        return f"{match.group(1)}d"
    return text
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd ~/docker/internship-discord-bot && pytest tests/test_workday_scraper.py -v`
Expected: PASS (10 tests)

- [ ] **Step 5: Commit**

```bash
cd ~/docker/internship-discord-bot
git add scraper/workday_scraper.py tests/test_workday_scraper.py
git commit -m "Add generic Workday intern-job scraper"
```

---

### Task 2: Watched-companies config store

**Files:**
- Create: `utils/watched_companies_store.py`
- Create: `watched_companies.example.json`
- Create: `watched_companies.json` (real, gitignored — seeded with NVIDIA)
- Modify: `.gitignore`
- Test: `tests/test_watched_companies_store.py`

**Interfaces:**
- Consumes: nothing new.
- Produces: `load_watched_companies() -> List[Dict[str, Any]]`, `get_enabled_watched_companies() -> List[Dict[str, Any]]`, module attribute `WATCHED_COMPANIES_PATH: Path` (tests monkeypatch this, same pattern as `database.db.DB_PATH`).

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_watched_companies_store.py
"""Tests for utils/watched_companies_store.py."""

from __future__ import annotations

import json

import pytest

from utils import watched_companies_store as store


@pytest.fixture(autouse=True)
def _isolated_watched_companies(tmp_path, monkeypatch):
    monkeypatch.setattr(store, "WATCHED_COMPANIES_PATH", tmp_path / "watched_companies.json")


def _write(companies):
    store.WATCHED_COMPANIES_PATH.write_text(json.dumps(companies))


def test_load_watched_companies_returns_empty_list_when_file_missing():
    assert store.load_watched_companies() == []


def test_load_watched_companies_reads_saved_entries():
    _write([{"id": "nvidia", "name": "NVIDIA", "enabled": True}])
    assert store.load_watched_companies() == [{"id": "nvidia", "name": "NVIDIA", "enabled": True}]


def test_get_enabled_watched_companies_filters_disabled():
    _write([
        {"id": "a", "name": "A", "enabled": True},
        {"id": "b", "name": "B", "enabled": False},
    ])
    assert [c["id"] for c in store.get_enabled_watched_companies()] == ["a"]


def test_get_enabled_watched_companies_defaults_missing_enabled_field_to_true():
    _write([{"id": "a", "name": "A"}])
    assert [c["id"] for c in store.get_enabled_watched_companies()] == ["a"]


def test_load_watched_companies_returns_empty_list_for_non_list_json():
    _write({"not": "a list"})
    assert store.load_watched_companies() == []
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd ~/docker/internship-discord-bot && pytest tests/test_watched_companies_store.py -v`
Expected: FAIL — `utils/watched_companies_store.py` does not exist yet.

- [ ] **Step 3: Write the implementation**

```python
# utils/watched_companies_store.py
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
```

```json
// watched_companies.example.json
[]
```

```json
// watched_companies.json (real file — this one is gitignored, do not force-add it)
[
  {
    "id": "nvidia",
    "name": "NVIDIA",
    "ats": "workday",
    "tenant_host": "nvidia.wd5.myworkdayjobs.com",
    "site": "NVIDIAExternalCareerSite",
    "intern_facet_id": "0c40f6bd1d8f10adf6dae42e46d44a17",
    "enabled": true
  }
]
```

Add one line to `.gitignore` (it currently lists `.env`, `internships.db`, `config.json`, `sources.json`, `__pycache__/`, `*.pyc`, `.venv/`, `venv/`, `instance/`):

```
watched_companies.json
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd ~/docker/internship-discord-bot && pytest tests/test_watched_companies_store.py -v`
Expected: PASS (5 tests)

- [ ] **Step 5: Commit**

```bash
cd ~/docker/internship-discord-bot
git add utils/watched_companies_store.py watched_companies.example.json .gitignore tests/test_watched_companies_store.py
git commit -m "Add watched-companies config store (hand-edited JSON, gitignored)"
```

(`watched_companies.json` itself is intentionally not staged — it's gitignored, same as `config.json`/`sources.json`.)

---

### Task 3: `fast_lane_subscribers` DB table

**Files:**
- Modify: `database/db.py`
- Test: `tests/test_db.py`

**Interfaces:**
- Consumes: existing `_connect()`, `init_db()`, `now_iso()` from this same file.
- Produces: `add_fast_lane_subscriber(user_id: str) -> None`, `remove_fast_lane_subscriber(user_id: str) -> bool`, `list_fast_lane_subscribers() -> List[str]` (ordered oldest-added-first).

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_db.py`:

```python
def test_add_fast_lane_subscriber_then_list():
    db.add_fast_lane_subscriber("111")
    db.add_fast_lane_subscriber("222")
    assert db.list_fast_lane_subscribers() == ["111", "222"]


def test_add_fast_lane_subscriber_is_idempotent():
    db.add_fast_lane_subscriber("111")
    db.add_fast_lane_subscriber("111")
    assert db.list_fast_lane_subscribers() == ["111"]


def test_remove_fast_lane_subscriber_returns_true_when_removed():
    db.add_fast_lane_subscriber("111")
    assert db.remove_fast_lane_subscriber("111") is True
    assert db.list_fast_lane_subscribers() == []


def test_remove_fast_lane_subscriber_returns_false_when_absent():
    assert db.remove_fast_lane_subscriber("999") is False


def test_list_fast_lane_subscribers_empty_by_default():
    assert db.list_fast_lane_subscribers() == []
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd ~/docker/internship-discord-bot && pytest tests/test_db.py -v -k fast_lane`
Expected: FAIL — `add_fast_lane_subscriber` etc. don't exist yet (`AttributeError`).

- [ ] **Step 3: Write the implementation**

In `database/db.py`, add the new table to `init_db()` — insert this block right after the existing `member_profiles` `CREATE TABLE IF NOT EXISTS` block (after line 126, before the `_ensure_column(...)` calls):

```python
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS fast_lane_subscribers (
                user_id TEXT PRIMARY KEY,
                added_at TEXT NOT NULL
            )
            """
        )
```

Then add these three functions near the end of the file, after `list_member_profiles()` (before `_PRESERVED_STATUSES`):

```python
def add_fast_lane_subscriber(user_id: str) -> None:
    """Admin-managed explicit list for the Workday fast lane — see
    /fast_lane_add. Not a Discord role, unlike premium_role_id."""
    init_db()
    with _connect() as conn:
        conn.execute(
            "INSERT INTO fast_lane_subscribers(user_id, added_at) VALUES (?, ?) "
            "ON CONFLICT(user_id) DO NOTHING",
            (str(user_id), now_iso()),
        )
        conn.commit()


def remove_fast_lane_subscriber(user_id: str) -> bool:
    init_db()
    with _connect() as conn:
        cursor = conn.execute(
            "DELETE FROM fast_lane_subscribers WHERE user_id = ?",
            (str(user_id),),
        )
        conn.commit()
        return cursor.rowcount > 0


def list_fast_lane_subscribers() -> List[str]:
    init_db()
    with _connect() as conn:
        rows = conn.execute(
            "SELECT user_id FROM fast_lane_subscribers ORDER BY added_at ASC"
        ).fetchall()
    return [row["user_id"] for row in rows]
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd ~/docker/internship-discord-bot && pytest tests/test_db.py -v`
Expected: PASS (all tests, including the 5 new ones)

- [ ] **Step 5: Commit**

```bash
cd ~/docker/internship-discord-bot
git add database/db.py tests/test_db.py
git commit -m "Add fast_lane_subscribers table and CRUD helpers"
```

---

### Task 4: `scanner.run_watched_company_scan`

**Files:**
- Modify: `scanner.py`
- Test: `tests/test_scanner.py`

**Interfaces:**
- Consumes: `scraper.workday_scraper.scrape_workday_intern_jobs(company_config) -> ScrapeResult` (Task 1), `utils.watched_companies_store.get_enabled_watched_companies() -> List[Dict]` (Task 2), existing `utils.filters.passes_filters`/`passes_age_filter`, existing `utils.relevance.classify_relevance`, existing `database.db.upsert_internship`/`update_internship_relevance`/`set_state`/`init_db`.
- Produces: `run_watched_company_scan(config: Dict[str, Any]) -> Dict[str, Any]` returning `{"companies_scanned", "total_found_before_filters", "total_found_after_filters", "new_jobs", "errors", "scan_time"}`. Also produces the extracted helper `_process_scraped_internships(internships: List[Dict], config: Dict) -> Tuple[List[Dict], int]` (returns `(new_jobs, count_after_filters)`), reused by both `run_scan` and `run_watched_company_scan`.

This task first refactors `run_scan` to extract shared filtering/dedupe/LLM-scoring logic into `_process_scraped_internships`, confirms existing tests still pass unchanged, then adds `run_watched_company_scan` on top of that shared helper.

- [ ] **Step 1: Extract `_process_scraped_internships` from `run_scan` (refactor, no behavior change)**

Replace the body of `scanner.py` (currently `run_scan` is the only function besides imports) with:

```python
"""Run all enabled internship sources and store new jobs in SQLite."""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, Dict, List, Tuple

from database.db import init_db, set_state, update_internship_relevance, upsert_internship
from scraper.github_scraper import scrape_github_readme
from scraper.workday_scraper import scrape_workday_intern_jobs
from utils.filters import passes_age_filter, passes_filters
from utils.relevance import classify_relevance
from utils.source_store import get_enabled_sources, update_source_fetch_cache
from utils.watched_companies_store import get_enabled_watched_companies

LOGGER = logging.getLogger(__name__)


def _process_scraped_internships(
    internships: List[Dict[str, Any]], config: Dict[str, Any]
) -> Tuple[List[Dict[str, Any]], int]:
    """Apply age/keyword filters, dedupe/store, and optional LLM relevance
    scoring to one source's already-scraped internships.

    Shared by run_scan (GitHub/manual sources) and run_watched_company_scan
    (Workday-based watched companies) so both cadences get identical
    filtering/dedupe/scoring behavior. Returns (new_jobs, count_after_filters).
    """
    include_keywords = config.get("include_keywords", [])
    exclude_keywords = config.get("exclude_keywords", [])
    max_posting_age_days = config.get("max_posting_age_days", 3)
    llm_filter_enabled = bool(config.get("llm_filter_enabled", False))
    llm_min_quality_score = int(config.get("llm_min_quality_score", 1))

    after_filters = 0
    new_jobs: List[Dict[str, Any]] = []

    for internship in internships:
        if not passes_filters(internship, include_keywords, exclude_keywords):
            continue
        if not passes_age_filter(internship, max_posting_age_days):
            continue
        after_filters += 1
        db_id, is_new = upsert_internship(internship)
        internship["id"] = db_id

        # Still store closed roles (keeps dedupe/dashboard accurate), but
        # don't post something to Discord that's already unavailable.
        if not is_new or internship.get("status") == "closed":
            continue

        if llm_filter_enabled:
            # Only spend an LLM call on postings that are actually new —
            # the whole point is to rank/trim what we're about to post.
            verdict = classify_relevance(internship, config)
            internship["quality_score"] = verdict.quality_score
            internship["llm_reason"] = verdict.reason
            update_internship_relevance(db_id, verdict.quality_score, verdict.reason)
            if not verdict.relevant or verdict.quality_score < llm_min_quality_score:
                continue

        new_jobs.append(internship)

    return new_jobs, after_filters


def run_scan(config: Dict[str, Any]) -> Dict[str, Any]:
    """Scan enabled sources.json entries (GitHub READMEs) and return a summary."""
    init_db()
    sources = get_enabled_sources()

    total_found = 0
    total_after_filters = 0
    new_jobs: List[Dict[str, Any]] = []
    errors: List[str] = []

    for source in sources:
        source_url = source.get("url", "")
        source_type = source.get("type", "github_readme")
        try:
            if source_type == "github_readme":
                result = scrape_github_readme(
                    source_url,
                    preferred_url=source.get("resolved_raw_url", ""),
                    etag=source.get("etag", ""),
                )
                update_source_fetch_cache(source.get("id", ""), result.raw_url, result.etag)
                internships = result.internships
            else:
                LOGGER.info("Skipping unsupported automated source type: %s", source_type)
                continue

            total_found += len(internships)
            source_new_jobs, after_filters = _process_scraped_internships(internships, config)
            total_after_filters += after_filters
            new_jobs.extend(source_new_jobs)

        except Exception as exc:  # Keep scanning even if one repo breaks.
            message = f"{source_url}: {exc}"
            LOGGER.exception("Source failed: %s", message)
            errors.append(message)

    scan_time = datetime.now(timezone.utc).isoformat()
    set_state("last_scan_time", scan_time)
    set_state("last_scan_found_count", str(total_after_filters))

    return {
        "sources_scanned": len(sources),
        "total_found_before_filters": total_found,
        "total_found_after_filters": total_after_filters,
        "new_jobs": new_jobs,
        "errors": errors,
        "scan_time": scan_time,
    }


def run_watched_company_scan(config: Dict[str, Any]) -> Dict[str, Any]:
    """Scan enabled watched_companies.json entries (Workday) and return a
    summary. Meant to run on a much shorter interval than run_scan — see
    bot.py's scheduled_watched_company_scan (fast_scan_interval_minutes)."""
    init_db()
    companies = get_enabled_watched_companies()

    total_found = 0
    total_after_filters = 0
    new_jobs: List[Dict[str, Any]] = []
    errors: List[str] = []

    for company in companies:
        company_name = company.get("name", "")
        try:
            result = scrape_workday_intern_jobs(company)
            internships = result.internships

            total_found += len(internships)
            source_new_jobs, after_filters = _process_scraped_internships(internships, config)
            total_after_filters += after_filters
            new_jobs.extend(source_new_jobs)

        except Exception as exc:  # Keep scanning other companies even if one fails.
            message = f"{company_name}: {exc}"
            LOGGER.exception("Watched-company scan failed: %s", message)
            errors.append(message)

    scan_time = datetime.now(timezone.utc).isoformat()
    set_state("last_watched_company_scan_time", scan_time)
    set_state("last_watched_company_scan_found_count", str(total_after_filters))

    return {
        "companies_scanned": len(companies),
        "total_found_before_filters": total_found,
        "total_found_after_filters": total_after_filters,
        "new_jobs": new_jobs,
        "errors": errors,
        "scan_time": scan_time,
    }
```

- [ ] **Step 2: Run the existing scanner tests to confirm the refactor didn't break anything**

Run: `cd ~/docker/internship-discord-bot && pytest tests/test_scanner.py -v`
Expected: PASS (all 5 existing tests, unchanged) — this confirms the extraction was behavior-preserving before adding new code on top.

- [ ] **Step 3: Write the failing tests for `run_watched_company_scan`**

Append to `tests/test_scanner.py`:

```python
from scraper.workday_scraper import ScrapeResult as WorkdayScrapeResult


def _watched_company():
    return [{
        "id": "nvidia",
        "name": "NVIDIA",
        "tenant_host": "nvidia.wd5.myworkdayjobs.com",
        "site": "NVIDIAExternalCareerSite",
        "intern_facet_id": "abc123",
    }]


def _run_watched_with_jobs(jobs, config):
    with patch("scanner.get_enabled_watched_companies", return_value=_watched_company()), \
         patch("scanner.scrape_workday_intern_jobs", return_value=WorkdayScrapeResult(internships=jobs)):
        return scanner.run_watched_company_scan(config)


def test_watched_company_scan_stores_and_returns_new_jobs():
    jobs = [_job(company="NVIDIA")]
    result = _run_watched_with_jobs(jobs, _base_config())

    assert [j["company"] for j in result["new_jobs"]] == ["NVIDIA"]
    assert result["companies_scanned"] == 1
    stored_companies = {row["company"] for row in list_internships(limit=10)}
    assert stored_companies == {"NVIDIA"}


def test_watched_company_scan_applies_same_keyword_filters_as_run_scan():
    jobs = [_job(company="NVIDIA", title="Senior SWE")]
    result = _run_watched_with_jobs(jobs, _base_config(exclude_keywords=["senior"]))
    assert result["new_jobs"] == []


def test_watched_company_scan_records_errors_without_stopping():
    with patch("scanner.get_enabled_watched_companies", return_value=_watched_company()), \
         patch("scanner.scrape_workday_intern_jobs", side_effect=RuntimeError("boom")):
        result = scanner.run_watched_company_scan(_base_config())

    assert result["companies_scanned"] == 1
    assert len(result["errors"]) == 1
    assert result["errors"][0] == "NVIDIA: boom"
    assert result["new_jobs"] == []


def test_watched_company_scan_runs_llm_filter_when_enabled():
    jobs = [_job(company="NVIDIA")]
    verdict = RelevanceResult(relevant=True, quality_score=5, reason="strong match", source="llm")

    with patch("scanner.classify_relevance", return_value=verdict):
        result = _run_watched_with_jobs(jobs, _base_config(llm_filter_enabled=True, llm_min_quality_score=1))

    assert result["new_jobs"][0]["quality_score"] == 5
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd ~/docker/internship-discord-bot && pytest tests/test_scanner.py -v`
Expected: PASS (all 9 tests: 5 original + 4 new)

- [ ] **Step 5: Commit**

```bash
cd ~/docker/internship-discord-bot
git add scanner.py tests/test_scanner.py
git commit -m "Add run_watched_company_scan, sharing filter/dedupe logic with run_scan"
```

---

### Task 5: Wire the fast lane into `bot.py`

**Files:**
- Modify: `bot.py`
- Modify: `utils/config_loader.py`
- Modify: `config.example.json`
- Modify: `docker-compose.yml`
- Test: `tests/test_bot.py`

**Interfaces:**
- Consumes: `scanner.run_watched_company_scan` (Task 4), `database.db.{add_fast_lane_subscriber, remove_fast_lane_subscriber, list_fast_lane_subscribers}` (Task 3), `utils.watched_companies_store.{load_watched_companies, get_enabled_watched_companies}` (Task 2), existing `bot.get_premium_guild()`, `bot.post_jobs_to_discord()`, `bot.send_premium_digests()`, `utils.formatting.{internship_to_embed, chunk_list}`.
- Produces: `send_fast_lane_alerts(new_jobs: List[dict], guild: discord.Guild) -> None`, `watched_company_scan_and_post() -> dict`, plus the `/fast_lane_add`, `/fast_lane_remove`, `/fast_lane_list` slash commands and a `scheduled_watched_company_scan` task loop.

- [ ] **Step 1: Add the `fast_scan_interval_minutes` config default**

In `utils/config_loader.py`, add one key to `DEFAULT_CONFIG` right after `"scan_interval_minutes": 360,`:

```python
    "scan_interval_minutes": 360,
    # Separate, much shorter cadence for watched_companies.json (Workday
    # fast lane) — see fast_lane_subscribers / /fast_lane_add. Independent
    # of scan_interval_minutes/auto_scan_enabled's GitHub-source cadence.
    "fast_scan_interval_minutes": 10,
```

Add the same key to `config.example.json` right after `"scan_interval_minutes": 240,`:

```json
  "scan_interval_minutes": 240,
  "fast_scan_interval_minutes": 10,
```

- [ ] **Step 2: Write the failing tests for `send_fast_lane_alerts`**

Append to `tests/test_bot.py`:

```python
def test_send_fast_lane_alerts_noop_when_no_new_jobs():
    guild = MagicMock()
    with patch.object(bot_module, "list_fast_lane_subscribers") as mock_list:
        _run(bot_module.send_fast_lane_alerts([], guild))
    mock_list.assert_not_called()


def test_send_fast_lane_alerts_noop_when_no_subscribers():
    guild = MagicMock()
    with patch.object(bot_module, "list_fast_lane_subscribers", return_value=[]):
        _run(bot_module.send_fast_lane_alerts([{"id": 1, "company": "NVIDIA"}], guild))
    guild.get_member.assert_not_called()


def test_send_fast_lane_alerts_dms_each_listed_subscriber():
    member = MagicMock()
    member.send = AsyncMock()
    guild = MagicMock()
    guild.get_member = lambda uid: member if uid == 111 else None

    with patch.object(bot_module, "list_fast_lane_subscribers", return_value=["111"]):
        _run(bot_module.send_fast_lane_alerts(
            [{"id": 1, "company": "NVIDIA", "title": "SWE Intern"}], guild
        ))

    member.send.assert_awaited_once()
    assert len(member.send.call_args.kwargs["embeds"]) == 1


def test_send_fast_lane_alerts_skips_forbidden_member_but_continues_others():
    class _Forbidden403:
        status = 403
        reason = "Forbidden"
        headers = {}

    blocked = MagicMock()
    blocked.send = AsyncMock(side_effect=discord.Forbidden(_Forbidden403(), "DMs closed"))
    ok = MagicMock()
    ok.send = AsyncMock()
    guild = MagicMock()
    guild.get_member = lambda uid: {111: blocked, 222: ok}.get(uid)

    with patch.object(bot_module, "list_fast_lane_subscribers", return_value=["111", "222"]):
        _run(bot_module.send_fast_lane_alerts([{"id": 1, "company": "NVIDIA"}], guild))

    blocked.send.assert_awaited_once()
    ok.send.assert_awaited_once()


def test_send_fast_lane_alerts_skips_subscriber_missing_from_guild_cache():
    guild = MagicMock()
    guild.get_member = lambda uid: None
    with patch.object(bot_module, "list_fast_lane_subscribers", return_value=["999"]):
        _run(bot_module.send_fast_lane_alerts([{"id": 1, "company": "NVIDIA"}], guild))  # must not raise


def test_watched_company_scan_and_post_posts_new_jobs_and_alerts_fast_lane():
    new_jobs = [{"id": 1, "company": "NVIDIA"}]

    with patch.object(bot_module.asyncio, "to_thread", new=AsyncMock(return_value={"new_jobs": new_jobs})), \
         patch.object(bot_module, "post_jobs_to_discord", new=AsyncMock(return_value=1)) as mock_post, \
         patch.object(bot_module, "send_premium_digests", new=AsyncMock()), \
         patch.object(bot_module, "get_premium_guild", return_value=MagicMock()), \
         patch.object(bot_module, "send_fast_lane_alerts", new=AsyncMock()) as mock_alert:
        result = _run(bot_module.watched_company_scan_and_post())

    mock_post.assert_awaited_once_with(new_jobs)
    mock_alert.assert_awaited_once()
    assert result["posted_count"] == 1


def test_watched_company_scan_and_post_skips_fast_lane_alert_without_a_guild():
    new_jobs = [{"id": 1, "company": "NVIDIA"}]
    with patch.object(bot_module.asyncio, "to_thread", new=AsyncMock(return_value={"new_jobs": new_jobs})), \
         patch.object(bot_module, "post_jobs_to_discord", new=AsyncMock(return_value=1)), \
         patch.object(bot_module, "send_premium_digests", new=AsyncMock()), \
         patch.object(bot_module, "get_premium_guild", return_value=None), \
         patch.object(bot_module, "send_fast_lane_alerts", new=AsyncMock()) as mock_alert:
        _run(bot_module.watched_company_scan_and_post())

    mock_alert.assert_not_awaited()
```

- [ ] **Step 3: Run tests to verify they fail**

Run: `cd ~/docker/internship-discord-bot && pytest tests/test_bot.py -v -k "fast_lane or watched_company"`
Expected: FAIL — `send_fast_lane_alerts` / `watched_company_scan_and_post` don't exist yet (`AttributeError`).

- [ ] **Step 4: Implement the fast-lane functions, task loop, and commands**

In `bot.py`, update the `database.db` import block (currently lines 18-29) to add the three new functions:

```python
from database.db import (
    add_fast_lane_subscriber,
    get_db_file_size_bytes,
    get_member_profile,
    get_unposted,
    init_db,
    list_fast_lane_subscribers,
    list_member_profiles,
    mark_posted,
    remove_fast_lane_subscriber,
    run_storage_maintenance,
    set_member_profile,
    stats,
    upsert_internship,
)
```

Change the `scanner` import (line 30) to:

```python
from scanner import run_scan, run_watched_company_scan
```

Add one import line after the `utils.source_store` import (line 38):

```python
from utils.watched_companies_store import get_enabled_watched_companies, load_watched_companies
```

Add `send_fast_lane_alerts` and `watched_company_scan_and_post` right after the existing `send_premium_digests` function (after line 210, before `async def scan_and_post()`):

```python
async def send_fast_lane_alerts(new_jobs: List[dict], guild: discord.Guild) -> None:
    """Instant DM to an admin-managed explicit subscriber list (see
    /fast_lane_add) for postings from watched_companies.json. Separate from
    and in addition to the premium personal digest — no Discord role, no LLM
    scoring, no /set_profile dependency. The whole point of this tier is
    speed for a short explicit list, not personalization for a broad one.
    """
    if not new_jobs:
        return

    subscriber_ids = list_fast_lane_subscribers()
    if not subscriber_ids:
        return

    embeds = [internship_to_embed(job) for job in new_jobs]
    for user_id in subscriber_ids:
        member = guild.get_member(int(user_id))
        if member is None:
            LOGGER.warning("Fast-lane subscriber %s not found in guild cache; skipping", user_id)
            continue

        for index, batch in enumerate(chunk_list(embeds, 5)):
            content = "🚨 Fast-lane alert: new watched-company internship posted:" if index == 0 else None
            try:
                await member.send(content=content, embeds=batch)
            except discord.Forbidden:
                LOGGER.warning("Could not DM fast-lane subscriber %s (DMs closed); skipping", user_id)
                break
            except discord.HTTPException:
                LOGGER.exception("Failed to send a fast-lane alert batch to %s", user_id)
                continue


async def watched_company_scan_and_post() -> dict:
    """Scan watched_companies.json (Workday fast lane) and post/alert.

    Deliberately simpler than scan_and_post(): no backlog merge or
    quality-score sort, since watched-company volume per scan is inherently
    small (a handful of intern postings per company) and this runs every
    fast_scan_interval_minutes rather than every scan_interval_minutes, so a
    backlog is not expected to build up the way it can for GitHub sources.
    """
    result = await asyncio.to_thread(run_watched_company_scan, config)

    posted_count = await post_jobs_to_discord(result["new_jobs"])
    result["posted_count"] = posted_count

    try:
        await send_premium_digests(result["new_jobs"])
    except Exception:
        LOGGER.exception("Premium digest step failed for watched-company scan; other posting is unaffected")

    guild = get_premium_guild()
    if guild is not None:
        try:
            await send_fast_lane_alerts(result["new_jobs"], guild)
        except Exception:
            LOGGER.exception("Fast-lane alert step failed; other posting is unaffected")

    return result
```

Add a second task loop right after the existing `before_scheduled_scan` (after line 301, before the `heartbeat` loop):

```python
@tasks.loop(minutes=10)
async def scheduled_watched_company_scan() -> None:
    try:
        LOGGER.info("Running scheduled watched-company (fast-lane) scan")
        await watched_company_scan_and_post()
    except Exception:
        LOGGER.exception("Scheduled watched-company scan failed")


@scheduled_watched_company_scan.before_loop
async def before_scheduled_watched_company_scan() -> None:
    await asyncio.sleep(int(config.get("fast_scan_interval_minutes", 10)) * 60)
```

In `on_ready()`, add the watched-company loop start right after the existing `scheduled_scan` start block (after line 269):

```python
    if config.get("auto_scan_enabled") and not scheduled_watched_company_scan.is_running():
        scheduled_watched_company_scan.change_interval(
            minutes=int(config.get("fast_scan_interval_minutes", 10))
        )
        scheduled_watched_company_scan.start()
```

And change the startup-scan block (currently lines 281-287) to run both scans independently, so one failing doesn't block the other:

```python
    if config.get("auto_scan_on_start") and not startup_scan_completed:
        LOGGER.info("auto_scan_on_start is enabled. Running first scan.")
        try:
            await scan_and_post()
        except Exception:
            LOGGER.exception("Startup scan failed")
        try:
            await watched_company_scan_and_post()
        except Exception:
            LOGGER.exception("Startup watched-company scan failed")
        startup_scan_completed = True
```

Add the three new admin commands right after `set_premium_role_command` (after line 436, before `class ProfileModal`):

```python
@bot.tree.command(
    name="fast_lane_add",
    description="Add a member to the fast-lane instant-DM list for watched companies (admin only).",
)
@app_commands.describe(member="The member to add")
@app_commands.default_permissions(manage_guild=True)
@app_commands.guild_only()
async def fast_lane_add_command(interaction: discord.Interaction, member: discord.Member) -> None:
    add_fast_lane_subscriber(str(member.id))
    await interaction.response.send_message(
        f"Added {member.mention} to the fast-lane list. They'll get an instant DM when a watched "
        "company (see watched_companies.json) posts a new internship.",
        ephemeral=True,
    )


@bot.tree.command(
    name="fast_lane_remove",
    description="Remove a member from the fast-lane instant-DM list (admin only).",
)
@app_commands.describe(member="The member to remove")
@app_commands.default_permissions(manage_guild=True)
@app_commands.guild_only()
async def fast_lane_remove_command(interaction: discord.Interaction, member: discord.Member) -> None:
    removed = remove_fast_lane_subscriber(str(member.id))
    if removed:
        await interaction.response.send_message(f"Removed {member.mention} from the fast-lane list.", ephemeral=True)
    else:
        await interaction.response.send_message(f"{member.mention} wasn't on the fast-lane list.", ephemeral=True)


@bot.tree.command(
    name="fast_lane_list",
    description="Show who's on the fast-lane instant-DM list (admin only).",
)
@app_commands.default_permissions(manage_guild=True)
@app_commands.guild_only()
async def fast_lane_list_command(interaction: discord.Interaction) -> None:
    subscriber_ids = list_fast_lane_subscribers()
    if not subscriber_ids:
        await interaction.response.send_message("No fast-lane subscribers yet. Use /fast_lane_add.", ephemeral=True)
        return
    lines = [f"<@{user_id}>" for user_id in subscriber_ids]
    await interaction.response.send_message("Fast-lane subscribers:\n" + "\n".join(lines), ephemeral=True)
```

Update the `/status` command (currently lines 526-552) to add fast-lane visibility — insert these two lines right after the existing `f"Premium role: ..."` line:

```python
        f"Fast-lane scan: every `{config.get('fast_scan_interval_minutes', 10)}` minutes, "
        f"`{len(list_fast_lane_subscribers())}` subscriber(s)\n"
        f"Watched companies: `{len(get_enabled_watched_companies())}` enabled / "
        f"`{len(load_watched_companies())}` total\n"
```

Update `/help` (currently lines 595-612) to document the new commands — insert these lines right after the existing `"/set_premium_role <role>` — ..." line:

```python
        "`/fast_lane_add <member>` — admin: add a member to the instant-DM list for watched companies\n"
        "`/fast_lane_remove <member>` — admin: remove a member from that list\n"
        "`/fast_lane_list` — admin: show current fast-lane subscribers\n"
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `cd ~/docker/internship-discord-bot && pytest tests/test_bot.py -v`
Expected: PASS (all tests, including the 7 new ones)

- [ ] **Step 6: Add the `watched_companies.json` bind mount to `docker-compose.yml`**

In `docker-compose.yml`, add one line to the `volumes:` list under `internship-bot`, right after `- ./scraper:/app/scraper:ro`:

```yaml
      - ./watched_companies.json:/app/watched_companies.json:ro
```

- [ ] **Step 7: Run the full test suite**

Run: `cd ~/docker/internship-discord-bot && pytest -v`
Expected: PASS (every test in the project, old and new)

- [ ] **Step 8: Commit**

```bash
cd ~/docker/internship-discord-bot
git add bot.py utils/config_loader.py config.example.json docker-compose.yml tests/test_bot.py
git commit -m "Wire Workday fast lane into bot.py: scan loop, DM alerts, admin commands"
```

---

### Task 6: Deploy and verify against the live bot

This task has no automated tests — it's confirming the feature works against the real running container and real Discord server. `watched_companies.json` was already created on disk in Task 2, and `docker-compose.yml`'s volumes list changed in Task 5, so the container needs to be recreated (not just restarted) to pick up the new bind mount.

- [ ] **Step 1: Recreate the container**

```bash
cd ~/docker/internship-discord-bot
docker compose up -d
```

- [ ] **Step 2: Check startup logs for errors and confirm both scans ran**

```bash
docker logs internship-bot --tail 100
```

Expected: no tracebacks; a "Slash commands synced..." line; both "Running scheduled internship scan"-style startup activity and a watched-company scan log line (from the `auto_scan_on_start` startup block calling `watched_company_scan_and_post()`).

- [ ] **Step 3: Confirm NVIDIA postings landed in the database**

```bash
docker exec internship-bot python3 -c "
from database.db import list_internships
rows = [r for r in list_internships(limit=50) if r['source_type'] == 'workday']
print(len(rows), 'workday row(s)')
for r in rows:
    print(r['company'], '|', r['title'], '|', r['uploaded_at'])
"
```

Expected: rows with `source_type == "workday"` and `company == "NVIDIA"` (as many as are currently open on NVIDIA's "Intern (Fixed Term)" facet).

- [ ] **Step 4: In Discord, add yourself to the fast lane and confirm the commands work**

Run `/fast_lane_add @yourself`, then `/fast_lane_list` (should show you), then `/status` (should show the new "Fast-lane scan" and "Watched companies" lines with correct counts).

- [ ] **Step 5: Confirm an actual DM arrives on the next cycle**

Either wait up to `fast_scan_interval_minutes` (10) for the next scheduled tick, or `docker restart internship-bot` to trigger another `auto_scan_on_start` pass immediately (safe — the dedupe DB means no duplicate channel posts, but note this restart will NOT re-DM postings already marked as seen/not-new; if every current NVIDIA posting is already in the DB from Step 3, temporarily testing end-to-end DM delivery requires either a genuinely new NVIDIA posting or a one-off manual call, e.g. `docker exec internship-bot python3 -c "import asyncio; ..."` is not practical here — simplest real verification is just waiting for the next real new NVIDIA posting, or trusting the unit tests in Task 5 Step 2 for the DM-sending logic itself and using this step only to confirm the commands and scan loop are live).

- [ ] **Step 6: Confirm this doesn't break the existing premium digest or channel posting**

Check that the shared Discord channel still received the NVIDIA postings from Step 3 as normal embeds (same as any other source), and that no errors appeared in the logs related to `send_premium_digests`.
