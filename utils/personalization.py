"""Per-member personalized match scoring for the premium tier.

utils/relevance.py scores a posting once for the whole server. This scores it
again per premium member against their own short interest blurb (set via
/set_profile), so the personalized DM digest can surface only what's actually
relevant to *that* person instead of just the server-wide ranking.

Same fail-open contract as utils/relevance.py: if Ollama is unreachable or
returns something unusable, callers get a neutral score back rather than an
exception — a flaky LLM call should never crash a scan or block the digest.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Dict

from utils.ollama_client import OllamaError, generate_json
from utils.relevance import DEFAULT_OLLAMA_HOST, DEFAULT_OLLAMA_MODEL, NEUTRAL_QUALITY_SCORE

LOGGER = logging.getLogger(__name__)

MIN_MATCH_SCORE = 1
MAX_MATCH_SCORE = 5

_PROMPT_TEMPLATE = """A student described what internships they're interested in:
"{profile_blurb}"

Judge how well this ONE posting matches THEIR stated interests specifically \
(not just whether it's a reasonable internship in general):
  Company: {company}
  Role: {title}
  Location: {location}
  Tags: {tags}

Reply with ONLY a JSON object, no other text, matching this shape exactly:
{{"match_score": 1-5 integer, "reason": "one short sentence addressed to the student, e.g. 'Matches your interest in backend/Go roles'"}}

Guidance: 5 = strongly matches their stated interests; 3 = plausible but not a clear \
match to what they described; 1 = doesn't match their stated interests at all.
"""


@dataclass
class PersonalMatchResult:
    match_score: int
    reason: str
    source: str  # "llm" or "fallback"


def format_profile_for_prompt(profile: Dict[str, str]) -> str:
    """Turn a structured member profile (skills/target_roles/education_level/
    location_pref) into one descriptive line for the match prompt below.

    Gives the model cleanly labeled fields to reason over instead of a single
    free-text blurb the member typed themselves.
    """
    labels = (
        ("skills", "Skills/interests"),
        ("target_roles", "Target roles"),
        ("education_level", "Education level"),
        ("location_pref", "Location preference"),
    )
    parts = [f"{label}: {profile[field]}" for field, label in labels if profile.get(field)]
    return ". ".join(parts) if parts else "No profile details provided"


def _fallback(reason: str) -> PersonalMatchResult:
    return PersonalMatchResult(match_score=NEUTRAL_QUALITY_SCORE, reason=reason, source="fallback")


def score_personal_match(
    internship: Dict[str, Any], profile_blurb: str, config: Dict[str, Any]
) -> PersonalMatchResult:
    """Judge one posting against one member's profile blurb. Always returns a
    usable result (fail-open)."""
    host = config.get("ollama_host") or DEFAULT_OLLAMA_HOST
    model = config.get("ollama_model") or DEFAULT_OLLAMA_MODEL
    timeout = float(config.get("llm_timeout_seconds", 15))

    prompt = _PROMPT_TEMPLATE.format(
        profile_blurb=profile_blurb,
        company=internship.get("company", "Unknown"),
        title=internship.get("title", "Unknown"),
        location=internship.get("location", "Unknown"),
        tags=", ".join(internship.get("tags", [])) or "none",
    )

    try:
        parsed = generate_json(host=host, model=model, prompt=prompt, timeout=timeout)
    except OllamaError as exc:
        LOGGER.warning("Personal match check failed, using neutral score: %s", exc)
        return _fallback("Ollama unavailable; neutral score used")

    try:
        match_score = int(parsed["match_score"])
        reason = str(parsed.get("reason", "")).strip() or "No reason given"
    except (KeyError, TypeError, ValueError) as exc:
        LOGGER.warning("Personal match check returned an unexpected shape %r: %s", parsed, exc)
        return _fallback("Unexpected model output; neutral score used")

    match_score = max(MIN_MATCH_SCORE, min(MAX_MATCH_SCORE, match_score))
    return PersonalMatchResult(match_score=match_score, reason=reason, source="llm")
