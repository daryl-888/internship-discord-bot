"""Tests for the markdown-table README parser and its fetch/caching helpers.

This parser is the most format-fragile part of the project — it has to survive
whatever table style each internship-list repo happens to use. These tests pin
down the shapes we already know it has to handle.
"""

from __future__ import annotations

from unittest.mock import patch

import pytest

from scraper import github_scraper as gs


SOURCE_URL = "https://github.com/example/internships"


def test_parse_simplifyjobs_style_table():
    markdown = """
# Software Engineering Internships

| Company | Role | Location | Application | Age |
|---|---|---|---|---|
| **Acme Corp** | [Software Engineer Intern](https://acme.example/apply) | Remote | [Apply](https://acme.example/apply) | 2d |
""".strip()

    internships = gs.parse_markdown_tables(markdown, source_url=SOURCE_URL)

    assert len(internships) == 1
    job = internships[0]
    assert job["company"] == "Acme Corp"
    assert job["title"] == "Software Engineer Intern"
    assert job["location"] == "Remote"
    assert job["application_url"] == "https://acme.example/apply"
    assert job["uploaded_at"] == "2d"
    assert "software" in job["tags"]
    assert "internship" in job["tags"]
    assert ("faang" in job["tags"]) != ("non-faang" in job["tags"])


def test_continuation_rows_inherit_previous_company():
    markdown = """
| Company | Role | Location | Application |
|---|---|---|---|
| Acme Corp | Backend Intern | Remote | https://acme.example/backend |
| ↳ | Frontend Intern | NYC | https://acme.example/frontend |
""".strip()

    internships = gs.parse_markdown_tables(markdown, source_url=SOURCE_URL)

    assert len(internships) == 2
    assert internships[0]["company"] == "Acme Corp"
    assert internships[1]["company"] == "Acme Corp"
    assert internships[1]["title"] == "Frontend Intern"


def test_dash_placeholder_company_inherits_previous_company():
    markdown = """
| Company | Role | Location | Application |
|---|---|---|---|
| Acme Corp | Backend Intern | Remote | https://acme.example/backend |
| - | Data Intern | Remote | https://acme.example/data |
""".strip()

    internships = gs.parse_markdown_tables(markdown, source_url=SOURCE_URL)

    assert internships[1]["company"] == "Acme Corp"


def test_closed_status_detected_from_row_text():
    markdown = """
| Company | Role | Location | Application |
|---|---|---|---|
| Acme Corp | Backend Intern | Remote | Closed |
""".strip()

    internships = gs.parse_markdown_tables(markdown, source_url=SOURCE_URL)

    assert internships[0]["status"] == "closed"


def test_rows_missing_company_or_title_are_skipped():
    markdown = """
| Company | Role | Location | Application |
|---|---|---|---|
|  |  | Remote | https://example.com/apply |
""".strip()

    internships = gs.parse_markdown_tables(markdown, source_url=SOURCE_URL)

    assert internships == []


def test_parse_html_table_basic():
    markdown = """
## Software Engineering Internship Roles

<table>
<thead>
<tr>
<th>Company</th>
<th>Role</th>
<th>Location</th>
<th>Application</th>
<th>Age</th>
</tr>
</thead>
<tbody>
<tr>
<td><strong><a href="https://simplify.jobs/c/Acme?utm_source=GHList">Acme Corp</a></strong></td>
<td>Software Engineer Intern</td>
<td>Austin, TX</td>
<td><div align="center"><a href="https://acme.example/apply?ref=x"><img src="https://i.imgur.com/fbjwDvo.png" width="50" alt="Apply"></a> <a href="https://simplify.jobs/p/abc?utm_source=GHList"><img src="https://i.imgur.com/aVnQdox.png" width="26" alt="Simplify"></a></div></td>
<td>2d</td>
</tr>
</tbody>
</table>
""".strip()

    internships = gs.parse_markdown_tables(markdown, source_url=SOURCE_URL)

    assert len(internships) == 1
    job = internships[0]
    assert job["company"] == "Acme Corp"
    assert job["title"] == "Software Engineer Intern"
    assert job["location"] == "Austin, TX"
    assert job["application_url"] == "https://acme.example/apply?ref=x"
    assert job["uploaded_at"] == "2d"
    assert job["source_url"] == SOURCE_URL


