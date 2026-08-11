"""Discord internship bot entry point.

Run locally with:
    python bot.py
"""

from __future__ import annotations

import asyncio
import logging
from typing import Dict, List

import discord
import requests
from discord import app_commands
from discord.ext import commands, tasks

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
from scanner import run_scan, run_watched_company_scan
from scraper.jobright_manual import build_manual_jobright_job
from scraper.linkedin_manual import build_manual_linkedin_job
from utils.config_loader import load_config, save_config
from utils.filters import passes_age_filter
from utils.formatting import chunk_list, internship_to_embed, personal_match_to_embed
from utils.personalization import format_profile_for_prompt, score_personal_match
from utils.relevance import NEUTRAL_QUALITY_SCORE
from utils.source_store import add_source, load_sources, remove_source
from utils.watched_companies_store import get_enabled_watched_companies, load_watched_companies

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)
LOGGER = logging.getLogger("internship-bot")

config = load_config()

intents = discord.Intents.default()
# Slash commands do not need message_content intent. Keeping it off makes setup easier.
# Members intent IS required (and privileged — enable "Server Members Intent" in the
# Discord Developer Portal's Bot tab) for the premium tier: it's how the bot resolves
# which members hold the premium role so it knows who to send personalized DMs to.
intents.members = True
bot = commands.Bot(command_prefix="!", intents=intents)
startup_scan_completed = False


def get_post_channel() -> discord.TextChannel | None:
    channel_id = str(config.get("discord_channel_id", "")).strip()
    if not channel_id:
        return None
    channel = bot.get_channel(int(channel_id))
    return channel if isinstance(channel, discord.TextChannel) else None


def is_premium_member(member: discord.Member) -> bool:
    """Premium status is just: does this member hold the configured role.

    Officers assign/remove the role manually (e.g. when dues are paid), so
    there's no billing logic here at all — just a role check.
    """
    role_id = str(config.get("premium_role_id", "")).strip()
    if not role_id:
        return False
    return any(str(role.id) == role_id for role in getattr(member, "roles", []))


def get_premium_guild() -> discord.Guild | None:
    """The single guild this bot serves (premium DMs are single-server for now)."""
    guild_id = str(config.get("discord_guild_id", "")).strip()
    if guild_id:
        return bot.get_guild(int(guild_id))
    if len(bot.guilds) == 1:
        return bot.guilds[0]
    return None


async def post_jobs_to_discord(jobs: List[dict]) -> int:
    """Post jobs in small embed batches and return the count posted."""
    channel = get_post_channel()
    if channel is None:
        LOGGER.warning("No valid Discord channel configured. Use /set_channel first.")
        return 0

    max_posts = int(config.get("max_posts_per_scan", 20))
    jobs_to_post = jobs[:max_posts]
    posted_count = 0

    # Discord allows up to 10 embeds per message. Use 5 for cleaner reading.
    for batch in chunk_list(jobs_to_post, 5):
        embeds = [internship_to_embed(job) for job in batch]
        try:
            await channel.send(embeds=embeds)
        except discord.HTTPException:
            LOGGER.exception("Failed to post a batch of %s job(s); will retry next scan", len(batch))
            continue

        # Mark this batch posted immediately so a later batch's failure can't
        # cause an already-sent batch to be re-sent on the next scan.
        mark_posted([job["id"] for job in batch if "id" in job])
        posted_count += len(batch)
        await asyncio.sleep(1)

    return posted_count


def _merge_unique_jobs(*job_lists: List[dict]) -> List[dict]:
    """Combine job lists, keeping the first occurrence of each database id."""
    merged: List[dict] = []
    seen_ids = set()
    for jobs in job_lists:
        for job in jobs:
            job_id = job.get("id")
            if job_id is not None:
                if job_id in seen_ids:
                    continue
                seen_ids.add(job_id)
            merged.append(job)
    return merged


