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