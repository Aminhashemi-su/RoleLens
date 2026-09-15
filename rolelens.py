#!/usr/bin/env python3
"""RoleLens: semantic Swedish job discovery with deterministic orchestration.

Runtime design:
  * Discovery costs no model tokens. Arbetsförmedlingen JobStream delivers
    every Platsbanken ad added or changed, optional JobSearch queries reach the
    open backlog, and built-in collectors read public career-site feeds.
  * Every new or edited ad is ranked against the candidate profile - a weighted
    role vocabulary, profile embeddings and the competencies JobTech's
    enrichment finds requested - and only the top share reaches a model. No
    title decides anything on its own.
  * Deterministic rules settle what needs no model, an optional cheap first
    read settles clear rejections, and the judge evaluates the rest.
  * SQLite provides durable idempotency and evaluation history.
  * Vertex Gemini performs semantic evaluation, with one Azure fallback.
  * stdout is reserved for user notifications, so a script-only scheduler can
    deliver it verbatim. Operational logs always go to stderr.

The runtime has no third-party Python dependencies.
"""

from __future__ import annotations

import argparse
import array
import contextlib
import dataclasses
import datetime as dt
import email.utils
import hashlib
import html
import json
import logging
import math
import operator
import os
import random
import re
import sqlite3
import stat
import sys
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any, Callable, Iterable, Iterator, Mapping, Sequence

try:  # POSIX run lock
    import fcntl
except ImportError:  # pragma: no cover - Windows
    fcntl = None  # type: ignore[assignment]
    import msvcrt

APP_NAME = "rolelens"
APP_VERSION = "2.0.0"
SCHEMA_VERSION = "3"
DEFAULT_HOME = Path.home() / ".rolelens"
PROJECT_URL = "https://github.com/Aminhashemi-su/RoleLens"
JOBSTREAM_BASE_URL = "https://jobstream.api.jobtechdev.se"
JOBSTREAM_CURSOR_KEY = "jobstream_cursor"
JOBSEARCH_BASE_URL = "https://jobsearch.api.jobtechdev.se"
GEMINI_BASE_URL = "https://aiplatform.googleapis.com/v1/publishers/google/models"
PRIMARY_PROVIDER = "vertex_gemini"
FALLBACK_PROVIDER = "azure_gpt5mini"
RETRYABLE_HTTP_CODES = {408, 409, 425, 429, 500, 502, 503, 504}
# Answers meaning the provider will not serve this account at all: a bad key,
# missing permission, billing or credit switched off, or a retired model.
ACCESS_REFUSED_HTTP_CODES = {401, 403, 404}
UTC = dt.timezone.utc

# Ranking. These were calibrated together against a full day of Platsbanken and
# the matches an earlier keyword-search build had delivered; changing one of
# them invalidates that measurement, so they are constants rather than settings.
DEFAULT_EMBEDDING_MODEL = "gemini-embedding-001"
EMBEDDING_USD_PER_MILLION_TOKENS = {"gemini-embedding-001": 0.15}
# Judge tokens are priced at Gemini Flash rates (input, output per million), the
# dearer of the two providers, so the budget estimate errs high.
JUDGE_USD_PER_MILLION_TOKENS = (0.75, 3.75)
# Room for a batch of ten evaluations plus the model's thinking, which counts
# against the same limit. At 12,000 a live answer was cut off mid-string. Only
# tokens actually produced are billed.
JUDGE_MAX_OUTPUT_TOKENS = 24_000
# The first read: a cheap model reads every ad the rules let through and settles
# the clear rejections; whatever it passes or is unsure about goes to the judge.
# Priced at gemini-2.5-flash-lite rates, input and output per million tokens.
TRIAGE_USD_PER_MILLION_TOKENS = (0.10, 0.40)
TRIAGE_MAX_OUTPUT_TOKENS = 8_192
# Head and tail of each description: requirements usually close an ad.
TRIAGE_DESCRIPTION_CHARS = 2_400
# A rejection settles an ad only below this fit. Over a few hundred judged ads,
# with a thinking budget of 1024, any cut from 45 to 60 settled three quarters of
# them and lost no ad the rules would send as a card. Without thinking the first
# read rejected genuine matches, so thinking stays on by default.
TRIAGE_SETTLE_BELOW_FIT = 50
EMBEDDING_DIMENSIONS = 768
RANK_TEXT_CHARS = 3000  # title plus description, read by both scores
PROFILE_FACET_CHARS = 6000
RRF_K = 60
# Below this many ranked ads a percentile says little, so selection fails open.
RANKING_MIN_POOL = 200
# Ads embedded per progress step, so a failure part-way keeps what was done.
EMBEDDING_CHUNK = 200
# Vertex meters embedding input tokens per minute. A 429 from that quota is
# waited out rather than treated as a failure, within a bound per run; a first
# day's backlog needs one or two pauses, a routine run none.
EMBEDDING_QUOTA_PAUSE_SECONDS = 60
EMBEDDING_QUOTA_MAX_PAUSES = 10
# Arbetsförmedlingen's JobAd Enrichments API: the competencies and occupations an
# ad requests, each with the probability that the employer really requires it.
# Free and keyless. As a third ranking order it kept 100 of 102 held-out matches
# at the 15% cut, against 96 without it.
ENRICHMENT_URL = "https://jobad-enrichments-api.jobtechdev.se/enrichtextdocuments"
ENRICHMENT_BATCH = 10
ENRICHMENT_PREDICTION_FLOOR = 0.5
ENRICHMENT_VERSION = "profile-concepts-v1"
# The parts of matcher_profile.json that say who the candidate is. Constraints,
# cautions and policy say how to judge, and stay out. career_profile.json is
# never read here, so it still never leaves the machine.
PROFILE_FACET_SECTIONS: tuple[str, ...] = (
    "candidate_core", "differentiators", "strong_capabilities", "secondary_capabilities",
    "technology_evidence", "education_signal", "role_families_to_recognize_semantically",
)
RANKING_COLUMNS: tuple[tuple[str, str], ...] = (
    ("rank_content_hash", "TEXT"),
    ("rank_profile_key", "TEXT"),
    ("vocabulary_score", "REAL"),
    ("embedding_score", "REAL"),
    ("enrichment_score", "REAL"),
    ("enrichment_json", "TEXT"),
    ("rank_percentile", "REAL"),
    ("selection_state", "TEXT"),
    ("selection_reason", "TEXT"),
    ("ranked_at", "TEXT"),
)

# Platsbanken deadlines are Swedish calendar dates. Comparing them against the
# UTC date keeps a vacancy that closed at midnight alive for the last hour or
# two of the UTC day, so date arithmetic uses the market's own timezone.
MARKET_TIMEZONE = "Europe/Stockholm"
try:  # pragma: no cover - platform dependent
    from zoneinfo import ZoneInfo

    _MARKET_TZ: Any = ZoneInfo(MARKET_TIMEZONE)
except Exception:  # pragma: no cover - no tz database on this platform
    _MARKET_TZ = None

LOG = logging.getLogger(APP_NAME)


def market_today(now: "dt.datetime | None" = None) -> dt.date:
    """Today's calendar date in the job market's timezone.

    Falls back to the UTC date when the platform ships no tz database. That
    fallback is lenient rather than strict: it can keep a just-closed vacancy
    eligible for a couple of hours, never the reverse.
    """
    moment = now or dt.datetime.now(UTC)
    if _MARKET_TZ is None:
        return moment.astimezone(UTC).date()
    return moment.astimezone(_MARKET_TZ).date()


# ---------------------------------------------------------------------------
# Candidate language proficiency.
#
# The candidate's level is configuration, not code: it comes from
# matcher_profile.json (`constraints.<language>`). Nothing here assumes a
# particular candidate. An absent or unrecognised value stays UNKNOWN and is
# never silently turned into a failure.
# ---------------------------------------------------------------------------
CEFR_SCALE: tuple[str, ...] = ("none", "A1", "A2", "B1", "B2", "C1", "C2")

# Deterministic thresholds, so proficiency comparisons live in one place.
#   PROFESSIONAL - what an advertisement means by "fluent/professional/advanced".
#   WORKING      - close enough to penalise rather than hard-block, because a
#                  false negative costs far more here than a wasted click.
PROFESSIONAL_LANGUAGE_LEVEL = "C1"
WORKING_LANGUAGE_LEVEL = "B2"

_LANGUAGE_ALIASES: dict[str, str] = {
    "none": "none", "no": "none", "nil": "none", "zero": "none", "ingen": "none",
    "beginner": "A1", "basic": "A1", "elementary": "A1", "nyborjare": "A1",
    "intermediate": "B1", "medel": "B1",
    "upper intermediate": "B2", "upperintermediate": "B2",
    "advanced": "C1", "professional": "C1", "business": "C1",
    "professional working proficiency": "C1", "working proficiency": "C1",
    "fluent": "C1", "flytande": "C1",
    "native": "C2", "native speaker": "C2", "mother tongue": "C2",
    "modersmal": "C2", "bilingual": "C2",
}
_CEFR_TOKEN = re.compile(r"\b([ABC][12])\b", re.IGNORECASE)
_UNSPECIFIED = {
    "", "unknown", "unspecified", "not specified", "not stated",
    "n/a", "na", "none specified", "null",
}


@dataclasses.dataclass(frozen=True)
class LanguageLevel:
    """One candidate proficiency, comparable on the CEFR scale.

    `rank` is None when the level is unknown. Unknown is a state of its own: it
    is never treated as `none`, and it never satisfies a requirement.
    """

    label: str
    rank: int | None

    @property
    def known(self) -> bool:
        return self.rank is not None

    def at_least(self, level: str) -> bool:
        """True only when the level is known and reaches `level`."""
        return self.rank is not None and self.rank >= CEFR_SCALE.index(level)

    def describe(self, language: str) -> str:
        if not self.known:
            return f"{language} level is not specified in the candidate profile"
        return f"{language} level is {self.label}"


UNKNOWN_LANGUAGE_LEVEL = LanguageLevel("unknown", None)


def normalize_language_level(value: Any) -> LanguageLevel:
    """Parse a free-text proficiency into a comparable level.

    Accepts 'A2', 'a2, progressing', 'Fluent', 'native speaker',
    'B1 (intermediate)'. An explicit CEFR token always wins over a prose alias,
    so 'A2, working towards fluent' resolves to A2 rather than to fluent.
    """
    if isinstance(value, Mapping):
        value = value.get("level", "")
    if not isinstance(value, str):
        return UNKNOWN_LANGUAGE_LEVEL
    text = " ".join(value.replace("å", "a").replace("ä", "a").replace("ö", "o").split())
    folded = text.casefold()
    if folded in _UNSPECIFIED:
        return UNKNOWN_LANGUAGE_LEVEL

    token = _CEFR_TOKEN.search(text)
    if token is not None:
        label = token.group(1).upper()
        return LanguageLevel(label, CEFR_SCALE.index(label))

    # Longest alias first, so "upper intermediate" beats "intermediate".
    for alias in sorted(_LANGUAGE_ALIASES, key=len, reverse=True):
        if re.search(rf"\b{re.escape(alias)}\b", folded):
            canonical = _LANGUAGE_ALIASES[alias]
            label = alias if alias in {"fluent", "native"} else canonical
            return LanguageLevel(label, CEFR_SCALE.index(canonical))
    return UNKNOWN_LANGUAGE_LEVEL


def candidate_language_level(profile: Mapping[str, Any], language: str = "swedish") -> LanguageLevel:
    """Read one candidate language level from the matcher profile.

    Looks at `constraints.<language>` first, which is where the profile schema
    keeps it, then at an optional `languages.<language>` map. Anything missing or
    unparseable is UNKNOWN.
    """
    if not isinstance(profile, Mapping):
        return UNKNOWN_LANGUAGE_LEVEL
    constraints = profile.get("constraints")
    if isinstance(constraints, Mapping) and language in constraints:
        return normalize_language_level(constraints[language])
    languages = profile.get("languages")
    if isinstance(languages, Mapping) and language in languages:
        return normalize_language_level(languages[language])
    return UNKNOWN_LANGUAGE_LEVEL


class RoleLensError(RuntimeError):
    """Expected operational failure that should make a scheduled run fail loudly."""


class ConfigurationError(RoleLensError):
    """Invalid or missing local configuration."""


class RemoteAPIError(RoleLensError):
    """Remote API failed after bounded retries."""


class RateLimitError(RemoteAPIError):
    """Remote provider rate limit after bounded HTTP retries.

    This is recoverable: jobs remain pending and the next scheduled run can
    resume without losing discovery state.
    """


class TemporaryProviderError(RemoteAPIError):
    """A transport, rate-limit, timeout, unavailable, or temporary 5xx failure.

    Carries the HTTP status when there was one. Without it every cause -- a
    rate limit, a 503, a socket timeout -- logs identically, and you cannot
    tell afterwards which one you actually hit.
    """

    def __init__(self, provider: str, message: str, *, status: int | None = None) -> None:
        super().__init__(message)
        self.provider = provider
        self.status = status

    @property
    def reason(self) -> str:
        """Short, countable label for this failure.

        Deliberately bounded: these become dictionary keys in the run record,
        so an arbitrary provider message must never end up as one.
        """
        if self.status is not None:
            return f"http_{self.status}"
        match = re.search(r"transport failure:\s*(\w{1,40})", str(self))
        return f"transport_{match.group(1)}" if match else "transport_unknown"


class ProviderAccessError(RemoteAPIError):
    """The provider refused this account: a bad key, missing permission, billing
    or credit switched off, or a model it no longer serves.

    Not temporary, yet the other provider can still judge the jobs, so
    orchestration falls back exactly as it does for a transport failure.
    """

    def __init__(self, provider: str, message: str, *, status: int) -> None:
        super().__init__(message)
        self.provider = provider
        self.status = status

    @property
    def reason(self) -> str:
        return f"access_http_{self.status}"


class ModelOutputError(RemoteAPIError):
    """Provider answered, but the model output violated the evaluation contract."""

    def __init__(
        self,
        message: str,
        *,
        prompt_tokens: int = 0,
        completion_tokens: int = 0,
        failure_path: str | None = None,
    ) -> None:
        super().__init__(message)
        self.prompt_tokens = max(0, int(prompt_tokens))
        self.completion_tokens = max(0, int(completion_tokens))
        self.failure_path = failure_path

    @property
    def usage(self) -> dict[str, int]:
        return {
            "prompt_tokens": self.prompt_tokens,
            "completion_tokens": self.completion_tokens,
        }


# ---------------------------------------------------------------------------
# Career sites: company job boards RoleLens reads directly.
#
# Each entry in config.career_sites names a platform and the few values that
# platform needs. Everything else in the entry is passed to the collector as an
# option. See docs/career-sites.md.
# ---------------------------------------------------------------------------
CAREER_SITE_REQUIRED: dict[str, tuple[str, ...]] = {
    "teamtailor": ("url",),
    "varbi": ("url",),
    "greenhouse": ("board",),
    "lever": ("board",),
    "ashby": ("board",),
    "smartrecruiters": ("board",),
    "workday": ("url", "tenant", "site"),
    "successfactors": ("url",),
}
_CAREER_SITE_NAME = re.compile(r"^[a-z0-9][a-z0-9_.-]{0,62}$")
_CAREER_SITE_FIELDS = frozenset({"platform", "name", "url", "company", "enabled"})


@dataclasses.dataclass(frozen=True)
class CareerSite:
    """One configured career site."""

    name: str
    platform: str
    url: str
    company: str
    options: Mapping[str, Any]

    def option(self, key: str, default: Any = None) -> Any:
        value = self.options.get(key)
        return default if value is None else value


def parse_career_sites(value: Any) -> tuple[CareerSite, ...]:
    """Validate config.career_sites. A disabled entry is checked, then skipped."""
    if value is None:
        return ()
    if not isinstance(value, list):
        raise ConfigurationError("config.career_sites must be a list of career-site objects")
    sites: list[CareerSite] = []
    names: set[str] = set()
    for index, entry in enumerate(value):
        where = f"config.career_sites[{index}]"
        if not isinstance(entry, dict):
            raise ConfigurationError(f"{where} must be an object")
        platform = str(entry.get("platform", "")).strip().casefold()
        if platform not in CAREER_SITE_REQUIRED:
            raise ConfigurationError(
                f"{where}.platform must be one of: {', '.join(sorted(CAREER_SITE_REQUIRED))}")
        name = str(entry.get("name", "")).strip()
        if not _CAREER_SITE_NAME.match(name):
            raise ConfigurationError(
                f"{where}.name must be a short lowercase id such as 'acme' (letters, digits, '.', '_', '-')")
        if name in names:
            raise ConfigurationError(f"{where}.name {name!r} is used by another career site")
        names.add(name)
        for key in CAREER_SITE_REQUIRED[platform]:
            if not isinstance(entry.get(key), str) or not entry[key].strip():
                raise ConfigurationError(f"{where} ({platform}) needs a non-empty {key!r}")
        url = str(entry.get("url") or "").strip().rstrip("/")
        if url and not url.startswith("https://"):
            raise ConfigurationError(f"{where}.url must start with https://")
        enabled = entry.get("enabled", True)
        if not isinstance(enabled, bool):
            raise ConfigurationError(f"{where}.enabled must be true or false")
        if not enabled:
            continue
        options = {key: item for key, item in entry.items() if key not in _CAREER_SITE_FIELDS}
        sites.append(CareerSite(name, platform, url, str(entry.get("company") or "").strip(), options))
    return tuple(sites)


@dataclasses.dataclass(frozen=True)
class Settings:
    home: Path
    db_path: Path
    profile_dir: Path
    secrets_path: Path
    lock_path: Path
    primary_provider: str
    fallback_provider: str
    use_jobstream: bool
    jobstream_lookback_hours: int
    jobstream_max_window_hours: int
    search_terms: tuple[str, ...]
    location_terms: tuple[str, ...]
    include_unlocated_searches: bool
    search_limit: int
    query_delay_seconds: float
    career_sites: tuple[CareerSite, ...]
    career_site_max_details: int
    max_candidates_per_run: int
    max_jobs_per_batch: int
    max_run_seconds: int
    max_prompt_chars: int
    max_job_description_chars: int
    max_notifications_per_run: int
    jobstream_timeout_seconds: int
    jobsearch_timeout_seconds: int
    career_site_timeout_seconds: int
    gateway_timeout_seconds: int
    http_retries: int
    northern_exclusions: tuple[str, ...]
    preferred_locations: tuple[str, ...]
    evaluate_top_share: float
    degraded_top_share: float
    explore_share: float
    use_enrichment: bool
    exclude_student_roles: bool
    ranking_reference_days: int
    max_rank_per_run: int
    embedding_model: str
    embedding_batch_size: int
    embedding_timeout_seconds: int
    monthly_budget_usd: float
    triage_model: str
    triage_thinking_budget: int
    triage_batch_size: int

    @classmethod
    def load(cls, home: Path) -> "Settings":
        config_path = home / "config.json"
        if not config_path.exists():
            raise ConfigurationError(f"Missing configuration: {config_path}")
        try:
            raw = json.loads(config_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ConfigurationError(f"Cannot read {config_path}: {exc}") from exc
        if not isinstance(raw, dict):
            raise ConfigurationError(f"{config_path} must hold a JSON object")

        def strings(key: str) -> tuple[str, ...]:
            value = raw.get(key, [])
            if not isinstance(value, list) or not all(isinstance(x, str) and x.strip() for x in value):
                raise ConfigurationError(f"config.{key} must be a list of non-empty strings")
            return tuple(x.strip() for x in value)

        def positive_int(key: str, default: int) -> int:
            value = raw.get(key, default)
            if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
                raise ConfigurationError(f"config.{key} must be a positive integer")
            return value

        def positive_number(key: str, default: float) -> float:
            value = raw.get(key, default)
            if isinstance(value, bool) or not isinstance(value, (int, float)) or value <= 0:
                raise ConfigurationError(f"config.{key} must be a positive number")
            return float(value)

        def non_negative_number(key: str, default: float) -> float:
            value = raw.get(key, default)
            if isinstance(value, bool) or not isinstance(value, (int, float)) or value < 0:
                raise ConfigurationError(f"config.{key} must be a number of at least 0")
            return float(value)

        def share(key: str, default: float) -> float:
            value = raw.get(key, default)
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not 0 < value <= 1:
                raise ConfigurationError(f"config.{key} must be a number above 0 and at most 1")
            return float(value)

        def fraction(key: str, default: float) -> float:
            value = raw.get(key, default)
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not 0 <= value <= 1:
                raise ConfigurationError(f"config.{key} must be a number from 0 to 1")
            return float(value)

        def boolean(key: str, default: bool) -> bool:
            value = raw.get(key, default)
            if not isinstance(value, bool):
                raise ConfigurationError(f"config.{key} must be true or false")
            return value

        embedding_model = str(raw.get("embedding_model", DEFAULT_EMBEDDING_MODEL)).strip()
        if not embedding_model:
            raise ConfigurationError("config.embedding_model must be a non-empty string")
        triage_model = raw.get("triage_model", "")
        if not isinstance(triage_model, str):
            raise ConfigurationError("config.triage_model must be a model name, or empty to let the judge read every ad")
        triage_thinking_budget = raw.get("triage_thinking_budget", 1024)
        if (
            isinstance(triage_thinking_budget, bool)
            or not isinstance(triage_thinking_budget, int)
            or not (triage_thinking_budget == 0 or 512 <= triage_thinking_budget <= 24_576)
        ):
            raise ConfigurationError("config.triage_thinking_budget must be 0, or from 512 to 24576 tokens")

        use_jobstream = boolean("use_jobstream", True)
        search_terms = strings("search_terms")
        career_sites = parse_career_sites(raw.get("career_sites"))
        if not (use_jobstream or search_terms or career_sites):
            raise ConfigurationError(
                "config.json enables no discovery source: turn on use_jobstream, "
                "or add search_terms or career_sites")

        home = home.expanduser().resolve()
        return cls(
            home=home,
            db_path=home / str(raw.get("database", "data/rolelens.db")),
            profile_dir=home / str(raw.get("profile_dir", "profile")),
            secrets_path=home / str(raw.get("secrets_file", "secrets.env")),
            lock_path=home / str(raw.get("lock_file", "data/rolelens.lock")),
            primary_provider=str(raw.get("primary_provider", PRIMARY_PROVIDER)),
            fallback_provider=str(raw.get("fallback_provider", FALLBACK_PROVIDER)),
            use_jobstream=use_jobstream,
            # Hours of stream replayed when there is no cursor yet, and the widest
            # window ever requested. An outage longer than that loses the gap
            # rather than asking JobStream for a month of history in one call.
            jobstream_lookback_hours=min(168, positive_int("jobstream_lookback_hours", 24)),
            jobstream_max_window_hours=min(168, positive_int("jobstream_max_window_hours", 72)),
            search_terms=search_terms,
            location_terms=strings("location_terms"),
            include_unlocated_searches=boolean("include_unlocated_searches", True),
            search_limit=min(100, positive_int("search_limit", 50)),
            query_delay_seconds=non_negative_number("query_delay_ms", 120) / 1000.0,
            career_sites=career_sites,
            # Detail pages fetched per career site per run, on the platforms whose
            # listing carries no description. A large board fills in over a few runs.
            career_site_max_details=min(200, positive_int("career_site_max_details", 40)),
            # Completeness beats punctuality: a run evaluates its whole frozen
            # snapshot. The two limits below are emergency valves, not throughput
            # caps - one bounds snapshot memory, the other bounds wall clock.
            max_candidates_per_run=min(500, positive_int("max_candidates_per_run", 300)),
            max_jobs_per_batch=min(10, positive_int("max_jobs_per_batch", 10)),
            max_run_seconds=positive_int("max_run_seconds", 3000),
            max_prompt_chars=positive_int("max_prompt_chars", 180_000),
            max_job_description_chars=positive_int("max_job_description_chars", 9_000),
            max_notifications_per_run=positive_int("max_notifications_per_run", 8),
            jobstream_timeout_seconds=positive_int("jobstream_timeout_seconds", 90),
            jobsearch_timeout_seconds=positive_int("jobsearch_timeout_seconds", 30),
            career_site_timeout_seconds=positive_int("career_site_timeout_seconds", 45),
            gateway_timeout_seconds=positive_int("gateway_timeout_seconds", 150),
            http_retries=min(8, positive_int("http_retries", 4)),
            northern_exclusions=tuple(x.casefold() for x in strings("northern_exclusions")),
            preferred_locations=strings("preferred_locations"),
            # The share of the ranking window the evaluator reads, and the share
            # for ads ranked while embeddings were unavailable. In calibration
            # every delivered match sat in the top few percent of the ranking;
            # 15% lies between cuts that kept 92 and 101 of 102 held-out matches.
            # The vocabulary alone needs the wider 30%.
            evaluate_top_share=share("evaluate_top_share", 0.15),
            degraded_top_share=share("degraded_top_share", 0.30),
            # A random sample of the ads the cut leaves out is judged anyway, so a
            # match the ranking misses is still delivered and the miss is visible.
            explore_share=fraction("explore_share", 0.03),
            # Rank on the competencies each ad requests as a third order.
            use_enrichment=boolean("use_enrichment", True),
            # Internships, theses and student jobs are outside a full-time search.
            exclude_student_roles=boolean("exclude_student_roles", True),
            ranking_reference_days=min(14, positive_int("ranking_reference_days", 3)),
            max_rank_per_run=min(20_000, positive_int("max_rank_per_run", 6_000)),
            embedding_model=embedding_model,
            embedding_batch_size=min(50, positive_int("embedding_batch_size", 20)),
            embedding_timeout_seconds=positive_int("embedding_timeout_seconds", 60),
            # Estimated provider spend per UTC month; judging pauses once it is reached.
            monthly_budget_usd=positive_number("monthly_budget_usd", 50.0),
            # A cheap model reads each ad first and settles the clear rejections;
            # empty sends every ad straight to the judge.
            triage_model=triage_model.strip(),
            triage_thinking_budget=triage_thinking_budget,
            triage_batch_size=min(40, positive_int("triage_batch_size", 20)),
        )


@dataclasses.dataclass
class JobRecord:
    source_job_id: str
    title: str
    company: str
    url: str
    municipality: str
    region: str
    country: str
    remote: bool
    fully_remote: bool
    application_deadline: str | None
    published_at: str | None
    employment_type: str
    scope: str
    description: str
    raw_json: dict[str, Any]
    matched_queries: set[str] = dataclasses.field(default_factory=set)
    discovery_score: int = 0

    @property
    def location(self) -> str:
        parts: list[str] = []
        for value in (self.municipality, self.region):
            if value and value not in parts:
                parts.append(value)
        if self.remote:
            parts.append("Remote/Hybrid indicated")
        return ", ".join(parts) or "Location not specified"

    @property
    def content_hash(self) -> str:
        canonical = json.dumps(
            {
                "title": self.title,
                "company": self.company,
                "url": self.url,
                "location": self.location,
                "fully_remote": self.fully_remote,
                "deadline": self.application_deadline,
                "employment_type": self.employment_type,
                "scope": self.scope,
                "description": self.description,
            },
            sort_keys=True,
            ensure_ascii=False,
            separators=(",", ":"),
        )
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


@dataclasses.dataclass(frozen=True)
class Evaluation:
    source_job_id: str
    career_fit: int
    opportunity_score: int
    confidence: float
    actual_role: str
    why_fit: tuple[str, ...]
    candidate_evidence: tuple[str, ...]
    must_have_assessment: tuple[dict[str, str], ...]
    gaps: tuple[str, ...]
    blockers: tuple[dict[str, str], ...]
    language_risk: str
    seniority_risk: str
    location_note: str
    decision: str
    raw: dict[str, Any]


@dataclasses.dataclass
class RunStats:
    # JobStream. `stream_entries` counts everything returned, unpublications
    # included; `unique_jobs` counts live ads kept from the stream.
    stream_entries: int = 0
    stream_removed: int = 0
    stream_malformed: int = 0
    stream_window_clamped: bool = False
    unique_jobs: int = 0
    # JobSearch keyword queries.
    queries_attempted: int = 0
    queries_succeeded: int = 0
    search_hits: int = 0
    # Career sites read, failed, postings seen, stored, and left out because the
    # same vacancy is already stored from another source.
    career_sites_read: int = 0
    career_sites_failed: int = 0
    career_site_jobs_seen: int = 0
    career_site_jobs_stored: int = 0
    career_site_jobs_known: int = 0
    # Sources that failed while the others were still read.
    discovery_failures: list[str] = dataclasses.field(default_factory=list)
    jobs_upserted: int = 0
    jobs_prefiltered: int = 0
    pending_selected: int = 0
    evaluated: int = 0
    notified: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    reasoning_tokens: int = 0
    total_tokens: int = 0
    fallback_calls: int = 0
    # Why the transport fallback fired, keyed as http_429 / transport_TimeoutError.
    fallback_reasons: dict[str, int] = dataclasses.field(default_factory=dict)
    mode: str = "live"
    batches_processed: int = 0
    cleanup_batches: int = 0
    duplicates_suppressed: int = 0
    # Settled without a model because the deterministic rules block the ad.
    rules_screened: int = 0
    snapshot_size: int = 0
    unresolved: int = 0
    deferred: int = 0
    # Ranking: ads scored this run, how many reached the evaluator, and whether
    # the percentile was trusted (fail_open) and embeddings answered (degraded).
    jobs_ranked: int = 0
    jobs_selected: int = 0
    jobs_explored: int = 0
    ranking_pool: int = 0
    ranking_fail_open: bool = False
    ranking_degraded: bool = False
    embedding_calls: int = 0
    embedding_tokens: int = 0
    embedding_quota_pauses: int = 0
    # Why embeddings failed this run (http_403, http_429, ...), or None.
    embedding_failure: str | None = None
    # Ads enriched this run, and why enrichment failed if it did.
    jobs_enriched: int = 0
    enrichment_failure: str | None = None
    # The monthly budget, the estimated spend before this run, whether judging
    # was paused because of it, and how many jobs were left waiting.
    budget_usd: float = 0.0
    month_to_date_usd: float = 0.0
    budget_paused: bool = False
    budget_waiting: int = 0
    # The first read: calls made, ads it settled, ads it sent to the judge, its
    # tokens, and the failure that switched it off for the run, if any.
    triage_calls: int = 0
    triage_settled: int = 0
    triage_escalated: int = 0
    triage_prompt_tokens: int = 0
    triage_completion_tokens: int = 0
    triage_reasoning_tokens: int = 0
    triage_failure: str | None = None
    # Ads judged a second time because the first score sat near the card line,
    # and how many of those moved between card and no card.
    second_judgements: int = 0
    second_judgement_changes: int = 0


@dataclasses.dataclass(frozen=True)
class ProviderBatchResult:
    provider: str
    model: str
    evaluations: tuple[Evaluation, ...]
    unresolved_ids: frozenset[str]
    usage: dict[str, int]
    error: str | None = None
    # "output"       the envelope could not be parsed at all
    # "completeness" the envelope was fine, some requested IDs came back missing
    # "transport"    both providers failed to answer
    error_kind: str | None = None


def _acquire_lock(handle: Any) -> bool:
    """Take an exclusive non-blocking lock; False when another process holds it."""
    if fcntl is not None:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return False
        return True
    handle.seek(0)
    try:  # pragma: no cover - Windows
        msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
    except OSError:  # pragma: no cover - Windows
        return False
    return True  # pragma: no cover - Windows


def _release_lock(handle: Any) -> None:
    with contextlib.suppress(OSError):
        if fcntl is not None:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        else:  # pragma: no cover - Windows
            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)