def test_parse_html_table_continuation_row_arrow_inherits_company():
    markdown = """
<table>
<thead>
<tr><th>Company</th><th>Role</th><th>Location</th><th>Application</th></tr>
</thead>
<tbody>
<tr>
<td><strong><a href="https://simplify.jobs/c/Acme">Acme Corp</a></strong></td>
<td>Backend Intern</td>
<td>Remote</td>
<td><a href="https://acme.example/backend">Apply</a></td>
</tr>
<tr>
<td>↳</td>
<td>Frontend Intern</td>
<td>NYC</td>
<td><a href="https://acme.example/frontend">Apply</a></td>
</tr>
</tbody>
</table>
""".strip()

    internships = gs.parse_markdown_tables(markdown, source_url=SOURCE_URL)

    assert len(internships) == 2
    assert internships[1]["company"] == "Acme Corp"
    assert internships[1]["title"] == "Frontend Intern"


def test_parse_html_table_multi_location_details_cell():
    markdown = """
<table>
<thead><tr><th>Company</th><th>Role</th><th>Location</th><th>Application</th></tr></thead>
<tbody>
<tr>
<td><strong><a href="https://simplify.jobs/c/Acme">Acme Corp</a></strong></td>
<td>Software Engineer Intern</td>
<td><details><summary><strong>2 locations</strong></summary>Austin, TX<br>Remote</details></td>
<td><a href="https://acme.example/apply">Apply</a></td>
</tr>
</tbody>
</table>
""".strip()

    internships = gs.parse_markdown_tables(markdown, source_url=SOURCE_URL)

    assert len(internships) == 1
    assert "Austin, TX" in internships[0]["location"]
    assert "Remote" in internships[0]["location"]


def test_parse_html_and_markdown_tables_in_same_document():
    markdown = """
## Section One

| Company | Role | Location | Application |
|---|---|---|---|
| Pipe Corp | Backend Intern | Remote | https://pipe.example/apply |

## Section Two

<table>
<thead><tr><th>Company</th><th>Role</th><th>Location</th><th>Application</th></tr></thead>
<tbody>
<tr>
<td>HTML Corp</td>
<td>Frontend Intern</td>
<td>NYC</td>
<td><a href="https://html.example/apply">Apply</a></td>
</tr>
</tbody>
</table>
""".strip()

    internships = gs.parse_markdown_tables(markdown, source_url=SOURCE_URL)

    companies = {job["company"] for job in internships}
    assert companies == {"Pipe Corp", "HTML Corp"}


def test_extract_best_url_ignores_html_quote_boundary():
    cell = '<a href="https://acme.example/apply?ref=x&utm_source=y">Apply</a>'
    assert gs.extract_best_url(cell) == "https://acme.example/apply?ref=x&utm_source=y"


def test_duplicate_rows_in_same_table_are_deduped():
    markdown = """
| Company | Role | Location | Application |
|---|---|---|---|
| Acme Corp | Backend Intern | Remote | https://acme.example/backend |
| Acme Corp | Backend Intern | Remote | https://acme.example/backend |
""".strip()

    internships = gs.parse_markdown_tables(markdown, source_url=SOURCE_URL)

    assert len(internships) == 1


@pytest.mark.parametrize(
    "cell,expected",
    [
        ("**Acme Corp**", "Acme Corp"),
        ("[Acme Corp](https://acme.example)", "Acme Corp"),
        ("![logo](https://acme.example/logo.png) Acme Corp", "Acme Corp"),
        ("Remote<br>Hybrid", "Remote, Hybrid"),
        ("`Acme Corp`", "Acme Corp"),
    ],
)
def test_extract_display_text_strips_markdown_noise(cell, expected):
    assert gs.extract_display_text(cell) == expected


def test_extract_best_url_skips_badge_images_and_raw_github_links():
    cell = (
        "[![Apply](https://raw.githubusercontent.com/org/repo/main/badge.svg)]"
        "(https://real.example/apply)"
    )
    assert gs.extract_best_url(cell) == "https://real.example/apply"


def test_extract_best_url_falls_back_to_only_url_when_all_filtered():
    cell = "https://raw.githubusercontent.com/org/repo/main/README.md"
    assert gs.extract_best_url(cell) == cell


@pytest.mark.parametrize(
    "header,expected",
    [
        ("Company", "company"),
        ("Employer", "company"),
        ("Role/Position", "title"),
        ("Location/Office", "location"),
        ("Apply Link", "application"),
        ("Date Posted", "age"),
        ("Something Else", "something else"),
    ],
)
def test_normalize_header_variants(header, expected):
    assert gs.normalize_header(header) == expected


def test_infer_tags_matches_keywords_and_internship_markers():
    tags = gs.infer_tags("Machine Learning Co-op, GPU/CUDA team")
    assert "ai" in tags
    assert "gpu" in tags
    assert "internship" in tags


def test_infer_tags_defaults_to_internship_when_nothing_matches():
    assert gs.infer_tags("Mystery Role") == ["internship"]


