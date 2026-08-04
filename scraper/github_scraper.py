"""GitHub README internship scraper.

This scraper is intentionally simple and safe:
- It fetches public GitHub README markdown through raw.githubusercontent.com.
- It parses Markdown tables.
- It does not log in, bypass rate limits, or scrape private data.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Dict, Iterable, List, Optional, Tuple
from urllib.parse import urlparse

import requests

from utils.tags import add_company_classification_tag

LOGGER = logging.getLogger(__name__)


@dataclass
class ScrapeResult:
    internships: List[Dict]
    raw_url: str
    etag: str
    not_modified: bool

REQUEST_HEADERS = {
    "User-Agent": "local-discord-internship-bot/1.0 (+https://github.com/)"
}

TAG_KEYWORDS = {
    "software": ["software", "swe", "developer", "full stack", "fullstack"],
    "backend": ["backend", "back-end", "server"],
    "frontend": ["frontend", "front-end", "react", "web"],
    "data": ["data", "analytics", "analyst"],
    "ai": ["ai", "machine learning", "ml", "llm", "generative"],
    "quant": ["quant", "trading", "researcher", "finance"],
    "hardware": ["hardware", "electrical", "embedded", "fpga", "asic"],
    "gpu": ["gpu", "cuda", "parallel", "compiler", "accelerator"],
    "cloud": ["cloud", "aws", "azure", "gcp", "platform"],
    "security": ["security", "cyber", "threat"],
}


def scrape_github_readme(
    source_url: str, preferred_url: str = "", etag: str = ""
) -> ScrapeResult:
    """Fetch a GitHub README and return normalized internship dictionaries.

    ``preferred_url`` is the raw URL that resolved successfully last time (if
    any), so repeat scans can skip straight to the right branch instead of
    guessing HEAD/main/master/dev again. ``etag`` lets the server tell us
    nothing changed (HTTP 304) so we can skip re-parsing entirely.
    """
    markdown, raw_url, new_etag, not_modified = fetch_readme(
        source_url, preferred_url=preferred_url, etag=etag
    )
    if not_modified:
        LOGGER.info("README unchanged since last scan for %s", source_url)
        return ScrapeResult(internships=[], raw_url=raw_url, etag=new_etag, not_modified=True)

    internships = parse_markdown_tables(markdown, source_url=source_url, raw_url=raw_url)
    LOGGER.info("Scraped %s internships from %s", len(internships), source_url)
    return ScrapeResult(internships=internships, raw_url=raw_url, etag=new_etag, not_modified=False)


def fetch_readme(
    source_url: str, preferred_url: str = "", etag: str = ""
) -> Tuple[str, str, str, bool]:
    """Fetch README markdown from GitHub.

    Tries ``preferred_url`` first when given (the URL that worked last scan),
    then falls back through the usual branch-name candidates. Returns
    ``(markdown, raw_url, etag, not_modified)``; ``markdown`` is empty and
    ``not_modified`` is True when the server confirms nothing changed.
    """
    candidate_urls = build_raw_candidates(source_url)
    if preferred_url:
        candidate_urls = [preferred_url] + [url for url in candidate_urls if url != preferred_url]

    last_error: Optional[Exception] = None

    for raw_url in candidate_urls:
        headers = dict(REQUEST_HEADERS)
        if etag and raw_url == preferred_url:
            headers["If-None-Match"] = etag

        try:
            response = requests.get(raw_url, headers=headers, timeout=20)
            if response.status_code == 304:
                return "", raw_url, etag, True
            if response.status_code == 200 and response.text.strip():
                return response.text, raw_url, response.headers.get("ETag", ""), False
            LOGGER.warning("README fetch failed %s: HTTP %s", raw_url, response.status_code)
        except requests.RequestException as exc:
            last_error = exc
            LOGGER.warning("README fetch error %s: %s", raw_url, exc)

    if last_error:
        raise RuntimeError(f"Could not fetch README for {source_url}: {last_error}")
    raise RuntimeError(f"Could not fetch README for {source_url}")


def build_raw_candidates(source_url: str) -> List[str]:
    parsed = urlparse(source_url)

    if parsed.netloc == "raw.githubusercontent.com":
        return [source_url]

    if "github.com" not in parsed.netloc:
        raise ValueError("Only GitHub README URLs are supported by github_scraper.py")

    path_parts = [part for part in parsed.path.strip("/").split("/") if part]
    if len(path_parts) < 2:
        raise ValueError("GitHub URL must look like https://github.com/owner/repo")

    owner, repo = path_parts[0], path_parts[1].replace(".git", "")

    # Direct file URL: https://github.com/owner/repo/blob/branch/path/to/README.md
    if len(path_parts) >= 5 and path_parts[2] == "blob":
        branch = path_parts[3]
        file_path = "/".join(path_parts[4:])
        return [f"https://raw.githubusercontent.com/{owner}/{repo}/{branch}/{file_path}"]

    # GitHub raw supports HEAD in many cases. We also try common defaults.
    return [
        f"https://raw.githubusercontent.com/{owner}/{repo}/HEAD/README.md",
        f"https://raw.githubusercontent.com/{owner}/{repo}/main/README.md",
        f"https://raw.githubusercontent.com/{owner}/{repo}/master/README.md",
        f"https://raw.githubusercontent.com/{owner}/{repo}/dev/README.md",
    ]


def parse_markdown_tables(markdown: str, source_url: str, raw_url: str = "") -> List[Dict]:
    lines = markdown.splitlines()
    internships: List[Dict] = []
    current_section = ""
    previous_company = ""
    seen_keys = set()
    i = 0

    while i < len(lines):
        line = lines[i].strip()

        if line.startswith("#"):
            current_section = clean_text(line.lstrip("#").strip())
            i += 1
            continue

        if line.lower().startswith("<table"):
            table_lines = []
            while i < len(lines):
                table_lines.append(lines[i])
                closed = "</table>" in lines[i].lower()
                i += 1
                if closed:
                    break

            new_internships, previous_company = _parse_html_table_rows(
                "\n".join(table_lines),
                previous_company=previous_company,
                source_url=source_url,
                section=current_section,
            )
            for internship in new_internships:
                key = (
                    internship["company"].lower(),
                    internship["title"].lower(),
                    internship.get("application_url", "").lower(),
                )
                if key not in seen_keys:
                    internships.append(internship)
                    seen_keys.add(key)
            continue

        if _looks_like_table_start(lines, i):
            header_cells = split_table_row(lines[i])
            normalized_headers = [normalize_header(cell) for cell in header_cells]
            i += 2  # skip separator row

            while i < len(lines) and "|" in lines[i]:
                row_cells = split_table_row(lines[i])
                if len(row_cells) < 2:
                    i += 1
                    continue

                internship, previous_company = parse_table_row(
                    headers=normalized_headers,
                    cells=row_cells,
                    previous_company=previous_company,
                    source_url=source_url,
                    section=current_section,
                )

                if internship:
                    key = (
                        internship["company"].lower(),
                        internship["title"].lower(),
                        internship.get("application_url", "").lower(),
                    )
                    if key not in seen_keys:
                        internships.append(internship)
                        seen_keys.add(key)
                i += 1
            continue

        i += 1

    return internships


_HTML_ROW_RE = re.compile(r"<tr[^>]*>(.*?)</tr>", re.IGNORECASE | re.DOTALL)
_HTML_TH_RE = re.compile(r"<th[^>]*>(.*?)</th>", re.IGNORECASE | re.DOTALL)
_HTML_TD_RE = re.compile(r"<td[^>]*>(.*?)</td>", re.IGNORECASE | re.DOTALL)


def _parse_html_table_rows(
    table_html: str, previous_company: str, source_url: str, section: str
) -> Tuple[List[Dict], str]:
    """Parse one <table>...</table> block (some repos, e.g. SimplifyJobs,
    render their listing as raw HTML instead of markdown pipe syntax).

    Reuses parse_table_row's cell handling as-is: extract_display_text and
    extract_best_url already strip arbitrary HTML tags generically, so a raw
    <td>...</td> inner-HTML string works as a "cell" without any HTML-specific
    text extraction.
    """
    internships: List[Dict] = []
    headers: List[str] = []

    for row_html in _HTML_ROW_RE.findall(table_html):
        header_cells = _HTML_TH_RE.findall(row_html)
        if header_cells:
            headers = [normalize_header(extract_display_text(cell)) for cell in header_cells]
            continue

        cells = _HTML_TD_RE.findall(row_html)
        if len(cells) < 2:
            continue

        internship, previous_company = parse_table_row(
            headers=headers,
            cells=cells,
            previous_company=previous_company,
            source_url=source_url,
            section=section,
        )
        if internship:
            internships.append(internship)

    return internships, previous_company


def _looks_like_table_start(lines: List[str], index: int) -> bool:
    if index + 1 >= len(lines):
        return False
    return "|" in lines[index] and is_separator_row(lines[index + 1])


def is_separator_row(line: str) -> bool:
    cleaned = line.strip().strip("|").replace(" ", "")
    if not cleaned:
        return False
    cells = cleaned.split("|")
    return all(re.fullmatch(r":?-{3,}:?", cell or "") for cell in cells)


def split_table_row(line: str) -> List[str]:
    stripped = line.strip()
    if stripped.startswith("|"):
        stripped = stripped[1:]
    if stripped.endswith("|"):
        stripped = stripped[:-1]
    return [cell.strip() for cell in stripped.split("|")]


def normalize_header(header: str) -> str:
    header = clean_text(header).lower()
    if any(word in header for word in ["company", "employer", "organization"]):
        return "company"
    if any(word in header for word in ["role", "title", "position", "job"]):
        return "title"
    if any(word in header for word in ["location", "office"]):
        return "location"
    if any(word in header for word in ["application", "apply", "link"]):
        return "application"
    if "age" in header or "date" in header:
        return "age"
    return header


def parse_table_row(
    headers: List[str],
    cells: List[str],
    previous_company: str,
    source_url: str,
    section: str,
) -> Tuple[Optional[Dict], str]:
    row = {headers[index] if index < len(headers) else f"col_{index}": cell for index, cell in enumerate(cells)}

    # Fall back to common table order: Company | Role | Location | Application | Age
    company_cell = row.get("company", cells[0] if len(cells) > 0 else "")
    title_cell = row.get("title", cells[1] if len(cells) > 1 else "")
    location_cell = row.get("location", cells[2] if len(cells) > 2 else "")
    application_cell = row.get("application", cells[3] if len(cells) > 3 else "")
    uploaded_cell = row.get("age", cells[4] if len(cells) > 4 else "")

    company = extract_display_text(company_cell)
    title = extract_display_text(title_cell)
    location = extract_display_text(location_cell)
    uploaded_at = extract_display_text(uploaded_cell)
    application_url = extract_best_url(application_cell) or extract_best_url(title_cell) or extract_best_url(company_cell)

    # Some repos use ↳ for additional roles from the same company.
    if not company or company.startswith("↳") or company in {"-", "—"}:
        company = previous_company
    elif company:
        previous_company = company

    title = title.lstrip("↳").strip()

    if not company or not title:
        return None, previous_company

    status = "closed" if "closed" in " ".join(cells).lower() else "unknown"

    combined_text = " ".join([company, title, location, section])
    internship = {
        "company": company,
        "title": title,
        "location": location or "Unknown",
        "application_url": application_url or source_url,
        "source_url": source_url,
        "source_type": "github_readme",
        "date_found": datetime.now(timezone.utc).isoformat(),
        "uploaded_at": uploaded_at,
        "tags": add_company_classification_tag(infer_tags(combined_text), company),
        "status": status,
    }
    return internship, previous_company


def extract_display_text(markdown_cell: str) -> str:
    text = markdown_cell.strip()

    # Replace markdown links with their label.
    text = re.sub(r"!\[[^\]]*\]\([^)]*\)", "", text)
    text = re.sub(r"\[([^\]]+)\]\([^)]*\)", r"\1", text)
    text = re.sub(r"<br\s*/?>", ", ", text, flags=re.IGNORECASE)
    text = re.sub(r"<[^>]+>", " ", text)
    text = text.replace("**", "").replace("__", "").replace("`", "")
    return clean_text(text)


def clean_text(text: str) -> str:
    text = re.sub(r"&amp;", "&", text)
    text = re.sub(r"\s+", " ", text)
    return text.strip(" \t\n\r|-")


def extract_best_url(markdown_cell: str) -> str:
    # Excludes quotes too so this also works on raw HTML cells (href="...")
    # from parse_html_tables, not just markdown [label](url)/bare-URL cells.
    urls = re.findall(r"https?://[^)\s>\"']+", markdown_cell)
    if not urls:
        return ""

    cleaned_urls = [url.rstrip(")].,;\"'") for url in urls]
    filtered = []
    for url in cleaned_urls:
        lower = url.lower()
        if "raw.githubusercontent.com" in lower:
            continue
        if lower.endswith(".svg") or lower.endswith(".png"):
            continue
        filtered.append(url)

    return filtered[0] if filtered else cleaned_urls[0]


def infer_tags(text: str) -> List[str]:
    lower = text.lower()
    tags: List[str] = []
    for tag, keywords in TAG_KEYWORDS.items():
        if any(keyword in lower for keyword in keywords):
            tags.append(tag)
    if "intern" in lower or "co-op" in lower or "coop" in lower:
        tags.append("internship")
    return list(dict.fromkeys(tags)) or ["internship"]
