"""Load and save local configuration for the internship bot."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Dict

from dotenv import load_dotenv

ROOT_DIR = Path(__file__).resolve().parents[1]
CONFIG_PATH = ROOT_DIR / "config.json"

DEFAULT_CONFIG: Dict[str, Any] = {
    "discord_channel_id": "",
    "scan_interval_minutes": 360,
    # Separate, much shorter cadence for watched_companies.json (Workday
    # fast lane) — see fast_lane_subscribers / /fast_lane_add. Independent
    # of scan_interval_minutes/auto_scan_enabled's GitHub-source cadence.
    "fast_scan_interval_minutes": 10,
    "auto_scan_enabled": True,
    "auto_scan_on_start": True,
    "max_posts_per_scan": 20,
    "include_keywords": [
        "software",
        "swe",
        "intern",
        "internship",
        "co-op",
        "data",
        "ai",
        "machine learning",
        "backend",
        "frontend",
        "cloud",
        "quant",
        "gpu",
        "cuda",
        "hardware",
    ],
    "exclude_keywords": [
        "senior",
        "staff",
        "principal",
        "full-time",
        "new grad",
    ],
    # Postings older than this (per the source's Age/Date column, e.g. "3d",
    # "1mo") are skipped entirely at scan time so the channel doesn't fill up
    # with stale listings. <= 0 disables this filter.
    "max_posting_age_days": 3,
    # Optional second-pass filtering/ranking using a local Ollama model. Off by
    # default since it requires a running Ollama server with a model pulled.
    "llm_filter_enabled": False,
    "ollama_host": "http://192.168.1.84:11434",
    "ollama_model": "llama3.2:3b",
    "llm_timeout_seconds": 15,
    "llm_min_quality_score": 1,
    # Premium tier: members with this role get a personalized DM digest after
    # each scan (see /set_premium_role, /set_profile). Empty = feature off.
    "premium_role_id": "",
    "personal_digest_top_n": 5,
    "personal_digest_min_score": 4,
    # Uptime Kuma Push-monitor heartbeat. Empty = feature off (no HTTP surface
    # otherwise, so this is a periodic outbound ping, not an inbound check).
    "uptime_kuma_push_url": "",
    "heartbeat_interval_minutes": 5,
    # Storage maintenance always runs (no external dependency); this just
    # controls how far back it prunes. <= 0 disables pruning but still
    # checkpoints/vacuums the database on the same schedule.
    "data_retention_days": 180,
    "storage_maintenance_interval_hours": 24,
}


def load_config() -> Dict[str, Any]:
    """Load config.json and .env, then return one merged config dictionary."""
    load_dotenv(ROOT_DIR / ".env")

    config = DEFAULT_CONFIG.copy()
    if CONFIG_PATH.exists():
        with CONFIG_PATH.open("r", encoding="utf-8") as f:
            user_config = json.load(f)
            config.update(user_config)

    # Environment variables win over config.json when present.
    if os.getenv("DISCORD_CHANNEL_ID"):
        config["discord_channel_id"] = os.getenv("DISCORD_CHANNEL_ID")
    if os.getenv("SCAN_INTERVAL_MINUTES"):
        config["scan_interval_minutes"] = int(os.getenv("SCAN_INTERVAL_MINUTES", "240"))
    if os.getenv("AUTO_SCAN_ENABLED"):
        config["auto_scan_enabled"] = os.getenv("AUTO_SCAN_ENABLED", "").strip().lower() in {"1", "true", "yes", "on"}
    if os.getenv("AUTO_SCAN_ON_START"):
        config["auto_scan_on_start"] = os.getenv("AUTO_SCAN_ON_START", "").strip().lower() in {"1", "true", "yes", "on"}
    if os.getenv("MAX_POSTS_PER_SCAN"):
        config["max_posts_per_scan"] = int(os.getenv("MAX_POSTS_PER_SCAN", "20"))
    if os.getenv("LLM_FILTER_ENABLED"):
        config["llm_filter_enabled"] = os.getenv("LLM_FILTER_ENABLED", "").strip().lower() in {
            "1",
            "true",
            "yes",
            "on",
        }
    if os.getenv("OLLAMA_HOST"):
        config["ollama_host"] = os.getenv("OLLAMA_HOST")
    if os.getenv("OLLAMA_MODEL"):
        config["ollama_model"] = os.getenv("OLLAMA_MODEL")
    if os.getenv("UPTIME_KUMA_PUSH_URL"):
        config["uptime_kuma_push_url"] = os.getenv("UPTIME_KUMA_PUSH_URL")

    config["discord_token"] = os.getenv("DISCORD_TOKEN", "")
    config["discord_guild_id"] = os.getenv("DISCORD_GUILD_ID", "")
    return config


def save_config(config: Dict[str, Any]) -> None:
    """Save non-secret config values back to config.json."""
    safe_config = {k: v for k, v in config.items() if k not in {"discord_token", "discord_guild_id"}}
    with CONFIG_PATH.open("w", encoding="utf-8") as f:
        json.dump(safe_config, f, indent=2)
