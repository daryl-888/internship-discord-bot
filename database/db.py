"""SQLite database helpers for internship storage and duplicate detection."""

from __future__ import annotations

import hashlib
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from utils.tags import add_company_classification_tag

ROOT_DIR = Path(__file__).resolve().parents[1]
DB_PATH = ROOT_DIR / "internships.db"


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    # WAL lets the dashboard read while the bot writes (and vice versa) without
    # "database is locked" errors; busy_timeout makes writers retry instead of
    # failing immediately when they do briefly collide.
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=5000")
    return conn


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def normalize_value(value: str) -> str:
    return " ".join((value or "").strip().lower().split())


_TRACKING_PARAM_PREFIXES = ("utm_",)
_TRACKING_PARAM_NAMES = {
    "fbclid",
    "gclid",
    "msclkid",
    "mc_cid",
    "mc_eid",
    "ref",
    "ref_src",
    "source",
    "igshid",
}


def strip_tracking_params(url: str) -> str:
    """Drop known tracking params (utm_*, fbclid, ...) and any fragment.

    Many ATS platforms (Greenhouse, Lever, Workday) encode the actual job id
    in a query param, so we only remove params known to be pure tracking
    noise rather than stripping the whole query string — otherwise two
    different real postings could collide into the same dedupe key.
    """
    if not url:
        return url
    try:
        parts = urlsplit(url)
    except ValueError:
        return url

    kept_params = [
        (key, value)
        for key, value in parse_qsl(parts.query, keep_blank_values=True)
        if key.lower() not in _TRACKING_PARAM_NAMES
        and not key.lower().startswith(_TRACKING_PARAM_PREFIXES)
    ]
    new_query = urlencode(kept_params)
    return urlunsplit((parts.scheme, parts.netloc, parts.path, new_query, ""))


