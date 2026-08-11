"""Tests for bot.py's posting pipeline: dedupe/merge, quality ranking,
per-batch resilience when Discord rejects a send, and the premium-tier
personalized DM digest."""

from __future__ import annotations

import asyncio
import copy
from unittest.mock import AsyncMock, MagicMock, patch

import discord
import pytest

import bot as bot_module


@pytest.fixture(autouse=True)
def _reset_bot_config():
    """Tests mutate the module-level `bot_module.config` dict; restore it so
    one test's changes (e.g. premium_role_id) can't leak into another."""
    original = copy.deepcopy(bot_module.config)
    yield
    bot_module.config.clear()
    bot_module.config.update(original)


def test_merge_unique_jobs_dedupes_by_id_and_prefers_first_occurrence():
    backlog = [{"id": 1, "company": "A"}, {"id": 2, "company": "B"}]
    new_jobs = [{"id": 2, "company": "B-dup"}, {"id": 3, "company": "C"}]

    merged = bot_module._merge_unique_jobs(backlog, new_jobs)

    assert [job["id"] for job in merged] == [1, 2, 3]
    assert merged[1]["company"] == "B"  # backlog's version won, not the duplicate


def test_quality_score_defaults_to_neutral_when_missing_or_wrong_type():
    from utils.relevance import NEUTRAL_QUALITY_SCORE

    assert bot_module._quality_score({}) == NEUTRAL_QUALITY_SCORE
    assert bot_module._quality_score({"quality_score": None}) == NEUTRAL_QUALITY_SCORE
    assert bot_module._quality_score({"quality_score": 5}) == 5


def test_jobs_sort_best_score_first_preserving_order_among_ties():
    jobs = [
        {"id": 1, "company": "Low"},
        {"id": 2, "company": "High", "quality_score": 5},
        {"id": 3, "company": "Mid", "quality_score": 3},
        {"id": 4, "company": "Bottom", "quality_score": 1},
    ]

    jobs.sort(key=bot_module._quality_score, reverse=True)

    assert [job["company"] for job in jobs] == ["High", "Low", "Mid", "Bottom"]


class _FakeResponse:
    status = 500
    reason = "Internal Server Error"
    headers = {}


def _run(coro):
    return asyncio.run(coro)


def test_post_jobs_to_discord_skips_and_does_not_mark_a_failed_batch():
    jobs = [{"id": i, "company": str(i)} for i in range(1, 7)]  # two batches of 5 + 1

    call_count = {"n": 0}

    async def fake_send(*args, **kwargs):
        call_count["n"] += 1
        if call_count["n"] == 1:
            raise discord.HTTPException(_FakeResponse(), "boom")

    fake_channel = MagicMock()
    fake_channel.send = fake_send
    marked_batches = []

    with patch.object(bot_module, "get_post_channel", return_value=fake_channel), \
         patch.object(bot_module, "mark_posted", side_effect=lambda ids: marked_batches.append(list(ids))), \
         patch.object(bot_module, "internship_to_embed", side_effect=lambda job: MagicMock()), \
         patch.object(bot_module.asyncio, "sleep", new=AsyncMock()):
        bot_module.config["max_posts_per_scan"] = 20
        posted_count = _run(bot_module.post_jobs_to_discord(jobs))

    assert call_count["n"] == 2
    assert marked_batches == [[6]]  # only the second (successful) batch got marked
    assert posted_count == 1


def test_post_jobs_to_discord_returns_zero_without_a_configured_channel():
    with patch.object(bot_module, "get_post_channel", return_value=None):
        posted_count = _run(bot_module.post_jobs_to_discord([{"id": 1}]))

    assert posted_count == 0


def test_scan_and_post_drops_backlog_jobs_that_aged_out_since_being_queued():
    # "Fresh" backlog entry (still under the 3-day cutoff) should survive;
    # "Stale" was postable when first scraped but has since aged out while
    # waiting in the unposted backlog, and must not reach Discord.
    new_jobs = []
    backlog = [
        {"id": 1, "company": "Stale", "uploaded_at": "5d"},
        {"id": 2, "company": "Fresh", "uploaded_at": "1d"},
    ]
    posted_order = []

    async def fake_post_jobs_to_discord(jobs):
        posted_order.extend(job["company"] for job in jobs)
        return len(jobs)

    with patch.object(bot_module.asyncio, "to_thread", new=AsyncMock(return_value={"new_jobs": new_jobs})), \
         patch.object(bot_module, "get_unposted", return_value=backlog), \
         patch.object(bot_module, "post_jobs_to_discord", side_effect=fake_post_jobs_to_discord):
        bot_module.config["max_posts_per_scan"] = 20
        bot_module.config["max_posting_age_days"] = 3
        _run(bot_module.scan_and_post())

    assert posted_order == ["Fresh"]