class FileLock:
    """Non-blocking process lock. A second scheduled run exits quietly."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self._fh: Any = None

    def __enter__(self) -> "FileLock":
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._fh = self.path.open("a+", encoding="utf-8")
        if not _acquire_lock(self._fh):
            self._fh.close()
            self._fh = None
            raise RoleLensError("Another RoleLens run is already active")
        self._fh.seek(0)
        self._fh.truncate()
        self._fh.write(str(os.getpid()))
        self._fh.flush()
        return self

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        if self._fh is not None:
            _release_lock(self._fh)
            self._fh.close()
            self._fh = None


class HttpStatusError(RemoteAPIError):
    """A non-retryable HTTP answer, with its status kept for the caller."""

    def __init__(self, message: str, *, status: int) -> None:
        super().__init__(message)
        self.status = status


class HttpClient:
    """Bounded retries over urllib, for every public API RoleLens reads."""

    def __init__(self, *, retries: int, user_agent: str) -> None:
        self.retries = retries
        self.user_agent = user_agent

    def json_request(
        self,
        method: str,
        url: str,
        *,
        headers: Mapping[str, str] | None = None,
        payload: Mapping[str, Any] | None = None,
        timeout: int = 30,
        expect: type = dict,
    ) -> Any:
        final_headers = {
            "Accept": "application/json",
            "User-Agent": self.user_agent,
            **(dict(headers or {})),
        }
        data: bytes | None = None
        if payload is not None:
            data = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
            final_headers.setdefault("Content-Type", "application/json")

        def parse(body: bytes) -> Any:
            if not body:
                return expect()
            parsed = json.loads(body.decode("utf-8"))
            # JobStream and some career sites answer with an array; most endpoints with an object.
            if not isinstance(parsed, expect):
                raise RemoteAPIError(f"Expected JSON {expect.__name__} from {redact_url(url)}")
            return parsed

        return self._request(method, url, final_headers, data, timeout, parse)

    def fetch(self, url: str, *, timeout: int = 30, accept: str = "*/*") -> bytes:
        """GET a document as bytes, for RSS feeds and server-rendered pages."""
        headers = {"Accept": accept, "User-Agent": self.user_agent}
        return self._request("GET", url, headers, None, timeout, lambda body: body)

    def _request(
        self,
        method: str,
        url: str,
        headers: Mapping[str, str],
        data: bytes | None,
        timeout: int,
        parse: Callable[[bytes], Any],
    ) -> Any:
        last_error: BaseException | None = None
        for attempt in range(self.retries + 1):
            request = urllib.request.Request(url, data=data, headers=dict(headers), method=method)
            try:
                with urllib.request.urlopen(request, timeout=timeout) as response:
                    return parse(response.read())
            except urllib.error.HTTPError as exc:
                last_error = exc
                body = exc.read().decode("utf-8", errors="replace")[:1500] if exc.fp is not None else ""
                if exc.code not in RETRYABLE_HTTP_CODES or attempt >= self.retries:
                    message = f"HTTP {exc.code} from {redact_url(url)}: {body or exc.reason}"
                    if exc.code == 429:
                        raise RateLimitError(message) from exc
                    raise HttpStatusError(message, status=exc.code) from exc
                delay = retry_delay(attempt, exc.headers.get("Retry-After") if exc.headers else None)
                LOG.warning("HTTP %s, retrying in %.1fs: %s", exc.code, delay, redact_url(url))
                time.sleep(delay)
            except (urllib.error.URLError, TimeoutError, ConnectionError, json.JSONDecodeError,
                    UnicodeDecodeError) as exc:
                last_error = exc
                if attempt >= self.retries:
                    raise RemoteAPIError(f"Request failed: {redact_url(url)}: {exc}") from exc
                delay = retry_delay(attempt, None)
                LOG.warning("Network/decoding error, retrying in %.1fs: %s", delay, exc)
                time.sleep(delay)
        raise RemoteAPIError(f"Request failed: {last_error}")


class Database:
    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        self.conn = sqlite3.connect(path, timeout=30)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys = ON")
        self.conn.execute("PRAGMA journal_mode = WAL")
        self.conn.execute("PRAGMA synchronous = NORMAL")
        self.conn.execute("PRAGMA busy_timeout = 5000")
        try:
            self._migrate()
        except BaseException:
            self.conn.close()
            raise

    def close(self) -> None:
        self.conn.close()

    def _migrate(self) -> None:
        with self.conn:
            self.conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS meta (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS jobs (
                    id INTEGER PRIMARY KEY,
                    source TEXT NOT NULL,
                    source_job_id TEXT NOT NULL,
                    content_hash TEXT NOT NULL,
                    title TEXT NOT NULL,
                    company TEXT NOT NULL,
                    url TEXT NOT NULL,
                    municipality TEXT NOT NULL,
                    region TEXT NOT NULL,
                    country TEXT NOT NULL,
                    remote INTEGER NOT NULL,
                    fully_remote INTEGER NOT NULL,
                    application_deadline TEXT,
                    published_at TEXT,
                    employment_type TEXT NOT NULL,
                    scope TEXT NOT NULL,
                    description TEXT NOT NULL,
                    matched_queries_json TEXT NOT NULL,
                    discovery_score INTEGER NOT NULL DEFAULT 0,
                    raw_json TEXT NOT NULL,
                    first_seen_at TEXT NOT NULL,
                    last_seen_at TEXT NOT NULL,
                    content_changed_at TEXT NOT NULL,
                    UNIQUE(source, source_job_id)
                );

                CREATE INDEX IF NOT EXISTS idx_jobs_discovery
                    ON jobs(discovery_score DESC, published_at DESC);

                CREATE TABLE IF NOT EXISTS evaluations (
                    id INTEGER PRIMARY KEY,
                    job_id INTEGER NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
                    content_hash TEXT NOT NULL,
                    profile_version TEXT NOT NULL,
                    model TEXT NOT NULL,
                    career_fit INTEGER NOT NULL CHECK(career_fit BETWEEN 0 AND 100),
                    opportunity_score INTEGER NOT NULL CHECK(opportunity_score BETWEEN 0 AND 100),
                    confidence REAL NOT NULL CHECK(confidence BETWEEN 0 AND 1),
                    actual_role TEXT NOT NULL,
                    why_fit_json TEXT NOT NULL,
                    candidate_evidence_json TEXT NOT NULL,
                    must_have_json TEXT NOT NULL,
                    gaps_json TEXT NOT NULL,
                    blockers_json TEXT NOT NULL,
                    language_risk TEXT NOT NULL,
                    seniority_risk TEXT NOT NULL,
                    location_note TEXT NOT NULL,
                    decision TEXT NOT NULL,
                    raw_json TEXT NOT NULL,
                    evaluated_at TEXT NOT NULL,
                    UNIQUE(job_id, content_hash, profile_version)
                );

                CREATE INDEX IF NOT EXISTS idx_evaluations_notify
                    ON evaluations(decision, opportunity_score DESC, evaluated_at DESC);

                -- Repost bookkeeping. Additive and never deletes a source job.
                CREATE TABLE IF NOT EXISTS job_fingerprints (
                    job_id INTEGER PRIMARY KEY REFERENCES jobs(id) ON DELETE CASCADE,
                    fingerprint TEXT NOT NULL,
                    canonical_job_id INTEGER REFERENCES jobs(id) ON DELETE SET NULL,
                    reason TEXT,
                    detected_at TEXT NOT NULL
                );

                CREATE INDEX IF NOT EXISTS idx_job_fingerprints_value
                    ON job_fingerprints(fingerprint);

                CREATE TABLE IF NOT EXISTS notifications (
                    evaluation_id INTEGER PRIMARY KEY REFERENCES evaluations(id) ON DELETE CASCADE,
                    emitted_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS runs (
                    id INTEGER PRIMARY KEY,
                    started_at TEXT NOT NULL,
                    finished_at TEXT,
                    status TEXT NOT NULL,
                    stats_json TEXT NOT NULL,
                    error TEXT
                );

                -- One embedding per distinct profile, so the profile is embedded
                -- once rather than on every run.
                CREATE TABLE IF NOT EXISTS profile_embeddings (
                    profile_key TEXT PRIMARY KEY,
                    model TEXT NOT NULL,
                    facet_names_json TEXT NOT NULL,
                    vectors BLOB NOT NULL,
                    created_at TEXT NOT NULL
                );
                """
            )
            existing = self.conn.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()
            existing_version = existing["value"] if existing is not None else None
            if existing_version not in {None, "1", "2", SCHEMA_VERSION}:
                raise ConfigurationError(
                    f"Unsupported database schema {existing_version}; expected 1 to {SCHEMA_VERSION}"
                )

            # Schema v2 adds a durable content-change timestamp. Existing v1 rows
            # are backfilled with first_seen_at, which makes them historical until
            # their content actually changes after live mode is activated.
            columns = {row["name"] for row in self.conn.execute("PRAGMA table_info(jobs)")}
            if "content_changed_at" not in columns:
                self.conn.execute("ALTER TABLE jobs ADD COLUMN content_changed_at TEXT")
            self.conn.execute(
                "UPDATE jobs SET content_changed_at=first_seen_at "
                "WHERE content_changed_at IS NULL OR content_changed_at=''"
            )
            self.conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_jobs_content_changed "
                "ON jobs(content_changed_at DESC, discovery_score DESC)"
            )

            # Schema v3 adds ranking. Every column is nullable: a row without
            # them is simply unranked, and never enters the evaluation queue.
            for name, kind in RANKING_COLUMNS:
                if name not in columns:
                    self.conn.execute(f"ALTER TABLE jobs ADD COLUMN {name} {kind}")
            self.conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_jobs_selection "
                "ON jobs(selection_state, rank_percentile)"
            )

            if existing is None:
                self.conn.execute("INSERT INTO meta(key,value) VALUES('schema_version',?)", (SCHEMA_VERSION,))
            elif existing_version != SCHEMA_VERSION:
                self.conn.execute(
                    "UPDATE meta SET value=? WHERE key='schema_version'",
                    (SCHEMA_VERSION,),
                )

    def start_run(self) -> int:
        now = iso_now()
        with self.conn:
            cur = self.conn.execute(
                "INSERT INTO runs(started_at,status,stats_json) VALUES(?,?,?)",
                (now, "running", "{}"),
            )
        return int(cur.lastrowid)

    def finish_run(self, run_id: int, status_value: str, stats: RunStats, error: str | None = None) -> None:
        with self.conn:
            self.conn.execute(
                "UPDATE runs SET finished_at=?, status=?, stats_json=?, error=? WHERE id=?",
                (iso_now(), status_value, json.dumps(dataclasses.asdict(stats), sort_keys=True), error, run_id),
            )

    def upsert_job(self, job: JobRecord, *, source: str = "platsbanken") -> None:
        now = iso_now()
        with self.conn:
            self.conn.execute(
                """
                INSERT INTO jobs(
                    source, source_job_id, content_hash, title, company, url,
                    municipality, region, country, remote, fully_remote, application_deadline,
                    published_at, employment_type, scope, description,
                    matched_queries_json, discovery_score, raw_json,
                    first_seen_at, last_seen_at, content_changed_at
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(source, source_job_id) DO UPDATE SET
                    content_hash=excluded.content_hash,
                    title=excluded.title,
                    company=excluded.company,
                    url=excluded.url,
                    municipality=excluded.municipality,
                    region=excluded.region,
                    country=excluded.country,
                    remote=excluded.remote,
                    fully_remote=excluded.fully_remote,
                    application_deadline=excluded.application_deadline,
                    published_at=excluded.published_at,
                    employment_type=excluded.employment_type,
                    scope=excluded.scope,
                    description=excluded.description,
                    matched_queries_json=excluded.matched_queries_json,
                    discovery_score=MAX(jobs.discovery_score, excluded.discovery_score),
                    raw_json=excluded.raw_json,
                    last_seen_at=excluded.last_seen_at,
                    content_changed_at=CASE
                        WHEN jobs.content_hash <> excluded.content_hash
                        THEN excluded.content_changed_at
                        ELSE jobs.content_changed_at
                    END,
                    -- An edited ad has to earn its place in the queue again.
                    selection_state=CASE
                        WHEN jobs.content_hash <> excluded.content_hash
                        THEN NULL
                        ELSE jobs.selection_state
                    END
                """,
                (
                    source,
                    job.source_job_id,
                    job.content_hash,
                    job.title,
                    job.company,
                    job.url,
                    job.municipality,
                    job.region,
                    job.country,
                    int(job.remote),
                    int(job.fully_remote),
                    job.application_deadline,
                    job.published_at,
                    job.employment_type,
                    job.scope,
                    job.description,
                    json.dumps(sorted(job.matched_queries), ensure_ascii=False),
                    job.discovery_score,
                    json.dumps(job.raw_json, ensure_ascii=False, separators=(",", ":")),
                    now,
                    now,
                    now,
                ),
            )

    def content_hashes(self, source: str, *, prefix: str = "") -> dict[str, str]:
        """source_job_id -> content hash for one source, optionally one id prefix."""
        pattern = prefix.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
        return {
            str(row["source_job_id"]): str(row["content_hash"])
            for row in self.conn.execute(
                "SELECT source_job_id, content_hash FROM jobs WHERE source=? AND source_job_id LIKE ? ESCAPE '\\'",
                (source, pattern),
            )
        }

    def get_meta(self, key: str) -> str | None:
        row = self.conn.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return str(row["value"]) if row is not None else None

    def set_meta(self, key: str, value: str) -> None:
        with self.conn:
            self.conn.execute(
                "INSERT INTO meta(key,value) VALUES(?,?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (key, value),
            )

    def activate_live_mode(self) -> str:
        existing = self.get_meta("live_since")
        if existing:
            return existing
        value = iso_now()
        self.set_meta("live_since", value)
        return value

    def pending_jobs(
        self,
        profile_version: str,
        limit: int,
        *,
        respect_live_mode: bool = True,
    ) -> list[sqlite3.Row]:
        live_since = self.get_meta("live_since") if respect_live_mode else None
        live_clause = ""
        params: list[Any] = [profile_version]
        if live_since:
            live_clause = "AND j.content_changed_at >= ?"
            params.append(live_since)
        params.append(limit)
        return list(
            self.conn.execute(
                f"""
                SELECT j.*
                FROM jobs j
                LEFT JOIN evaluations e
                  ON e.job_id = j.id
                 AND e.content_hash = j.content_hash
                 AND e.profile_version = ?
                WHERE e.id IS NULL
                  AND j.selection_state = 'selected'
                  {live_clause}
                ORDER BY
                    COALESCE(j.rank_percentile, 1.0) ASC,
                    j.discovery_score DESC,
                    COALESCE(j.published_at, j.first_seen_at) DESC,
                    j.id DESC
                LIMIT ?
                """,
                tuple(params),
            )
        )

    def historical_pending(
        self,
        profile_version: str,
        limit: int,
        *,
        today: dt.date,
    ) -> list[sqlite3.Row]:
        """Unevaluated jobs frozen behind the live cutoff that are still open.

        Deliberately separate from `pending_jobs`: the live selector is
        untouched, and this one can never return a live job. Ordered by
        urgency, because a recovery run should reach the vacancies closing
        soonest first. Undated vacancies sort last, since they cannot expire.
        """
        live_since = self.get_meta("live_since")
        if not live_since:
            return []
        return list(
            self.conn.execute(
                """
                SELECT j.*
                FROM jobs j
                LEFT JOIN evaluations e
                  ON e.job_id = j.id
                 AND e.content_hash = j.content_hash
                 AND e.profile_version = ?
                WHERE e.id IS NULL
                  AND j.selection_state = 'selected'
                  AND j.content_changed_at < ?
                  AND (j.application_deadline IS NULL
                       OR j.application_deadline = ''
                       OR substr(j.application_deadline, 1, 10) >= ?)
                ORDER BY
                    CASE WHEN j.application_deadline IS NULL
                              OR j.application_deadline = '' THEN 1 ELSE 0 END,
                    substr(j.application_deadline, 1, 10) ASC,
                    j.discovery_score DESC,
                    COALESCE(j.published_at, j.first_seen_at) DESC,
                    j.id ASC
                LIMIT ?
                """,
                (profile_version, live_since, today.isoformat(), limit),
            )
        )

    def historical_counts(self, profile_version: str, *, today: dt.date) -> dict[str, int]:
        """Read-only breakdown of the historical backlog. No provider calls."""
        live_since = self.get_meta("live_since")
        if not live_since:
            return {}
        horizon = {
            "closing_today": 0,
            "closing_tomorrow": 0,
            "closing_within_3_days": 0,
            "closing_within_7_days": 0,
        }
        rows = self.conn.execute(
            """
            SELECT j.application_deadline AS deadline, e.id AS evaluation_id
            FROM jobs j
            LEFT JOIN evaluations e
              ON e.job_id = j.id
             AND e.content_hash = j.content_hash
             AND e.profile_version = ?
            WHERE j.content_changed_at < ?
              AND j.selection_state = 'selected'
            """,
            (profile_version, live_since),
        ).fetchall()

        unevaluated = still_open = expired = evaluated = 0
        for row in rows:
            if row["evaluation_id"] is not None:
                evaluated += 1
                continue
            unevaluated += 1
            deadline = clean_text(row["deadline"])
            if deadline and application_expired(deadline, today):
                expired += 1
                continue
            still_open += 1
            if not deadline:
                continue
            try:
                days = (dt.date.fromisoformat(deadline[:10]) - today).days
            except ValueError:
                continue
            if days <= 0:
                horizon["closing_today"] += 1
            elif days == 1:
                horizon["closing_tomorrow"] += 1
            if 0 <= days <= 3:
                horizon["closing_within_3_days"] += 1
            if 0 <= days <= 7:
                horizon["closing_within_7_days"] += 1

        awaiting = int(
            self.conn.execute(
                """
                SELECT COUNT(*)
                FROM evaluations e
                JOIN jobs j ON j.id = e.job_id
                LEFT JOIN notifications n ON n.evaluation_id = e.id
                WHERE n.evaluation_id IS NULL
                  AND e.decision IN ('notify_strong','notify_good','notify_stretch','notify_verify')
                  AND j.content_changed_at < ?
                """,
                (live_since,),
            ).fetchone()[0]
        )
        return {
            "historical_total": len(rows),
            "historical_unevaluated": unevaluated,
            "historical_still_open_pending": still_open,
            "historical_expired_unevaluated": expired,
            "historical_evaluated": evaluated,
            "historical_matches_awaiting_delivery": awaiting,
            **horizon,
        }

    def pending_count(self, profile_version: str, *, respect_live_mode: bool) -> int:
        live_since = self.get_meta("live_since") if respect_live_mode else None
        live_clause = ""
        params: list[Any] = [profile_version]
        if live_since:
            live_clause = "AND j.content_changed_at >= ?"
            params.append(live_since)
        row = self.conn.execute(
            f"""
            SELECT COUNT(*)
            FROM jobs j
            LEFT JOIN evaluations e
              ON e.job_id=j.id
             AND e.content_hash=j.content_hash
             AND e.profile_version=?
            WHERE e.id IS NULL
              AND j.selection_state = 'selected'
              {live_clause}
            """,
            tuple(params),
        ).fetchone()
        return int(row[0])

    def unranked_jobs(self, ranking_key: str, *, since: str, limit: int) -> list[sqlite3.Row]:
        """Ads in the ranking window that need a rank.

        New or edited ads, ads ranked under another profile or vocabulary, ads
        interrupted before selection, and ads passed over while embeddings were
        unavailable. Newest first, so a capped run always covers the latest.
        """
        return list(
            self.conn.execute(
                """
                SELECT id, source_job_id, content_hash, title, description
                FROM jobs
                WHERE content_changed_at >= ?
                  AND (selection_state IS NULL
                       OR rank_content_hash IS NOT content_hash
                       OR rank_profile_key IS NOT ?
                       OR (selection_state = 'not_selected' AND embedding_score IS NULL))
                ORDER BY content_changed_at DESC, id DESC
                LIMIT ?
                """,
                (since, ranking_key, limit),
            )
        )

    def save_rank_scores(
        self,
        ranking_key: str,
        scores: Sequence[tuple[int, str, float, float | None, float | None, str | None]],
    ) -> None:
        """Store (job id, content hash, vocabulary, embedding and enrichment
        scores, requested concepts as JSON).

        The old decision is cleared in the same step, so a run interrupted
        before selection leaves the ads unranked rather than stale.
        """
        now = iso_now()
        with self.conn:
            self.conn.executemany(
                "UPDATE jobs SET rank_content_hash=?, rank_profile_key=?, vocabulary_score=?, "
                "embedding_score=?, enrichment_score=?, enrichment_json=?, ranked_at=?, "
                "rank_percentile=NULL, selection_state=NULL, selection_reason=NULL "
                "WHERE id=?",
                [
                    (content_hash, ranking_key, vocabulary, embedding, enrichment, concepts, now, job_id)
                    for job_id, content_hash, vocabulary, embedding, enrichment, concepts in scores
                ],
            )

    def ranking_reference(self, ranking_key: str, *, since: str) -> list[sqlite3.Row]:
        """Every ad in the window ranked the current way: the pool a percentile is taken over."""
        return list(
            self.conn.execute(
                "SELECT id, vocabulary_score, embedding_score, enrichment_score FROM jobs "
                "WHERE content_changed_at >= ? AND rank_profile_key = ? "
                "AND rank_content_hash = content_hash AND vocabulary_score IS NOT NULL",
                (since, ranking_key),
            )
        )

    def save_selection(self, decisions: Sequence[tuple[int, float, str, str | None]]) -> None:
        """Store (job id, top percentile, 'selected' or 'not_selected', reason).

        The reason says why a selected ad is read: 'rank', 'explore' or 'fail_open'.
        """
        with self.conn:
            self.conn.executemany(
                "UPDATE jobs SET rank_percentile=?, selection_state=?, selection_reason=? WHERE id=?",
                [(percentile, state, reason, job_id) for job_id, percentile, state, reason in decisions],
            )

    def get_profile_embedding(self, profile_key: str) -> list[array.array] | None:
        row = self.conn.execute(
            "SELECT facet_names_json, vectors FROM profile_embeddings WHERE profile_key=?",
            (profile_key,),
        ).fetchone()
        if row is None:
            return None
        flat = array.array("f")
        flat.frombytes(row["vectors"])
        count = len(json.loads(row["facet_names_json"]))
        if not count or len(flat) != count * EMBEDDING_DIMENSIONS:
            return None
        return [flat[i * EMBEDDING_DIMENSIONS:(i + 1) * EMBEDDING_DIMENSIONS] for i in range(count)]

    def save_profile_embedding(
        self,
        profile_key: str,
        model: str,
        names: Sequence[str],
        vectors: Sequence[array.array],
    ) -> None:
        flat = array.array("f")
        for vector in vectors:
            flat.extend(vector)
        with self.conn:
            self.conn.execute(
                "INSERT INTO profile_embeddings(profile_key, model, facet_names_json, vectors, created_at) "
                "VALUES(?,?,?,?,?) ON CONFLICT(profile_key) DO UPDATE SET "
                "model=excluded.model, facet_names_json=excluded.facet_names_json, "
                "vectors=excluded.vectors, created_at=excluded.created_at",
                (profile_key, model, json.dumps(list(names), ensure_ascii=False), flat.tobytes(), iso_now()),
            )

    def save_evaluation(
        self,
        row: sqlite3.Row,
        evaluation: Evaluation,
        *,
        profile_version: str,
        model: str,
    ) -> None:
        with self.conn:
            self.conn.execute(
                """
                INSERT INTO evaluations(
                    job_id, content_hash, profile_version, model,
                    career_fit, opportunity_score, confidence, actual_role,
                    why_fit_json, candidate_evidence_json, must_have_json,
                    gaps_json, blockers_json, language_risk, seniority_risk,
                    location_note, decision, raw_json, evaluated_at
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(job_id, content_hash, profile_version) DO NOTHING
                """,
                (
                    row["id"],
                    row["content_hash"],
                    profile_version,
                    model,
                    evaluation.career_fit,
                    evaluation.opportunity_score,
                    evaluation.confidence,
                    evaluation.actual_role,
                    json.dumps(evaluation.why_fit, ensure_ascii=False),
                    json.dumps(evaluation.candidate_evidence, ensure_ascii=False),
                    json.dumps(evaluation.must_have_assessment, ensure_ascii=False),
                    json.dumps(evaluation.gaps, ensure_ascii=False),
                    json.dumps(evaluation.blockers, ensure_ascii=False),
                    evaluation.language_risk,
                    evaluation.seniority_risk,
                    evaluation.location_note,
                    evaluation.decision,
                    json.dumps(evaluation.raw, ensure_ascii=False, separators=(",", ":")),
                    iso_now(),
                ),
            )

    def unnotified(self, limit: int, *, historical: bool = False) -> list[sqlite3.Row]:
        live_since = self.get_meta("live_since")
        scope_clause = ""
        scope_params: tuple[Any, ...] = ()
        if live_since:
            comparison = "<" if historical else ">="
            scope_clause = f"AND j.content_changed_at {comparison} ?"
            scope_params = (live_since,)
        elif historical:
            # Nothing is historical until the backlog has been frozen.
            return []
        return list(
            self.conn.execute(
                """
                SELECT
                    e.id AS evaluation_id,
                    j.id AS job_id,
                    j.description,
                    e.career_fit,
                    e.opportunity_score,
                    e.actual_role,
                    e.why_fit_json,
                    e.gaps_json,
                    e.blockers_json,
                    e.decision,
                    j.title,
                    j.company,
                    j.url,
                    j.municipality,
                    j.region,
                    j.remote,
                    j.application_deadline
                FROM evaluations e
                JOIN jobs j ON j.id=e.job_id
                LEFT JOIN notifications n ON n.evaluation_id=e.id
                WHERE n.evaluation_id IS NULL
                  AND e.decision IN ('notify_strong','notify_good','notify_stretch','notify_verify')
                  -- One vacancy, one alert. Notifications key on the evaluation, so a
                  -- re-evaluation (a new profile version, an edited ad) mints a fresh id
                  -- and would deliver a job the candidate has already read.
                  AND NOT EXISTS (
                      SELECT 1 FROM notifications prior
                      JOIN evaluations e2 ON e2.id = prior.evaluation_id
                      WHERE e2.job_id = j.id
                  )
                  {scope_clause}
                ORDER BY e.opportunity_score DESC, e.career_fit DESC, e.evaluated_at ASC
                LIMIT ?
                """.format(scope_clause=scope_clause),
                (*scope_params, limit),
            )
        )

    def record_fingerprint(
        self,
        job_id: int,
        fingerprint: str,
        canonical_job_id: int | None,
        reason: str | None,
    ) -> None:
        with self.conn:
            self.conn.execute(
                "INSERT INTO job_fingerprints(job_id,fingerprint,canonical_job_id,reason,detected_at) "
                "VALUES(?,?,?,?,?) "
                "ON CONFLICT(job_id) DO UPDATE SET "
                "fingerprint=excluded.fingerprint, canonical_job_id=excluded.canonical_job_id, "
                "reason=excluded.reason, detected_at=excluded.detected_at",
                (job_id, fingerprint, canonical_job_id, reason, iso_now()),
            )

    def evaluated_job_rows(self) -> list[sqlite3.Row]:
        """Jobs that already carry an evaluation, for repost comparison."""
        return list(
            self.conn.execute(
                "SELECT DISTINCT j.id, j.title, j.company, j.municipality, j.region, j.description "
                "FROM jobs j JOIN evaluations e ON e.job_id=j.id"
            )
        )

    def known_fingerprints(self) -> dict[str, int]:
        return {
            str(row["fingerprint"]): int(row["job_id"])
            for row in self.conn.execute(
                "SELECT job_id, fingerprint FROM job_fingerprints WHERE canonical_job_id IS NULL"
            )
        }

    def mark_notifications_emitted(self, evaluation_ids: Sequence[int]) -> None:
        if not evaluation_ids:
            return
        now = iso_now()
        with self.conn:
            self.conn.executemany(
                "INSERT OR IGNORE INTO notifications(evaluation_id, emitted_at) VALUES(?,?)",
                [(value, now) for value in evaluation_ids],
            )

    def month_to_date_cost_usd(self, now: dt.datetime | None = None) -> float:
        """Estimated provider spend of every run recorded since the first of this UTC month."""
        month_start = (now or dt.datetime.now(UTC)).strftime("%Y-%m-01")
        total = 0.0
        for row in self.conn.execute("SELECT stats_json FROM runs WHERE started_at >= ?", (month_start,)):
            try:
                stats = json.loads(row["stats_json"] or "{}")
            except json.JSONDecodeError:
                continue
            if isinstance(stats, dict):
                total += estimated_run_cost_usd(stats)
        return total

    def status(self, profile_version: str | None = None) -> dict[str, Any]:
        def scalar(sql: str) -> int:
            return int(self.conn.execute(sql).fetchone()[0])

        live_since = self.get_meta("live_since")
        result: dict[str, Any] = {
            "jobs": scalar("SELECT COUNT(*) FROM jobs"),
            "jobs_by_source": {
                str(row[0]): int(row[1])
                for row in self.conn.execute("SELECT source, COUNT(*) FROM jobs GROUP BY source ORDER BY source")
            },
            "evaluations": scalar("SELECT COUNT(*) FROM evaluations"),
            "notifications_emitted": scalar("SELECT COUNT(*) FROM notifications"),
            "runs": scalar("SELECT COUNT(*) FROM runs"),
            "jobs_selected": scalar("SELECT COUNT(*) FROM jobs WHERE selection_state='selected'"),
            "jobs_not_selected": scalar("SELECT COUNT(*) FROM jobs WHERE selection_state='not_selected'"),
            "jobs_unranked": scalar("SELECT COUNT(*) FROM jobs WHERE selection_state IS NULL"),
            "month_to_date_cost_usd": round(self.month_to_date_cost_usd(), 2),
            "mode": "live" if live_since else "bootstrap",
            "live_since": live_since,
        }
        if profile_version:
            result["pending_all"] = self.pending_count(
                profile_version, respect_live_mode=False
            )
            result["pending_live"] = self.pending_count(
                profile_version, respect_live_mode=True
            )
        return result


