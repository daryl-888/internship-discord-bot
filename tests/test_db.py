"""Tests for the SQLite layer: dedupe-key stability and relevance persistence."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from database import db


def test_strip_tracking_params_removes_known_tracking_params():
    url = "https://acme.example/apply?utm_source=simplify&utm_campaign=x&fbclid=123"
    assert db.strip_tracking_params(url) == "https://acme.example/apply"


def test_strip_tracking_params_removes_fragment():
    assert db.strip_tracking_params("https://acme.example/apply#apply-now") == "https://acme.example/apply"


def test_strip_tracking_params_preserves_real_ats_job_ids():
    # Greenhouse/Lever/Workday etc. often encode the actual job id in a query
    # param — stripping the whole query string would collide distinct jobs.
    a = db.strip_tracking_params("https://boards.greenhouse.io/acme/jobs?gh_jid=111")
    b = db.strip_tracking_params("https://boards.greenhouse.io/acme/jobs?gh_jid=222")
    assert a != b


def test_strip_tracking_params_handles_empty_url():
    assert db.strip_tracking_params("") == ""


def test_build_dedupe_key_stable_across_tracking_param_changes():
    key_a = db.build_dedupe_key("Acme", "SWE Intern", "https://acme.example/apply?utm_source=a")
    key_b = db.build_dedupe_key("Acme", "SWE Intern", "https://acme.example/apply?utm_source=b")
    assert key_a == key_b


def test_build_dedupe_key_distinguishes_different_jobs():
    key_a = db.build_dedupe_key("Acme", "SWE Intern", "https://acme.example/apply?gh_jid=1")
    key_b = db.build_dedupe_key("Acme", "SWE Intern", "https://acme.example/apply?gh_jid=2")
    assert key_a != key_b


def _sample_internship(**overrides):
    job = {
        "company": "Acme",
        "title": "SWE Intern",
        "location": "Remote",
        "application_url": "https://acme.example/apply",
        "source_url": "https://github.com/example/internships",
        "source_type": "github_readme",
        "tags": ["software"],
        "status": "unknown",
    }
    job.update(overrides)
    return job


def test_upsert_internship_reports_new_then_not_new():
    job = _sample_internship()
    first_id, first_is_new = db.upsert_internship(dict(job))
    second_id, second_is_new = db.upsert_internship(dict(job))

    assert first_is_new
    assert not second_is_new
    assert first_id == second_id


def test_update_internship_relevance_persists_score_and_reason():
    job_id, _ = db.upsert_internship(_sample_internship())
    db.update_internship_relevance(job_id, 4, "Strong in-field match")

    rows = {row["id"]: row for row in db.list_internships(limit=10)}
    assert rows[job_id]["quality_score"] == 4
    assert rows[job_id]["llm_reason"] == "Strong in-field match"


def test_get_unposted_orders_oldest_first():
    id_a, _ = db.upsert_internship(_sample_internship(company="A", application_url="https://a.example"))
    id_b, _ = db.upsert_internship(_sample_internship(company="B", application_url="https://b.example"))

    unposted_ids = [row["id"] for row in db.get_unposted(limit=10)]
    assert unposted_ids == [id_a, id_b]


def test_mark_posted_excludes_from_unposted():
    job_id, _ = db.upsert_internship(_sample_internship())
    db.mark_posted([job_id])

    assert db.get_unposted(limit=10) == []


def test_member_profile_defaults_to_none():
    assert db.get_member_profile("999") is None


def test_set_member_profile_then_get():
    db.set_member_profile(
        "111", skills="Backend/Go", target_roles="SWE intern", education_level="Sophomore", location_pref="Remote OK"
    )
    assert db.get_member_profile("111") == {
        "skills": "Backend/Go",
        "target_roles": "SWE intern",
        "education_level": "Sophomore",
        "location_pref": "Remote OK",
    }


def test_set_member_profile_overwrites_previous_value():
    db.set_member_profile("111", skills="First")
    db.set_member_profile("111", skills="Updated")
    assert db.get_member_profile("111")["skills"] == "Updated"


def test_set_member_profile_defaults_unset_fields_to_empty_string():
    db.set_member_profile("111", skills="Backend/Go")
    profile = db.get_member_profile("111")
    assert profile["target_roles"] == ""
    assert profile["education_level"] == ""
    assert profile["location_pref"] == ""


def test_list_member_profiles_returns_all_saved_profiles():
    db.set_member_profile("111", skills="Backend/Go")
    db.set_member_profile("222", skills="Frontend/React")

    profiles = db.list_member_profiles()
    assert profiles["111"]["skills"] == "Backend/Go"
    assert profiles["222"]["skills"] == "Frontend/React"


def _insert_aged_row(status: str, days_old: int, dedupe_suffix: str) -> None:
    db.init_db()
    old_time = (datetime.now(timezone.utc) - timedelta(days=days_old)).isoformat()
    with db._connect() as conn:
        conn.execute(
            """
            INSERT INTO internships (
                dedupe_key, company, title, location, application_url, source_url,
                source_type, tags, uploaded_at, first_seen, last_seen, posted_to_discord, status
            ) VALUES (?, 'Co', 'Role', '', '', '', '', '', '', ?, ?, 1, ?)
            """,
            (f"key-{dedupe_suffix}", old_time, old_time, status),
        )
        conn.commit()


def test_prune_old_internships_deletes_stale_closed_unknown_ignored():
    _insert_aged_row("closed", 200, "a")
    _insert_aged_row("unknown", 200, "b")
    _insert_aged_row("ignored", 200, "c")

    deleted = db.prune_old_internships(retention_days=180)

    assert deleted == 3
    assert db.list_internships(limit=10) == []


def test_prune_old_internships_preserves_active_applied_saved_regardless_of_age():
    _insert_aged_row("active", 200, "a")
    _insert_aged_row("applied", 200, "b")
    _insert_aged_row("saved", 200, "c")

    deleted = db.prune_old_internships(retention_days=180)

    assert deleted == 0
    assert len(db.list_internships(limit=10)) == 3


def test_prune_old_internships_leaves_recent_rows_alone():
    _insert_aged_row("closed", 5, "a")  # newer than the retention window

    deleted = db.prune_old_internships(retention_days=180)

    assert deleted == 0
    assert len(db.list_internships(limit=10)) == 1


def test_prune_old_internships_disabled_when_retention_not_positive():
    _insert_aged_row("closed", 200, "a")

    assert db.prune_old_internships(retention_days=0) == 0
    assert db.prune_old_internships(retention_days=-1) == 0
    assert len(db.list_internships(limit=10)) == 1


def test_checkpoint_and_vacuum_does_not_raise():
    _insert_aged_row("closed", 5, "a")
    db.checkpoint_and_vacuum()  # just needs to not raise


def test_run_storage_maintenance_prunes_and_reports_deleted_count():
    _insert_aged_row("closed", 200, "a")
    _insert_aged_row("active", 200, "b")

    result = db.run_storage_maintenance(retention_days=180)

    assert result == {"deleted": 1}
    assert len(db.list_internships(limit=10)) == 1


def test_get_db_file_size_bytes_reflects_an_initialized_database():
    db.init_db()
    assert db.get_db_file_size_bytes() > 0


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