def test_scan_and_post_sorts_merged_jobs_by_quality_before_posting():
    new_jobs = [{"id": 1, "company": "Low"}, {"id": 2, "company": "High", "quality_score": 5}]
    backlog = [{"id": 3, "company": "Mid", "quality_score": 3}]
    posted_order = []

    async def fake_post_jobs_to_discord(jobs):
        posted_order.extend(job["company"] for job in jobs)
        return len(jobs)

    with patch.object(bot_module, "run_scan", return_value={"new_jobs": new_jobs}), \
         patch.object(bot_module.asyncio, "to_thread", new=AsyncMock(return_value={"new_jobs": new_jobs})), \
         patch.object(bot_module, "get_unposted", return_value=backlog), \
         patch.object(bot_module, "post_jobs_to_discord", side_effect=fake_post_jobs_to_discord):
        bot_module.config["max_posts_per_scan"] = 20
        _run(bot_module.scan_and_post())

    assert posted_order == ["High", "Mid", "Low"]


def _member(user_id, role_ids):
    member = MagicMock()
    member.id = user_id
    member.roles = [MagicMock(id=rid) for rid in role_ids]
    return member


def test_is_premium_member_false_when_no_role_configured():
    bot_module.config["premium_role_id"] = ""
    assert bot_module.is_premium_member(_member(1, [111, 222])) is False


def test_is_premium_member_true_when_member_has_configured_role():
    bot_module.config["premium_role_id"] = "222"
    assert bot_module.is_premium_member(_member(1, [111, 222])) is True


def test_is_premium_member_false_when_member_lacks_configured_role():
    bot_module.config["premium_role_id"] = "999"
    assert bot_module.is_premium_member(_member(1, [111, 222])) is False


def test_get_premium_guild_prefers_configured_guild_id():
    bot_module.config["discord_guild_id"] = "42"
    fake_guild = MagicMock()
    with patch.object(bot_module.bot, "get_guild", return_value=fake_guild) as mock_get_guild:
        result = bot_module.get_premium_guild()

    mock_get_guild.assert_called_once_with(42)
    assert result is fake_guild


def test_get_premium_guild_falls_back_to_sole_guild():
    bot_module.config["discord_guild_id"] = ""
    fake_guild = MagicMock()
    with patch.object(type(bot_module.bot), "guilds", new=[fake_guild]):
        assert bot_module.get_premium_guild() is fake_guild


def test_get_premium_guild_returns_none_when_ambiguous():
    bot_module.config["discord_guild_id"] = ""
    with patch.object(type(bot_module.bot), "guilds", new=[MagicMock(), MagicMock()]):
        assert bot_module.get_premium_guild() is None


def test_build_personal_digests_filters_by_min_score_and_sorts_desc():
    from utils.personalization import PersonalMatchResult

    jobs = [{"id": 1, "company": "A"}, {"id": 2, "company": "B"}, {"id": 3, "company": "C"}]
    verdicts = {1: 5, 2: 2, 3: 4}

    def fake_score(job, blurb, config):
        return PersonalMatchResult(match_score=verdicts[job["id"]], reason=f"r{job['id']}", source="llm")

    with patch.object(bot_module, "score_personal_match", side_effect=fake_score):
        digests = bot_module.build_personal_digests(
            jobs,
            {"u1": {"skills": "backend", "target_roles": "", "education_level": "", "location_pref": ""}},
            {"personal_digest_top_n": 5, "personal_digest_min_score": 4},
        )

    assert [j["company"] for j in digests["u1"]] == ["A", "C"]  # 2 excluded (score 2 < 4)


def test_build_personal_digests_respects_top_n():
    from utils.personalization import PersonalMatchResult

    jobs = [{"id": i, "company": str(i)} for i in range(3)]
    with patch.object(
        bot_module, "score_personal_match",
        return_value=PersonalMatchResult(match_score=5, reason="r", source="llm"),
    ):
        digests = bot_module.build_personal_digests(
            jobs,
            {"u1": {"skills": "backend", "target_roles": "", "education_level": "", "location_pref": ""}},
            {"personal_digest_top_n": 1, "personal_digest_min_score": 1},
        )

    assert len(digests["u1"]) == 1


def test_build_personal_digests_short_circuits_without_calls_on_empty_input():
    with patch.object(bot_module, "score_personal_match") as mock_score:
        assert bot_module.build_personal_digests([], {"u1": "backend"}, {}) == {}
        assert bot_module.build_personal_digests([{"id": 1}], {}, {}) == {}
    mock_score.assert_not_called()