class JobStreamClient:
    """Arbetsförmedlingen JobStream: every Platsbanken ad added, changed or
    unpublished since a timestamp, in one request, with no API key.

    Keyword searches only fetch the ads whose words someone thought to search
    for. The stream fetches all of them and lets ranking decide.
    """

    def __init__(self, http: HttpClient, settings: Settings) -> None:
        self.http = http
        self.settings = settings

    def stream(self, updated_after: dt.datetime) -> list[Any]:
        stamp = updated_after.replace(microsecond=0).strftime("%Y-%m-%dT%H:%M:%S")
        return self.http.json_request(
            "GET",
            f"{JOBSTREAM_BASE_URL}/v2/stream?updated-after={stamp}",
            timeout=self.settings.jobstream_timeout_seconds,
            expect=list,
        )


class JobSearchClient:
    """Arbetsförmedlingen JobSearch: open Platsbanken ads matching a query.

    JobStream only replays recent changes, so keyword queries are how a fresh
    installation reaches ads that were published before it started.
    """

    def __init__(self, http: HttpClient, settings: Settings) -> None:
        self.http = http
        self.settings = settings

    def search(self, query: str) -> list[dict[str, Any]]:
        params = urllib.parse.urlencode({"q": query, "limit": self.settings.search_limit})
        result = self.http.json_request(
            "GET",
            f"{JOBSEARCH_BASE_URL}/search?{params}",
            timeout=self.settings.jobsearch_timeout_seconds,
        )
        hits = result.get("hits", [])
        if not isinstance(hits, list):
            raise RemoteAPIError("JobSearch response did not contain a hits array")
        return [hit for hit in hits if isinstance(hit, dict)]

    def ad(self, ad_id: str) -> dict[str, Any]:
        return self.http.json_request(
            "GET",
            f"{JOBSEARCH_BASE_URL}/ad/{urllib.parse.quote(ad_id, safe='')}",
            timeout=self.settings.jobsearch_timeout_seconds,
        )


def swedish_prompt_clause(level: LanguageLevel) -> str:
    """Describe the configured Swedish level, and what follows from it."""
    if not level.known:
        return (
            "The candidate's Swedish level is not specified in the profile. Treat Swedish proficiency as UNKNOWN: "
            "do not assume it is sufficient and do not assume it is missing. "
        )
    if level.at_least(PROFESSIONAL_LANGUAGE_LEVEL):
        return (
            f"Candidate Swedish is {level.label}, which meets professional/fluent Swedish requirements. "
        )
    if level.at_least(WORKING_LANGUAGE_LEVEL):
        return (
            f"Candidate Swedish is {level.label}, below full professional proficiency but close to it. "
            "Treat explicit mandatory professional/fluent Swedish as a partial gap and a material risk, not an absolute bar. "
        )
    return (
        f"Candidate Swedish is {level.label}, below professional working proficiency. "
    )


def citizenship_prompt_clause(profile: Mapping[str, Any] | None) -> str:
    """Describe the configured eligibility position, and what follows from it.

    Silence means UNKNOWN, which the engine preserves rather than guessing. The
    distinction that decides the outcome is between a nationality requirement,
    which no permit can satisfy, and a right-to-work requirement, which a permit
    already held does satisfy. Swedish advertisements word the two almost
    identically and often in the same sentence.
    """
    constraints = profile.get("constraints") if isinstance(profile, Mapping) else None
    if not isinstance(constraints, Mapping):
        return ""

    def read(key: str) -> str:
        return clean_text(constraints.get(key)).casefold()

    if read("swedish_citizenship") not in {"yes", "no"}:
        return ""
    if read("swedish_citizenship") == "yes":
        return "The candidate is a Swedish citizen, so nationality requirements are met. "

    barred = ["Swedish citizenship"]
    if read("eu_citizenship") == "no":
        barred.append("EU/EEA citizenship")
    if read("permanent_residence") == "no":
        barred.append("a permanent residence permit")
    clause = (
        "The candidate does not hold, and cannot obtain in time, " + ", ".join(barred[:-1])
        + (" or " if len(barred) > 1 else "") + barred[-1] + ". "
        "Where an advertisement makes any of those mandatory - including a Swedish security classification "
        "that requires citizenship - the requirement is unmet: record it as a hard blocker with the reason "
        "naming which one, not as unknown. "
    )
    if read("work_permit") == "yes":
        clause += (
            "The candidate does hold a valid work permit and needs no employer sponsorship and no relocation "
            "package. So a requirement phrased as the right to work, existing work authorisation, a valid work "
            "permit, or an inability of the employer to sponsor a visa is MET and is never a blocker, even when "
            "the same sentence also mentions citizenship. Read which of the two the advertisement actually "
            "requires before deciding. "
        )
    return clause


def semantic_system_prompt(
    swedish: LanguageLevel = UNKNOWN_LANGUAGE_LEVEL,
    profile: Mapping[str, Any] | None = None,
) -> str:
    return (
        "You are an evidence-disciplined semantic career matcher for Swedish job discovery. "
        "Evaluate what the person would actually do, not the advertised title. Understand English and Swedish. "
        "Use only candidate evidence supplied below; never invent experience or turn unknown facts into unmet facts. "
        "Score career_fit only for long-term role/content alignment. Practical constraints such as language, location, "
        "citizenship, clearance, or timing belong in opportunity_score and blockers, never career_fit. "
        + swedish_prompt_clause(swedish) + citizenship_prompt_clause(profile) +
        "A Swedish-language advertisement alone is not a Swedish-language requirement. "
        "Judge explicit mandatory fluent/professional/advanced Swedish against the candidate level stated above; preferred or optional "
        "Swedish is never a blocker. Ordinary background/security screening does not imply citizenship or clearance eligibility. "
        "If citizenship or security eligibility is explicitly required and candidate evidence does not resolve it, preserve UNKNOWN. "
        "When the candidate object carries a knowledge_catalogue, it is the authoritative record of the candidate's "
        "experience, education and skill levels: check every requirement against it, treat a skill it marks "
        "not_evidenced as absent, and take seniority from its dated roles, never from a title. The application "
        "settles total years of experience, senior titles and student roles from those dates itself. Years demanded "
        "in one named field need your reading: when an advertisement asks for years in a specific discipline, role or "
        "technology (for example \"2+ years in QA and software testing\" or \"3+ years of professional Python "
        "backend\"), add a must_have_assessment row whose requirement begins with \"Years in field:\" followed by "
        "that demand. A demand for years of experience, software development or programming in general, or for "
        "several years without a named field, is total experience and gets no such row. Mark the row met when the "
        "catalogue's dated roles show that same kind of work for those years, partial when related or shorter work "
        "covers part of it, and unmet when the catalogue shows no such work. Do not lower career_fit or "
        "opportunity_score and do not add a blocker because of this row; the application applies its effect. "
        "Unknown years of experience are unknown or partial, not automatically unmet. Founder/CTO titles are not proof of "
        "staff-level seniority. "
        "Record a hard blocker when the centre of the job is work the candidate has no evidence for: a named primary "
        "language or framework the role is built on, a specialist engineering discipline such as vision, hardware or "
        "embedded systems, a named enterprise platform, or a function that is not software engineering at all such as "
        "delivery coordination or project management. Judge what the person would spend most of their week doing, not "
        "the length of the requirements list. A peripheral tool, a listed nice-to-have, or something a strong engineer "
        "picks up on the job is never a hard blocker. "
        "Return exactly one evaluation for every supplied source_job_id, no duplicates and no other IDs. "
        "Keep explanations concise. The application derives the final notification decision deterministically."
    )


def azure_chat_url(base_url: str) -> str:
    parts = urllib.parse.urlsplit(base_url.strip())
    path = parts.path.rstrip("/")
    if not path.endswith("/chat/completions"):
        path += "/chat/completions"
    return urllib.parse.urlunsplit((parts.scheme, parts.netloc, path, parts.query, ""))


def provider_json_request(
    *,
    provider: str,
    url: str,
    headers: Mapping[str, str],
    payload: Mapping[str, Any],
    timeout: int,
) -> dict[str, Any]:
    """Make exactly one provider call; orchestration owns the only fallback."""
    body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    request = urllib.request.Request(
        url,
        data=body,
        headers={
            "Accept": "application/json",
            "Content-Type": "application/json",
            "User-Agent": f"{APP_NAME}/{APP_VERSION}",
            **dict(headers),
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            raw = response.read()
    except urllib.error.HTTPError as exc:
        message = f"{provider} returned HTTP {exc.code}"
        if exc.code in {408, 425, 429, 500, 502, 503, 504}:
            retry_after = exc.headers.get("Retry-After") if exc.headers else None
            if retry_after:
                message += f" (Retry-After: {retry_after})"
            raise TemporaryProviderError(provider, message, status=exc.code) from exc
        body_text = ""
        if exc.code == 400:
            # Google answers an invalid API key with 400, not 401.
            with contextlib.suppress(Exception):
                body_text = exc.read(4000).decode("utf-8", errors="replace")
        if exc.code in ACCESS_REFUSED_HTTP_CODES or "API key not valid" in body_text or "API_KEY_INVALID" in body_text:
            raise ProviderAccessError(provider, message, status=exc.code) from exc
        raise RemoteAPIError(message) from exc
    except (urllib.error.URLError, TimeoutError, ConnectionError) as exc:
        raise TemporaryProviderError(provider, f"{provider} transport failure: {type(exc).__name__}") from exc

    try:
        parsed = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ModelOutputError(f"{provider} returned an invalid JSON envelope") from exc
    if not isinstance(parsed, dict):
        raise ModelOutputError(f"{provider} response envelope was not an object")
    return parsed


def empty_usage() -> dict[str, int]:
    return {
        "prompt_tokens": 0,
        "completion_tokens": 0,
        "reasoning_tokens": 0,
        "total_tokens": 0,
    }


def extract_azure_response(response: Mapping[str, Any]) -> tuple[str, dict[str, int]]:
    try:
        choices = response["choices"]
        content = choices[0]["message"]["content"]
    except (KeyError, IndexError, TypeError) as exc:
        raise ModelOutputError("Azure HTTP 200 response is missing completion content") from exc
    if not isinstance(content, str) or not content.strip():
        raise ModelOutputError("Azure HTTP 200 response has empty completion content")
    usage_raw = response.get("usage") if isinstance(response.get("usage"), dict) else {}
    details = usage_raw.get("completion_tokens_details") if isinstance(usage_raw.get("completion_tokens_details"), dict) else {}
    usage = {
        "prompt_tokens": int(usage_raw.get("prompt_tokens") or 0),
        "completion_tokens": int(usage_raw.get("completion_tokens") or 0),
        "reasoning_tokens": int(details.get("reasoning_tokens") or 0),
        "total_tokens": int(usage_raw.get("total_tokens") or 0),
    }
    return content.strip(), usage


def extract_gemini_response(response: Mapping[str, Any]) -> tuple[str, dict[str, int]]:
    try:
        candidates = response["candidates"]
        parts = candidates[0]["content"]["parts"]
        text_parts = [part["text"] for part in parts if isinstance(part, dict) and isinstance(part.get("text"), str)]
    except (KeyError, IndexError, TypeError) as exc:
        raise ModelOutputError("Gemini HTTP 200 response is missing candidate content") from exc
    content = "".join(text_parts).strip()
    if not content:
        raise ModelOutputError("Gemini HTTP 200 response has empty candidate content")
    usage_raw = response.get("usageMetadata") if isinstance(response.get("usageMetadata"), dict) else {}
    usage = {
        "prompt_tokens": int(usage_raw.get("promptTokenCount") or 0),
        "completion_tokens": int(usage_raw.get("candidatesTokenCount") or 0),
        "reasoning_tokens": int(usage_raw.get("thoughtsTokenCount") or 0),
        "total_tokens": int(usage_raw.get("totalTokenCount") or 0),
    }
    return content, usage


def archive_model_response(settings: Settings, provider: str, content: str, *, failure: bool) -> str | None:
    stamp = dt.datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ")
    path = (
        settings.home / "data" / "model_failures" / f"{stamp}-{provider}.txt"
        if failure
        else settings.home / "data" / f"last_{provider}_response.txt"
    )
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
        return str(path)
    except OSError as exc:
        LOG.warning("Could not archive %s model response: %s", provider, exc)
        return None


def parse_provider_evaluations(
    content: str,
    jobs: Sequence[sqlite3.Row],
    *,
    provider: str,
    model: str,
    usage: dict[str, int],
    swedish: LanguageLevel = UNKNOWN_LANGUAGE_LEVEL,
    eligibility: Mapping[str, str] | None = None,
    experience_years: float | None = None,
    exclude_student_roles: bool = False,
) -> ProviderBatchResult:
    expected = {str(row["source_job_id"]): row for row in jobs}
    expected_ids = set(expected)
    errors: list[str] = []
    try:
        parsed = json.loads(content)
    except json.JSONDecodeError as exc:
        return ProviderBatchResult(provider, model, (), frozenset(expected_ids), usage,
                                   f"invalid JSON: {compact_sentence(exc, 160)}", "output")
    if not isinstance(parsed, dict) or not isinstance(parsed.get("evaluations"), list):
        return ProviderBatchResult(provider, model, (), frozenset(expected_ids), usage,
                                   "response missing evaluations array", "output")

    raw_items = parsed["evaluations"]
    counts: dict[str, int] = {}
    for item in raw_items:
        if isinstance(item, dict):
            item_id = clean_text(item.get("source_job_id"))
            if item_id:
                counts[item_id] = counts.get(item_id, 0) + 1
    duplicates = {item_id for item_id, count in counts.items() if count > 1}
    if duplicates:
        errors.append("duplicate IDs: " + ", ".join(sorted(duplicates)[:5]))

    accepted: dict[str, Evaluation] = {}
    unknown_ids: set[str] = set()
    for item in raw_items:
        if not isinstance(item, dict):
            errors.append("non-object evaluation")
            continue
        item_id = clean_text(item.get("source_job_id"))
        if not item_id:
            errors.append("evaluation missing source_job_id")
            continue
        if item_id not in expected_ids:
            unknown_ids.add(item_id)
            continue
        if item_id in duplicates:
            continue
        try:
            accepted[item_id] = validate_evaluation(
                item, job=expected[item_id], swedish=swedish, eligibility=eligibility,
                experience_years=experience_years, exclude_student_roles=exclude_student_roles)
        except RemoteAPIError as exc:
            errors.append(f"{item_id}: {compact_sentence(exc, 140)}")
    if unknown_ids:
        errors.append("unexpected IDs: " + ", ".join(sorted(unknown_ids)[:5]))

    unresolved = expected_ids - set(accepted)
    if unresolved:
        errors.append("missing/invalid IDs: " + ", ".join(sorted(unresolved)[:5]))
    return ProviderBatchResult(
        provider=provider,
        model=model,
        evaluations=tuple(accepted.values()),
        unresolved_ids=frozenset(unresolved),
        usage=usage,
        error="; ".join(errors) if errors else None,
        # The envelope parsed, so anything left over is a completeness gap. That
        # is a normal quirk of constrained generation, never a provider failure.
        error_kind="completeness" if errors else None,
    )


class ProviderMatcher:
    """Provider-neutral matcher with equivalent structured-output prompts."""

    def __init__(
        self,
        settings: Settings,
        secrets: Mapping[str, str],
        matcher_profile: dict[str, Any],
        matcher_rules: dict[str, Any],
        provider: str,
    ) -> None:
        self.settings = settings
        self.matcher_profile = matcher_profile
        self.matcher_rules = matcher_rules
        self.provider = provider
        # Candidate proficiency is configuration; the engine never assumes a level.
        self.swedish = candidate_language_level(matcher_profile, "swedish")
        # Likewise eligibility: unstated stays UNKNOWN, stated is allowed to decide.
        self.eligibility = candidate_eligibility(matcher_profile)
        # And seniority: counted from the knowledge catalogue's dated roles, when supplied.
        self.experience_years = catalogue_experience_years(matcher_profile.get("knowledge_catalogue"))
        self.exclude_student_roles = bool(getattr(settings, "exclude_student_roles", False))
        if provider == PRIMARY_PROVIDER:
            self.model = secrets.get("VERTEX_GEMINI_MODEL", "")
            self.api_key = secrets.get("VERTEX_GEMINI_API_KEY", "")
            if not self.model or not self.api_key:
                raise ConfigurationError("Vertex Gemini credentials/model are incomplete")
            quoted_model = urllib.parse.quote(self.model, safe="")
            self.url = f"{GEMINI_BASE_URL}/{quoted_model}:generateContent"
        elif provider == FALLBACK_PROVIDER:
            self.model = secrets.get("AZURE_OPENAI_DEPLOYMENT", "")
            self.api_key = secrets.get("AZURE_OPENAI_API_KEY", "")
            base_url = secrets.get("AZURE_OPENAI_BASE_URL", "")
            if not self.model or not self.api_key or not base_url:
                raise ConfigurationError("Azure OpenAI credentials/deployment are incomplete")
            self.url = azure_chat_url(base_url)
        else:
            raise ConfigurationError(f"Automatic provider routing does not support {provider!r}")

    def evaluate(self, jobs: Sequence[sqlite3.Row]) -> ProviderBatchResult:
        expected_ids = frozenset(str(row["source_job_id"]) for row in jobs)
        if not jobs:
            return ProviderBatchResult(self.provider, self.model, (), expected_ids, empty_usage())
        payload_jobs = [row_to_model_job(row, self.settings.max_job_description_chars) for row in jobs]
        user_payload = {"candidate": self.matcher_profile, "matching_rules": self.matcher_rules, "jobs": payload_jobs}
        system_prompt = semantic_system_prompt(self.swedish, self.matcher_profile)
        if self.provider == PRIMARY_PROVIDER:
            request_payload = {
                "systemInstruction": {"parts": [{"text": system_prompt}]},
                "contents": [{"role": "user", "parts": [{"text": json.dumps(user_payload, ensure_ascii=False, separators=(",", ":"))}]}],
                "generationConfig": {
                    "maxOutputTokens": JUDGE_MAX_OUTPUT_TOKENS,
                    "responseMimeType": "application/json",
                    "responseJsonSchema": evaluation_schema(),
                    "thinkingConfig": {"thinkingLevel": "MEDIUM"},
                },
            }
            headers = {"x-goog-api-key": self.api_key}
        else:
            request_payload = {
                "model": self.model,
                "messages": [
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": json.dumps(user_payload, ensure_ascii=False, separators=(",", ":"))},
                ],
                "stream": False,
                "response_format": {
                    "type": "json_schema",
                    "json_schema": {"name": "career_job_evaluations", "strict": True, "schema": evaluation_schema()},
                },
                "reasoning_effort": "low",
                "max_completion_tokens": JUDGE_MAX_OUTPUT_TOKENS,
            }
            headers = {"api-key": self.api_key}

        try:
            response = provider_json_request(
                provider=self.provider,
                url=self.url,
                headers=headers,
                payload=request_payload,
                timeout=self.settings.gateway_timeout_seconds,
            )
            if self.provider == PRIMARY_PROVIDER:
                content, usage = extract_gemini_response(response)
            else:
                content, usage = extract_azure_response(response)
        except ModelOutputError as exc:
            return ProviderBatchResult(self.provider, self.model, (), expected_ids, exc.usage,
                                       compact_sentence(exc, 500), "output")

        result = parse_provider_evaluations(
            content, jobs, provider=self.provider, model=self.model, usage=usage,
            swedish=self.swedish, eligibility=self.eligibility,
            experience_years=self.experience_years, exclude_student_roles=self.exclude_student_roles,
        )
        archive_model_response(self.settings, self.provider, content, failure=bool(result.error))
        return result


def describe_provider_failure(error: TemporaryProviderError | ProviderAccessError) -> str:
    kind = "refused access" if isinstance(error, ProviderAccessError) else "temporary failure"
    return f"{kind} ({error.reason})"


def evaluate_with_fallback(
    primary: ProviderMatcher,
    fallback: ProviderMatcher,
    jobs: Sequence[sqlite3.Row],
    *,
    stats: "RunStats | None" = None,
) -> tuple[ProviderBatchResult, bool]:
    """One primary call, and at most one fallback call.

    The fallback answers when the primary is unreachable or temporarily failing,
    and also when it refuses this account outright - a bad key, billing or credit
    switched off - because the other provider can still judge the jobs.
    """
    try:
        return primary.evaluate(jobs), False
    except (TemporaryProviderError, ProviderAccessError) as primary_error:
        # Log the cause, not just the fact. A rate limit and a socket timeout
        # need different responses.
        LOG.warning(
            "%s %s: %s; trying %s once",
            primary.provider, describe_provider_failure(primary_error), primary_error, fallback.provider,
        )
        if stats is not None:
            key = primary_error.reason
            stats.fallback_reasons[key] = stats.fallback_reasons.get(key, 0) + 1
        try:
            return fallback.evaluate(jobs), True
        except (TemporaryProviderError, ProviderAccessError) as fallback_error:
            expected_ids = frozenset(str(row["source_job_id"]) for row in jobs)
            error = (
                f"{primary.provider} {describe_provider_failure(primary_error)}; "
                f"{fallback.provider} {describe_provider_failure(fallback_error)}"
            )
            LOG.warning("%s: %s / %s", error, primary_error, fallback_error)
            if stats is not None:
                key = f"both_failed:{primary_error.reason}"
                stats.fallback_reasons[key] = stats.fallback_reasons.get(key, 0) + 1
            return ProviderBatchResult(
                provider=fallback.provider,
                model=fallback.model,
                evaluations=(),
                unresolved_ids=expected_ids,
                usage=empty_usage(),
                error=error,
                error_kind="transport",
            ), True


# A second judgement near the card line. Two identical judge runs over the same
# delivered cards disagreed on card or no card for about one in seven of them,
# every one with an opportunity score between 55 and 75. An ad whose first score
# lands in this band is judged again and decided on the mean of both.
SECOND_JUDGEMENT_BAND = (52, 77)


def is_hard_blocker(blocker: Mapping[str, str]) -> bool:
    return clean_text(blocker.get("type")).casefold() == "hard"


def combine_judgements(first: Evaluation, second: Evaluation) -> Evaluation:
    """One decision from two judgements of the same ad.

    The scores are averaged, which halves the run-to-run noise without leaning
    either way. A hard blocker counts only when both judgements found one, so a
    blocker one run imagined cannot erase a match the other run saw.
    """
    career_fit = round((first.career_fit + second.career_fit) / 2)
    opportunity = round((first.opportunity_score + second.opportunity_score) / 2)
    first_hard = any(is_hard_blocker(b) for b in first.blockers)
    second_hard = any(is_hard_blocker(b) for b in second.blockers)
    blockers = second.blockers if first_hard and not second_hard else first.blockers
    decision = classify_decision(career_fit, opportunity, blockers)
    keeps_card = first.raw.get(FIELD_YEARS_KEEP_CARD) or second.raw.get(FIELD_YEARS_KEEP_CARD)
    if keeps_card and not decision.startswith("notify"):
        decision = "notify_stretch"
    raw = dict(first.raw)
    raw["second_judgement"] = {
        "first": {"career_fit": first.career_fit, "opportunity_score": first.opportunity_score,
                  "decision": first.decision},
        "second": {"career_fit": second.career_fit, "opportunity_score": second.opportunity_score,
                   "decision": second.decision},
    }
    return dataclasses.replace(
        first, career_fit=career_fit, opportunity_score=opportunity,
        confidence=round((first.confidence + second.confidence) / 2, 3),
        blockers=blockers, decision=decision, raw=raw,
    )


def judge_near_the_line_again(
    primary: "ProviderMatcher",
    fallback: "ProviderMatcher",
    evaluations: Sequence[Evaluation],
    rows_by_source_id: Mapping[str, sqlite3.Row],
    *,
    stats: RunStats,
) -> list[Evaluation]:
    """Judge once more the ads scored near the card line, and merge the two.

    An ad the second call does not answer keeps its first judgement.
    """
    low, high = SECOND_JUDGEMENT_BAND
    near = [
        evaluation for evaluation in evaluations
        if low <= evaluation.opportunity_score <= high and evaluation.source_job_id in rows_by_source_id
    ]
    if not near:
        return list(evaluations)
    again, used_fallback = evaluate_with_fallback(
        primary, fallback, [rows_by_source_id[evaluation.source_job_id] for evaluation in near], stats=stats)
    stats.fallback_calls += int(used_fallback)
    add_provider_usage(stats, again.usage)
    second = {evaluation.source_job_id: evaluation for evaluation in again.evaluations}
    merged: dict[str, Evaluation] = {}
    for first in near:
        other = second.get(first.source_job_id)
        if other is None:
            continue
        combined = combine_judgements(first, other)
        stats.second_judgements += 1
        if combined.decision.startswith("notify") != first.decision.startswith("notify"):
            stats.second_judgement_changes += 1
        merged[first.source_job_id] = combined
    return [merged.get(evaluation.source_job_id, evaluation) for evaluation in evaluations]


@dataclasses.dataclass
class BatchPassResult:
    """Outcome of one sequential pass over a fixed list of batches."""

    unresolved: list[sqlite3.Row] = dataclasses.field(default_factory=list)
    deferred: list[sqlite3.Row] = dataclasses.field(default_factory=list)
    provider_error: str | None = None


def run_batch_pass(
    db: "Database",
    primary: "ProviderMatcher",
    fallback: "ProviderMatcher",
    batches: Sequence[Sequence[sqlite3.Row]],
    *,
    profile_version: str,
    stats: RunStats,
    deadline: float,
    reserve_seconds: int,
    cleanup: bool = False,
) -> BatchPassResult:
    """Evaluate every batch once, in order.

    A short batch is a completeness gap, not a failure: the valid rows are
    saved, the missing IDs are collected, and processing continues. Only an
    unparseable envelope or both providers failing stops the pass, and whatever
    was never attempted comes back so the caller can report it as pending.
    """
    outcome = BatchPassResult()
    for index, batch in enumerate(batches):
        remaining = [row for later in batches[index:] for row in later]
        # Start a batch only when its worst case still fits the emergency budget.
        if index and time.monotonic() + reserve_seconds > deadline:
            LOG.warning("Emergency runtime budget reached; %d job(s) left pending", len(remaining))
            outcome.deferred.extend(remaining)
            return outcome

        result, used_fallback = evaluate_with_fallback(primary, fallback, batch, stats=stats)
        if cleanup:
            stats.cleanup_batches += 1
        else:
            stats.batches_processed += 1
        stats.fallback_calls += int(used_fallback)
        add_provider_usage(stats, result.usage)

        by_source_id = {str(row["source_job_id"]): row for row in batch}
        evaluations = list(result.evaluations)
        if time.monotonic() + reserve_seconds <= deadline:
            evaluations = judge_near_the_line_again(primary, fallback, evaluations, by_source_id, stats=stats)
        for evaluation in evaluations:
            row = by_source_id.get(evaluation.source_job_id)
            if row is None:
                continue
            db.save_evaluation(
                row, evaluation, profile_version=profile_version,
                model=f"{result.provider}:{result.model}",
            )
            stats.evaluated += 1

        if result.error_kind in {"output", "transport"}:
            # The provider did not answer usably at all. Preserve this batch and
            # everything after it rather than hammer something that is broken.
            LOG.warning(
                "%s failure on batch %d via %s: %s",
                result.error_kind, index + 1, result.provider,
                compact_sentence(result.error or "", 300),
            )
            outcome.provider_error = result.error
            outcome.deferred.extend(remaining)
            return outcome

        missing = [by_source_id[x] for x in sorted(result.unresolved_ids) if x in by_source_id]
        if missing:
            LOG.info(
                "Batch %d returned %d of %d; %d ID(s) unresolved",
                index + 1, len(result.evaluations), len(batch), len(missing),
            )
            outcome.unresolved.extend(missing)
    return outcome


# ---------------------------------------------------------------------------
# The first read.
#
# A cheap model reads what the rules let through, with a prompt a fraction of
# the judge's size: a short candidate card, the rules, and one short line back
# per ad. A confident rejection is stored like a judgement and the ad is done.
# Every ad it passes, every ad it is unsure about and every ad it fails to answer
# for goes to the judge, so the first read can save money but never cost a match.
# ---------------------------------------------------------------------------
def triage_system_prompt(exclude_student_roles: bool = True) -> str:
    """The first-read instructions. Everything about the candidate is in the card."""
    student = ", or it is an internship, thesis or student job" if exclude_student_roles else ""
    return (
        "You pre-screen Swedish job ads for one candidate. A stronger model fully judges every ad you pass, so reject "
        "only ads that are clearly not worth judging, and pass whenever you are unsure. Read Swedish and English.\n"
        "Reject when the centre of the work is a profession or specialist discipline outside the candidate's target "
        "roles and capabilities, including anything the candidate card lists under 'Not a fit' or 'No evidence of', "
        "or when the role is built on a primary language, platform or discipline the candidate has no evidence for.\n"
        "Reject when the ad makes mandatory what the candidate cannot meet: a people-manager or head role, clearly more "
        "years of experience than the card states, a citizenship, residence or clearance status the candidate lacks, "
        f"or a Swedish level above the candidate's{student}.\n"
        "Pass everything whose centre matches the candidate's target roles or strong capabilities, however unfamiliar "
        "the title.\n"
        "Answer every ad exactly once with its id, a reason of at most eight words, fit 0-100 for how well the work "
        "suits the candidate, and the verdict pass or reject."
    )


TRIAGE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "ads": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "id": {"type": "string"},
                    "reason": {"type": "string"},
                    "fit": {"type": "integer", "minimum": 0, "maximum": 100},
                    "verdict": {"type": "string", "enum": ["pass", "reject"]},
                },
                "required": ["id", "reason", "fit", "verdict"],
            },
        },
    },
    "required": ["ads"],
}