def _quality_score(job: dict) -> int:
    score = job.get("quality_score")
    return score if isinstance(score, int) else NEUTRAL_QUALITY_SCORE


def build_personal_digests(
    new_jobs: List[dict], profiles_by_user_id: Dict[str, Dict[str, str]], config: dict
) -> Dict[str, List[dict]]:
    """Score this scan's new jobs against each premium member's structured
    profile and keep their top matches. Does many blocking Ollama calls, so
    callers should run this via asyncio.to_thread instead of awaiting it
    directly.
    """
    if not new_jobs or not profiles_by_user_id:
        return {}

    top_n = int(config.get("personal_digest_top_n", 5))
    min_score = int(config.get("personal_digest_min_score", 4))
    digests: Dict[str, List[dict]] = {}

    for user_id, profile in profiles_by_user_id.items():
        blurb = format_profile_for_prompt(profile)
        matches = []
        for job in new_jobs:
            verdict = score_personal_match(job, blurb, config)
            if verdict.match_score >= min_score:
                matches.append({**job, "match_score": verdict.match_score, "match_reason": verdict.reason})
        matches.sort(key=lambda job: job["match_score"], reverse=True)
        if matches:
            digests[user_id] = matches[:top_n]

    return digests


async def send_personal_digests(digests: Dict[str, List[dict]], guild: discord.Guild) -> None:
    for user_id, matches in digests.items():
        member = guild.get_member(int(user_id))
        if member is None:
            LOGGER.warning("Premium member %s not found in guild cache; skipping their digest", user_id)
            continue

        embeds = [
            personal_match_to_embed(job, job["match_score"], job["match_reason"]) for job in matches
        ]
        for index, batch in enumerate(chunk_list(embeds, 5)):
            content = "Your personalized internship matches from this scan:" if index == 0 else None
            try:
                await member.send(content=content, embeds=batch)
            except discord.Forbidden:
                LOGGER.warning("Could not DM premium member %s (DMs closed); skipping", user_id)
                break
            except discord.HTTPException:
                LOGGER.exception("Failed to send a personal digest batch to %s", user_id)
                continue


async def send_premium_digests(new_jobs: List[dict]) -> None:
    """Premium members (see /set_premium_role) with a saved /set_profile blurb
    get a personalized DM highlighting their best matches from this scan, on
    top of (not instead of) the shared channel feed everyone else gets."""
    if not config.get("premium_role_id"):
        return

    guild = get_premium_guild()
    if guild is None:
        return

    profiles = list_member_profiles()
    if not profiles:
        return

    premium_user_ids = {str(member.id) for member in guild.members if is_premium_member(member)}
    relevant_profiles = {uid: blurb for uid, blurb in profiles.items() if uid in premium_user_ids}
    if not relevant_profiles:
        return

    digests = await asyncio.to_thread(build_personal_digests, new_jobs, relevant_profiles, config)
    if digests:
        await send_personal_digests(digests, guild)


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


async def scan_and_post() -> dict:
    # Scanning does blocking network I/O; run it off the event loop so the bot
    # keeps responding to Discord (heartbeats, other commands) while it scans.
    result = await asyncio.to_thread(run_scan, config)

    # Jobs found in an earlier scan but never posted (because that scan hit
    # max_posts_per_scan) are queued here so they get caught up instead of lost.
    # But a job can also sit unposted long enough to age past max_posting_age_days
    # by the time it's finally its turn — re-check the same age filter run_scan()
    # already applied at scrape time, using each job's freshest uploaded_at.
    max_posts = int(config.get("max_posts_per_scan", 20))
    max_posting_age_days = config.get("max_posting_age_days", 3)
    backlog = get_unposted(limit=max_posts * 5)
    backlog = [job for job in backlog if passes_age_filter(job, max_posting_age_days)]
    jobs_to_post = _merge_unique_jobs(backlog, result["new_jobs"])

    # Best matches first, so when there are more postings than max_posts_per_scan
    # can send in one go, the strongest ones win the scarce slots instead of
    # whichever happened to appear earliest in a README. Sort is stable, so
    # postings with equal (or no) score keep their original FIFO order.
    jobs_to_post.sort(key=_quality_score, reverse=True)

    posted_count = await post_jobs_to_discord(jobs_to_post)
    result["posted_count"] = posted_count

    try:
        await send_premium_digests(result["new_jobs"])
    except Exception:
        LOGGER.exception("Premium digest step failed; shared-channel posting is unaffected")

    return result