def build_dedupe_key(company: str, title: str, application_url: str) -> str:
    raw = "|".join([
        normalize_value(company),
        normalize_value(title),
        normalize_value(strip_tracking_params(application_url)),
    ])
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def init_db() -> None:
    with _connect() as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS internships (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                dedupe_key TEXT UNIQUE NOT NULL,
                company TEXT NOT NULL,
                title TEXT NOT NULL,
                location TEXT,
                application_url TEXT,
                source_url TEXT,
                source_type TEXT,
                tags TEXT,
                uploaded_at TEXT,
                first_seen TEXT NOT NULL,
                last_seen TEXT NOT NULL,
                posted_to_discord INTEGER DEFAULT 0,
                status TEXT DEFAULT 'unknown'
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS app_state (
                key TEXT PRIMARY KEY,
                value TEXT
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS member_profiles (
                user_id TEXT PRIMARY KEY,
                skills TEXT,
                target_roles TEXT,
                education_level TEXT,
                location_pref TEXT,
                updated_at TEXT NOT NULL
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS fast_lane_subscribers (
                user_id TEXT PRIMARY KEY,
                added_at TEXT NOT NULL
            )
            """
        )
        _ensure_column(conn, "member_profiles", "skills", "TEXT")
        _ensure_column(conn, "member_profiles", "target_roles", "TEXT")
        _ensure_column(conn, "member_profiles", "education_level", "TEXT")
        _ensure_column(conn, "member_profiles", "location_pref", "TEXT")
        _ensure_column(conn, "internships", "uploaded_at", "TEXT")
        _ensure_column(conn, "internships", "quality_score", "INTEGER")
        _ensure_column(conn, "internships", "llm_reason", "TEXT")
        conn.commit()


def _ensure_column(conn: sqlite3.Connection, table: str, column: str, column_type: str) -> None:
    columns = {
        row["name"]
        for row in conn.execute(f"PRAGMA table_info({table})").fetchall()
    }
    if column not in columns:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {column_type}")


def upsert_internship(internship: Dict[str, Any]) -> Tuple[int, bool]:
    """
    Insert or update an internship.

    Returns (database_id, is_new). is_new is True only the first time a posting is seen.
    """
    init_db()
    company = internship.get("company") or "Unknown Company"
    title = internship.get("title") or "Unknown Internship"
    application_url = internship.get("application_url") or ""
    dedupe_key = build_dedupe_key(company, title, application_url)
    current_time = now_iso()
    tags_list = add_company_classification_tag(internship.get("tags", []), company)
    internship["tags"] = tags_list
    tags = ",".join(tags_list)
    uploaded_at = internship.get("uploaded_at") or internship.get("date_found") or ""

    with _connect() as conn:
        existing = conn.execute(
            "SELECT id FROM internships WHERE dedupe_key = ?",
            (dedupe_key,),
        ).fetchone()

        if existing:
            conn.execute(
                """
                UPDATE internships
                SET last_seen = ?, location = COALESCE(NULLIF(?, ''), location),
                    source_url = COALESCE(NULLIF(?, ''), source_url),
                    source_type = COALESCE(NULLIF(?, ''), source_type),
                    tags = COALESCE(NULLIF(?, ''), tags),
                    uploaded_at = COALESCE(NULLIF(?, ''), uploaded_at)
                WHERE dedupe_key = ?
                """,
                (
                    current_time,
                    internship.get("location", ""),
                    internship.get("source_url", ""),
                    internship.get("source_type", ""),
                    tags,
                    uploaded_at,
                    dedupe_key,
                ),
            )
            conn.commit()
            return int(existing["id"]), False

        cursor = conn.execute(
            """
            INSERT INTO internships (
                dedupe_key, company, title, location, application_url, source_url,
                source_type, tags, uploaded_at, first_seen, last_seen, posted_to_discord, status
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, ?)
            """,
            (
                dedupe_key,
                company,
                title,
                internship.get("location", ""),
                application_url,
                internship.get("source_url", ""),
                internship.get("source_type", "unknown"),
                tags,
                uploaded_at,
                current_time,
                current_time,
                internship.get("status", "unknown"),
            ),
        )
        conn.commit()
        return int(cursor.lastrowid), True


def mark_posted(internship_ids: Iterable[int]) -> None:
    ids = list(internship_ids)
    if not ids:
        return
    placeholders = ",".join("?" for _ in ids)
    with _connect() as conn:
        conn.execute(
            f"UPDATE internships SET posted_to_discord = 1 WHERE id IN ({placeholders})",
            ids,
        )
        conn.commit()


def list_internships(limit: int = 100, status: Optional[str] = None) -> List[Dict[str, Any]]:
    init_db()
    with _connect() as conn:
        if status:
            rows = conn.execute(
                "SELECT * FROM internships WHERE status = ? ORDER BY first_seen DESC LIMIT ?",
                (status, limit),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM internships ORDER BY first_seen DESC LIMIT ?",
                (limit,),
            ).fetchall()
    return [_row_to_dict(row) for row in rows]


def get_unposted(limit: int = 20) -> List[Dict[str, Any]]:
    init_db()
    with _connect() as conn:
        rows = conn.execute(
            """
            SELECT * FROM internships
            WHERE posted_to_discord = 0
            ORDER BY first_seen ASC
            LIMIT ?
            """,
            (limit,),
        ).fetchall()
    return [_row_to_dict(row) for row in rows]


def update_internship_relevance(internship_id: int, quality_score: int, reason: str) -> None:
    """Persist the local-LLM relevance judgement so the dashboard and future
    scans (via get_unposted) can see and sort on it."""
    with _connect() as conn:
        conn.execute(
            "UPDATE internships SET quality_score = ?, llm_reason = ? WHERE id = ?",
            (quality_score, reason, internship_id),
        )
        conn.commit()


def update_internship_status(internship_id: int, status: str) -> None:
    allowed = {"active", "closed", "unknown", "applied", "ignored", "saved"}
    if status not in allowed:
        raise ValueError(f"Unsupported status: {status}")
    with _connect() as conn:
        conn.execute("UPDATE internships SET status = ? WHERE id = ?", (status, internship_id))
        conn.commit()


def set_state(key: str, value: str) -> None:
    init_db()
    with _connect() as conn:
        conn.execute(
            "INSERT INTO app_state(key, value) VALUES(?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, value),
        )
        conn.commit()


def get_state(key: str, default: str = "") -> str:
    init_db()
    with _connect() as conn:
        row = conn.execute("SELECT value FROM app_state WHERE key = ?", (key,)).fetchone()
    return row["value"] if row else default


def stats() -> Dict[str, Any]:
    init_db()
    with _connect() as conn:
        total = conn.execute("SELECT COUNT(*) AS count FROM internships").fetchone()["count"]
        unposted = conn.execute("SELECT COUNT(*) AS count FROM internships WHERE posted_to_discord = 0").fetchone()["count"]
        applied = conn.execute("SELECT COUNT(*) AS count FROM internships WHERE status = 'applied'").fetchone()["count"]
    return {
        "total": total,
        "unposted": unposted,
        "applied": applied,
        "last_scan_time": get_state("last_scan_time", "Never"),
        "last_scan_found_count": get_state("last_scan_found_count", "0"),
    }


_PROFILE_FIELDS = ("skills", "target_roles", "education_level", "location_pref")


def set_member_profile(
    user_id: str,
    *,
    skills: str = "",
    target_roles: str = "",
    education_level: str = "",
    location_pref: str = "",
) -> None:
    """Save (or replace) a premium member's structured interest profile."""
    init_db()
    with _connect() as conn:
        conn.execute(
            """
            INSERT INTO member_profiles(
                user_id, skills, target_roles, education_level, location_pref, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(user_id) DO UPDATE SET
                skills = excluded.skills,
                target_roles = excluded.target_roles,
                education_level = excluded.education_level,
                location_pref = excluded.location_pref,
                updated_at = excluded.updated_at
            """,
            (str(user_id), skills, target_roles, education_level, location_pref, now_iso()),
        )
        conn.commit()


def get_member_profile(user_id: str) -> Optional[Dict[str, str]]:
    init_db()
    with _connect() as conn:
        row = conn.execute(
            "SELECT skills, target_roles, education_level, location_pref "
            "FROM member_profiles WHERE user_id = ?",
            (str(user_id),),
        ).fetchone()
    return {field: (row[field] or "") for field in _PROFILE_FIELDS} if row else None


def list_member_profiles() -> Dict[str, Dict[str, str]]:
    """Return {user_id: {skills, target_roles, education_level, location_pref}}
    for every member who has set a profile."""
    init_db()
    with _connect() as conn:
        rows = conn.execute(
            "SELECT user_id, skills, target_roles, education_level, location_pref FROM member_profiles"
        ).fetchall()
    return {
        row["user_id"]: {field: (row[field] or "") for field in _PROFILE_FIELDS}
        for row in rows
    }


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


_PRESERVED_STATUSES = {"active", "applied", "saved"}


def prune_old_internships(retention_days: int) -> int:
    """Delete stale closed/unknown/ignored postings older than retention_days.

    Postings a member has flagged active/applied/saved are kept regardless of
    age — those carry personal value the rest don't. Returns rows deleted.
    retention_days <= 0 disables pruning entirely (returns 0).
    """
    if retention_days <= 0:
        return 0

    init_db()
    cutoff = (datetime.now(timezone.utc) - timedelta(days=retention_days)).isoformat()
    placeholders = ",".join("?" for _ in _PRESERVED_STATUSES)
    with _connect() as conn:
        cursor = conn.execute(
            f"""
            DELETE FROM internships
            WHERE last_seen < ?
              AND status NOT IN ({placeholders})
            """,
            (cutoff, *_PRESERVED_STATUSES),
        )
        conn.commit()
        return cursor.rowcount


def checkpoint_and_vacuum() -> None:
    """Flush the WAL file and reclaim disk space freed by deletes.

    Cheap on a small database, so it's safe to call on every maintenance tick
    regardless of whether pruning actually deleted anything this time.
    """
    conn = _connect()
    try:
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        conn.isolation_level = None  # VACUUM can't run inside a transaction.
        conn.execute("VACUUM")
    finally:
        conn.close()


def run_storage_maintenance(retention_days: int) -> Dict[str, int]:
    """Prune stale rows, then checkpoint/VACUUM. Meant to run on a schedule."""
    deleted = prune_old_internships(retention_days)
    checkpoint_and_vacuum()
    return {"deleted": deleted}


def get_db_file_size_bytes() -> int:
    return DB_PATH.stat().st_size if DB_PATH.exists() else 0


def _row_to_dict(row: sqlite3.Row) -> Dict[str, Any]:
    data = dict(row)
    data["tags"] = [tag for tag in (data.get("tags") or "").split(",") if tag]
    return data