@dataclasses.dataclass(frozen=True)
class TriageVerdict:
    source_job_id: str
    passed: bool
    fit: int
    reason: str


@dataclasses.dataclass(frozen=True)
class TriageResult:
    verdicts: dict[str, TriageVerdict]
    usage: dict[str, int]
    error: str | None = None


def triage_candidate_card(profile: Mapping[str, Any]) -> str:
    """The candidate in a few hundred tokens: what a first read needs and nothing more."""
    def joined(key: str, separator: str = "; ") -> str:
        value = profile.get(key)
        if not isinstance(value, list):
            return ""
        return separator.join(clean_text(item) for item in value if clean_text(item))

    catalogue = profile.get("knowledge_catalogue")
    catalogue = catalogue if isinstance(catalogue, Mapping) else {}
    years = catalogue_experience_years(catalogue)
    groups = catalogue.get("capability_catalogue")
    lacks = [
        clean_text(item.get("skill"))
        for group in (groups.values() if isinstance(groups, Mapping) else [])
        if isinstance(group, list)
        for item in group
        if isinstance(item, Mapping) and clean_text(item.get("level")).startswith(("not_", "limited_or_not"))
    ]
    constraints = profile.get("constraints") if isinstance(profile.get("constraints"), Mapping) else {}
    eligibility = candidate_eligibility(profile) or {}
    barred = barred_statuses(eligibility)
    permit = "Holds a Swedish work permit; " if eligibility.get("work_permit") == "yes" else ""
    lines = [
        f"Core: {clean_text(profile.get('candidate_core'))}",
        f"Professional experience: {years:g} years" if years is not None else "",
        f"Strong: {joined('strong_capabilities')}",
        f"Also: {joined('secondary_capabilities')}",
        f"Tech: {joined('technology_evidence', ', ')}",
        f"Target roles: {joined('role_families_to_recognize_semantically')}",
        f"Not a fit: {joined('out_of_scope_work')}",
        f"No evidence of: {', '.join(lacks)}" if lacks else "",
        f"Education: {clean_text(profile.get('education_signal'))}",
        f"Swedish: {clean_text(constraints.get('swedish'))}",
        f"{permit}lacks {', '.join(barred)}" if barred else "",
    ]
    return "\n".join(line for line in lines if line and not line.endswith(": "))


def triage_ad_text(row: Mapping[str, Any]) -> str:
    """One ad for the first read: a header line, then the head and tail of the text."""
    location = ", ".join(x for x in (row["municipality"], row["region"]) if x) or "location unspecified"
    header = " | ".join((str(row["source_job_id"]), clean_text(row["title"]), clean_text(row["company"]), location))
    body = " ".join(clean_text(row["description"]).split())
    return f"{header}\n{truncate_middle(body, TRIAGE_DESCRIPTION_CHARS)}"


def parse_triage_verdicts(
    content: str, rows: Sequence[Mapping[str, Any]]
) -> tuple[dict[str, TriageVerdict], str | None]:
    """Usable verdicts by source_job_id, and what was wrong with the rest."""
    expected = {str(row["source_job_id"]) for row in rows}
    try:
        parsed = json.loads(content)
    except json.JSONDecodeError as exc:
        return {}, f"invalid JSON: {compact_sentence(exc, 160)}"
    items = parsed.get("ads") if isinstance(parsed, dict) else None
    if not isinstance(items, list):
        return {}, "response missing ads array"
    verdicts: dict[str, TriageVerdict] = {}
    repeated: set[str] = set()
    for item in items:
        if not isinstance(item, dict):
            continue
        ad_id = clean_text(item.get("id"))
        verdict = clean_text(item.get("verdict")).casefold()
        fit = item.get("fit")
        if ad_id not in expected or verdict not in {"pass", "reject"}:
            continue
        if isinstance(fit, bool) or not isinstance(fit, int) or not 0 <= fit <= 100:
            continue
        if ad_id in verdicts or ad_id in repeated:
            # Two answers for one ad cancel out, and the judge reads it.
            verdicts.pop(ad_id, None)
            repeated.add(ad_id)
            continue
        verdicts[ad_id] = TriageVerdict(ad_id, verdict == "pass", fit, compact_sentence(item.get("reason"), 160))
    missing = len(expected) - len(verdicts)
    return verdicts, (f"no usable verdict for {missing} of {len(expected)} ads" if missing else None)