@bot.event
async def on_ready() -> None:
    global startup_scan_completed

    init_db()
    LOGGER.info("Logged in as %s", bot.user)

    # Guild sync is much faster than global sync while building/testing locally.
    guild_id = str(config.get("discord_guild_id", "")).strip()
    try:
        if guild_id:
            guild = discord.Object(id=int(guild_id))
            bot.tree.copy_global_to(guild=guild)
            await bot.tree.sync(guild=guild)
            LOGGER.info("Slash commands synced to guild %s", guild_id)
        else:
            await bot.tree.sync()
            LOGGER.info("Slash commands synced globally. This can take a while to appear.")
    except Exception:
        LOGGER.exception("Failed to sync slash commands")

    if config.get("auto_scan_enabled") and not scheduled_scan.is_running():
        scheduled_scan.change_interval(minutes=int(config.get("scan_interval_minutes", 240)))
        scheduled_scan.start()

    if config.get("auto_scan_enabled") and not scheduled_watched_company_scan.is_running():
        scheduled_watched_company_scan.change_interval(
            minutes=int(config.get("fast_scan_interval_minutes", 10))
        )
        scheduled_watched_company_scan.start()

    if config.get("uptime_kuma_push_url") and not heartbeat.is_running():
        heartbeat.change_interval(minutes=int(config.get("heartbeat_interval_minutes", 5)))
        heartbeat.start()

    if not storage_maintenance.is_running():
        storage_maintenance.change_interval(
            hours=int(config.get("storage_maintenance_interval_hours", 24))
        )
        storage_maintenance.start()

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


@tasks.loop(minutes=240)
async def scheduled_scan() -> None:
    try:
        LOGGER.info("Running scheduled internship scan")
        await scan_and_post()
    except Exception:
        LOGGER.exception("Scheduled scan failed")


@scheduled_scan.before_loop
async def before_scheduled_scan() -> None:
    await asyncio.sleep(int(config.get("scan_interval_minutes", 240)) * 60)


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


@tasks.loop(minutes=5)
async def heartbeat() -> None:
    """Ping an Uptime Kuma Push monitor so it can alert if this process dies
    or hangs. Bot has no HTTP surface, so push (we ping it) instead of pull
    (it pings us) is the natural fit. No-ops if unconfigured.
    """
    push_url = str(config.get("uptime_kuma_push_url", "")).strip()
    if not push_url:
        return
    try:
        await asyncio.to_thread(requests.get, push_url, timeout=10)
    except requests.RequestException:
        # Don't retry harder or crash — Kuma itself being briefly unreachable
        # isn't this bot's problem, and the next tick will try again shortly.
        LOGGER.warning("Uptime Kuma heartbeat request failed (network issue)")


@tasks.loop(hours=24)
async def storage_maintenance() -> None:
    """Prune stale rows and reclaim disk space on a schedule. Always runs
    (not config-gated) since it has no external dependency and is cheap even
    when there's nothing to prune; data_retention_days<=0 skips pruning
    specifically while still checkpointing/vacuuming.
    """
    retention_days = int(config.get("data_retention_days", 180))
    try:
        result = await asyncio.to_thread(run_storage_maintenance, retention_days)
        LOGGER.info("Storage maintenance done: pruned %s old row(s)", result["deleted"])
    except Exception:
        LOGGER.exception("Storage maintenance failed")


