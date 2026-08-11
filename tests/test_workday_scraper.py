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


def _posting(
    title="SWE Intern, Fall 2026",
    posted_on="Posted Today",
    external_path="/job/US-CA/SWE-Intern_JR1",
    locations_text="US, CA, Santa Clara",
):
    return {
        "title": title,
        "externalPath": external_path,
        "locationsText": locations_text,
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


def test_is_us_location_matches_common_country_labels():
    assert ws._is_us_location("US, CA, Santa Clara") is True
    assert ws._is_us_location("USA, NY, New York") is True
    assert ws._is_us_location("United States, TX, Austin") is True
    assert ws._is_us_location("United States of America, WA, Seattle") is True


def test_is_us_location_rejects_non_us_and_empty():
    assert ws._is_us_location("Taiwan, Taipei") is False
    assert ws._is_us_location("China, Shanghai") is False
    assert ws._is_us_location("") is False
    assert ws._is_us_location(None) is False


def test_scrape_workday_intern_jobs_filters_out_non_us_postings():
    postings = [
        _posting(title="US Role", external_path="/job/us-role", locations_text="US, CA, Santa Clara"),
        _posting(title="Taiwan Role", external_path="/job/tw-role", locations_text="Taiwan, Taipei"),
        _posting(title="China Role", external_path="/job/cn-role", locations_text="China, Shanghai"),
    ]
    with patch.object(ws.requests, "post", return_value=_response(postings, total=3)):
        result = ws.scrape_workday_intern_jobs(COMPANY)

    assert [job["title"] for job in result.internships] == ["US Role"]


def test_scrape_workday_intern_jobs_raises_on_http_error():
    error_response = MagicMock()
    error_response.raise_for_status.side_effect = ws.requests.HTTPError("500")

    with patch.object(ws.requests, "post", return_value=error_response):
        try:
            ws.scrape_workday_intern_jobs(COMPANY)
            assert False, "expected HTTPError to propagate"
        except ws.requests.HTTPError:
            pass