class TriageClient:
    """The first read, on Vertex Gemini with the judge's key."""

    def __init__(self, settings: Settings, secrets: Mapping[str, str], matcher_profile: Mapping[str, Any]) -> None:
        self.settings = settings
        self.model = settings.triage_model
        self.api_key = secrets.get("VERTEX_GEMINI_API_KEY", "")
        if not self.model or not self.api_key:
            raise ConfigurationError("The first read needs config.triage_model and VERTEX_GEMINI_API_KEY")
        quoted_model = urllib.parse.quote(self.model, safe="")
        self.url = f"{GEMINI_BASE_URL}/{quoted_model}:generateContent"
        self.card = triage_candidate_card(matcher_profile)

    def request_payload(self, rows: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
        ads = "\n\n".join(triage_ad_text(row) for row in rows)
        text = f"CANDIDATE\n{self.card}\n\nADS, each opening with: id | title | employer | location\n\n{ads}"
        return {
            "systemInstruction": {"parts": [{"text": triage_system_prompt(self.settings.exclude_student_roles)}]},
            "contents": [{"role": "user", "parts": [{"text": text}]}],
            "generationConfig": {
                "temperature": 0,
                "maxOutputTokens": TRIAGE_MAX_OUTPUT_TOKENS,
                "responseMimeType": "application/json",
                "responseJsonSchema": TRIAGE_SCHEMA,
                "thinkingConfig": {"thinkingBudget": self.settings.triage_thinking_budget},
            },
        }

    def read(self, rows: Sequence[Mapping[str, Any]]) -> TriageResult:
        if not rows:
            return TriageResult({}, empty_usage())
        response = provider_json_request(
            provider=PRIMARY_PROVIDER,
            url=self.url,
            headers={"x-goog-api-key": self.api_key},
            payload=self.request_payload(rows),
            timeout=self.settings.gateway_timeout_seconds,
        )
        try:
            content, usage = extract_gemini_response(response)
        except ModelOutputError as exc:
            return TriageResult({}, {**empty_usage(), **exc.usage}, compact_sentence(exc, 300))
        verdicts, error = parse_triage_verdicts(content, rows)
        return TriageResult(verdicts, usage, error)


def triage_settles(verdict: TriageVerdict) -> bool:
    """Only a rejection well below a possible match stands without the judge."""
    return not verdict.passed and verdict.fit < TRIAGE_SETTLE_BELOW_FIT


def triage_evaluation(verdict: TriageVerdict, model: str) -> Evaluation:
    """A settled first read, stored like a judgement so the ad is never read again."""
    reason = verdict.reason or "clearly not a match"
    return Evaluation(
        source_job_id=verdict.source_job_id,
        career_fit=verdict.fit,
        opportunity_score=verdict.fit,
        confidence=0.5,
        actual_role="Settled by the first read",
        why_fit=(),
        candidate_evidence=(),
        must_have_assessment=(),
        gaps=(),
        blockers=({"type": "strong", "reason": f"First read ({model}): {reason}"},),
        language_risk="",
        seniority_risk="",
        location_note="",
        decision="store_no_notify",
        raw={"first_read": {"model": model, "verdict": "reject", "fit": verdict.fit, "reason": reason}},
    )


def run_triage_pass(
    db: "Database",
    client: "TriageClient",
    rows: Sequence[sqlite3.Row],
    *,
    profile_version: str,
    stats: RunStats,
    deadline: float,
    reserve_seconds: int,
) -> tuple[list[sqlite3.Row], list[sqlite3.Row]]:
    """Settle what the first read rejects with confidence; return (for the judge, deferred).

    Fails open. A temporary failure sends that batch to the judge; a refusal, or
    a second temporary failure in a row, sends the rest of the run.
    """
    batches = list(
        iter_batches(
            rows,
            max_jobs=client.settings.triage_batch_size,
            max_chars=client.settings.max_prompt_chars,
            max_job_description_chars=TRIAGE_DESCRIPTION_CHARS,
        )
    )
    to_judge: list[sqlite3.Row] = []
    deferred: list[sqlite3.Row] = []
    temporary_failures = 0
    for index, batch in enumerate(batches):
        if index and time.monotonic() + reserve_seconds > deadline:
            deferred = [row for later in batches[index:] for row in later]
            LOG.warning("Emergency runtime budget reached in the first read; %d job(s) left pending", len(deferred))
            break
        try:
            result = client.read(batch)
        except TemporaryProviderError as error:
            temporary_failures += 1
            LOG.warning("First read %s: %s; the judge reads this batch", describe_provider_failure(error), error)
            if temporary_failures < 2:
                to_judge.extend(batch)
                continue
            to_judge.extend(row for later in batches[index:] for row in later)
            break
        except RemoteAPIError as error:
            stats.triage_failure = getattr(error, "reason", None) or type(error).__name__
            LOG.warning("First read failed (%s): %s; the judge reads the rest of this run", stats.triage_failure, error)
            to_judge.extend(row for later in batches[index:] for row in later)
            break
        temporary_failures = 0
        stats.triage_calls += 1
        stats.triage_prompt_tokens += int(result.usage.get("prompt_tokens") or 0)
        stats.triage_completion_tokens += int(result.usage.get("completion_tokens") or 0)
        stats.triage_reasoning_tokens += int(result.usage.get("reasoning_tokens") or 0)
        if result.error:
            LOG.info("First read, batch %d: %s", index + 1, result.error)
        for row in batch:
            verdict = result.verdicts.get(str(row["source_job_id"]))
            if verdict is not None and triage_settles(verdict):
                db.save_evaluation(
                    row, triage_evaluation(verdict, client.model),
                    profile_version=profile_version, model=f"{PRIMARY_PROVIDER}:{client.model}",
                )
                stats.triage_settled += 1
                stats.evaluated += 1
            else:
                to_judge.append(row)
    stats.triage_escalated += len(to_judge)
    return to_judge, deferred


def iso_now() -> str:
    return dt.datetime.now(UTC).isoformat(timespec="microseconds")


def retry_delay(attempt: int, retry_after: str | None) -> float:
    if retry_after:
        try:
            return min(60.0, max(0.5, float(retry_after)))
        except ValueError:
            pass
    return min(30.0, (2**attempt) + random.uniform(0.0, 0.6))


def redact_url(url: str) -> str:
    """Scheme, host and path only: a query string can carry a credential."""
    parts = urllib.parse.urlsplit(url)
    return urllib.parse.urlunsplit((parts.scheme, parts.netloc, parts.path, "", ""))


def clean_text(value: Any) -> str:
    if value is None:
        return ""
    text = html.unescape(str(value))
    text = re.sub(r"<[^>]+>", " ", text)
    text = text.replace("­", "").replace("\xa0", " ")
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n\s*\n\s*\n+", "\n\n", text)
    return text.strip()


def label(value: Any) -> str:
    if isinstance(value, dict):
        return clean_text(value.get("label") or value.get("name") or "")
    return clean_text(value)


def boolish(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    if isinstance(value, str):
        return value.strip().casefold() in {"1", "true", "yes", "ja"}
    return False


def detect_work_mode(hit: Mapping[str, Any], description: str) -> tuple[bool, bool]:
    """Return (remote_or_hybrid, fully_remote).

    Arbetsförmedlingen's remote signal can include partial remote work, so it is
    not safe to use that signal to bypass a hard physical-location exclusion.
    """
    lowered = description.casefold()
    any_remote_phrases = (
        "remote work", "work remotely", "arbete på distans", "arbeta på distans",
        "jobba på distans", "distansarbete", "delvis på distans", "hybrid",
    )
    full_remote_phrases = (
        "fully remote", "100% remote", "remote anywhere in sweden",
        "work from anywhere in sweden", "helt på distans", "100 % på distans",
        "distans på heltid",
    )
    api_remote = any(boolish(hit.get(key)) for key in ("remote_work", "remote", "work_from_home"))
    fully_remote = any(phrase in lowered for phrase in full_remote_phrases)
    any_remote = api_remote or fully_remote or any(phrase in lowered for phrase in any_remote_phrases)
    return any_remote, fully_remote


def detect_remote(hit: Mapping[str, Any], description: str) -> bool:
    """Any remote signal, hybrid included."""
    return detect_work_mode(hit, description)[0]


def merge_description(hit: Mapping[str, Any]) -> str:
    desc = hit.get("description") if isinstance(hit.get("description"), dict) else {}
    sections: list[tuple[str, str]] = []
    main = clean_text(desc.get("text"))
    if main:
        sections.append(("Description", main))
    for key, title in (
        ("requirements", "Requirements"),
        ("needs", "Needs"),
        ("conditions", "Conditions"),
        ("company_information", "Company information"),
    ):
        value = clean_text(desc.get(key))
        if value and value not in main:
            sections.append((title, value))
    return "\n\n".join(f"{title}:\n{text}" for title, text in sections)


def normalize_job(hit: Mapping[str, Any]) -> JobRecord:
    """Flatten a JobStream or JobSearch ad into a JobRecord."""
    source_job_id = clean_text(hit.get("id") or hit.get("external_id"))
    if not source_job_id:
        raise ValueError("Job ad has no id")
    title = clean_text(hit.get("headline")) or "Untitled role"
    employer = hit.get("employer") if isinstance(hit.get("employer"), dict) else {}
    company = clean_text(employer.get("name") or employer.get("workplace")) or "Unknown employer"
    workplace = hit.get("workplace_address") if isinstance(hit.get("workplace_address"), dict) else {}
    municipality = clean_text(workplace.get("municipality") or workplace.get("city"))
    region = clean_text(workplace.get("region"))
    country = clean_text(workplace.get("country"))
    description = merge_description(hit)
    application = hit.get("application_details") if isinstance(hit.get("application_details"), dict) else {}
    url = clean_text(application.get("url") or hit.get("webpage_url") or employer.get("url"))
    if not url:
        url = f"https://arbetsformedlingen.se/platsbanken/annonser/{urllib.parse.quote(source_job_id)}"

    scope_obj = hit.get("scope_of_work") if isinstance(hit.get("scope_of_work"), dict) else {}
    if not scope_obj and isinstance(hit.get("scopeofwork"), dict):
        scope_obj = hit.get("scopeofwork")
    scope = ""
    if scope_obj:
        minimum = scope_obj.get("min")
        maximum = scope_obj.get("max")
        if minimum is not None or maximum is not None:
            scope = f"{minimum if minimum is not None else '?'}-{maximum if maximum is not None else '?'}%"
    if not scope:
        scope = label(hit.get("working_hours_type") or hit.get("workinghourstype"))

    published = clean_text(hit.get("publication_date") or hit.get("published_at")) or None
    deadline = clean_text(hit.get("application_deadline")) or None
    remote, fully_remote = detect_work_mode(hit, description)
    return JobRecord(
        source_job_id=source_job_id,
        title=title,
        company=company,
        url=url,
        municipality=municipality,
        region=region,
        country=country,
        remote=remote,
        fully_remote=fully_remote,
        application_deadline=deadline,
        published_at=published,
        employment_type=label(hit.get("employment_type")),
        scope=scope,
        description=description,
        raw_json=dict(hit),
    )


def application_expired(deadline: str | None, today: dt.date | None = None) -> bool:
    if not deadline:
        return False
    today = today or dt.datetime.now(UTC).date()
    match = re.match(r"^(\d{4})-(\d{2})-(\d{2})", deadline)
    if not match:
        return False
    try:
        value = dt.date(*(int(part) for part in match.groups()))
    except ValueError:
        return False
    return value < today


def job_field(job: Any, name: str) -> Any:
    """Read one field from a JobRecord or from a stored `jobs` row."""
    if isinstance(job, (sqlite3.Row, Mapping)):
        try:
            return job[name]
        except (KeyError, IndexError):
            return None
    return getattr(job, name, None)


def prefilter_reason(job: Any, settings: Settings, *, today: dt.date | None = None) -> str:
    """Deterministic exclusions, shared by discovery and historical recovery.

    Returns the reason to exclude, or "" to keep. `today` lets the caller pin
    the calendar date; discovery leaves it unset and keeps its original UTC
    behaviour, historical recovery passes the market date.
    """
    if application_expired(job_field(job, "application_deadline"), today):
        return "expired"
    country = str(job_field(job, "country") or "").casefold()
    if country and country not in {"sverige", "sweden"}:
        return "outside_sweden"
    if not job_field(job, "fully_remote"):
        location = f"{job_field(job, 'municipality')} {job_field(job, 'region')}".casefold()
        if any(term in location for term in settings.northern_exclusions):
            return "excluded_northern_location"
    if not job_field(job, "description"):
        return "missing_description"
    return ""


def should_prefilter(job: JobRecord, settings: Settings) -> tuple[bool, str]:
    reason = prefilter_reason(job, settings)
    return bool(reason), reason


def discovery_score(job: JobRecord, settings: Settings) -> int:
    """A tie-breaker only: location preference and remote.

    Title words never add to it - that would be exactly the title matching the
    ranking exists to avoid. Profile-based ranking decides the order.
    """
    score = 0
    location = f"{job.municipality} {job.region}".casefold()
    for index, preferred in enumerate(settings.preferred_locations):
        if preferred.casefold() in location:
            score += max(1, 8 - index)
            break
    if job.remote:
        score += 2
    return score


def load_json_file(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise ConfigurationError(f"Missing required file: {path}") from exc
    except (OSError, json.JSONDecodeError) as exc:
        raise ConfigurationError(f"Cannot read {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise ConfigurationError(f"Expected JSON object in {path}")
    return value


SECRET_KEYS: tuple[str, ...] = (
    "VERTEX_GEMINI_API_KEY",
    "VERTEX_GEMINI_MODEL",
    "AZURE_OPENAI_API_KEY",
    "AZURE_OPENAI_BASE_URL",
    "AZURE_OPENAI_DEPLOYMENT",
    # Kept only for explicit, manual benchmark scripts.
    "AI_GATEWAY_API_KEY",
)


def load_secrets(path: Path) -> dict[str, str]:
    """KEY=VALUE pairs from the secrets file; real environment variables win."""
    result: dict[str, str] = {}
    if path.exists():
        if os.name == "posix":
            mode = stat.S_IMODE(path.stat().st_mode)
            if mode & 0o077:
                raise ConfigurationError(
                    f"Secrets file permissions are too open ({oct(mode)}). Run: chmod 600 {path}"
                )
        for raw_line in path.read_text(encoding="utf-8").splitlines():
            line = raw_line.strip()
            if not line or line.startswith("#"):
                continue
            if line.startswith("export "):
                line = line[7:].lstrip()
            if "=" not in line:
                # Never echo the line: it may hold a credential.
                raise ConfigurationError(f"Invalid secrets line in {path}: expected KEY=VALUE")
            key, value = line.split("=", 1)
            value = value.strip()
            if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
                value = value[1:-1]
            result[key.strip()] = value
    for key in SECRET_KEYS:
        if os.getenv(key):
            result[key] = os.environ[key]
    return result


def profile_version(matcher_profile: Mapping[str, Any], matcher_rules: Mapping[str, Any]) -> str:
    canonical = json.dumps(
        {"profile": matcher_profile, "rules": matcher_rules},
        sort_keys=True,
        ensure_ascii=False,
        separators=(",", ":"),
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:16]


def truncate_middle(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    marker = "\n\n[... middle omitted deterministically ...]\n\n"
    available = max(0, limit - len(marker))
    head = int(available * 0.62)
    tail = available - head
    return text[:head].rstrip() + marker + text[-tail:].lstrip()


def row_to_model_job(row: sqlite3.Row, max_description_chars: int) -> dict[str, Any]:
    return {
        "source_job_id": str(row["source_job_id"]),
        "title": row["title"],
        "company": row["company"],
        "location": ", ".join(x for x in (row["municipality"], row["region"]) if x) or "unspecified",
        "remote_or_hybrid_indicated": bool(row["remote"]),
        "fully_remote_indicated": bool(row["fully_remote"]),
        "application_deadline": row["application_deadline"],
        "employment_type": row["employment_type"],
        "scope": row["scope"],
        "url": row["url"],
        "description": truncate_middle(row["description"], max_description_chars),
    }


FINGERPRINT_URL = re.compile(r"https?://\S+")
FINGERPRINT_NOISE = re.compile(r"[^\w\s]", re.UNICODE)


def fingerprint_text(value: Any) -> str:
    """Casefolded, punctuation-free, whitespace-collapsed form used for comparison."""
    text = FINGERPRINT_URL.sub(" ", clean_text(value).casefold())
    return re.sub(r"\s+", " ", FINGERPRINT_NOISE.sub(" ", text)).strip()


def duplicate_fingerprint(company: Any, title: Any, location: Any, description: Any) -> str:
    """Stable identity for a vacancy.

    Employer, title and location are part of the key, so the same advertisement
    published for several cities stays several distinct vacancies. The body is
    compared exactly after normalization, so only a genuine repost collides and
    a materially rewritten ad does not.
    """
    parts = (
        fingerprint_text(company),
        fingerprint_text(title),
        fingerprint_text(location),
        hashlib.sha256(fingerprint_text(description).encode("utf-8")).hexdigest(),
    )
    return hashlib.sha256("\x1f".join(parts).encode("utf-8")).hexdigest()


def row_fingerprint(row: Mapping[str, Any]) -> str:
    location = clean_text(row["municipality"]) or clean_text(row["region"])
    return duplicate_fingerprint(row["company"], row["title"], location, row["description"])


def pair_key(company: Any, title: Any) -> str:
    """Employer and title, normalised: the same posting found through another source."""
    return f"{fingerprint_text(company)}|{fingerprint_text(title)}"


def screen_before_judging(
    db: "Database",
    rows: Sequence[sqlite3.Row],
    settings: Settings,
    matcher_profile: Mapping[str, Any],
    profile_version: str,
    stats: RunStats,
) -> list[sqlite3.Row]:
    """Settle, without a model, what needs none, and return the rest.

    An ad the deterministic rules block anyway - seniority against the knowledge
    catalogue, a student role, a citizenship the candidate lacks, mandatory
    Swedish above the candidate's level - is stored with those blockers. It
    leaves the queue exactly as a judged one does, and costs nothing.
    """
    swedish = candidate_language_level(matcher_profile, "swedish")
    eligibility = candidate_eligibility(matcher_profile)
    years = catalogue_experience_years(matcher_profile.get("knowledge_catalogue"))
    remaining: list[sqlite3.Row] = []
    for row in rows:
        item: dict[str, Any] = {
            "source_job_id": str(row["source_job_id"]), "career_fit": 0, "opportunity_score": 0,
            "confidence": 1.0, "actual_role": "", "why_fit": [], "candidate_evidence": [],
            "must_have_assessment": [], "gaps": [], "blockers": [], "language_risk": "",
            "seniority_risk": "", "location_note": "",
        }
        normalized = normalize_evaluation_policy(
            item, row, swedish=swedish, eligibility=eligibility,
            experience_years=years, exclude_student_roles=settings.exclude_student_roles,
        )
        if any(is_hard_blocker(blocker) for blocker in normalized["blockers"]):
            normalized["actual_role"] = "Settled by the deterministic rules"
            db.save_evaluation(row, validate_evaluation(normalized), profile_version=profile_version, model="rules")
            stats.rules_screened += 1
            continue
        remaining.append(row)
    return remaining


def suppress_duplicate_candidates(
    db: "Database", candidates: Sequence[sqlite3.Row]
) -> tuple[list[sqlite3.Row], list[tuple[sqlite3.Row, int]]]:
    """Drop reposts before they cost a model call, keeping every source row."""
    known = db.known_fingerprints()
    for row in db.evaluated_job_rows():
        known.setdefault(row_fingerprint(row), int(row["id"]))

    kept: list[sqlite3.Row] = []
    duplicates: list[tuple[sqlite3.Row, int]] = []
    for row in candidates:
        job_id = int(row["id"])
        fingerprint = row_fingerprint(row)
        canonical = known.get(fingerprint)
        if canonical is not None and canonical != job_id:
            db.record_fingerprint(
                job_id, fingerprint, canonical, f"Repost of job {canonical}: same employer, title, location and body."
            )
            duplicates.append((row, canonical))
            continue
        db.record_fingerprint(job_id, fingerprint, None, None)
        known[fingerprint] = job_id
        kept.append(row)
    return kept, duplicates


def deduplicate_notifications(
    rows: Sequence[sqlite3.Row],
) -> tuple[list[sqlite3.Row], list[tuple[sqlite3.Row, int]]]:
    """Second guard: never send two cards for the same vacancy."""
    seen: dict[str, int] = {}
    kept: list[sqlite3.Row] = []
    duplicates: list[tuple[sqlite3.Row, int]] = []
    for row in rows:
        fingerprint = row_fingerprint(row)
        canonical = seen.get(fingerprint)
        if canonical is not None:
            duplicates.append((row, canonical))
            continue
        seen[fingerprint] = int(row["job_id"])
        kept.append(row)
    return kept, duplicates


def iter_batches(
    rows: Sequence[sqlite3.Row],
    *,
    max_jobs: int,
    max_chars: int,
    max_job_description_chars: int,
) -> Iterator[list[sqlite3.Row]]:
    batch: list[sqlite3.Row] = []
    size = 0
    for row in rows:
        estimate = len(json.dumps(row_to_model_job(row, max_job_description_chars), ensure_ascii=False))
        if batch and (len(batch) >= max_jobs or size + estimate > max_chars):
            yield batch
            batch = []
            size = 0
        batch.append(row)
        size += estimate
    if batch:
        yield batch


def evaluation_schema() -> dict[str, Any]:
    string_array = {"type": "array", "items": {"type": "string"}}
    return {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "evaluations": {
                "type": "array",
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "properties": {
                        "source_job_id": {"type": "string"},
                        "career_fit": {"type": "integer", "minimum": 0, "maximum": 100},
                        "opportunity_score": {"type": "integer", "minimum": 0, "maximum": 100},
                        "confidence": {"type": "number", "minimum": 0, "maximum": 1},
                        "actual_role": {"type": "string"},
                        "why_fit": string_array,
                        "candidate_evidence": string_array,
                        "must_have_assessment": {
                            "type": "array",
                            "items": {
                                "type": "object",
                                "additionalProperties": False,
                                "properties": {
                                    "requirement": {"type": "string"},
                                    "status": {"type": "string", "enum": ["met", "partial", "unmet", "unknown"]},
                                    "reason": {"type": "string"},
                                },
                                "required": ["requirement", "status", "reason"],
                            },
                        },
                        "gaps": string_array,
                        "blockers": {
                            "type": "array",
                            "items": {
                                "type": "object",
                                "additionalProperties": False,
                                "properties": {
                                    "type": {"type": "string", "enum": ["hard", "strong", "unknown"]},
                                    "reason": {"type": "string"},
                                },
                                "required": ["type", "reason"],
                            },
                        },
                        "language_risk": {"type": "string"},
                        "seniority_risk": {"type": "string"},
                        "location_note": {"type": "string"},
                    },
                    "required": [
                        "source_job_id", "career_fit", "opportunity_score", "confidence", "actual_role",
                        "why_fit", "candidate_evidence", "must_have_assessment", "gaps", "blockers",
                        "language_risk", "seniority_risk", "location_note"
                    ],
                },
            }
        },
        "required": ["evaluations"],
    }


def bounded_int(value: Any, field: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or not 0 <= value <= 100:
        raise RemoteAPIError(f"Invalid {field}: expected integer 0..100")
    return value


def bounded_float(value: Any, field: str) -> float:
    if not isinstance(value, (int, float)) or isinstance(value, bool) or not 0 <= float(value) <= 1:
        raise RemoteAPIError(f"Invalid {field}: expected number 0..1")
    return float(value)


def string_list(value: Any, field: str) -> tuple[str, ...]:
    if not isinstance(value, list) or not all(isinstance(x, str) for x in value):
        raise RemoteAPIError(f"Invalid {field}: expected string array")
    return tuple(clean_text(x) for x in value if clean_text(x))


def object_list(value: Any, field: str) -> tuple[dict[str, str], ...]:
    if not isinstance(value, list) or not all(isinstance(x, dict) for x in value):
        raise RemoteAPIError(f"Invalid {field}: expected object array")
    return tuple({str(k): clean_text(v) for k, v in item.items()} for item in value)


SWEDISH_MANDATORY_PATTERNS = (
    r"\b(?:fluent|professional|advanced|excellent)\s+(?:written and spoken\s+)?swedish\b",
    r"\bswedish(?: language)?\s+(?:is\s+)?(?:required|mandatory|a requirement)\b",
    r"\bmust\s+(?:speak|write|communicate in|be fluent in)\s+swedish\b",
    r"\bsvenska\s+(?:är\s+)?(?:ett krav|krävs|obligatoriskt)\b",
    r"\b(?:flytande|mycket goda|goda)\s+(?:kunskaper\s+i\s+)?svenska\b",
    r"\bbehärska(?:r)?\s+svenska\b",
    r"\bsvenska\s+i\s+tal\s+och\s+skrift\b",
    r"\b(?:i\s+)?tal\s+och\s+skrift\s+på\s+svenska\b",
)
SWEDISH_OPTIONAL_PATTERNS = (
    r"\bswedish.{0,45}\b(?:preferred|optional|a plus|an advantage|desirable|merit)\b",
    r"\b(?:preferred|optional|a plus|an advantage|desirable|meriterande|fördel).{0,45}\b(?:swedish|svenska)\b",
    r"\b(?:swedish|svenska)\s+(?:is\s+)?not required\b",
)
# An explicit denial that Swedish is required must win over any generic
# mandatory-sounding phrase elsewhere in the same advertisement.
SWEDISH_NEGATION_PATTERNS = (
    r"\b(?:kunskaper\s+i\s+)?svenska\s+(?:krävs|behövs)\s+inte\b",
    r"\bsvenska\s+är\s+inte\s+(?:ett\s+)?krav\b",
    r"\binge(?:t|n)\s+krav\s+på\s+(?:kunskaper\s+i\s+)?svenska\b",
    r"\b(?:swedish|svenska)\s+(?:language\s+)?(?:skills\s+)?(?:is|are)\s+not\s+required\b",
    r"\bno\s+(?:swedish|svenska)\s+(?:skills\s+)?(?:is\s+|are\s+)?required\b",
)
CITIZENSHIP_PATTERN = re.compile(
    r"\b(?:swedish|sweden|eu|european)\s+(?:citizen|citizenship)\b|"
    r"\bcitizenship\s+(?:is\s+)?required\b|\bmedborgarskap\b|\bmedborgare\b",
    re.IGNORECASE,
)
CLEARANCE_ELIGIBILITY_PATTERN = re.compile(
    r"\beligib(?:le|ility)\s+for\s+(?:a\s+)?(?:security\s+)?clearance\b|"
    r"\bsecurity clearance\s+(?:is\s+)?required\b|"
    r"\bmust\s+(?:qualify|be able to qualify)\s+for\s+(?:a\s+)?(?:security\s+)?clearance\b|"
    r"\bsäkerhetsklarering\s+(?:krävs|är ett krav)\b",
    re.IGNORECASE,
)
CITIZENSHIP_ALTERNATIVE_PATTERN = re.compile(
    r"\bwork\s+permits?\b|\bright\s+to\s+work\b|\bwork\s+authoris?z?ations?\b|"
    r"\barbetstillstånd\w*|\buppehållstillstånd\w*|\bpermission\s+to\s+work\b",
    re.IGNORECASE,
)
SCREENING_PATTERN = re.compile(
    r"\bbackground (?:check|screening)\b|\bsecurity screening\b|\bsäkerhetsprövning\b",
    re.IGNORECASE,
)


def matches_any(text: str, patterns: Sequence[str]) -> bool:
    return any(re.search(pattern, text, flags=re.IGNORECASE | re.DOTALL) for pattern in patterns)


def mentions(value: Any, terms: Sequence[str]) -> bool:
    folded = clean_text(value).casefold()
    return any(term in folded for term in terms)


def mandatory_language_outcome(level: LanguageLevel, language: str = "Swedish") -> dict[str, Any]:
    """Resolve an explicit mandatory professional/fluent requirement.

    The whole candidate-specific part of the Swedish policy lives here, driven
    by the configured level rather than by a level baked into the code.

      >= PROFESSIONAL (C1)  met      no blocker
      == WORKING (B2)       partial  strong blocker, opportunity capped at 69
      <= B1                 unmet    hard blocker,   opportunity capped at 49
      unknown               unknown  unknown blocker, no cap, never "unmet"
    """
    if not level.known:
        return {
            "status": "unknown",
            "blocker": "unknown",
            "cap": None,
            "reason": (
                f"The advertisement requires professional {language}, and the candidate profile "
                f"does not state a {language} level. Verify before applying."
            ),
            "risk": (
                f"Unknown: the ad requires professional {language} and the profile does not state a level."
            ),
            "change": "mandatory_language_unknown_preserved",
        }
    if level.at_least(PROFESSIONAL_LANGUAGE_LEVEL):
        return {
            "status": "met",
            "blocker": None,
            "cap": None,
            "reason": (
                f"The advertisement requires professional {language}; candidate {language} is "
                f"{level.label}, which meets it."
            ),
            "risk": f"None: candidate {language} is {level.label}.",
            "change": "mandatory_language_met",
        }
    if level.at_least(WORKING_LANGUAGE_LEVEL):
        return {
            "status": "partial",
            "blocker": "strong",
            "cap": 69,
            "reason": (
                f"The advertisement requires professional {language}; candidate {language} is "
                f"{level.label}, below full professional proficiency."
            ),
            "risk": (
                f"Material risk: the ad requires professional {language} and candidate {language} "
                f"is {level.label}."
            ),
            "change": "mandatory_language_partial",
        }
    return {
        "status": "unmet",
        "blocker": "hard",
        "cap": 49,
        "reason": (
            f"The advertisement explicitly requires professional {language}; candidate {language} "
            f"is {level.label}."
        ),
        "risk": (
            f"Hard blocker: the ad explicitly requires professional {language} and candidate "
            f"{language} is {level.label}."
        ),
        "change": "mandatory_swedish_enforced",
    }


def mentions_swedish_language(text: Any) -> bool:
    """True for a Swedish *language* requirement, false for Swedish citizenship.

    "Mandatory Swedish" and "Swedish citizenship" share a word and nothing else.
    Matching on the word alone made the language normaliser delete citizenship
    rows, which then could never become blockers.
    """
    return mentions(text, ("swedish", "svenska")) and not mentions(text, ("citizen", "medborg"))


def candidate_eligibility(profile: Mapping[str, Any]) -> dict[str, str] | None:
    """The candidate's nationality/residence position, or None when unstated.

    None keeps the conservative behaviour: an explicit citizenship demand is
    preserved as UNKNOWN rather than guessed at.
    """
    if not isinstance(profile, Mapping):
        return None
    constraints = profile.get("constraints")
    if not isinstance(constraints, Mapping):
        return None

    def read(key: str) -> str:
        return clean_text(constraints.get(key)).casefold()

    if read("swedish_citizenship") not in {"yes", "no"}:
        return None
    return {
        "swedish_citizenship": read("swedish_citizenship"),
        "eu_citizenship": read("eu_citizenship"),
        "permanent_residence": read("permanent_residence"),
        "work_permit": read("work_permit"),
    }


def barred_statuses(eligibility: Mapping[str, str] | None) -> list[str]:
    """Which of the statuses an ad may demand the candidate cannot produce."""
    if not eligibility or eligibility.get("swedish_citizenship") == "yes":
        return []
    barred = ["Swedish citizenship"]
    if eligibility.get("eu_citizenship") == "no":
        barred.append("EU/EEA citizenship")
    if eligibility.get("permanent_residence") == "no":
        barred.append("permanent residence")
    return barred


# ---------------------------------------------------------------------------
# Seniority and scope, settled from the candidate's knowledge catalogue.
#
# How long the candidate has worked is a fact the catalogue records as dated
# roles, so the engine counts it instead of asking the model to guess. The ad
# side is read verbatim: a stated number only counts in a sentence that states
# a requirement, never in one that marks a merit, a company's age or a
# contract's length.
# ---------------------------------------------------------------------------
EXPERIENCE_GAP_HARD_YEARS = 2.0
SENIOR_TITLE_MIN_YEARS = 5.0
STRETCH_CAP = 64
# A stretch card must still be a strong career fit: in end-to-end checks the
# stretch cards at career fit 70 were the ones read as noise.
STRETCH_MIN_CAREER_FIT = 75
_NUMBER_WORDS = {
    "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7, "eight": 8, "nine": 9, "ten": 10,
    "ett": 1, "en": 1, "två": 2, "tre": 3, "fyra": 4, "fem": 5, "sex": 6, "sju": 7, "åtta": 8, "nio": 9, "tio": 10,
}
_PERIOD_PATTERN = re.compile(
    r"(\d{4})(?:-(\d{1,2}))?\s*(?:to|till|-|–|—)\s*(present|now|current|pågående|nu|(\d{4})(?:-(\d{1,2}))?)",
    re.IGNORECASE,
)
_SENTENCE_SPLIT = re.compile(r"(?<=[.!?])\s+|[\n\r]+|[•●▪·;]")
REQUIRED_YEARS_PATTERN = re.compile(
    r"(?<![\w.])(\d{1,2}|" + "|".join(_NUMBER_WORDS) + r")\s*(\+|or more|eller mer)?\s*"
    r"(?:(?:-|–|to|till)\s*\d{1,2}\s*)?(?:years?|yrs?|års?|år)(?!\w)",
    re.IGNORECASE,
)
STRONG_REQUIREMENT_CUE = re.compile(r"(minimum|at least|min\.|minst|required|requires|must have|kräver|krav)", re.IGNORECASE)
EXPERIENCE_CUE = re.compile(
    r"(experience|erfarenhet|background|track record|arbetat|worked|in (?:developing|building|designing|working))",
    re.IGNORECASE,
)
MERIT_CUE = re.compile(
    r"(meriterande|merit|nice[- ]to[- ]have|a plus|an advantage|a bonus|fördel|önskvärt|preferably|preferred|"
    r"gärna|desirable|not a requirement|inget krav|inte ett krav)",
    re.IGNORECASE,
)
COMPANY_AGE_CUE = re.compile(
    r"(founded|grundad|grundades|history|historia|anniversary|jubileum|been in business|in business for|på marknaden|years old|"
    r"år gammal|years ago|år sedan|since (?:19|20)\d\d|sedan (?:19|20)\d\d)",
    re.IGNORECASE,
)
SEVERAL_YEARS_PATTERN = re.compile(r"(flera års|flerårig|several years|multiple years|a number of years)", re.IGNORECASE)
LONG_EXPERIENCE_PATTERN = re.compile(r"(mångårig|many years|lång erfarenhet|gedigen erfarenhet)", re.IGNORECASE)
# An individual senior grade is a stretch, as an architect title is; a
# people-management title stays out of reach. When a shadow judge re-read ads
# rejected for a senior title alone, a fifth of them became strong stretch cards,
# and none of those carried a management title.
SENIOR_TITLE_PATTERN = re.compile(
    # "Staff" counts only as a level ("Staff Engineer"), never in "Member of Technical Staff".
    r"(?<!\w)(senior|sr|lead|principal|"
    r"staff (?:engineer|software|developer|data|machine|ml|scientist|designer|product|architect))(?!\w)",
    re.IGNORECASE,
)
MANAGEMENT_TITLE_PATTERN = re.compile(
    r"(?<!\w)(head of|chief|director|chef|team leader|teamleader)(?!\w)", re.IGNORECASE)
ARCHITECT_TITLE_PATTERN = re.compile(r"(architect|arkitekt)", re.IGNORECASE)
# Years demanded in one named field: the judge reports them in a must-have row
# with this prefix, and the application decides what they cost.
FIELD_YEARS_PREFIX = "years in field"
FIELD_YEARS_KEEP_CARD = "_field_years_keep_card"
EARLY_CAREER_TITLE_PATTERN = re.compile(r"(?<!\w)(junior|trainee|graduate|entry)(?!\w)", re.IGNORECASE)
EARLY_CAREER_TEXT_PATTERN = re.compile(
    r"(nyexaminerad|newly graduated|recent graduate|graduate programme|graduate program|entry[- ]level|"
    r"early[- ]career|few years into your career|(?<!\d)[01]\s*(?:-|–)\s*[23]\s*(?:years|års|år))",
    re.IGNORECASE,
)
STUDENT_TITLE_PATTERN = re.compile(
    r"(?<!\w)(intern|internship|praktikant|praktik|thesis|exjobb|examensarbete|sommarjobb|summer job|"
    r"student job|studentjobb)(?!\w)",
    re.IGNORECASE,
)
STUDENT_LEDE_PATTERN = re.compile(
    r"(praktikant|praktikplats|internship|examensarbete|exjobb|thesis project|master's thesis|sommarjobb)",
    re.IGNORECASE,
)


def catalogue_experience_years(catalogue: Any, today: dt.date | None = None) -> float | None:
    """Years of professional experience in the catalogue's dated roles.

    Overlapping roles are counted once. A role without a month counts from its
    first month and to its last, erring towards more experience rather than
    less. None when the catalogue dates no role at all.
    """
    if not isinstance(catalogue, Mapping):
        return None
    today = today or market_today()
    spans: list[tuple[int, int]] = []
    for entry in catalogue.get("professional_experience") or []:
        if not isinstance(entry, Mapping):
            continue
        match = _PERIOD_PATTERN.search(str(entry.get("period") or ""))
        if not match:
            continue
        start_month = int(match.group(2)) if match.group(2) and 1 <= int(match.group(2)) <= 12 else 1
        start = int(match.group(1)) * 12 + start_month - 1
        if match.group(4) is None:
            end = today.year * 12 + today.month - 1
        else:
            end_month = int(match.group(5)) if match.group(5) and 1 <= int(match.group(5)) <= 12 else 12
            end = int(match.group(4)) * 12 + end_month - 1
        if end >= start:
            spans.append((start, end + 1))
    if not spans:
        return None
    spans.sort()
    months = 0
    current_start, current_end = spans[0]
    for start, end in spans[1:]:
        if start > current_end:
            months += current_end - current_start
            current_start, current_end = start, end
        else:
            current_end = max(current_end, end)
    months += current_end - current_start
    return round(months / 12, 2)


def required_experience_years(description: str) -> tuple[int | None, str]:
    """The largest years-of-experience requirement an ad states, and the
    sentence stating it. A number only counts beside a requirement cue, a '+',
    or the word experience; merits, company ages and contract lengths never do."""
    best: tuple[int | None, str] = (None, "")
    for sentence in _SENTENCE_SPLIT.split(description or ""):
        text = sentence.strip()
        if not text or MERIT_CUE.search(text) or COMPANY_AGE_CUE.search(text):
            continue
        experience = bool(EXPERIENCE_CUE.search(text))
        found: list[int] = []
        for match in REQUIRED_YEARS_PATTERN.finditer(text):
            raw = match.group(1).casefold()
            count = int(raw) if raw.isdigit() else _NUMBER_WORDS.get(raw)
            if count is None or not 1 <= count <= 15:
                continue
            if match.group(2) or experience or STRONG_REQUIREMENT_CUE.search(text):
                found.append(count)
        if experience and SEVERAL_YEARS_PATTERN.search(text):
            found.append(3)
        if experience and LONG_EXPERIENCE_PATTERN.search(text):
            found.append(4)
        if found and (best[0] is None or max(found) > best[0]):
            best = (max(found), text[:160])
    return best


def seniority_and_scope_findings(
    title: str,
    description: str,
    experience_years: float | None,
    exclude_student_roles: bool,
) -> tuple[list[dict[str, str]], int | None, list[str]]:
    """Blockers, an opportunity cap and change labels from the ad's own words.

    Seniority is only judged when the knowledge catalogue dates the candidate's
    experience; without it nothing here decides anything about seniority.
    """
    blockers: list[dict[str, str]] = []
    changes: list[str] = []
    cap: int | None = None
    if exclude_student_roles and (
        STUDENT_TITLE_PATTERN.search(title) or STUDENT_LEDE_PATTERN.search(description[:600])
    ):
        blockers.append({"type": "hard", "reason": "Internship, thesis or student role, outside this full-time search."})
        changes.append("student_role_out_of_scope")
    if experience_years is None:
        return blockers, cap, changes
    early_career = bool(EARLY_CAREER_TITLE_PATTERN.search(title) or EARLY_CAREER_TEXT_PATTERN.search(description))
    required, quote = required_experience_years(description)
    if required is not None and required > experience_years:
        if required - experience_years >= EXPERIENCE_GAP_HARD_YEARS and not early_career:
            blockers.append({
                "type": "hard",
                "reason": f'Requires {required}+ years of experience against {experience_years:g} '
                          f'in the knowledge catalogue: "{quote}"',
            })
            changes.append("experience_years_unmet")
        else:
            cap = STRETCH_CAP
            changes.append("experience_years_stretch")
    if experience_years < SENIOR_TITLE_MIN_YEARS and not early_career:
        if MANAGEMENT_TITLE_PATTERN.search(title):
            blockers.append({
                "type": "hard",
                "reason": f'Management title "{clean_text(title)}" against {experience_years:g} years '
                          f"in the knowledge catalogue.",
            })
            changes.append("management_title_unmet")
        elif SENIOR_TITLE_PATTERN.search(title):
            cap = STRETCH_CAP
            changes.append("senior_title_stretch")
        elif ARCHITECT_TITLE_PATTERN.search(title):
            cap = STRETCH_CAP
            changes.append("architect_title_stretch")
    return blockers, cap, changes


def normalize_evaluation_policy(
    item: Mapping[str, Any],
    job: Mapping[str, Any],
    *,
    swedish: LanguageLevel = UNKNOWN_LANGUAGE_LEVEL,
    eligibility: Mapping[str, str] | None = None,
    experience_years: float | None = None,
    exclude_student_roles: bool = False,
) -> dict[str, Any]:
    """Apply narrow, auditable policy facts before deriving the decision."""
    normalized = dict(item)
    description = clean_text(job["description"])
    must_haves = [dict(x) for x in item.get("must_have_assessment", []) if isinstance(x, dict)]
    blockers = [dict(x) for x in item.get("blockers", []) if isinstance(x, dict)]
    changes: list[str] = []

    negated_swedish = matches_any(description, SWEDISH_NEGATION_PATTERNS)
    optional_swedish = matches_any(description, SWEDISH_OPTIONAL_PATTERNS) or negated_swedish
    mandatory_swedish = matches_any(description, SWEDISH_MANDATORY_PATTERNS) and not negated_swedish
    if mandatory_swedish:
        outcome = mandatory_language_outcome(swedish)
        swedish_rows = [x for x in must_haves if mentions_swedish_language(x.get("requirement"))]
        if swedish_rows:
            for row in swedish_rows:
                row["status"] = outcome["status"]
                row["reason"] = outcome["reason"]
        else:
            must_haves.append({
                "requirement": "Mandatory Swedish proficiency",
                "status": outcome["status"],
                "reason": outcome["reason"],
            })
        blockers = [x for x in blockers if not mentions_swedish_language(x.get("reason"))]
        if outcome["blocker"] is not None:
            blockers.append({"type": outcome["blocker"], "reason": outcome["reason"]})
        if outcome["cap"] is not None:
            normalized["opportunity_score"] = min(
                bounded_int(item.get("opportunity_score"), "opportunity_score"), outcome["cap"]
            )
        normalized["language_risk"] = outcome["risk"]
        changes.append(outcome["change"])
    else:
        # Being written in Swedish, or merely preferring Swedish, is not a must-have.
        must_haves = [x for x in must_haves if not mentions_swedish_language(x.get("requirement"))]
        blockers = [x for x in blockers if not mentions_swedish_language(x.get("reason"))]
        if optional_swedish:
            normalized["language_risk"] = "Swedish is optional/preferred and is not a blocker."
            changes.append("optional_swedish_not_blocking")

    explicit_citizenship = bool(CITIZENSHIP_PATTERN.search(description))
    explicit_clearance = bool(CLEARANCE_ELIGIBILITY_PATTERN.search(description))
    screening_only = bool(SCREENING_PATTERN.search(description)) and not explicit_citizenship and not explicit_clearance
    sensitive_terms = ("citizen", "citizenship", "medborg", "clearance", "security eligibility")
    clearance_terms = ("clearance", "security eligibility")
    # Citizenship is only forced to UNKNOWN while the profile does not answer it.
    citizenship_unresolved = explicit_citizenship and eligibility is None
    barred = barred_statuses(eligibility)
    # A permit the candidate already holds satisfies a right-to-work demand, so the
    # ad has to be asking for nationality itself before this can bite.
    permit_route = bool(CITIZENSHIP_ALTERNATIVE_PATTERN.search(description))
    citizenship_barred = explicit_citizenship and bool(barred) and not permit_route
    if citizenship_barred:
        # Whether the candidate holds a nationality is a fact the profile states,
        # not a judgement, so the engine settles it instead of asking the model. A
        # conditional demand ("citizenship may be required") still counts: it cannot
        # be satisfied either, and leaving it as unknown just forwards the dead end.
        reason = (
            "Requires " + " or ".join(barred) + ", which the candidate does not hold, "
            "and the ad offers no work-permit alternative."
        )
        must_haves = [x for x in must_haves if not mentions(x.get("requirement"), sensitive_terms)]
        must_haves.append({
            "requirement": "Citizenship or residence status",
            "status": "unmet",
            "reason": reason,
        })
        blockers = [x for x in blockers if not mentions(x.get("reason"), sensitive_terms)]
        blockers.append({"type": "hard", "reason": reason})
        changes.append("citizenship_barred")
    elif screening_only:
        must_haves = [x for x in must_haves if not mentions(x.get("requirement"), sensitive_terms)]
        blockers = [x for x in blockers if not mentions(x.get("reason"), sensitive_terms)]
        changes.append("screening_not_converted_to_eligibility")
    elif citizenship_unresolved or explicit_clearance:
        requirement = "Citizenship eligibility" if citizenship_unresolved else "Security-clearance eligibility"
        # With citizenship answered, only clearance is rewritten, so the evaluator's
        # own citizenship finding survives instead of being flattened to unknown.
        terms = sensitive_terms if citizenship_unresolved else clearance_terms
        related = [x for x in must_haves if mentions(x.get("requirement"), terms)]
        if related:
            for row in related:
                row["status"] = "unknown"
                row["reason"] = f"{requirement} is explicit in the ad but unresolved by candidate evidence."
        else:
            must_haves.append({
                "requirement": requirement,
                "status": "unknown",
                "reason": f"{requirement} is explicit in the ad but unresolved by candidate evidence.",
            })
        blockers = [x for x in blockers if not mentions(x.get("reason"), terms)]
        blockers.append({"type": "unknown", "reason": f"{requirement} requires candidate verification."})
        changes.append("explicit_eligibility_preserved_unknown")

    def years_in_field(row: Mapping[str, Any]) -> bool:
        """A row the judge wrote for years demanded in one named field."""
        return clean_text(row.get("requirement")).casefold().startswith(FIELD_YEARS_PREFIX)

    # Lack of evidence for a numeric tenure claim is unknown, not proof of failure.
    for row in must_haves:
        if years_in_field(row):
            continue
        combined = f"{clean_text(row.get('requirement'))} {clean_text(row.get('reason'))}"
        if re.search(r"\b\d+\+?\s*(?:years?|yrs?|år)\b", combined, flags=re.IGNORECASE) and mentions(
            row.get("reason"), ("no evidence", "not stated", "not specified", "not provided", "cannot confirm", "cannot verify", "unknown", "unclear")
        ):
            if clean_text(row.get("status")).casefold() == "unmet":
                row["status"] = "unknown"
                changes.append("unknown_tenure_preserved")

    # Any genuinely unmet item in the model's mandatory-requirement list is hard,
    # except years in a named field, which the application prices below.
    for row in must_haves:
        if clean_text(row.get("status")).casefold() == "unmet" and not years_in_field(row):
            reason = f"Mandatory requirement unmet: {clean_text(row.get('requirement'))}"
            if not any(x.get("type") == "hard" and clean_text(x.get("reason")) == reason for x in blockers):
                blockers.append({"type": "hard", "reason": reason})

    # Seniority and scope: the catalogue's dates against the ad's own words.
    title = ""
    with contextlib.suppress(KeyError, IndexError, TypeError):
        title = clean_text(job["title"])
    found, cap, found_changes = seniority_and_scope_findings(
        title, description, experience_years, exclude_student_roles)
    blockers.extend(found)
    if cap is not None:
        normalized["opportunity_score"] = min(
            bounded_int(normalized.get("opportunity_score"), "opportunity_score"), cap)
    changes.extend(found_changes)

    # Years in one named field: a gap caps the ad at a stretch and never removes a
    # card it would otherwise be. In a shadow judge this demoted wrong-field cards
    # and deleted none of the real matches.
    if any(years_in_field(row) and clean_text(row.get("status")).casefold() in {"partial", "unmet"}
           for row in must_haves):
        blockers = [
            x for x in blockers
            if not (is_hard_blocker(x) and FIELD_YEARS_PREFIX in clean_text(x.get("reason")).casefold())
        ]
        opportunity = bounded_int(normalized.get("opportunity_score"), "opportunity_score")
        career_fit = bounded_int(normalized.get("career_fit"), "career_fit")
        normalized[FIELD_YEARS_KEEP_CARD] = classify_decision(career_fit, opportunity, blockers).startswith("notify")
        normalized["opportunity_score"] = min(opportunity, STRETCH_CAP)
        changes.append("field_years_stretch")

    normalized["must_have_assessment"] = must_haves
    normalized["blockers"] = blockers
    normalized["_policy_changes"] = changes
    return normalized


def genuine_unknown_blocker(blocker: Mapping[str, str]) -> bool:
    return clean_text(blocker.get("type")).casefold() == "unknown" and mentions(
        blocker.get("reason"),
        ("citizen", "citizenship", "medborg", "clearance", "security eligibility", "work authorization"),
    )


def classify_decision(career_fit: int, opportunity: int, blockers: Sequence[Mapping[str, str]]) -> str:
    blocker_types = {str(x.get("type", "")).casefold() for x in blockers}
    if "hard" in blocker_types:
        return "store_no_notify"
    if any(genuine_unknown_blocker(x) for x in blockers) and career_fit >= 85 and opportunity >= 55:
        return "notify_verify"
    if opportunity >= 85:
        return "notify_strong"
    if opportunity >= 70:
        return "notify_good"
    if opportunity >= 60 and career_fit >= STRETCH_MIN_CAREER_FIT:
        return "notify_stretch"
    return "store_no_notify"


def validate_evaluation(
    item: Mapping[str, Any],
    *,
    job: Mapping[str, Any] | None = None,
    swedish: LanguageLevel = UNKNOWN_LANGUAGE_LEVEL,
    eligibility: Mapping[str, str] | None = None,
    experience_years: float | None = None,
    exclude_student_roles: bool = False,
) -> Evaluation:
    item = (
        normalize_evaluation_policy(
            item, job, swedish=swedish, eligibility=eligibility,
            experience_years=experience_years, exclude_student_roles=exclude_student_roles,
        )
        if job is not None else dict(item)
    )
    source_job_id = clean_text(item.get("source_job_id"))
    if not source_job_id:
        raise RemoteAPIError("Evaluation missing source_job_id")
    career_fit = bounded_int(item.get("career_fit"), "career_fit")
    opportunity = bounded_int(item.get("opportunity_score"), "opportunity_score")
    confidence = bounded_float(item.get("confidence"), "confidence")
    must_haves = object_list(item.get("must_have_assessment"), "must_have_assessment")
    blockers = object_list(item.get("blockers"), "blockers")
    valid_states = {"met", "partial", "unmet", "unknown"}
    for req in must_haves:
        if req.get("status") not in valid_states:
            raise RemoteAPIError(f"Invalid must-have status for {source_job_id}")
    valid_blockers = {"hard", "strong", "unknown"}
    for blocker in blockers:
        if blocker.get("type") not in valid_blockers:
            raise RemoteAPIError(f"Invalid blocker type for {source_job_id}")
    decision = classify_decision(career_fit, opportunity, blockers)
    if item.get(FIELD_YEARS_KEEP_CARD) and not decision.startswith("notify"):
        # A years-in-field gap may demote a card to a stretch, never remove it.
        decision = "notify_stretch"
    return Evaluation(
        source_job_id=source_job_id,
        career_fit=career_fit,
        opportunity_score=opportunity,
        confidence=confidence,
        actual_role=clean_text(item.get("actual_role")),
        why_fit=string_list(item.get("why_fit"), "why_fit"),
        candidate_evidence=string_list(item.get("candidate_evidence"), "candidate_evidence"),
        must_have_assessment=must_haves,
        gaps=string_list(item.get("gaps"), "gaps"),
        blockers=blockers,
        language_risk=clean_text(item.get("language_risk")),
        seniority_risk=clean_text(item.get("seniority_risk")),
        location_note=clean_text(item.get("location_note")),
        decision=decision,
        raw=dict(item),
    )


KNOWLEDGE_CATALOGUE_FILE = "knowledge_catalogue.json"
# Who the candidate is, as opposed to what they can do, never reaches a model.
CATALOGUE_IDENTITY_KEYS = ("candidate",)


def load_profile_bundle(settings: Settings) -> tuple[dict[str, Any], dict[str, Any], str]:
    """The matcher profile, with the knowledge catalogue attached when present,
    the matching rules, and the version both together define."""
    matcher_profile = load_json_file(settings.profile_dir / "matcher_profile.json")
    matcher_rules = load_json_file(settings.profile_dir / "matcher_rules_v1_1.json")
    catalogue_path = settings.profile_dir / KNOWLEDGE_CATALOGUE_FILE
    if catalogue_path.exists():
        catalogue = load_json_file(catalogue_path)
        matcher_profile["knowledge_catalogue"] = {
            key: value for key, value in catalogue.items() if key not in CATALOGUE_IDENTITY_KEYS
        }
    return matcher_profile, matcher_rules, profile_version(matcher_profile, matcher_rules)


# ---------------------------------------------------------------------------
# Discovery: Platsbanken through JobStream and JobSearch.
# ---------------------------------------------------------------------------
def fetch_jobstream(
    client: Any,
    db: "Database",
    settings: Settings,
    stats: RunStats,
    *,
    now: dt.datetime | None = None,
) -> tuple[dict[str, JobRecord], str]:
    """Read the stream since the stored cursor.

    Returns the live jobs and the cursor to store once they are persisted. The
    caller advances the cursor, not this function: advancing before the jobs are
    stored would turn a crash into a silent gap. Unpublished ads are counted and
    skipped, because nothing downstream has a closed state to move them into.
    """
    current = (now or dt.datetime.now(UTC)).replace(tzinfo=None, microsecond=0)
    since: dt.datetime | None = None
    stored = db.get_meta(JOBSTREAM_CURSOR_KEY)
    if stored:
        try:
            since = dt.datetime.fromisoformat(stored)
        except ValueError:
            LOG.warning("Ignoring unreadable JobStream cursor %r", stored)
    if since is None:
        since = current - dt.timedelta(hours=settings.jobstream_lookback_hours)
    oldest = current - dt.timedelta(hours=settings.jobstream_max_window_hours)
    if since < oldest:
        LOG.warning(
            "JobStream cursor %s is more than %dh old; clamping. Ads changed in the gap "
            "are not replayed.",
            since.isoformat(), settings.jobstream_max_window_hours,
        )
        stats.stream_window_clamped = True
        since = oldest

    # One second of overlap: a duplicate costs nothing, a gap loses a job.
    ads = client.stream(since - dt.timedelta(seconds=1))
    stats.stream_entries = len(ads)
    jobs: dict[str, JobRecord] = {}
    for ad in ads:
        if not isinstance(ad, dict):
            continue
        if ad.get("removed"):
            stats.stream_removed += 1
            continue
        try:
            job = normalize_job(ad)
        except (ValueError, TypeError) as exc:
            stats.stream_malformed += 1
            LOG.warning("Skipping malformed job ad: %s", exc)
            continue
        jobs[job.source_job_id] = job
    stats.unique_jobs = len(jobs)
    for job in jobs.values():
        job.discovery_score = discovery_score(job, settings)
    return jobs, current.isoformat()


def build_queries(settings: Settings) -> list[str]:
    """search_terms crossed with location_terms, deduplicated case-insensitively."""
    queries: list[str] = []
    seen: set[str] = set()
    for term in settings.search_terms:
        variants = [term] if settings.include_unlocated_searches or not settings.location_terms else []
        variants.extend(f"{term} {location}" for location in settings.location_terms)
        for query in variants:
            normalized = " ".join(query.split())
            if normalized.casefold() not in seen:
                seen.add(normalized.casefold())
                queries.append(normalized)
    return queries


def fetch_jobsearch(client: Any, settings: Settings, stats: RunStats) -> dict[str, JobRecord]:
    """Run every keyword query; a failed query is logged, all failing raises."""
    queries = build_queries(settings)
    LOG.info("Running %d JobSearch queries", len(queries))
    merged: dict[str, JobRecord] = {}
    failures = 0
    for index, query in enumerate(queries, start=1):
        stats.queries_attempted += 1
        try:
            hits = client.search(query)
            stats.queries_succeeded += 1
        except RemoteAPIError as exc:
            failures += 1
            LOG.error("JobSearch query failed [%s]: %s", query, compact_sentence(exc, 200))
            continue
        stats.search_hits += len(hits)
        for hit in hits:
            try:
                job = normalize_job(hit)
            except (ValueError, TypeError) as exc:
                LOG.warning("Skipping malformed job ad: %s", exc)
                continue
            if not job.description:
                try:
                    job = normalize_job(client.ad(job.source_job_id))
                except (RemoteAPIError, ValueError, TypeError) as exc:
                    LOG.warning("Could not load the full ad %s: %s", job.source_job_id, exc)
            existing = merged.setdefault(job.source_job_id, job)
            existing.matched_queries.add(query)
        if settings.query_delay_seconds and index < len(queries):
            time.sleep(settings.query_delay_seconds)
    if queries and stats.queries_succeeded == 0:
        raise RemoteAPIError(f"All {failures} JobSearch queries failed")
    for job in merged.values():
        job.discovery_score = discovery_score(job, settings)
    return merged


def persist_discovered_jobs(
    db: Database,
    jobs: Iterable[JobRecord],
    settings: Settings,
    stats: RunStats,
) -> None:
    for job in jobs:
        skip, reason = should_prefilter(job, settings)
        if skip:
            stats.jobs_prefiltered += 1
            LOG.debug("Prefiltered %s (%s): %s", job.source_job_id, reason, job.title)
            continue
        db.upsert_job(job)
        stats.jobs_upserted += 1


# ---------------------------------------------------------------------------
# Discovery: company career sites.
#
# Many employers publish vacancies on their own career site that never reach
# Platsbanken, and most of those sites run on a handful of applicant-tracking
# systems with a public feed. One collector per platform turns a feed into
# JobRecords; from then on a posting is ranked, screened and judged like any
# other ad. Collectors read public endpoints only, one site at a time, with a
# pause between detail pages.
# ---------------------------------------------------------------------------
CAREER_SITE_SOURCE = "career_site"

_HTML_SCRIPT_STYLE = re.compile(r"<(script|style)\b.*?</\1\s*>", re.IGNORECASE | re.DOTALL)
_HTML_BREAK = re.compile(r"<br\s*/?>", re.IGNORECASE)
_HTML_LIST_ITEM = re.compile(r"<li\b[^>]*>", re.IGNORECASE)
_HTML_BLOCK_END = re.compile(r"</(?:p|div|li|h[1-6]|tr|section|article|ul|ol)\s*>", re.IGNORECASE)
_HTML_TAG = re.compile(r"<[^>]+>")


def html_to_text(value: Any) -> str:
    """Readable plain text from posting HTML, keeping paragraph and list breaks."""
    if not value:
        return ""
    text = _HTML_SCRIPT_STYLE.sub(" ", str(value))
    text = _HTML_BREAK.sub("\n", text)
    text = _HTML_LIST_ITEM.sub("\n- ", text)
    text = _HTML_BLOCK_END.sub("\n", text)
    text = html.unescape(_HTML_TAG.sub(" ", text))
    text = text.replace("\r\n", "\n").replace("\r", "\n").replace("\xa0", " ").replace("­", "")
    lines = (" ".join(line.split()) for line in text.split("\n"))
    return re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).strip()


def parse_date(value: Any) -> str | None:
    """YYYY-MM-DD from an ISO date or timestamp, or an RFC 822 date; None otherwise."""
    text = clean_text(value)
    if not text:
        return None
    match = re.match(r"^(\d{4})-(\d{2})-(\d{2})", text)
    if match:
        with contextlib.suppress(ValueError):
            return dt.date(*(int(part) for part in match.groups())).isoformat()
        return None
    with contextlib.suppress(TypeError, ValueError, IndexError):
        return email.utils.parsedate_to_datetime(text).date().isoformat()
    return None


def text_list(value: Any) -> str:
    if isinstance(value, list):
        return ", ".join(clean_text(item) for item in value if clean_text(item))
    return clean_text(value)


# Folded place names. A location naming Sweden or a Swedish city is Swedish; one
# naming another country or a large foreign city is not; anything else falls
# back to the site's default country, because losing a Swedish role to an
# unrecognised office name costs more than reading a foreign one.
_SWEDISH_PLACES = (
    "sweden", "sverige", "stockholm", "goteborg", "gothenburg", "malmo", "lund", "uppsala", "linkoping",
    "vasteras", "orebro", "helsingborg", "norrkoping", "jonkoping", "umea", "lulea", "gavle", "boras",
    "sodertalje", "eskilstuna", "halmstad", "vaxjo", "karlstad", "sundsvall", "ostersund", "kista", "solna",
    "sundbyberg", "nacka", "skovde", "trollhattan", "karlskrona", "kalmar", "visby", "ludvika",
)
_FOREIGN_PLACES = (
    "united states", "usa", "us", "canada", "mexico", "brazil", "argentina", "colombia", "chile",
    "united kingdom", "uk", "england", "ireland", "scotland", "germany", "deutschland", "france", "spain",
    "portugal", "italy", "netherlands", "belgium", "poland", "czech", "hungary", "romania", "bulgaria",
    "austria", "switzerland", "greece", "turkey", "ukraine", "serbia", "croatia", "denmark", "danmark",
    "norway", "norge", "finland", "suomi", "iceland", "estonia", "latvia", "lithuania", "india", "china",
    "japan", "korea", "singapore", "malaysia", "indonesia", "vietnam", "taiwan", "australia", "new zealand",
    "israel", "egypt", "south africa", "nigeria", "kenya", "philippines", "uae", "dubai",
    "washington", "california", "texas", "new york", "san francisco", "seattle", "boston", "chicago",
    "austin", "toronto", "vancouver", "montreal", "sao paulo", "mexico city", "bogota",
    "london", "manchester", "dublin", "berlin", "munich", "munchen", "hamburg", "frankfurt", "cologne",
    "koln", "stuttgart", "dusseldorf", "paris", "lyon", "madrid", "barcelona", "lisbon", "porto", "milan",
    "milano", "rome", "amsterdam", "rotterdam", "brussels", "antwerp", "zurich", "geneva", "vienna", "wien",
    "prague", "praha", "warsaw", "warszawa", "krakow", "wroclaw", "budapest", "bucharest", "sofia",
    "athens", "istanbul", "kyiv", "belgrade", "copenhagen", "kobenhavn", "aarhus", "oslo", "bergen",
    "trondheim", "helsinki", "espoo", "tampere", "tallinn", "riga", "vilnius", "bangalore", "bengaluru",
    "mumbai", "delhi", "hyderabad", "chennai", "pune", "beijing", "shanghai", "shenzhen", "tokyo", "seoul",
    "taipei", "sydney", "melbourne", "auckland", "tel aviv", "cairo", "nairobi", "lagos",
)
_SWEDISH_PLACE_PATTERN = re.compile(r"(?<![a-z])(?:" + "|".join(map(re.escape, _SWEDISH_PLACES)) + r")(?![a-z])")
_FOREIGN_PLACE_PATTERN = re.compile(r"(?<![a-z])(?:" + "|".join(map(re.escape, _FOREIGN_PLACES)) + r")(?![a-z])")


def country_name(value: Any) -> str:
    """A structured country value as stored: 'Sverige' for Sweden, otherwise as given."""
    if isinstance(value, Mapping):
        value = value.get("descriptor") or value.get("name") or value.get("addressCountry")
    text = clean_text(value)
    return "Sverige" if text.casefold() in {"se", "swe", "sweden", "sverige"} else text


def place_country(text: str) -> str:
    """'Sverige' or 'Outside Sweden' when a free-text location says which, else ''."""
    folded = fold_text(text)
    if _SWEDISH_PLACE_PATTERN.search(folded):
        return "Sverige"
    if _FOREIGN_PLACE_PATTERN.search(folded):
        return "Outside Sweden"
    return ""


def career_site_job(
    site: CareerSite,
    *,
    key: Any,
    title: Any,
    url: Any,
    description: str,
    company: Any = "",
    city: Any = "",
    region: Any = "",
    location: Any = "",
    country: Any = "",
    remote: bool = False,
    published: str | None = None,
    deadline: str | None = None,
    employment_type: Any = "",
) -> JobRecord | None:
    """One posting as a JobRecord, or None while it carries no usable text."""
    key, title, description = clean_text(key), clean_text(title), description.strip()
    if not key or not title or len(description) < len(title) + 20:
        return None
    location = clean_text(location)
    municipality = clean_text(city) or location.split(",")[0].strip() or clean_text(site.option("city"))
    region = clean_text(region) or clean_text(site.option("region"))
    resolved_country = (
        country_name(country)
        or place_country(" ".join((location, municipality, region)))
        or clean_text(site.option("country", "Sverige"))
    )
    remote = remote or bool(re.search(r"\b(remote|distans)\b", location, re.IGNORECASE))
    any_remote, fully_remote = detect_work_mode({"remote": remote}, description)
    return JobRecord(
        source_job_id=f"{site.name}:{key}",
        title=title,
        company=site.company or clean_text(company) or site.name,
        url=clean_text(url) or site.url,
        municipality=municipality,
        region=region,
        country=resolved_country,
        remote=any_remote,
        fully_remote=fully_remote,
        application_deadline=deadline,
        published_at=published,
        employment_type=clean_text(employment_type),
        scope="",
        description=description,
        raw_json={"career_site": site.name, "platform": site.platform, "posting_key": key},
    )


def jobposting_place(posting: Mapping[str, Any]) -> tuple[str, str, str]:
    """(city, region, country) from a schema.org JobPosting's first location."""
    places = posting.get("jobLocation")
    place = places[0] if isinstance(places, list) and places else places
    address = place.get("address") if isinstance(place, Mapping) else None
    if not isinstance(address, Mapping):
        return "", "", ""
    city = clean_text(address.get("addressLocality"))
    region = clean_text(address.get("addressRegion"))
    country = country_name(address.get("addressCountry"))
    if region and country_name(region) == country:
        region = ""  # some feeds repeat the country as the region
    return city, region, country


def _mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _polite_pause(settings: Settings) -> None:
    if settings.query_delay_seconds:
        time.sleep(settings.query_delay_seconds)


def _site_int(site: CareerSite, key: str, default: int) -> int:
    value = site.option(key, default)
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ConfigurationError(f"career site {site.name}: {key} must be a positive integer")
    return value


def collect_teamtailor(http: HttpClient, site: CareerSite, settings: Settings, known: set[str]) -> list[JobRecord]:
    """Teamtailor publishes `{site}/jobs.json`: a JSON Feed with every open job,
    its full description and an embedded schema.org JobPosting."""
    feed = http.json_request("GET", f"{site.url}/jobs.json", timeout=settings.career_site_timeout_seconds)
    items = feed.get("items")
    if not isinstance(items, list):
        raise RemoteAPIError(f"The Teamtailor feed at {site.url} has no items array")
    feed_company = clean_text(feed.get("title"))
    jobs: list[JobRecord] = []
    for item in items:
        if not isinstance(item, Mapping):
            continue
        posting = _mapping(item.get("_jobposting"))
        city, region, country = jobposting_place(posting)
        job = career_site_job(
            site,
            key=item.get("id") or item.get("url"),
            title=posting.get("title") or item.get("title"),
            url=item.get("url"),
            description=html_to_text(posting.get("description") or item.get("content_html")),
            company=_mapping(posting.get("hiringOrganization")).get("name") or feed_company,
            city=city, region=region, country=country,
            remote=clean_text(posting.get("jobLocationType")).upper() == "TELECOMMUTE",
            published=parse_date(posting.get("datePosted") or item.get("date_published")),
            deadline=parse_date(posting.get("validThrough")),
            employment_type=text_list(posting.get("employmentType")),
        )
        if job is not None:
            jobs.append(job)
    return jobs


_VARBI_JOB_ID = re.compile(r"jobID:(\d+)", re.IGNORECASE)
_VARBI_FEED_TITLE = re.compile(r"^(?:new jobs at|nya lediga jobb hos)\s+", re.IGNORECASE)


def collect_varbi(http: HttpClient, site: CareerSite, settings: Settings, known: set[str]) -> list[JobRecord]:
    """Varbi, used by Swedish universities and agencies, publishes an RSS feed at
    `{site}/what:rssfeed/`. The feed names no city, so `city` and `region` come
    from the site's configuration."""
    body = http.fetch(
        f"{site.url}/what:rssfeed/",
        timeout=settings.career_site_timeout_seconds,
        accept="application/rss+xml, application/xml;q=0.9, */*;q=0.8",
    )
    try:
        # ElementTree resolves no external entities, and expat bounds entity expansion.
        root = ET.fromstring(body)
    except ET.ParseError as exc:
        raise RemoteAPIError(f"The Varbi feed at {site.url} is not valid XML: {exc}") from exc
    company = _VARBI_FEED_TITLE.sub("", clean_text(root.findtext("channel/title")))
    jobs: list[JobRecord] = []
    for item in root.iter("item"):
        link = clean_text(item.findtext("link"))
        job_id = _VARBI_JOB_ID.search(link)
        if job_id is None:
            continue
        job = career_site_job(
            site,
            key=job_id.group(1),
            title=item.findtext("title"),
            url=link,
            description=html_to_text(item.findtext("description")),
            company=company,
            published=parse_date(item.findtext("pubDate")),
        )
        if job is not None:
            jobs.append(job)
    return jobs


def collect_greenhouse(http: HttpClient, site: CareerSite, settings: Settings, known: set[str]) -> list[JobRecord]:
    """Greenhouse's public job-board API returns the whole board, descriptions included."""
    board = urllib.parse.quote(clean_text(site.option("board")), safe="")
    payload = http.json_request(
        "GET", f"https://boards-api.greenhouse.io/v1/boards/{board}/jobs?content=true",
        timeout=settings.career_site_timeout_seconds,
    )
    postings = payload.get("jobs")
    if not isinstance(postings, list):
        raise RemoteAPIError(f"The Greenhouse board {board} returned no jobs array")
    jobs: list[JobRecord] = []
    for posting in postings:
        if not isinstance(posting, Mapping):
            continue
        job = career_site_job(
            site,
            key=posting.get("id"),
            title=posting.get("title"),
            url=posting.get("absolute_url"),
            # Greenhouse sends the description as escaped HTML.
            description=html_to_text(html.unescape(str(posting.get("content") or ""))),
            company=posting.get("company_name"),
            location=_mapping(posting.get("location")).get("name"),
            published=parse_date(posting.get("first_published") or posting.get("updated_at")),
        )
        if job is not None:
            jobs.append(job)
    return jobs


def collect_lever(http: HttpClient, site: CareerSite, settings: Settings, known: set[str]) -> list[JobRecord]:
    """Lever's postings API returns every posting with its description. Boards on
    Lever's EU instance need `"instance": "eu"`."""
    board = urllib.parse.quote(clean_text(site.option("board")), safe="")
    host = "api.eu.lever.co" if clean_text(site.option("instance")).casefold() == "eu" else "api.lever.co"
    postings = http.json_request(
        "GET", f"https://{host}/v0/postings/{board}?mode=json",
        timeout=settings.career_site_timeout_seconds, expect=list,
    )
    jobs: list[JobRecord] = []
    for posting in postings:
        if not isinstance(posting, Mapping):
            continue
        categories = _mapping(posting.get("categories"))
        parts = [clean_text(posting.get("descriptionPlain")) or html_to_text(posting.get("description"))]
        for block in posting.get("lists") or []:
            if isinstance(block, Mapping):
                parts.append(f"{clean_text(block.get('text'))}\n{html_to_text(block.get('content'))}")
        parts.append(clean_text(posting.get("additionalPlain")))
        created = posting.get("createdAt")
        published = (
            dt.datetime.fromtimestamp(created / 1000, UTC).date().isoformat()
            if isinstance(created, (int, float)) and not isinstance(created, bool) else None
        )
        job = career_site_job(
            site,
            key=posting.get("id"),
            title=posting.get("text"),
            url=posting.get("hostedUrl") or posting.get("applyUrl"),
            description="\n\n".join(part for part in parts if part.strip()),
            location=categories.get("location"),
            country=posting.get("country"),
            remote=clean_text(posting.get("workplaceType")).casefold() == "remote",
            published=published,
            employment_type=categories.get("commitment"),
        )
        if job is not None:
            jobs.append(job)
    return jobs


def collect_ashby(http: HttpClient, site: CareerSite, settings: Settings, known: set[str]) -> list[JobRecord]:
    """Ashby's public job-board API returns the whole board, descriptions included."""
    board = urllib.parse.quote(clean_text(site.option("board")), safe="")
    payload = http.json_request(
        "GET", f"https://api.ashbyhq.com/posting-api/job-board/{board}?includeCompensation=false",
        timeout=settings.career_site_timeout_seconds,
    )
    postings = payload.get("jobs")
    if not isinstance(postings, list):
        raise RemoteAPIError(f"The Ashby board {board} returned no jobs array")
    jobs: list[JobRecord] = []
    for posting in postings:
        if not isinstance(posting, Mapping) or posting.get("isListed") is False:
            continue
        postal = _mapping(_mapping(posting.get("address")).get("postalAddress"))
        job = career_site_job(
            site,
            key=posting.get("id"),
            title=posting.get("title"),
            url=posting.get("jobUrl"),
            description=html_to_text(posting.get("descriptionHtml")) or clean_text(posting.get("descriptionPlain")),
            company=payload.get("name"),
            city=postal.get("addressLocality"),
            region=postal.get("addressRegion"),
            country=postal.get("addressCountry"),
            location=posting.get("location"),
            remote=bool(posting.get("isRemote")),
            published=parse_date(posting.get("publishedAt")),
            employment_type=posting.get("employmentType"),
        )
        if job is not None:
            jobs.append(job)
    return jobs


SMARTRECRUITERS_API = "https://api.smartrecruiters.com/v1/companies"
SMARTRECRUITERS_SECTIONS = ("companyDescription", "jobDescription", "qualifications", "additionalInformation")


def collect_smartrecruiters(
    http: HttpClient, site: CareerSite, settings: Settings, known: set[str]
) -> list[JobRecord]:
    """SmartRecruiters lists postings without descriptions, filtered server-side
    by `country` (default "se"). Each description is one detail request, so only
    postings not yet stored are fetched, at most career_site_max_details a run."""
    company = urllib.parse.quote(clean_text(site.option("board")), safe="")
    country = clean_text(site.option("country", "se"))
    listings: list[Mapping[str, Any]] = []
    for offset in range(0, 100 * _site_int(site, "max_pages", 10), 100):
        query = {"limit": 100, "offset": offset, **({"country": country} if country else {})}
        page = http.json_request(
            "GET", f"{SMARTRECRUITERS_API}/{company}/postings?{urllib.parse.urlencode(query)}",
            timeout=settings.career_site_timeout_seconds,
        )
        content = page.get("content")
        if not isinstance(content, list):
            raise RemoteAPIError(f"The SmartRecruiters board {company} returned no content array")
        listings.extend(item for item in content if isinstance(item, Mapping))
        if len(content) < 100:
            break
    jobs: list[JobRecord] = []
    details = 0
    for listing in listings:
        posting_id = clean_text(listing.get("id"))
        if not posting_id or posting_id in known:
            continue
        if details >= settings.career_site_max_details:
            break
        details += 1
        _polite_pause(settings)
        try:
            detail = http.json_request(
                "GET", f"{SMARTRECRUITERS_API}/{company}/postings/{urllib.parse.quote(posting_id, safe='')}",
                timeout=settings.career_site_timeout_seconds,
            )
        except RemoteAPIError as exc:
            LOG.warning("SmartRecruiters posting %s could not be read: %s", posting_id, compact_sentence(exc, 200))
            continue
        sections = _mapping(_mapping(detail.get("jobAd")).get("sections"))
        place = _mapping(listing.get("location"))
        job = career_site_job(
            site,
            key=posting_id,
            title=listing.get("name"),
            url=detail.get("postingUrl") or f"https://jobs.smartrecruiters.com/{company}/{posting_id}",
            description="\n\n".join(
                text for text in (html_to_text(_mapping(sections.get(name)).get("text"))
                                  for name in SMARTRECRUITERS_SECTIONS) if text
            ),
            company=_mapping(listing.get("company")).get("name"),
            city=place.get("city"),
            region=place.get("region"),
            country=place.get("country"),
            remote=bool(place.get("remote")),
            published=parse_date(listing.get("releasedDate")),
            employment_type=_mapping(listing.get("typeOfEmployment")).get("label"),
        )
        if job is not None:
            jobs.append(job)
    return jobs


WORKDAY_PAGE_SIZE = 20


def collect_workday(http: HttpClient, site: CareerSite, settings: Settings, known: set[str]) -> list[JobRecord]:
    """Workday career sites answer a JSON search at `{site}/wday/cxs/{tenant}/{site}/jobs`.

    `applied_facets` narrows the list server-side (a country, for instance) and
    `search_text` filters it. The list carries no descriptions, so only postings
    not yet stored get a detail request, at most career_site_max_details a run.
    """
    tenant = urllib.parse.quote(clean_text(site.option("tenant")), safe="")
    board = urllib.parse.quote(clean_text(site.option("site")), safe="")
    facets = site.option("applied_facets", {})
    if not isinstance(facets, Mapping):
        raise ConfigurationError(f"career site {site.name}: applied_facets must be an object")
    api = f"{site.url}/wday/cxs/{tenant}/{board}"
    postings: dict[str, Mapping[str, Any]] = {}
    total: int | None = None
    for page in range(_site_int(site, "max_pages", 25)):
        payload = http.json_request(
            "POST", f"{api}/jobs",
            payload={"appliedFacets": dict(facets), "limit": WORKDAY_PAGE_SIZE,
                     "offset": page * WORKDAY_PAGE_SIZE, "searchText": clean_text(site.option("search_text", ""))},
            timeout=settings.career_site_timeout_seconds,
        )
        if total is None:
            # Workday reports the total on the first page only.
            with contextlib.suppress(TypeError, ValueError):
                total = int(payload.get("total") or 0)
        batch = payload.get("jobPostings")
        if not isinstance(batch, list):
            raise RemoteAPIError(f"The Workday site {site.url} returned no jobPostings array")
        for posting in batch:
            path = clean_text(_mapping(posting).get("externalPath"))
            if path:
                postings.setdefault(path, posting)
        if len(batch) < WORKDAY_PAGE_SIZE or (page + 1) * WORKDAY_PAGE_SIZE >= (total or 0):
            break

    locale = clean_text(site.option("locale", "en-US"))
    jobs: list[JobRecord] = []
    details = 0
    for path, posting in postings.items():
        if path in known:
            continue
        if details >= settings.career_site_max_details:
            break
        details += 1
        _polite_pause(settings)
        try:
            info = _mapping(http.json_request(
                "GET", f"{api}{path}", timeout=settings.career_site_timeout_seconds).get("jobPostingInfo"))
        except RemoteAPIError as exc:
            LOG.warning("Workday posting %s could not be read: %s", path, compact_sentence(exc, 200))
            continue
        job = career_site_job(
            site,
            key=path,
            title=info.get("title") or posting.get("title"),
            url=info.get("externalUrl") or f"{site.url}/{locale}/{board}{path}",
            description=html_to_text(info.get("jobDescription")),
            location=info.get("location") or posting.get("locationsText"),
            country=info.get("country"),
            employment_type=info.get("timeType"),
        )
        if job is not None:
            jobs.append(job)
    return jobs


_SF_ROW = re.compile(
    r'<a[^>]+class="[^"]*jobTitle-link[^"]*"[^>]*href="(?P<href>/job/[^"]+)"[^>]*>(?P<title>.*?)</a>',
    re.IGNORECASE | re.DOTALL,
)
_SF_JOB_ID = re.compile(r"/job/[^/]*/(\d+)/?")
_SF_DESCRIPTION = re.compile(r'<span class="jobdescription">(.*?)</span>\s*</div>', re.IGNORECASE | re.DOTALL)
_SF_LOCATION = re.compile(r'<span[^>]+class="[^"]*jobLocation[^"]*"[^>]*>(.*?)</span>', re.IGNORECASE | re.DOTALL)


def collect_successfactors(
    http: HttpClient, site: CareerSite, settings: Settings, known: set[str]
) -> list[JobRecord]:
    """SAP SuccessFactors career sites have no public JSON API, but render the
    same markup for every customer: a search page listing `jobTitle-link` rows,
    filtered by `location_search` (default "Sweden"), and one page per job.
    Detail pages are fetched only for postings not yet stored."""
    query_base = {"q": "", "sortColumn": "referencedate", "sortDirection": "desc"}
    location_search = clean_text(site.option("location_search", "Sweden"))
    if location_search:
        query_base["locationsearch"] = location_search
    rows: dict[str, tuple[str, str]] = {}
    start = page_size = 0
    for _ in range(_site_int(site, "max_pages", 10)):
        query = {**query_base, **({"startrow": start} if start else {})}
        page = http.fetch(
            f"{site.url}/search/?{urllib.parse.urlencode(query)}",
            timeout=settings.career_site_timeout_seconds, accept="text/html,*/*;q=0.8",
        ).decode("utf-8", errors="replace")
        found: dict[str, tuple[str, str]] = {}
        for match in _SF_ROW.finditer(page):
            job_id = _SF_JOB_ID.search(match.group("href"))
            if job_id:  # the markup links each row twice
                found.setdefault(job_id.group(1), (match.group("href"), html_to_text(match.group("title"))))
        new = {job_id: row for job_id, row in found.items() if job_id not in rows}
        rows.update(new)
        page_size = page_size or len(found)
        if not new or len(found) < page_size:
            break
        start += len(found)

    jobs: list[JobRecord] = []
    details = 0
    for job_id, (href, title) in rows.items():
        if job_id in known:
            continue
        if details >= settings.career_site_max_details:
            break
        details += 1
        _polite_pause(settings)
        url = urllib.parse.urljoin(f"{site.url}/", href.lstrip("/"))
        try:
            page = http.fetch(url, timeout=settings.career_site_timeout_seconds, accept="text/html,*/*;q=0.8")
        except RemoteAPIError as exc:
            LOG.warning("SuccessFactors posting %s could not be read: %s", job_id, compact_sentence(exc, 200))
            continue
        text = page.decode("utf-8", errors="replace")
        body = _SF_DESCRIPTION.search(text)
        # A customised page loses the description wrapper; the whole page still reads.
        description = html_to_text(body.group(1)) if body else html_to_text(text)[:14_000]
        location = _SF_LOCATION.search(text)
        slug_city = urllib.parse.unquote(href.split("/")[2].split("-")[0]) if href.count("/") >= 3 else ""
        job = career_site_job(
            site,
            key=job_id,
            title=title,
            url=url,
            description=description,
            location=html_to_text(location.group(1)) if location else slug_city,
        )
        if job is not None:
            jobs.append(job)
    return jobs


CareerSiteCollector = Callable[[HttpClient, CareerSite, Settings, "set[str]"], "list[JobRecord]"]
CAREER_SITE_COLLECTORS: dict[str, CareerSiteCollector] = {
    "teamtailor": collect_teamtailor,
    "varbi": collect_varbi,
    "greenhouse": collect_greenhouse,
    "lever": collect_lever,
    "ashby": collect_ashby,
    "smartrecruiters": collect_smartrecruiters,
    "workday": collect_workday,
    "successfactors": collect_successfactors,
}


def collect_career_sites(http: HttpClient, db: Database, settings: Settings, stats: RunStats) -> list[str]:
    """Read every configured career site and store its postings; return the sites that failed.

    A failing site is logged and skipped, never fatal. A posting already stored
    from another source under the same employer and title is the same vacancy
    and stays out, and an unchanged posting is not written again.
    """
    elsewhere = {
        pair_key(row[0], row[1])
        for row in db.conn.execute("SELECT company, title FROM jobs WHERE source <> ?", (CAREER_SITE_SOURCE,))
    }
    failed: list[str] = []
    for site in settings.career_sites:
        stored = db.content_hashes(CAREER_SITE_SOURCE, prefix=f"{site.name}:")
        known = {source_id.split(":", 1)[1] for source_id in stored}
        try:
            jobs = CAREER_SITE_COLLECTORS[site.platform](http, site, settings, known)
        except Exception as exc:  # a broken or redesigned site must never stop discovery
            stats.career_sites_failed += 1
            failed.append(site.name)
            LOG.error("Career site %s (%s) failed: %s", site.name, site.platform, compact_sentence(exc, 300),
                      exc_info=not isinstance(exc, RoleLensError))
            continue
        stats.career_sites_read += 1
        written = 0
        for job in jobs:
            stats.career_site_jobs_seen += 1
            if pair_key(job.company, job.title) in elsewhere:
                stats.career_site_jobs_known += 1
                continue
            if stored.get(job.source_job_id) == job.content_hash:
                continue
            if should_prefilter(job, settings)[0]:
                stats.jobs_prefiltered += 1
                continue
            job.discovery_score = discovery_score(job, settings)
            db.upsert_job(job, source=CAREER_SITE_SOURCE)
            written += 1
        stats.career_site_jobs_stored += written
        LOG.info("Career site %s (%s): %d posting(s) read, %d new or changed",
                 site.name, site.platform, len(jobs), written)
    return failed


def discover(http: HttpClient, db: Database, settings: Settings, stats: RunStats) -> None:
    """Read every enabled source and store what it finds. Costs no model tokens.

    A failing source is reported and the others still run. Only a run in which
    every enabled source failed raises, so the scheduler surfaces it.
    """
    attempted = succeeded = 0
    if settings.use_jobstream:
        attempted += 1
        try:
            jobs, next_cursor = fetch_jobstream(JobStreamClient(http, settings), db, settings, stats)
            persist_discovered_jobs(db, jobs.values(), settings, stats)
            # Advance only once the batch is stored, so a failure anywhere above
            # replays the same window next run instead of silently skipping it.
            db.set_meta(JOBSTREAM_CURSOR_KEY, next_cursor)
            succeeded += 1
        except RemoteAPIError as exc:
            stats.discovery_failures.append("JobStream")
            LOG.error("JobStream failed: %s", compact_sentence(exc, 300))
    if settings.search_terms:
        attempted += 1
        try:
            found = fetch_jobsearch(JobSearchClient(http, settings), settings, stats)
            persist_discovered_jobs(db, found.values(), settings, stats)
            succeeded += 1
        except RemoteAPIError as exc:
            stats.discovery_failures.append("JobSearch")
            LOG.error("JobSearch failed: %s", compact_sentence(exc, 300))
    if settings.career_sites:
        attempted += 1
        failed = collect_career_sites(http, db, settings, stats)
        stats.discovery_failures.extend(f"career site {name}" for name in failed)
        if len(failed) < len(settings.career_sites):
            succeeded += 1
    LOG.info(
        "Discovery: %d stream entries (%d unpublished), %d JobSearch hits from %d/%d queries, "
        "%d career-site posting(s) from %d site(s); %d stored, %d prefiltered",
        stats.stream_entries, stats.stream_removed, stats.search_hits, stats.queries_succeeded,
        stats.queries_attempted, stats.career_site_jobs_seen, stats.career_sites_read,
        stats.jobs_upserted + stats.career_site_jobs_stored, stats.jobs_prefiltered,
    )
    if attempted and not succeeded:
        raise RemoteAPIError("Every discovery source failed: " + ", ".join(stats.discovery_failures))


# ---------------------------------------------------------------------------
# Ranking: which stored ads the evaluator reads.
#
# Independent orders over the same ads: a weighted role vocabulary built from
# the candidate's own files, the embedding similarity between an ad and the
# closest section of the matcher profile, and - optionally - the competencies
# JobTech's enrichment finds the ad requesting. Reciprocal rank fusion combines
# them, and an ad is selected when it sits within the top share of every ad
# ranked in the reference window. Every score reads the title and the
# description together, so no title decides anything on its own.
# ---------------------------------------------------------------------------
_FOLD_WHITESPACE = re.compile("[ \t ]+")


def fold_text(value: str) -> str:
    """Lowercase, strip diacritics, collapse spaces and tabs.

    Line breaks survive, so a phrase never matches across one.
    """
    decomposed = unicodedata.normalize("NFKD", value.lower())
    stripped = "".join(ch for ch in decomposed if not unicodedata.combining(ch))
    return _FOLD_WHITESPACE.sub(" ", stripped).strip()


def ranking_text(title: Any, description: Any) -> str:
    """The text every score reads, cut where the calibration cut it."""
    return f"{title}\n{description}"[:RANK_TEXT_CHARS]


@dataclasses.dataclass(frozen=True)
class RoleVocabulary:
    """Weighted role terms, matched word-aware against folded ad text.

    A space at either end of a term marks a word boundary on that side, and a
    term of four characters or fewer gets both, so " rag " never fires inside
    "uppdrag" nor " erp" inside "enterprise". A boundary applies only where the
    term itself starts or ends with a letter or digit, which keeps "rag-"
    matching "rag-baserad". Each term counts once per ad, and negative weights
    mark professions the candidate cannot do.
    """

    terms: tuple[tuple[re.Pattern[str], float, str], ...]
    fingerprint: str

    @classmethod
    def from_terms(cls, raw_terms: Mapping[str, Any]) -> "RoleVocabulary":
        compiled: list[tuple[re.Pattern[str], float, str]] = []
        for raw_term, weight in raw_terms.items():
            if isinstance(weight, bool) or not isinstance(weight, (int, float)):
                raise ConfigurationError(f"Vocabulary term {raw_term!r} needs a numeric weight")
            term = fold_text(raw_term)
            if not term:
                continue
            short = len(term) <= 4
            start = (raw_term[:1] == " " or short) and term[:1].isalnum()
            end = (raw_term[-1:] == " " or short) and term[-1:].isalnum()
            pattern = (
                (r"(?<![0-9a-z])" if start else "")
                + re.escape(term)
                + (r"(?![0-9a-z])" if end else "")
            )
            compiled.append((re.compile(pattern), float(weight), term))
        if not compiled:
            raise ConfigurationError("The role vocabulary has no terms")
        canonical = json.dumps(raw_terms, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
        return cls(tuple(compiled), hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:16])

    @classmethod
    def load(cls, path: Path) -> "RoleVocabulary":
        terms = load_json_file(path).get("terms")
        if not isinstance(terms, dict):
            raise ConfigurationError(f"{path} must map each term to a weight under 'terms'")
        return cls.from_terms(terms)

    def score(self, text: str) -> float:
        folded = fold_text(text)
        return float(sum(weight for pattern, weight, _ in self.terms if pattern.search(folded)))


def profile_facets(settings: Settings) -> tuple[tuple[str, str], ...]:
    """The candidate as separately embedded sections of matcher_profile.json.

    Sections stay apart so one strong area can match an ad without being
    averaged away by the rest, and a section too long for one embedding is
    split by its own keys rather than cut off.
    """
    profile = load_json_file(settings.profile_dir / "matcher_profile.json")
    facets: list[tuple[str, str]] = []
    for section in PROFILE_FACET_SECTIONS:
        value = profile.get(section)
        if not value:
            continue
        text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
        if len(text) <= PROFILE_FACET_CHARS or not isinstance(value, dict):
            facets.append((section, text[:PROFILE_FACET_CHARS]))
            continue
        for key, part in value.items():
            if part:
                part_text = part if isinstance(part, str) else json.dumps(part, ensure_ascii=False)
                facets.append((f"{section}.{key}", part_text[:PROFILE_FACET_CHARS]))
    if not facets:
        raise ConfigurationError(
            "matcher_profile.json has none of the sections that rank jobs: " + ", ".join(PROFILE_FACET_SECTIONS)
        )
    return tuple(facets)


@dataclasses.dataclass(frozen=True)
class RankingContext:
    """Everything a rank depends on, and the keys that change when it does."""

    vocabulary: RoleVocabulary
    facets: tuple[tuple[str, str], ...]
    embedding_model: str
    enrichment: bool = False

    @classmethod
    def load(cls, settings: Settings) -> "RankingContext":
        return cls(
            vocabulary=RoleVocabulary.load(settings.profile_dir / "role_vocabulary.json"),
            facets=profile_facets(settings),
            embedding_model=settings.embedding_model,
            enrichment=settings.use_enrichment,
        )

    @property
    def embedding_key(self) -> str:
        """Names one embedding of the profile; a new profile or model gets a new one."""
        canonical = json.dumps(
            {"model": self.embedding_model, "dimensions": EMBEDDING_DIMENSIONS, "facets": self.facets},
            ensure_ascii=False,
            separators=(",", ":"),
        )
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:16]

    @property
    def ranking_key(self) -> str:
        """Names one way of ranking. When it changes, the whole window is ranked again."""
        enrichment = ENRICHMENT_VERSION if self.enrichment else "no-enrichment"
        canonical = f"{self.embedding_key}:{self.vocabulary.fingerprint}:{RANK_TEXT_CHARS}:{RRF_K}:{enrichment}"
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:16]


class EmbeddingClient:
    """Vertex AI text embeddings through the publisher-model predict endpoint.

    Vectors are requested at EMBEDDING_DIMENSIONS and normalised here: shortened
    gemini-embedding-001 vectors come back unscaled, and a dot product is only a
    cosine similarity between unit vectors.
    """

    def __init__(
        self,
        settings: Settings,
        secrets: Mapping[str, str],
        http: HttpClient | None = None,
    ) -> None:
        self.model = settings.embedding_model
        self.api_key = secrets.get("VERTEX_GEMINI_API_KEY", "")
        if not self.api_key:
            raise ConfigurationError("VERTEX_GEMINI_API_KEY is required for ranking embeddings")
        self.url = f"{GEMINI_BASE_URL}/{urllib.parse.quote(self.model, safe='')}:predict"
        self.http = http or HttpClient(
            retries=settings.http_retries, user_agent=f"{APP_NAME}/{APP_VERSION}"
        )
        self.timeout = settings.embedding_timeout_seconds
        self.batch_size = settings.embedding_batch_size
        self.calls = 0
        self.tokens = 0
        self.quota_pauses = 0
        self.sleep = time.sleep

    def embed(self, texts: Sequence[str], task_type: str) -> list[array.array | None]:
        """One unit vector per text, in order.

        A text the model rejects on its own comes back as None. Anything else -
        a bad key, exhausted retries, a malformed answer - raises RemoteAPIError.
        """
        vectors: list[array.array | None] = []
        position = 0
        rejected_in_a_row = 0
        while position < len(texts):
            chunk = list(texts[position:position + self.batch_size])
            try:
                response = self.http.json_request(
                    "POST",
                    self.url,
                    headers={"x-goog-api-key": self.api_key},
                    payload={
                        "instances": [{"content": text, "task_type": task_type} for text in chunk],
                        "parameters": {"outputDimensionality": EMBEDDING_DIMENSIONS},
                    },
                    timeout=self.timeout,
                )
            except RateLimitError:
                if self.quota_pauses >= EMBEDDING_QUOTA_MAX_PAUSES:
                    raise
                self.quota_pauses += 1
                LOG.warning(
                    "Embedding quota reached; pausing %ds (%d of at most %d this run)",
                    EMBEDDING_QUOTA_PAUSE_SECONDS, self.quota_pauses, EMBEDDING_QUOTA_MAX_PAUSES,
                )
                self.sleep(EMBEDDING_QUOTA_PAUSE_SECONDS)
                continue
            except HttpStatusError as exc:
                # A bad key also answers 400; only a request-shaped 400 is recoverable.
                if exc.status != 400 or "API key" in str(exc) or "API_KEY" in str(exc):
                    raise
                if len(chunk) > 1:
                    self.batch_size = max(1, len(chunk) // 2)
                    LOG.warning("Embedding request rejected; %d ad(s) per request from now on", self.batch_size)
                    continue
                rejected_in_a_row += 1
                if rejected_in_a_row >= 3:
                    raise
                LOG.warning("The embedding model rejected one ad; it ranks on the vocabulary alone")
                vectors.append(None)
                position += 1
                continue
            self.calls += 1
            rejected_in_a_row = 0
            vectors.extend(self._vectors(response, len(chunk)))
            position += len(chunk)
        return vectors

    def _vectors(self, response: Mapping[str, Any], expected: int) -> list[array.array]:
        predictions = response.get("predictions")
        if not isinstance(predictions, list) or len(predictions) != expected:
            raise RemoteAPIError(f"Embedding response did not carry {expected} prediction(s)")
        vectors: list[array.array] = []
        for prediction in predictions:
            embeddings = prediction.get("embeddings") if isinstance(prediction, dict) else None
            raw_values = embeddings.get("values") if isinstance(embeddings, dict) else None
            try:
                values = [float(value) for value in raw_values]
            except (TypeError, ValueError) as exc:
                raise RemoteAPIError("Embedding response carried no numeric vector") from exc
            norm = math.sqrt(sum(value * value for value in values))
            if len(values) != EMBEDDING_DIMENSIONS or not norm:
                raise RemoteAPIError(
                    f"Embedding response vector is not a non-zero {EMBEDDING_DIMENSIONS}-dimensional vector"
                )
            statistics = embeddings.get("statistics")
            if isinstance(statistics, dict):
                with contextlib.suppress(TypeError, ValueError):
                    self.tokens += int(round(float(statistics.get("token_count") or 0)))
            vectors.append(array.array("f", (value / norm for value in values)))
        return vectors


def requested_concepts(candidates: Mapping[str, Any]) -> dict[str, float]:
    """The competencies and occupations an enriched text requests.

    Keys are 'comp:<label>' and 'occ:<label>', values the highest probability
    seen that the employer requires them; terms merely mentioned stay out.
    """
    concepts: dict[str, float] = {}
    for kind, prefix in (("competencies", "comp"), ("occupations", "occ")):
        for candidate in candidates.get(kind) or []:
            if not isinstance(candidate, dict):
                continue
            try:
                prediction = float(candidate.get("prediction") or 0.0)
            except (TypeError, ValueError):
                continue
            concept = str(candidate.get("concept_label") or "").strip().lower()
            if concept and prediction >= ENRICHMENT_PREDICTION_FLOOR:
                key = f"{prefix}:{concept}"
                concepts[key] = max(concepts.get(key, 0.0), prediction)
    return concepts


def enrichment_score(ad_concepts: Mapping[str, float], profile: Mapping[str, float]) -> float:
    """Requested concepts the candidate's profile also names, weighted by how
    surely the employer requires them. An occupation counts double."""
    return float(sum(
        prediction * (2.0 if key.startswith("occ:") else 1.0)
        for key, prediction in ad_concepts.items()
        if key in profile
    ))


class EnrichmentClient:
    """Arbetsförmedlingen's JobAd Enrichments API, ten texts per request."""

    def __init__(self, http: HttpClient, *, timeout: int = 120) -> None:
        self.http = http
        self.timeout = timeout
        self.calls = 0

    def enrich(self, documents: Sequence[tuple[str, str, str]]) -> dict[str, dict[str, float]]:
        """doc id -> requested concepts, for (doc id, headline, text) documents.

        A document the API rejects on its own is left out. Any other failure
        raises RemoteAPIError, after the HTTP client's own retries.
        """
        found: dict[str, dict[str, float]] = {}
        for start in range(0, len(documents), ENRICHMENT_BATCH):
            batch = list(documents[start:start + ENRICHMENT_BATCH])
            try:
                self._post(batch, found)
            except HttpStatusError as exc:
                if exc.status != 400:
                    raise
                for single in batch:
                    with contextlib.suppress(HttpStatusError):
                        self._post([single], found)
        return found

    def _post(self, batch: Sequence[tuple[str, str, str]], found: dict[str, dict[str, float]]) -> None:
        response = self.http.json_request(
            "POST",
            ENRICHMENT_URL,
            payload={
                "documents_input": [
                    {"doc_id": doc_id, "doc_headline": headline, "doc_text": text[:9000]}
                    for doc_id, headline, text in batch
                ],
                "include_terms_info": True,
                "include_sentences": False,
                "sort_by_prediction_score": "DESC",
            },
            timeout=self.timeout,
            expect=list,
        )
        self.calls += 1
        for document in response:
            if isinstance(document, dict) and document.get("doc_id") is not None:
                candidates = document.get("enriched_candidates")
                found[str(document["doc_id"])] = requested_concepts(
                    candidates if isinstance(candidates, dict) else {})


def profile_concepts(db: Database, context: RankingContext, enricher: Any) -> dict[str, float]:
    """The concepts the candidate's own profile names, enriched once per profile."""
    key = "profile_concepts:" + hashlib.sha256(
        json.dumps([ENRICHMENT_VERSION, context.facets], ensure_ascii=False).encode("utf-8")
    ).hexdigest()[:16]
    cached = db.get_meta(key)
    if cached:
        with contextlib.suppress(json.JSONDecodeError):
            concepts = json.loads(cached)
            if isinstance(concepts, dict) and concepts:
                return concepts
    found = enricher.enrich([(f"facet{n}", name, text) for n, (name, text) in enumerate(context.facets)])
    concepts = {}
    for document in found.values():
        for concept, prediction in document.items():
            concepts[concept] = max(concepts.get(concept, 0.0), prediction)
    if not concepts:
        raise RemoteAPIError("Enriching the profile named no concepts")
    db.set_meta(key, json.dumps(concepts, ensure_ascii=False, sort_keys=True))
    return concepts


def dot(a: Sequence[float], b: Sequence[float]) -> float:
    return sum(map(operator.mul, a, b))


def fractional_ranks(values: Sequence[float]) -> list[float]:
    """1-based ranks, highest value first, with tied values sharing their average rank.

    Averaging keeps ties neutral. Breaking them by storage order would favour
    whichever ads happened to be stored first, and giving a tied block its best
    position would select all of it at once.
    """
    order = sorted(range(len(values)), key=values.__getitem__, reverse=True)
    ranks = [0.0] * len(values)
    start = 0
    while start < len(order):
        end = start
        while end + 1 < len(order) and values[order[end + 1]] == values[order[start]]:
            end += 1
        shared = (start + end) / 2 + 1
        for index in order[start:end + 1]:
            ranks[index] = shared
        start = end + 1
    return ranks


def selection_percentiles(
    pool: Sequence[tuple[Any, ...]],
) -> dict[int, tuple[float, bool]]:
    """job id -> (share of the pool at or above it, ranked with embeddings).

    Each entry is (job id, vocabulary score, embedding score or None, and
    optionally an enrichment score or None). Ads with all three scores are
    ordered by reciprocal rank fusion of the three orders, among the ads that
    have all three. Ads without an enrichment score fuse vocabulary and
    embeddings among every ad with an embedding, and ads ranked while
    embeddings were unavailable are ordered by vocabulary alone, among every ad
    in the pool.
    """
    def fuse(rows: Sequence[tuple[Any, ...]], *columns: int) -> list[float]:
        ranks = [fractional_ranks([float(row[column]) for row in rows]) for column in columns]
        fused = [sum(1.0 / (RRF_K + order[n]) for order in ranks) for n in range(len(rows))]
        return [rank / len(rows) for rank in fractional_ranks(fused)]

    result: dict[int, tuple[float, bool]] = {}
    embedded = [row for row in pool if row[2] is not None]
    enriched = [row for row in embedded if len(row) > 3 and row[3] is not None]
    if embedded:
        for row, percentile in zip(embedded, fuse(embedded, 1, 2)):
            result[row[0]] = (percentile, True)
    if enriched:
        for row, percentile in zip(enriched, fuse(enriched, 1, 2, 3)):
            result[row[0]] = (percentile, True)
    if len(embedded) < len(pool):
        for row, percentile in zip(pool, fuse(pool, 1)):
            if row[2] is None:
                result[row[0]] = (percentile, False)
    return result


def embedding_failure_reason(error: BaseException) -> str:
    """A short, bounded label for why a ranking service failed, for the run record and the alert."""
    if isinstance(error, RateLimitError):
        return "http_429"
    status = getattr(error, "status", None)
    return f"http_{status}" if isinstance(status, int) else type(error).__name__


def explored(source_job_id: Any, content_hash: Any, share: float) -> bool:
    """Whether an ad the cut left out is judged anyway, as part of the random check.

    Decided by a hash of the ad and its content, so an ad gets the same answer on
    every re-rank: the sample is random across ads, never re-rolled until every
    ad has been read.
    """
    if share <= 0:
        return False
    digest = hashlib.sha256(f"explore:{source_job_id}:{content_hash}".encode("utf-8")).hexdigest()
    return int(digest[:12], 16) / float(16 ** 12) < share


def rank_new_jobs(
    db: Database,
    settings: Settings,
    stats: RunStats,
    *,
    context: RankingContext,
    client: Any,
    enricher: Any = None,
    now: dt.datetime | None = None,
) -> None:
    """Score every unranked ad in the window, then select the top share.

    An embedding failure never stops a run. The affected ads rank on the
    vocabulary alone against the wider degraded share, and those it leaves out
    are ranked again once embeddings answer.
    """
    current = now or dt.datetime.now(UTC)
    since = (current - dt.timedelta(days=settings.ranking_reference_days)).isoformat(timespec="microseconds")
    rows = db.unranked_jobs(context.ranking_key, since=since, limit=settings.max_rank_per_run)
    if not rows:
        return

    texts = [ranking_text(row["title"], row["description"]) for row in rows]
    embedding_scores: list[float | None] = [None] * len(rows)
    calls_before, tokens_before, pauses_before = client.calls, client.tokens, client.quota_pauses
    try:
        profile_vectors = db.get_profile_embedding(context.embedding_key)
        if profile_vectors is None:
            embedded = client.embed([text for _, text in context.facets], "RETRIEVAL_QUERY")
            if any(vector is None for vector in embedded):
                raise RemoteAPIError("The embedding model rejected a profile section")
            profile_vectors = [vector for vector in embedded if vector is not None]
            db.save_profile_embedding(
                context.embedding_key, context.embedding_model,
                [name for name, _ in context.facets], profile_vectors,
            )
        for start in range(0, len(texts), EMBEDDING_CHUNK):
            vectors = client.embed(texts[start:start + EMBEDDING_CHUNK], "RETRIEVAL_DOCUMENT")
            for offset, vector in enumerate(vectors):
                if vector is not None:
                    embedding_scores[start + offset] = max(dot(vector, facet) for facet in profile_vectors)
    except RemoteAPIError as exc:
        stats.embedding_failure = embedding_failure_reason(exc)
        LOG.warning("Embeddings unavailable; ranking on the vocabulary alone: %s", compact_sentence(exc, 300))
    finally:
        stats.embedding_calls += client.calls - calls_before
        stats.embedding_tokens += client.tokens - tokens_before
        stats.embedding_quota_pauses += client.quota_pauses - pauses_before
    if any(score is None for score in embedding_scores):
        stats.ranking_degraded = True

    # Enrichment is a third order, never a gate: without it the run ranks on
    # vocabulary and embeddings alone.
    enrichment_scores: list[float | None] = [None] * len(rows)
    enrichment_concepts: list[dict[str, float] | None] = [None] * len(rows)
    if context.enrichment and enricher is not None:
        try:
            wanted = profile_concepts(db, context, enricher)
            found = enricher.enrich([
                (str(row["source_job_id"]), str(row["title"]), str(row["description"])) for row in rows
            ])
            for n, row in enumerate(rows):
                concepts = found.get(str(row["source_job_id"]))
                if concepts is not None:
                    enrichment_concepts[n] = concepts
                    enrichment_scores[n] = enrichment_score(concepts, wanted)
        except RemoteAPIError as exc:
            stats.enrichment_failure = embedding_failure_reason(exc)
            LOG.warning("Enrichment unavailable; ranking without it this run: %s", compact_sentence(exc, 300))
        stats.jobs_enriched += sum(1 for score in enrichment_scores if score is not None)

    vocabulary_scores = [context.vocabulary.score(text) for text in texts]
    db.save_rank_scores(context.ranking_key, [
        (
            int(row["id"]), str(row["content_hash"]), vocabulary, embedding, enrichment,
            None if concepts is None else json.dumps(concepts, ensure_ascii=False, sort_keys=True),
        )
        for row, vocabulary, embedding, enrichment, concepts
        in zip(rows, vocabulary_scores, embedding_scores, enrichment_scores, enrichment_concepts)
    ])

    pool = [
        (
            int(row["id"]),
            float(row["vocabulary_score"]),
            None if row["embedding_score"] is None else float(row["embedding_score"]),
            None if row["enrichment_score"] is None else float(row["enrichment_score"]),
        )
        for row in db.ranking_reference(context.ranking_key, since=since)
    ]
    percentiles = selection_percentiles(pool)
    fail_open = len(pool) < RANKING_MIN_POOL
    decisions: list[tuple[int, float, str, str | None]] = []
    for row in rows:
        percentile, with_embeddings = percentiles[int(row["id"])]
        share = settings.evaluate_top_share if with_embeddings else settings.degraded_top_share
        reason = None
        if fail_open:
            reason = "fail_open"
        elif percentile <= share:
            reason = "rank"
        elif explored(row["source_job_id"], row["content_hash"], settings.explore_share):
            reason = "explore"
        decisions.append((int(row["id"]), percentile, "selected" if reason else "not_selected", reason))
    db.save_selection(decisions)

    selected_now = sum(1 for _, _, state, _ in decisions if state == "selected")
    explored_now = sum(1 for _, _, _, reason in decisions if reason == "explore")
    stats.jobs_ranked += len(rows)
    stats.jobs_selected += selected_now
    stats.jobs_explored += explored_now
    stats.ranking_pool = len(pool)
    if fail_open:
        stats.ranking_fail_open = True
        LOG.warning(
            "Only %d ranked ad(s) in the %d-day window; selecting all %d instead of trusting a percentile",
            len(pool), settings.ranking_reference_days, len(rows),
        )
    LOG.info(
        "Ranking: %d ad(s) ranked against %d in the window, %d selected (%d by the random check)%s; "
        "%d embedding call(s), %d quota pause(s), %d token(s), about $%.4f",
        len(rows), len(pool), selected_now, explored_now,
        " - DEGRADED, vocabulary only" if stats.ranking_degraded else "",
        stats.embedding_calls, stats.embedding_quota_pauses, stats.embedding_tokens,
        stats.embedding_tokens / 1_000_000 * EMBEDDING_USD_PER_MILLION_TOKENS.get(settings.embedding_model, 0.0),
    )


def format_notifications(rows: Sequence[sqlite3.Row]) -> str:
    if not rows:
        return ""
    # No header: the cards speak for themselves and the run summary that
    # follows already states the totals.
    lines: list[str] = []
    for index, row in enumerate(rows, start=1):
        decision_icon = {
            "notify_strong": "🟢",
            "notify_good": "🟢",
            "notify_stretch": "🟡",
            "notify_verify": "🟡",
        }.get(row["decision"], "•")
        location = ", ".join(x for x in (row["municipality"], row["region"]) if x) or "Location unspecified"
        if row["remote"]:
            location += " · remote/hybrid indicated"
        why = json.loads(row["why_fit_json"])
        gaps = json.loads(row["gaps_json"])
        blockers = json.loads(row["blockers_json"])

        lines.append(
            f"{decision_icon} {index}. {row['title']} · {row['company']}\n"
            f"Opportunity {row['opportunity_score']}% · Career fit {row['career_fit']}%\n"
            f"📍 {location}\n"
            f"What it really is: {compact_sentence(row['actual_role'], 230)}"
        )
        if why:
            lines.append("Why: " + " · ".join(compact_sentence(x, 130) for x in why[:3]))
        if gaps:
            lines.append("Gap: " + " · ".join(compact_sentence(x, 120) for x in gaps[:2]))
        if blockers:
            rendered = [
                f"{blocker.get('type', '?')}: {compact_sentence(blocker.get('reason', ''), 120)}"
                for blocker in blockers[:2]
            ]
            lines.append("⚠️ " + " · ".join(rendered))
        if row["application_deadline"]:
            lines.append(f"Deadline: {row['application_deadline']}")
        lines.append(f"🔗 {row['url']}\n")
    return "\n".join(lines).strip()


def compact_sentence(value: Any, limit: int) -> str:
    text = clean_text(value).replace("\n", " ")
    if len(text) <= limit:
        return text
    return text[: max(1, limit - 1)].rstrip() + "…"


# A profile file still carrying these has been installed from a template and
# not yet personalised. Cheap, deterministic, and no provider call.
TEMPLATE_MARKERS: tuple[str, ...] = (
    "THIS IS A SYNTHETIC EXAMPLE",
    "SYNTHETIC EXAMPLE CANDIDATE",
    "(fictional)",
    "profile_status\": \"example",
)
REQUIRED_PROFILE_FILES: tuple[str, ...] = (
    "career_profile.json", "matcher_profile.json", "search_lenses.json",
    "matcher_rules_v1_1.json", "role_vocabulary.json",
)
PERSONAL_PROFILE_FILES: tuple[str, ...] = (
    "career_profile.json", "matcher_profile.json", "search_lenses.json",
    "role_vocabulary.json", KNOWLEDGE_CATALOGUE_FILE,
)


def unedited_templates(settings: Settings) -> list[str]:
    """Profile files that still look like the shipped examples."""
    still_template: list[str] = []
    for name in PERSONAL_PROFILE_FILES:
        path = settings.profile_dir / name
        try:
            text = path.read_text(encoding="utf-8")
        except OSError:
            continue
        if any(marker in text for marker in TEMPLATE_MARKERS):
            still_template.append(name)
    return still_template


def doctor(settings: Settings, *, require_key: bool = True) -> dict[str, Any]:
    """Validate local configuration, profile and credentials. No network, no cost."""
    settings.home.mkdir(parents=True, exist_ok=True)
    settings.db_path.parent.mkdir(parents=True, exist_ok=True)
    missing = [str(settings.profile_dir / name) for name in REQUIRED_PROFILE_FILES
               if not (settings.profile_dir / name).exists()]
    if missing:
        raise ConfigurationError(
            "Missing profile files: " + ", ".join(missing)
            + ". Run install.sh to create them from the shipped examples."
        )
    if settings.primary_provider != PRIMARY_PROVIDER or settings.fallback_provider != FALLBACK_PROVIDER:
        raise ConfigurationError(
            f"Production routing is fixed to {PRIMARY_PROVIDER} with {FALLBACK_PROVIDER} fallback"
        )

    matcher_profile, matcher_rules, version = load_profile_bundle(settings)
    ranking = RankingContext.load(settings)
    secrets = load_secrets(settings.secrets_path)
    required_keys = (
        "VERTEX_GEMINI_API_KEY",
        "VERTEX_GEMINI_MODEL",
        "AZURE_OPENAI_API_KEY",
        "AZURE_OPENAI_BASE_URL",
        "AZURE_OPENAI_DEPLOYMENT",
    )
    missing_keys = [key for key in required_keys if not secrets.get(key)]
    templates = unedited_templates(settings)
    if require_key and missing_keys:
        hint = ""
        if templates:
            hint = " Also still unedited: " + ", ".join(templates) + " in " + str(settings.profile_dir) + "."
        raise ConfigurationError(
            f"Missing provider configuration in {settings.secrets_path}: {', '.join(missing_keys)}.{hint}"
        )
    swedish = candidate_language_level(matcher_profile, "swedish")
    todo: list[str] = []
    if templates:
        todo.append(
            "Personalise " + ", ".join(templates) + " in " + str(settings.profile_dir)
            + " - they still contain the shipped example candidate."
        )
    if missing_keys:
        todo.append("Add " + ", ".join(missing_keys) + " to " + str(settings.secrets_path) + " (chmod 600).")
    if not swedish.known:
        todo.append(
            "Set constraints.swedish in matcher_profile.json - the Swedish level is "
            "unspecified, so language requirements stay UNKNOWN rather than judged."
        )

    return {
        "version": APP_VERSION,
        "home": str(settings.home),
        "database": str(settings.db_path),
        "primary_provider": settings.primary_provider,
        "fallback_provider": settings.fallback_provider,
        "profile_version": version,
        "discovery": {
            "jobstream": settings.use_jobstream,
            "jobstream_lookback_hours": settings.jobstream_lookback_hours,
            "jobsearch_queries": len(build_queries(settings)),
            "career_sites": {site.name: site.platform for site in settings.career_sites},
        },
        "provider_credentials_present": not missing_keys,
        "missing_credentials": missing_keys,
        "profile_keys": len(matcher_profile),
        "rules_keys": len(matcher_rules),
        "unedited_example_profiles": templates,
        "candidate_swedish_level": swedish.label,
        "max_candidates_per_run": settings.max_candidates_per_run,
        "max_jobs_per_batch": settings.max_jobs_per_batch,
        "monthly_budget_usd": settings.monthly_budget_usd,
        "knowledge_catalogue": "knowledge_catalogue" in matcher_profile,
        "catalogue_experience_years": catalogue_experience_years(matcher_profile.get("knowledge_catalogue")),
        "exclude_student_roles": settings.exclude_student_roles,
        "first_read": {
            "model": settings.triage_model or "off, the judge reads every ad",
            "thinking_budget": settings.triage_thinking_budget,
            "ads_per_call": settings.triage_batch_size,
            "settles_rejections_below_fit": TRIAGE_SETTLE_BELOW_FIT,
        },
        "ranking": {
            "method": "role vocabulary + profile embeddings" + (" + JobTech enrichment" if settings.use_enrichment else "")
                      + ", reciprocal rank fusion",
            "embedding_model": settings.embedding_model,
            "vocabulary_terms": len(ranking.vocabulary.terms),
            "profile_sections": len(ranking.facets),
            "evaluate_top_share": settings.evaluate_top_share,
            "degraded_top_share": settings.degraded_top_share,
            "explore_share": settings.explore_share,
            "enrichment": settings.use_enrichment,
            "reference_days": settings.ranking_reference_days,
        },
        "ready": not missing_keys and not templates,
        "next_steps": todo,
    }


def add_provider_usage(stats: RunStats, usage: Mapping[str, int]) -> None:
    prompt = int(usage.get("prompt_tokens") or 0)
    completion = int(usage.get("completion_tokens") or 0)
    stats.prompt_tokens += prompt
    stats.completion_tokens += completion
    stats.reasoning_tokens += int(usage.get("reasoning_tokens") or 0)
    stats.total_tokens += int(usage.get("total_tokens") or (prompt + completion))


def estimated_run_cost_usd(stats: Mapping[str, Any]) -> float:
    """Provider spend of one run, from the token counts it recorded.

    Judge tokens are priced at the dearer provider's rates, and reasoning is
    added to output even where a provider already counts it there, so the
    estimate errs high rather than low.
    """
    def tokens(key: str) -> int:
        value = stats.get(key)
        return value if isinstance(value, int) and not isinstance(value, bool) and value > 0 else 0

    judge = (
        tokens("prompt_tokens") * JUDGE_USD_PER_MILLION_TOKENS[0]
        + (tokens("completion_tokens") + tokens("reasoning_tokens")) * JUDGE_USD_PER_MILLION_TOKENS[1]
    )
    first_read = (
        tokens("triage_prompt_tokens") * TRIAGE_USD_PER_MILLION_TOKENS[0]
        + (tokens("triage_completion_tokens") + tokens("triage_reasoning_tokens")) * TRIAGE_USD_PER_MILLION_TOKENS[1]
    )
    embedding = tokens("embedding_tokens") * max(EMBEDDING_USD_PER_MILLION_TOKENS.values())
    return (judge + first_read + embedding) / 1_000_000


def format_run_alerts(stats: RunStats) -> list[str]:
    """Problems worth a delivered line even though the run carried on."""
    alerts: list[str] = []
    if stats.discovery_failures:
        shown = stats.discovery_failures[:6]
        more = len(stats.discovery_failures) - len(shown)
        alerts.append(
            "⚠️ RoleLens: discovery failed for " + ", ".join(shown)
            + (f" and {more} more" if more else "") + "; the other sources were still read."
        )
    refusals = sorted({
        key.split(":", 1)[-1] for key in stats.fallback_reasons
        if key.split(":", 1)[-1].startswith("access_")
    })
    if refusals:
        alerts.append(
            f"⚠️ RoleLens: Vertex Gemini refused access ({', '.join(refusals)}), so the Azure "
            "fallback was tried. Check the Vertex key, billing or credit."
        )
    if stats.embedding_failure:
        alerts.append(
            f"⚠️ RoleLens: embeddings unavailable ({stats.embedding_failure}); "
            "jobs were ranked on the vocabulary alone this run."
        )
    if stats.budget_paused:
        alerts.append(
            f"⚠️ RoleLens: monthly budget reached (${stats.month_to_date_usd:.2f} of "
            f"${stats.budget_usd:.2f}); judging paused with {stats.budget_waiting} jobs waiting. "
            "Raise monthly_budget_usd or wait for the new month."
        )
    if stats.triage_failure:
        alerts.append(
            f"⚠️ RoleLens: the first-read model failed ({stats.triage_failure}), so the judge read the rest "
            "of this run's ads at full price. Check triage_model in config.json."
        )
    return alerts


def format_run_summary(
    evaluated: int,
    matches: int,
    queued: int = 0,
    *,
    failed: bool = False,
    ceiling_reached: bool = False,
    selected: int = 0,
) -> str:
    """One compact stats line for a run with matches or a problem.

    A run with neither returns "", so the scheduled job prints nothing and a
    scheduler that delivers stdout sends nothing. `evaluated` counts jobs the
    semantic matcher returned a usable result for, never discovered ads.
    `queued` counts frozen snapshot jobs that ended the run without one.
    """
    if not failed and not ceiling_reached and matches == 0:
        return ""
    noun = "match" if matches == 1 else "matches"
    if failed:
        return (
            f"⚠️ RoleLens: {evaluated} jobs checked · {matches} {noun} "
            f"· {queued} pending after provider error."
        )
    if ceiling_reached:
        # The snapshot was truncated by the emergency ceiling, so this run is not a
        # complete picture of the market and must never be reported as one.
        return (
            f"⚠️ RoleLens: candidate safety ceiling reached, {selected} selected "
            f"· {matches} {noun} · additional jobs remain queued."
        )
    if queued:
        return (
            f"\U0001f3af RoleLens: {evaluated} jobs checked · {matches} {noun} "
            f"· {queued} queued for next run."
        )
    return f"\U0001f3af RoleLens: {evaluated} jobs checked · {matches} {noun}."


def determine_run_status(partial_reasons: Sequence[str]) -> str:
    return "partial" if partial_reasons else "success"


def run_pipeline(settings: Settings, *, fetch_only: bool, evaluate_only: bool) -> int:
    info = doctor(settings, require_key=not fetch_only)
    LOG.info(
        "RoleLens %s, profile %s, primary=%s fallback=%s",
        APP_VERSION, info["profile_version"], settings.primary_provider, settings.fallback_provider,
    )
    http = HttpClient(retries=settings.http_retries, user_agent=f"{APP_NAME}/{APP_VERSION} (+{PROJECT_URL})")
    db = Database(settings.db_path)
    stats = RunStats()
    run_id = db.start_run()
    partial_reasons: list[str] = []
    unresolved_count = 0
    try:
        if not evaluate_only:
            discover(http, db, settings, stats)

        ceiling_reached = False
        snapshot_selected = 0
        if not fetch_only:
            matcher_profile, matcher_rules, version = load_profile_bundle(settings)
            secrets = load_secrets(settings.secrets_path)
            # Rank before the snapshot is frozen, so every stored ad - from this
            # run or an earlier `fetch` - competes for the evaluator.
            rank_new_jobs(
                db, settings, stats,
                context=RankingContext.load(settings),
                client=EmbeddingClient(settings, secrets, http),
                enricher=EnrichmentClient(http),
            )

            # Judging stops for the month once the estimated spend reaches the
            # budget. Ranked jobs stay queued and stored matches are still
            # delivered; ranking itself keeps running for a few cents a day.
            stats.budget_usd = settings.monthly_budget_usd
            stats.month_to_date_usd = round(db.month_to_date_cost_usd(), 4)
            stats.budget_paused = stats.month_to_date_usd >= settings.monthly_budget_usd

            # Freeze the candidate set for this run. It is taken once and never
            # re-queried, so a job discovered mid-run belongs to the next run and
            # this run stays auditable.
            ceiling = settings.max_candidates_per_run
            candidates = [] if stats.budget_paused else db.pending_jobs(version, ceiling)
            eligible_total = db.pending_count(version, respect_live_mode=True)
            if stats.budget_paused:
                stats.budget_waiting = eligible_total
                LOG.warning(
                    "Monthly budget reached: about $%.2f of $%.2f spent; %d job(s) wait unjudged",
                    stats.month_to_date_usd, settings.monthly_budget_usd, eligible_total,
                )
            # The ceiling is emergency protection, never a throughput limit. When it
            # bites, the run is explicitly incomplete rather than quietly truncated.
            ceiling_reached = len(candidates) >= ceiling and eligible_total > ceiling
            if ceiling_reached:
                LOG.warning(
                    "Candidate safety ceiling reached: %d of %d eligible job(s) selected; "
                    "the remainder stays queued",
                    len(candidates), eligible_total,
                )
            snapshot, duplicate_candidates = suppress_duplicate_candidates(db, candidates)
            stats.duplicates_suppressed = len(duplicate_candidates)
            stats.snapshot_size = len(snapshot)
            for row, canonical in duplicate_candidates:
                LOG.info(
                    "Repost suppressed before evaluation: %s is a repost of job %d",
                    row["source_job_id"], canonical,
                )

            snapshot = screen_before_judging(db, snapshot, settings, matcher_profile, version, stats)
            if stats.rules_screened:
                LOG.info("Settled without a model: %d blocked by the deterministic rules", stats.rules_screened)

            batches = list(
                iter_batches(
                    snapshot,
                    max_jobs=settings.max_jobs_per_batch,
                    max_chars=settings.max_prompt_chars,
                    max_job_description_chars=settings.max_job_description_chars,
                )
            )
            stats.pending_selected = len(snapshot)
            snapshot_selected = len(candidates)
            LOG.info(
                "Frozen snapshot: %d candidate(s) in %d batch(es); %d repost(s) suppressed",
                len(snapshot), len(batches), stats.duplicates_suppressed,
            )

            if batches:
                primary = ProviderMatcher(
                    settings, secrets, matcher_profile, matcher_rules, settings.primary_provider
                )
                fallback = ProviderMatcher(
                    settings, secrets, matcher_profile, matcher_rules, settings.fallback_provider
                )
                deadline = time.monotonic() + settings.max_run_seconds
                reserve = settings.gateway_timeout_seconds

                # The first read settles the clear rejections; the judge reads the rest.
                triage_deferred: list[sqlite3.Row] = []
                if settings.triage_model:
                    to_judge, triage_deferred = run_triage_pass(
                        db, TriageClient(settings, secrets, matcher_profile), snapshot,
                        profile_version=version, stats=stats,
                        deadline=deadline, reserve_seconds=reserve,
                    )
                    batches = list(
                        iter_batches(
                            to_judge,
                            max_jobs=settings.max_jobs_per_batch,
                            max_chars=settings.max_prompt_chars,
                            max_job_description_chars=settings.max_job_description_chars,
                        )
                    )
                    LOG.info(
                        "First read: %d settled, %d to the judge in %d batch(es), %d deferred",
                        stats.triage_settled, len(to_judge), len(batches), len(triage_deferred),
                    )

                outcome = run_batch_pass(
                    db, primary, fallback, batches,
                    profile_version=version, stats=stats,
                    deadline=deadline, reserve_seconds=reserve,
                )
                outcome.deferred.extend(triage_deferred)

                # Completeness matters more than punctuality, so unresolved IDs get
                # exactly one more attempt after the normal pass. Never recursive.
                if outcome.unresolved and outcome.provider_error is None:
                    cleanup_batches = list(
                        iter_batches(
                            outcome.unresolved,
                            max_jobs=settings.max_jobs_per_batch,
                            max_chars=settings.max_prompt_chars,
                            max_job_description_chars=settings.max_job_description_chars,
                        )
                    )
                    LOG.info(
                        "Cleanup pass: retrying %d unresolved ID(s) in %d batch(es)",
                        len(outcome.unresolved), len(cleanup_batches),
                    )
                    cleanup = run_batch_pass(
                        db, primary, fallback, cleanup_batches,
                        profile_version=version, stats=stats,
                        deadline=deadline, reserve_seconds=reserve, cleanup=True,
                    )
                    outcome.unresolved = cleanup.unresolved + cleanup.deferred
                    if cleanup.provider_error:
                        outcome.provider_error = cleanup.provider_error
                elif outcome.unresolved:
                    LOG.info("Skipping cleanup pass: the provider already failed this run")

                queued_rows = outcome.unresolved + outcome.deferred
                stats.unresolved = len(outcome.unresolved)
                stats.deferred = len(outcome.deferred)
                unresolved_count = len(queued_rows)
                if outcome.provider_error:
                    partial_reasons.append(outcome.provider_error)
                for row in queued_rows:
                    LOG.info("Still pending after this run: %s", row["source_job_id"])

            remaining_live = db.pending_count(version, respect_live_mode=True)
            notification_rows = db.unnotified(settings.max_notifications_per_run)
            notification_rows, duplicate_cards = deduplicate_notifications(notification_rows)
            for row, canonical in duplicate_cards:
                # Evaluated before suppression existed: retire the card quietly and
                # record why, so one vacancy never produces two delivered entries.
                db.record_fingerprint(
                    int(row["job_id"]),
                    row_fingerprint(row),
                    canonical,
                    f"Duplicate notification of job {canonical}: same employer, title, location and body.",
                )
                stats.duplicates_suppressed += 1
                LOG.info("Duplicate card suppressed for job %d (canonical %d)", int(row["job_id"]), canonical)

            message = format_notifications(notification_rows)
            if message:
                ids = [int(row["evaluation_id"]) for row in notification_rows]
                ids += [int(row["evaluation_id"]) for row, _ in duplicate_cards]
                db.mark_notifications_emitted(ids)
                stats.notified = len(notification_rows)
                print(message, flush=True)
            elif duplicate_cards:
                db.mark_notifications_emitted([int(row["evaluation_id"]) for row, _ in duplicate_cards])

            for alert in format_run_alerts(stats):
                print(alert, flush=True)
            # One compact stats line after any detailed matches, and only when the
            # run has matches or a problem: a quiet run prints nothing at all.
            summary = format_run_summary(
                stats.evaluated,
                stats.notified,
                # Frozen-snapshot jobs that ended the run without a result.
                max(0, stats.pending_selected - stats.evaluated),
                failed=bool(partial_reasons),
                ceiling_reached=ceiling_reached,
                selected=snapshot_selected,
            )
            if summary:
                print(summary, flush=True)
            LOG.info("Live pending after this run: %d", remaining_live)

        run_status = determine_run_status(partial_reasons)
        error = "; ".join(partial_reasons) if partial_reasons else None
        db.finish_run(run_id, run_status, stats, compact_sentence(error, 1000) if error else None)
        LOG.info(
            "Run complete: status=%s batches=%d evaluated=%d notified=%d "
            "unresolved=%d reposts_suppressed=%d tokens=%d total fallback=%d",
            run_status,
            stats.batches_processed,
            stats.evaluated,
            stats.notified,
            unresolved_count,
            stats.duplicates_suppressed,
            stats.total_tokens,
            stats.fallback_calls,
        )
        # Partial provider/output failures are recoverable: SQLite retains unresolved
        # jobs for a later scheduled run, so the scheduler should not raise a failed-task alert.
        return 0
    except BaseException as exc:
        with contextlib.suppress(Exception):
            db.finish_run(run_id, "error", stats, compact_sentence(exc, 1000))
        raise
    finally:
        db.close()


BACKFILL_REPORT_MAX_CHARS = 3500


def select_historical_candidates(
    db: "Database",
    settings: Settings,
    profile_version: str,
    *,
    today: dt.date,
) -> tuple[list[sqlite3.Row], dict[str, int]]:
    """Freeze one historical recovery snapshot, with exclusions counted."""
    rows = db.historical_pending(profile_version, settings.max_candidates_per_run, today=today)
    excluded: dict[str, int] = {}
    kept: list[sqlite3.Row] = []
    for row in rows:
        reason = prefilter_reason(row, settings, today=today)
        if reason:
            excluded[reason] = excluded.get(reason, 0) + 1
            continue
        kept.append(row)
    return kept, excluded


def summarize_deadlines(rows: Sequence[sqlite3.Row], limit: int = 20) -> list[str]:
    out: list[str] = []
    for row in rows[:limit]:
        deadline = clean_text(row["application_deadline"])[:10] or "no deadline"
        out.append(f"{deadline}  score {int(row['discovery_score']):>3}  {clean_text(row['title'])[:52]}")
    return out


def run_backfill(settings: Settings, *, dry_run: bool, limit: int | None = None) -> int:
    """Evaluate historical jobs the live selector will never reach.

    Deliberately has no discovery step and no delivery step. It writes
    evaluations and nothing else: notification state belongs to
    `backfill-report`, so running this by hand can never consume a match that
    was never actually delivered anywhere.
    """
    info = doctor(settings, require_key=not dry_run)
    today = market_today()
    db = Database(settings.db_path)
    stats = RunStats(mode="backfill")
    run_id: int | None = None
    try:
        version = info["profile_version"]
        if not db.get_meta("live_since"):
            print("Historical recovery needs a frozen backlog; this database is still in bootstrap mode.")
            return 0

        ceiling = settings.max_candidates_per_run if limit is None else max(1, limit)
        settings = dataclasses.replace(settings, max_candidates_per_run=ceiling)
        candidates, excluded = select_historical_candidates(db, settings, version, today=today)
        snapshot, duplicates = suppress_duplicate_candidates(db, candidates)
        stats.duplicates_suppressed = len(duplicates)
        stats.snapshot_size = len(snapshot)
        counts = db.historical_counts(version, today=today)
        batches = list(
            iter_batches(
                snapshot,
                max_jobs=settings.max_jobs_per_batch,
                max_chars=settings.max_prompt_chars,
                max_job_description_chars=settings.max_job_description_chars,
            )
        )

        if dry_run:
            remaining = counts.get("historical_still_open_pending", 0)
            per_run = max(1, ceiling)
            print("Historical backfill, DRY RUN. No provider calls, no writes.")
            print(f"  market date (Europe/Stockholm) : {today.isoformat()}")
            print(f"  live cutoff                    : {db.get_meta('live_since')}")
            print(f"  selected this run              : {len(snapshot)} (ceiling {ceiling})")
            print(f"  excluded by prefilter          : {excluded or 'none'}")
            print(f"  reposts suppressed             : {len(duplicates)}")
            print(f"  batches at {settings.max_jobs_per_batch}/call            : {len(batches)}")
            print(f"  provider calls this run        : {len(batches)} (+ at most 1 cleanup pass)")
            print(f"  still-open historical backlog  : {remaining}")
            print(f"  estimated runs to drain        : {(remaining + per_run - 1) // per_run}")
            print(f"  expired, never selected        : {counts.get('historical_expired_unevaluated', 0)}")
            if snapshot:
                print("  first selected, by urgency:")
                for line in summarize_deadlines(snapshot):
                    print(f"    {line}")
            return 0

        if not batches:
            print("Historical backfill: nothing left to evaluate.")
            return 0

        # From here the run spends money, so it is recorded like any other run.
        run_id = db.start_run()
        secrets = load_secrets(settings.secrets_path)
        matcher_profile, matcher_rules, _ = load_profile_bundle(settings)
        primary = ProviderMatcher(settings, secrets, matcher_profile, matcher_rules, settings.primary_provider)
        fallback = ProviderMatcher(settings, secrets, matcher_profile, matcher_rules, settings.fallback_provider)
        deadline_at = time.monotonic() + settings.max_run_seconds
        reserve = settings.gateway_timeout_seconds

        LOG.info("Historical backfill: %d job(s) in %d batch(es)", len(snapshot), len(batches))
        outcome = run_batch_pass(
            db, primary, fallback, batches,
            profile_version=version, stats=stats,
            deadline=deadline_at, reserve_seconds=reserve,
        )
        # Same completeness contract as the live pipeline: one bounded,
        # non-recursive cleanup pass, skipped when the provider already failed.
        if outcome.unresolved and outcome.provider_error is None:
            cleanup_batches = list(
                iter_batches(
                    outcome.unresolved,
                    max_jobs=settings.max_jobs_per_batch,
                    max_chars=settings.max_prompt_chars,
                    max_job_description_chars=settings.max_job_description_chars,
                )
            )
            LOG.info("Historical cleanup pass: %d unresolved ID(s)", len(outcome.unresolved))
            cleanup = run_batch_pass(
                db, primary, fallback, cleanup_batches,
                profile_version=version, stats=stats,
                deadline=deadline_at, reserve_seconds=reserve, cleanup=True,
            )
            outcome.unresolved = cleanup.unresolved + cleanup.deferred
            if cleanup.provider_error:
                outcome.provider_error = cleanup.provider_error

        db.finish_run(
            run_id,
            "partial" if outcome.provider_error else "success",
            stats,
            compact_sentence(outcome.provider_error, 1000) if outcome.provider_error else None,
        )
        run_id = None

        after = db.historical_counts(version, today=today)
        print("Historical backfill:")
        print(f"  {len(snapshot)} selected")
        print(f"  {stats.evaluated} evaluated")
        print(f"  {len(outcome.unresolved)} unresolved")
        print(f"  {len(outcome.deferred)} deferred by the runtime budget")
        print(f"  {after.get('historical_matches_awaiting_delivery', 0)} worthwhile matches stored, awaiting delivery")
        print(f"  {after.get('historical_still_open_pending', 0)} open historical jobs remaining")
        if outcome.provider_error:
            print(f"  provider error: {compact_sentence(outcome.provider_error, 200)}")
        print(f"  {stats.total_tokens:,} tokens · {stats.fallback_calls} transport fallback(s)"
              + (f" {stats.fallback_reasons}" if stats.fallback_reasons else ""))
        print("  nothing was delivered; run backfill-report to send stored matches")
        return 0
    except BaseException as exc:
        if run_id is not None:
            with contextlib.suppress(Exception):
                db.finish_run(run_id, "error", stats, compact_sentence(exc, 1000))
        raise
    finally:
        db.close()


def run_backfill_report(settings: Settings, *, limit: int | None = None) -> int:
    """Deliver stored historical matches, a bounded page at a time.

    Only the cards actually printed are marked delivered, so a truncated or
    undelivered page is retried rather than silently lost.
    """
    info = doctor(settings, require_key=False)
    db = Database(settings.db_path)
    try:
        version = info["profile_version"]
        today = market_today()
        page = settings.max_notifications_per_run if limit is None else max(1, limit)
        rows = db.unnotified(page, historical=True)
        rows, duplicate_cards = deduplicate_notifications(rows)

        emitted: list[sqlite3.Row] = []
        message = ""
        for row in rows:
            candidate = format_notifications([*emitted, row])
            if emitted and len(candidate) > BACKFILL_REPORT_MAX_CHARS:
                break
            emitted.append(row)
            message = candidate

        if emitted:
            print(message, flush=True)
            ids = [int(row["evaluation_id"]) for row in emitted]
            ids += [int(row["evaluation_id"]) for row, _ in duplicate_cards]
            db.mark_notifications_emitted(ids)
        elif duplicate_cards:
            db.mark_notifications_emitted([int(row["evaluation_id"]) for row, _ in duplicate_cards])

        waiting = db.historical_counts(version, today=today).get("historical_matches_awaiting_delivery", 0)
        if not emitted and not waiting:
            print("✅ Historical recovery: all worthwhile open matches delivered.", flush=True)
        elif waiting:
            noun = "match" if waiting == 1 else "matches"
            print(
                f"\U0001f3af Historical recovery: {len(emitted)} delivered · {waiting} {noun} still waiting.",
                flush=True,
            )
        else:
            print(
                f"✅ Historical recovery: {len(emitted)} delivered · none still waiting.",
                flush=True,
            )
        return 0
    finally:
        db.close()


def backfill_status(settings: Settings) -> dict[str, Any]:
    info = doctor(settings, require_key=False)
    db = Database(settings.db_path)
    try:
        today = market_today()
        counts = db.historical_counts(info["profile_version"], today=today)
        return {
            "market_date": today.isoformat(),
            "market_timezone": MARKET_TIMEZONE if _MARKET_TZ is not None else "UTC (no tz database)",
            "live_since": db.get_meta("live_since"),
            "candidates_per_run": settings.max_candidates_per_run,
            **(counts or {"note": "database is in bootstrap mode; nothing is historical yet"}),
        }
    finally:
        db.close()


def make_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="rolelens.py",
        description="RoleLens: semantic Swedish job discovery for script-only schedulers.",
    )
    parser.add_argument("--version", action="version", version=f"RoleLens {APP_VERSION}")
    parser.add_argument(
        "--home",
        type=Path,
        default=Path(os.getenv("ROLELENS_HOME", DEFAULT_HOME)),
        help="RoleLens home directory (default: ~/.rolelens, or ROLELENS_HOME)",
    )
    parser.add_argument("--verbose", action="store_true", help="Enable debug logs on stderr")
    # Not required: a scheduler that runs the script without arguments gets `run`.
    sub = parser.add_subparsers(dest="command")
    sub.add_parser("run", help="Discover, rank, evaluate and emit new matches (default)")
    sub.add_parser("fetch", help="Discover and store jobs only; no model call")
    sub.add_parser("evaluate", help="Rank and evaluate already-stored jobs only")
    sub.add_parser("doctor", help="Validate local configuration without network calls")
    sub.add_parser("status", help="Show local database counters")
    sub.add_parser(
        "activate",
        help="Freeze the historical backlog and switch future runs to new/changed jobs only",
    )

    backfill = sub.add_parser(
        "backfill",
        help="Evaluate open historical jobs the live selector skips. Never delivers.",
    )
    backfill.add_argument(
        "--dry-run", action="store_true",
        help="Show what would be selected. No provider calls, no writes.",
    )
    backfill.add_argument(
        "--limit", type=int, default=None,
        help="Override the per-run candidate ceiling for this invocation.",
    )
    sub.add_parser("backfill-status", help="Historical backlog counters. Read-only, free.")
    report = sub.add_parser(
        "backfill-report",
        help="Deliver one bounded page of stored historical matches",
    )
    report.add_argument(
        "--limit", type=int, default=None,
        help="Cards in this page (default: max_notifications_per_run).",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = make_parser()
    args = parser.parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        stream=sys.stderr,
    )
    command = args.command or "run"
    try:
        settings = Settings.load(args.home.expanduser().resolve())
        if command == "doctor":
            print(json.dumps(doctor(settings, require_key=True), indent=2, ensure_ascii=False))
            return 0
        if command == "status":
            _, _, version = load_profile_bundle(settings)
            db = Database(settings.db_path)
            try:
                print(json.dumps(db.status(version), indent=2, ensure_ascii=False))
            finally:
                db.close()
            return 0
        if command == "backfill-status":
            print(json.dumps(backfill_status(settings), indent=2, ensure_ascii=False))
            return 0
        if command == "backfill":
            if args.dry_run:
                # Read-only and free, so it does not contend for the run lock.
                return run_backfill(settings, dry_run=True, limit=args.limit)
            with FileLock(settings.lock_path):
                return run_backfill(settings, dry_run=False, limit=args.limit)
        if command == "backfill-report":
            with FileLock(settings.lock_path):
                return run_backfill_report(settings, limit=args.limit)
        if command == "activate":
            db = Database(settings.db_path)
            try:
                before = db.get_meta("live_since")
                live_since = db.activate_live_mode()
                print(
                    json.dumps(
                        {
                            "mode": "live",
                            "state": "already active" if before else "activated",
                            "live_since": live_since,
                            "note": "Future evaluate/run commands select only jobs first seen or changed at/after this cutoff.",
                        },
                        indent=2,
                        ensure_ascii=False,
                    )
                )
            finally:
                db.close()
            return 0
        try:
            with FileLock(settings.lock_path):
                return run_pipeline(
                    settings,
                    fetch_only=command == "fetch",
                    evaluate_only=command == "evaluate",
                )
        except RoleLensError as exc:
            if "already active" in str(exc):
                LOG.warning("%s; exiting silently", exc)
                return 0
            raise
    except RoleLensError as exc:
        LOG.error("%s", exc)
        return 2
    except KeyboardInterrupt:
        LOG.error("Interrupted")
        return 130
    except Exception:
        LOG.exception("Unexpected failure")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