@bot.tree.command(name="scan", description="Manually scan all enabled internship sources.")
@app_commands.guild_only()
async def scan_command(interaction: discord.Interaction) -> None:
    member = interaction.user
    if not isinstance(member, discord.Member) or not is_premium_member(member):
        await interaction.response.send_message(
            "Manual scanning is a premium-member feature. Ask an officer about premium membership.",
            ephemeral=True,
        )
        return

    await interaction.response.defer(ephemeral=True, thinking=True)
    result = await scan_and_post()
    await interaction.followup.send(
        "Scan complete. "
        f"Sources scanned: {result['sources_scanned']}. "
        f"Found after filters: {result['total_found_after_filters']}. "
        f"New jobs: {len(result['new_jobs'])}. "
        f"Posted: {result['posted_count']}. "
        f"Errors: {len(result['errors'])}.",
        ephemeral=True,
    )


@bot.tree.command(name="add_source", description="Add a GitHub internship README source URL.")
@app_commands.describe(url="GitHub repository or README URL")
@app_commands.default_permissions(manage_guild=True)
@app_commands.guild_only()
async def add_source_command(interaction: discord.Interaction, url: str) -> None:
    if "github.com" not in url and "raw.githubusercontent.com" not in url:
        await interaction.response.send_message(
            "Please add a GitHub README/repository URL. LinkedIn/Jobright links should use /add_manual_job.",
            ephemeral=True,
        )
        return
    source = add_source(url, "github_readme")
    await interaction.response.send_message(
        f"Added/enabled source `{source['id']}`: {source['url']}",
        ephemeral=True,
    )


@bot.tree.command(name="list_sources", description="Show all saved internship sources.")
@app_commands.default_permissions(manage_guild=True)
@app_commands.guild_only()
async def list_sources_command(interaction: discord.Interaction) -> None:
    sources = load_sources()
    if not sources:
        await interaction.response.send_message("No sources saved yet.", ephemeral=True)
        return

    lines = []
    for source in sources:
        status = "enabled" if source.get("enabled", True) else "disabled"
        lines.append(f"`{source['id']}` | {status} | {source.get('type')} | {source.get('url')}")

    message = "\n".join(lines)
    if len(message) > 1900:
        message = message[:1900] + "\n..."
    await interaction.response.send_message(message, ephemeral=True)


@bot.tree.command(name="remove_source", description="Remove a source by ID or exact URL.")
@app_commands.describe(url_or_id="Source ID from /list_sources or the exact URL")
@app_commands.default_permissions(manage_guild=True)
@app_commands.guild_only()
async def remove_source_command(interaction: discord.Interaction, url_or_id: str) -> None:
    removed = remove_source(url_or_id)
    if removed:
        await interaction.response.send_message("Source removed.", ephemeral=True)
    else:
        await interaction.response.send_message("Could not find that source.", ephemeral=True)


@bot.tree.command(name="set_channel", description="Set this channel as the internship posting channel.")
@app_commands.default_permissions(manage_guild=True)
@app_commands.guild_only()
async def set_channel_command(interaction: discord.Interaction) -> None:
    config["discord_channel_id"] = str(interaction.channel_id)
    save_config(config)
    await interaction.response.send_message(
        f"Set <#{interaction.channel_id}> as the internship posting channel.",
        ephemeral=True,
    )


@bot.tree.command(
    name="set_premium_role",
    description="Set which role counts as a premium member (admin only).",
)
@app_commands.describe(role="The role that marks someone as a paid/premium member")
@app_commands.default_permissions(manage_guild=True)
@app_commands.guild_only()
async def set_premium_role_command(interaction: discord.Interaction, role: discord.Role) -> None:
    config["premium_role_id"] = str(role.id)
    save_config(config)
    await interaction.response.send_message(
        f"Set {role.mention} as the premium member role. Members with this role get a "
        "personalized DM digest once they run /set_profile.",
        ephemeral=True,
    )


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