def test_build_raw_candidates_plain_repo_url():
    candidates = gs.build_raw_candidates("https://github.com/example/internships")
    assert candidates == [
        "https://raw.githubusercontent.com/example/internships/HEAD/README.md",
        "https://raw.githubusercontent.com/example/internships/main/README.md",
        "https://raw.githubusercontent.com/example/internships/master/README.md",
        "https://raw.githubusercontent.com/example/internships/dev/README.md",
    ]


def test_build_raw_candidates_blob_url_resolves_to_single_file():
    candidates = gs.build_raw_candidates(
        "https://github.com/example/internships/blob/main/README.md"
    )
    assert candidates == [
        "https://raw.githubusercontent.com/example/internships/main/README.md"
    ]


def test_build_raw_candidates_passes_through_raw_url():
    raw_url = "https://raw.githubusercontent.com/example/internships/main/README.md"
    assert gs.build_raw_candidates(raw_url) == [raw_url]


def test_build_raw_candidates_rejects_non_github_url():
    with pytest.raises(ValueError):
        gs.build_raw_candidates("https://example.com/not-github")


def test_build_raw_candidates_rejects_incomplete_path():
    with pytest.raises(ValueError):
        gs.build_raw_candidates("https://github.com/example")


class _FakeResponse:
    def __init__(self, status_code, text="", etag=""):
        self.status_code = status_code
        self.text = text
        self.headers = {"ETag": etag} if etag else {}


def test_fetch_readme_tries_preferred_url_first_and_captures_etag():
    preferred = "https://raw.githubusercontent.com/example/internships/main/README.md"
    calls = []

    def fake_get(url, headers=None, timeout=None):
        calls.append(url)
        if url == preferred:
            return _FakeResponse(200, text="| A | B |\n|---|---|\n", etag='"abc"')
        return _FakeResponse(404)

    with patch.object(gs.requests, "get", side_effect=fake_get):
        markdown, raw_url, etag, not_modified = gs.fetch_readme(
            "https://github.com/example/internships", preferred_url=preferred
        )

    assert calls == [preferred]
    assert raw_url == preferred
    assert etag == '"abc"'
    assert not not_modified


def test_fetch_readme_sends_if_none_match_for_preferred_url_and_handles_304():
    preferred = "https://raw.githubusercontent.com/example/internships/main/README.md"
    seen_headers = {}

    def fake_get(url, headers=None, timeout=None):
        seen_headers["If-None-Match"] = headers.get("If-None-Match")
        return _FakeResponse(304)

    with patch.object(gs.requests, "get", side_effect=fake_get):
        markdown, raw_url, etag, not_modified = gs.fetch_readme(
            "https://github.com/example/internships", preferred_url=preferred, etag='"abc"'
        )

    assert seen_headers["If-None-Match"] == '"abc"'
    assert not_modified
    assert markdown == ""


def test_fetch_readme_falls_back_through_candidates_without_preferred_url():
    def fake_get(url, headers=None, timeout=None):
        if url.endswith("/master/README.md"):
            return _FakeResponse(200, text="| A | B |\n|---|---|\n")
        return _FakeResponse(404)

    with patch.object(gs.requests, "get", side_effect=fake_get):
        markdown, raw_url, etag, not_modified = gs.fetch_readme(
            "https://github.com/example/internships"
        )

    assert raw_url.endswith("/master/README.md")
    assert not not_modified


def test_fetch_readme_raises_when_every_candidate_fails():
    def fake_get(url, headers=None, timeout=None):
        return _FakeResponse(404)

    with patch.object(gs.requests, "get", side_effect=fake_get):
        with pytest.raises(RuntimeError):
            gs.fetch_readme("https://github.com/example/internships")


def test_scrape_github_readme_returns_scrape_result_with_parsed_jobs():
    markdown = "| Company | Role |\n|---|---|\n| Acme Corp | Intern |\n"

    with patch.object(gs, "fetch_readme", return_value=(markdown, "raw-url", "etag-1", False)):
        result = gs.scrape_github_readme(SOURCE_URL)

    assert isinstance(result, gs.ScrapeResult)
    assert len(result.internships) == 1
    assert result.raw_url == "raw-url"
    assert result.etag == "etag-1"
    assert not result.not_modified


def test_scrape_github_readme_skips_parsing_when_not_modified():
    with patch.object(gs, "fetch_readme", return_value=("", "raw-url", "etag-1", True)):
        result = gs.scrape_github_readme(SOURCE_URL, preferred_url="raw-url", etag="etag-1")

    assert result.internships == []
    assert result.not_modified