def test_send_personal_digests_skips_forbidden_member_but_sends_to_others():
    class _Forbidden403:
        status = 403
        reason = "Forbidden"
        headers = {}

    digests = {
        "111": [{"id": 1, "company": "Acme", "match_score": 5, "match_reason": "great"}],
        "222": [{"id": 2, "company": "Beta", "match_score": 4, "match_reason": "good"}],
    }
    blocked_member = MagicMock()
    blocked_member.send = AsyncMock(side_effect=discord.Forbidden(_Forbidden403(), "DMs closed"))
    ok_member = MagicMock()
    ok_member.send = AsyncMock()

    guild = MagicMock()
    guild.get_member = lambda uid: {111: blocked_member, 222: ok_member}.get(uid)

    _run(bot_module.send_personal_digests(digests, guild))

    blocked_member.send.assert_awaited_once()
    ok_member.send.assert_awaited_once()
    assert len(ok_member.send.call_args.kwargs["embeds"]) == 1


def test_send_personal_digests_skips_member_missing_from_guild_cache():
    digests = {"111": [{"id": 1, "company": "Acme", "match_score": 5, "match_reason": "great"}]}
    guild = MagicMock()
    guild.get_member = lambda uid: None

    _run(bot_module.send_personal_digests(digests, guild))  # should not raise


def test_send_premium_digests_noop_when_role_not_configured():
    bot_module.config["premium_role_id"] = ""
    with patch.object(bot_module, "get_premium_guild") as mock_get_guild:
        _run(bot_module.send_premium_digests([{"id": 1}]))
    mock_get_guild.assert_not_called()


def test_send_premium_digests_noop_when_no_premium_members_have_profiles():
    bot_module.config["premium_role_id"] = "222"
    guild = MagicMock()
    guild.members = [_member(1, [111])]  # nobody has the premium role

    with patch.object(bot_module, "get_premium_guild", return_value=guild), \
         patch.object(bot_module, "list_member_profiles", return_value={"1": "backend"}), \
         patch.object(bot_module, "send_personal_digests") as mock_send:
        _run(bot_module.send_premium_digests([{"id": 1}]))

    mock_send.assert_not_called()


def test_send_premium_digests_full_flow_sends_only_to_premium_members_with_profiles():
    bot_module.config["premium_role_id"] = "222"
    premium_member = _member(111, [222])
    non_premium_member = _member(333, [111])
    guild = MagicMock()
    guild.members = [premium_member, non_premium_member]

    profiles = {"111": "backend/Go", "333": "frontend", "999": "no such member"}

    with patch.object(bot_module, "get_premium_guild", return_value=guild), \
         patch.object(bot_module, "list_member_profiles", return_value=profiles), \
         patch.object(bot_module.asyncio, "to_thread", new=AsyncMock(return_value={"111": [{"id": 1}]})), \
         patch.object(bot_module, "send_personal_digests", new=AsyncMock()) as mock_send:
        _run(bot_module.send_premium_digests([{"id": 1, "company": "Acme"}]))

    # Only the premium member's profile should have made it into the digest build.
    to_thread_call = bot_module.asyncio.to_thread
    mock_send.assert_awaited_once()
    sent_digests_arg = mock_send.call_args.args[0]
    assert sent_digests_arg == {"111": [{"id": 1}]}


def test_heartbeat_noop_when_url_not_configured():
    bot_module.config["uptime_kuma_push_url"] = ""
    with patch.object(bot_module.requests, "get") as mock_get:
        _run(bot_module.heartbeat.coro())
    mock_get.assert_not_called()


def test_heartbeat_pings_configured_push_url():
    bot_module.config["uptime_kuma_push_url"] = "http://192.168.1.84:3001/api/push/abc123"
    with patch.object(bot_module.requests, "get") as mock_get:
        _run(bot_module.heartbeat.coro())
    mock_get.assert_called_once()
    assert mock_get.call_args.args[0] == "http://192.168.1.84:3001/api/push/abc123"


def test_heartbeat_tolerates_request_failure():
    import requests as requests_module

    bot_module.config["uptime_kuma_push_url"] = "http://192.168.1.84:3001/api/push/abc123"
    with patch.object(bot_module.requests, "get", side_effect=requests_module.RequestException("down")):
        _run(bot_module.heartbeat.coro())  # must not raise


def test_storage_maintenance_calls_run_storage_maintenance_with_configured_retention():
    bot_module.config["data_retention_days"] = 90
    with patch.object(bot_module, "run_storage_maintenance", return_value={"deleted": 3}) as mock_maint:
        _run(bot_module.storage_maintenance.coro())
    mock_maint.assert_called_once_with(90)


def test_storage_maintenance_tolerates_internal_errors():
    with patch.object(bot_module, "run_storage_maintenance", side_effect=RuntimeError("disk full")):
        _run(bot_module.storage_maintenance.coro())  # must not raise


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