class ProfileModal(discord.ui.Modal, title="Set Your Internship Profile"):
    skills = discord.ui.TextInput(
        label="Skills / interests",
        placeholder="e.g. Python, backend, GPU/CUDA, machine learning",
        max_length=200,
    )
    target_roles = discord.ui.TextInput(
        label="Target roles",
        placeholder="e.g. SWE intern, data science intern",
        max_length=200,
        required=False,
    )
    education_level = discord.ui.TextInput(
        label="Education level / year",
        placeholder="e.g. Sophomore, Junior, Master's",
        max_length=100,
        required=False,
    )
    location_pref = discord.ui.TextInput(
        label="Location preference",
        placeholder="e.g. Remote OK, Bay Area, Austin TX",
        max_length=150,
        required=False,
    )

    async def on_submit(self, interaction: discord.Interaction) -> None:
        set_member_profile(
            str(interaction.user.id),
            skills=str(self.skills).strip(),
            target_roles=str(self.target_roles).strip(),
            education_level=str(self.education_level).strip(),
            location_pref=str(self.location_pref).strip(),
        )
        await interaction.response.send_message(
            "Saved. You'll get a personalized DM after each scan highlighting your best matches.",
            ephemeral=True,
        )


@bot.tree.command(
    name="set_profile",
    description="Premium members: set your structured internship profile for personalized matching.",
)
@app_commands.guild_only()
async def set_profile_command(interaction: discord.Interaction) -> None:
    member = interaction.user
    if not isinstance(member, discord.Member) or not is_premium_member(member):
        await interaction.response.send_message(
            "Personalized matching is a premium-member feature. Ask an officer about "
            "premium membership.",
            ephemeral=True,
        )
        return

    modal = ProfileModal()
    existing = get_member_profile(str(member.id)) or {}
    modal.skills.default = existing.get("skills", "")
    modal.target_roles.default = existing.get("target_roles", "")
    modal.education_level.default = existing.get("education_level", "")
    modal.location_pref.default = existing.get("location_pref", "")
    await interaction.response.send_modal(modal)


@bot.tree.command(name="my_profile", description="Show your saved internship interest profile.")
@app_commands.guild_only()
async def my_profile_command(interaction: discord.Interaction) -> None:
    member = interaction.user
    premium = isinstance(member, discord.Member) and is_premium_member(member)
    profile = get_member_profile(str(interaction.user.id))

    lines = [f"Premium member: `{premium}`"]
    if profile and any(profile.values()):
        if profile.get("skills"):
            lines.append(f"Skills/interests: {profile['skills']}")
        if profile.get("target_roles"):
            lines.append(f"Target roles: {profile['target_roles']}")
        if profile.get("education_level"):
            lines.append(f"Education level: {profile['education_level']}")
        if profile.get("location_pref"):
            lines.append(f"Location preference: {profile['location_pref']}")
    elif premium:
        lines.append("No profile saved yet. Use `/set_profile` to add one.")
    else:
        lines.append("Personalized matching is a premium-member feature.")
    await interaction.response.send_message("\n".join(lines), ephemeral=True)


