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
            if not _is_us_location(posting.get("locationsText", "")):
                continue
            internships.append(_build_internship(posting, company_name, careers_url))

        if len(postings) < PAGE_SIZE:
            break
        offset += PAGE_SIZE

    return ScrapeResult(internships=internships)


_US_COUNTRY_LABELS = {"us", "usa", "united states", "united states of america"}


def _is_us_location(locations_text: str) -> bool:
    """Workday's locationsText leads with the country ("US, CA, Santa Clara"
    vs. "Taiwan, Taipei") — verified against NVIDIA's tenant on 2026-08-11.
    Postings with an empty/unrecognized location are treated as non-US
    rather than let through, since the whole point of this filter is to
    only surface US roles.
    """
    if not locations_text:
        return False
    country = locations_text.split(",")[0].strip().lower()
    return country in _US_COUNTRY_LABELS


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
