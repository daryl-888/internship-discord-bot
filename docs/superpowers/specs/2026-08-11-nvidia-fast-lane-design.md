# NVIDIA / Watched-Company Fast Lane — Design

Date: 2026-08-11

## Purpose

The bot currently scans GitHub-README-based internship lists every
`scan_interval_minutes` (360 = 6h) and posts matches to the shared channel,
plus a personalized DM digest for premium members with a `/set_profile`
blurb. That cadence and audience don't fit a new need: get notified within
minutes when a specific target company (starting with NVIDIA, more to be
named later) posts a new internship, sent only to a short list of people the
server admin explicitly picks — independent of the existing premium tier.

## Non-goals

- Not replacing or modifying the existing premium-role personal digest.
- Not building a generic multi-ATS (Greenhouse/Lever/etc.) abstraction ahead
  of need — only Workday is implemented now, since NVIDIA uses it and it's
  common among large companies. Non-Workday companies named later will need
  their own scraper module when they come up.
- Not exposing company management via a slash command — `watched_companies.json`
  is hand-edited like `config.json` already is, consistent with this
  project's existing bind-mount-and-restart deploy model.

## Watched-company scraping

### Config: `watched_companies.json` (new file, project root)

```json
[
  {
    "id": "nvidia",
    "name": "NVIDIA",
    "ats": "workday",
    "tenant_host": "nvidia.wd5.myworkdayjobs.com",
    "site": "NVIDIAExternalCareerSite",
    "intern_facet_id": "0c40f6bd1d8f10adf6dae42e46d44a17",
    "enabled": true
  }
]
```