@bot.tree.command(name="status", description="Show bot status and scan stats.")
async def status_command(interaction: discord.Interaction) -> None:
    current_stats = stats()
    sources = load_sources()
    enabled_sources = [source for source in sources if source.get("enabled", True)]
    channel_id = config.get("discord_channel_id") or "Not set"
    db_size_mb = get_db_file_size_bytes() / (1024 * 1024)
    await interaction.response.send_message(
        "**Internship Bot Status**\n"
        f"Bot user: `{bot.user}`\n"
        f"Posting channel: `{channel_id}`\n"
        f"Auto scan: `{config.get('auto_scan_enabled')}` every `{config.get('scan_interval_minutes')}` minutes\n"
        f"Scan on startup: `{config.get('auto_scan_on_start')}`\n"
        f"LLM relevance filter: `{config.get('llm_filter_enabled', False)}`\n"
        f"Premium role: `{'configured' if config.get('premium_role_id') else 'not set'}`\n"
        f"Fast-lane scan: every `{config.get('fast_scan_interval_minutes', 10)}` minutes, "
        f"`{len(list_fast_lane_subscribers())}` subscriber(s)\n"
        f"Watched companies: `{len(get_enabled_watched_companies())}` enabled / "
        f"`{len(load_watched_companies())}` total\n"
        f"Uptime Kuma heartbeat: `{'enabled' if heartbeat.is_running() else 'disabled'}`\n"
        f"Sources: `{len(enabled_sources)}` enabled / `{len(sources)}` total\n"
        f"Last scan: `{current_stats['last_scan_time']}`\n"
        f"Jobs found last scan: `{current_stats['last_scan_found_count']}`\n"
        f"Total jobs in DB: `{current_stats['total']}`\n"
        f"Unposted jobs: `{current_stats['unposted']}`\n"
        f"Applied jobs: `{current_stats['applied']}`\n"
        f"Database size: `{db_size_mb:.2f} MB`\n"
        f"Data retention: `{config.get('data_retention_days', 180)}` days\n"
        f"Max posting age: `{config.get('max_posting_age_days', 3)}` days",
        ephemeral=True,
    )


@bot.tree.command(name="add_manual_job", description="Save a LinkedIn or Jobright job link manually.")
@app_commands.describe(
    source="Choose linkedin or jobright",
    url="Job link you manually copied",
    company="Company name",
    title="Role title",
    location="Location",
)
@app_commands.default_permissions(manage_guild=True)
@app_commands.guild_only()
async def add_manual_job_command(
    interaction: discord.Interaction,
    source: str,
    url: str,
    company: str = "Unknown Company",
    title: str = "Internship Link",
    location: str = "Unknown",
) -> None:
    source_lower = source.lower().strip()
    if source_lower == "linkedin":
        job = build_manual_linkedin_job(url, company, title, location)
    elif source_lower == "jobright":
        job = build_manual_jobright_job(url, company, title, location)
    else:
        await interaction.response.send_message("Source must be `linkedin` or `jobright`.", ephemeral=True)
        return

    db_id, is_new = upsert_internship(job)
    job["id"] = db_id

    posted = 0
    if is_new:
        posted = await post_jobs_to_discord([job])

    await interaction.response.send_message(
        f"Manual job saved. New: `{is_new}`. Posted: `{posted}`.",
        ephemeral=True,
    )


@bot.tree.command(name="help", description="Show available commands.")
async def help_command(interaction: discord.Interaction) -> None:
    await interaction.response.send_message(
        "**Commands**\n"
        "`/scan` — premium members: manually scan all enabled GitHub sources\n"
        "`/add_source <url>` — admin: add a GitHub internship repo/README\n"
        "`/list_sources` — admin: show saved sources\n"
        "`/remove_source <url_or_id>` — admin: remove a source\n"
        "`/set_channel` — admin: set this channel as the posting channel\n"
        "`/status` — show bot status and database stats\n"
        "`/add_manual_job <source> <url> [company] [title] [location]` — admin: save LinkedIn/Jobright links manually\n"
        "`/set_premium_role <role>` — admin: set which role gets personalized DM digests\n"
        "`/fast_lane_add <member>` — admin: add a member to the instant-DM list for watched companies\n"
        "`/fast_lane_remove <member>` — admin: remove a member from that list\n"
        "`/fast_lane_list` — admin: show current fast-lane subscribers\n"
        "`/set_profile` — premium members: fill out a short form (skills, target roles, "
        "education level, location) for personalized matching\n"
        "`/my_profile` — show your saved profile and premium status\n"
        "`/help` — show this message",
        ephemeral=True,
    )


def main() -> None:
    token = config.get("discord_token", "")
    if not token:
        raise RuntimeError("DISCORD_TOKEN is missing. Copy .env.example to .env and add your token.")
    bot.run(token)


if __name__ == "__main__":
    main()