`intern_facet_id` is Workday's own `workerSubType` facet value for
"Intern (Fixed Term)" on that tenant — verified live against NVIDIA's
public search endpoint on 2026-08-11 (`POST
https://nvidia.wd5.myworkdayjobs.com/wday/cxs/nvidia/NVIDIAExternalCareerSite/jobs`,
no auth, same request the site's own JS makes). Using this facet instead of
a free-text search avoids the false positives a plain `searchText=intern`
query produces once you page past the first ~20 results (Workday's search is
relevance-ranked full-text, not a title substring match, so deep pages
return unrelated senior roles).

Adding a future Workday-based company later means adding one more object to
this file with that company's own tenant host, site slug, and intern facet
id (found the same way: call the tenant's `/wday/cxs/<tenant>/<site>/jobs`
endpoint with an empty facet and inspect the `workerSubType` facet's
`values` for the intern-equivalent descriptor).

### `scraper/workday_scraper.py` (new)

`scrape_workday_intern_jobs(company_config: dict) -> List[Dict]`:

- POSTs to `https://{tenant_host}/wday/cxs/{tenant_slug}/{site}/jobs` with
  `appliedFacets: {"workerSubType": [intern_facet_id]}`, paginating
  (`limit=20`, incrementing `offset`) until all of `total` is fetched.
- Builds each internship dict in the same shape `github_scraper.py`
  produces: `company` (from `company_config["name"]`), `title`,
  `location` (from `locationsText`), `application_url` (tenant host +
  careers site path + `externalPath`), `source_url` (the public careers
  site URL), `source_type="workday"`, `tags` (via
  `github_scraper.infer_tags()` — reused, not duplicated), `status`.
- Converts Workday's bucketed `postedOn` text into the compact relative-age
  format `utils/filters.parse_age_in_days()` already parses, so no changes
  are needed there:
  - `"Posted Today"` → `"0d"`
  - `"Posted Yesterday"` → `"1d"`
  - `"Posted N Days Ago"` → `"Nd"`
  - `"Posted 30+ Days Ago"` → `"30d"`
  - Anything unrecognized is passed through as-is (existing filter code
    already treats unparseable age text as "let it through").
- Network/HTTP failures raise, same contract as `github_scraper.py` —
  `scanner.py`'s per-source `try/except` keeps other sources scanning.

## Scan scheduling

A second, independent scan loop in `bot.py`, alongside the existing one:

| Loop | Sources | Interval | Config key |
|------|---------|----------|------------|
| Existing | `sources.json` (`github_readme`, manual) | `scan_interval_minutes` (360) | already exists |
| New | `watched_companies.json` | `fast_scan_interval_minutes` (10, new default) | `fast_scan_interval_minutes` |

New `scanner.run_watched_company_scan(config) -> Dict[str, Any]`: same
shape/behavior as `run_scan()` (age + keyword filtering, optional LLM
relevance filter, `upsert_internship` dedup, returns `new_jobs`), but reads
`watched_companies.json` via a new `utils/watched_companies_store.py`
(mirrors `utils/source_store.py`) and calls `scrape_workday_intern_jobs()`
per enabled company instead of `scrape_github_readme()`.

Both loops write to the same `internships` table, so the dedupe key
(`company` + `title` + `application_url` hash) means a posting is only ever
"new" once regardless of which loop finds it first — the fast loop will
almost always win that race given the interval difference, which is the
point.

Watched-company postings that pass the filters still get posted to the
shared Discord channel and still flow into the existing premium personal
digest — this is additive, not a replacement for that pipeline.

## Fast-lane member selection (explicit list, not a role)

The server admin (Manage Server permission — same gate as
`/set_premium_role`, via `@app_commands.default_permissions(manage_guild=True)`
+ `@app_commands.guild_only()`) explicitly adds/removes individual members.
No Discord role is involved.

### DB: new table in `database/db.py`

```sql
CREATE TABLE IF NOT EXISTS fast_lane_subscribers (
    user_id TEXT PRIMARY KEY,
    added_at TEXT NOT NULL
)
```

New functions mirroring the existing `member_profiles` helpers:
`add_fast_lane_subscriber(user_id)`, `remove_fast_lane_subscriber(user_id)`,
`list_fast_lane_subscribers() -> List[str]`.

### New commands in `bot.py`

- `/fast_lane_add <member>` — admin-only, inserts the mentioned member's
  user id.
- `/fast_lane_remove <member>` — admin-only, removes it.
- `/fast_lane_list` — admin-only, shows current subscribers (ephemeral).

### DM delivery

New `send_fast_lane_alerts(new_jobs: List[dict], guild: discord.Guild)` in
`bot.py`, called after `run_watched_company_scan()` returns non-empty
`new_jobs` (same call site pattern as the existing
`await send_premium_digests(result["new_jobs"])` line, just for the new
loop). For each subscriber in `list_fast_lane_subscribers()`, resolves the
member via the guild cache (same `guild.get_member`/DM-closed handling
already used in `send_personal_digests`) and DMs a plain formatted message
per new job — company, title, location, apply link. No LLM scoring, no
`/set_profile` dependency; this tier is about speed, not personalization.

## Config additions to `config.json`

```json
"fast_scan_interval_minutes": 10
```

(`fast_lane_role_id` is explicitly NOT added — this tier uses the explicit
subscriber table above, not a role.)

## Testing

- `tests/test_workday_scraper.py`: pagination across multiple pages,
  facet application, each `postedOn` bucket → age-string conversion case,
  malformed/empty response handling.
- `tests/test_scanner.py`: extend or add coverage for
  `run_watched_company_scan()` mirroring existing `run_scan()` tests
  (filters applied, dedup, LLM-filter-disabled path).
- `tests/test_db.py`: `fast_lane_subscribers` add/remove/list round-trip.
- `tests/test_bot.py`: `send_fast_lane_alerts()` — new jobs DMed to listed
  subscribers only, DM-closed member handled without raising, empty
  subscriber list is a no-op.

## Error handling

Consistent with the rest of the bot: source-level failures (a bad Workday
response, a network timeout) are caught and logged per-company, not fatal
to the scan; DM failures (closed DMs, member left the guild) are caught
per-recipient so one failure doesn't block alerting everyone else.
