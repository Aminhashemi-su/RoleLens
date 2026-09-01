#!/usr/bin/env python3
"""RoleLens, semantic Swedish job discovery with deterministic orchestration.

Runtime design:
  * Arbetsförmedlingen JobSearch does discovery for zero LLM tokens.
  * SQLite provides durable idempotency and evaluation history.
  * Vertex Gemini performs semantic evaluation, with one Azure transport fallback.
  * stdout is reserved for user notifications, so a script-only scheduler can
    deliver it verbatim. Operational logs always go to stderr.

The runtime has no third-party Python dependencies.
"""

from __future__ import annotations

import argparse
import contextlib
import dataclasses
import datetime as dt
import fcntl
import hashlib
import html
import json
import logging
import os
import random
import re
import sqlite3
import stat
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, Iterable, Iterator, Mapping, Sequence

APP_NAME = "rolelens"
APP_VERSION = "1.5.3"
SCHEMA_VERSION = "2"
DEFAULT_HOME = Path.home() / ".rolelens"
JOBSEARCH_BASE_URL = "https://jobsearch.api.jobtechdev.se"
GEMINI_BASE_URL = "https://aiplatform.googleapis.com/v1/publishers/google/models"
PRIMARY_PROVIDER = "vertex_gemini"
FALLBACK_PROVIDER = "azure_gpt5mini"
RETRYABLE_HTTP_CODES = {408, 409, 425, 429, 500, 502, 503, 504}
UTC = dt.timezone.utc

LOG = logging.getLogger(APP_NAME)


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

    Looks at `constraints.<language>` first, which is where the existing schema
    already keeps it, then at an optional `languages.<language>` map. Anything
    missing or unparseable is UNKNOWN.
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
    """Expected operational failure that should make a cron run fail loudly."""


class ConfigurationError(RoleLensError):
    """Invalid or missing local configuration."""


class RemoteAPIError(RoleLensError):
    """Remote API failed after bounded retries."""


class RateLimitError(RemoteAPIError):
    """Remote provider rate limit after bounded HTTP retries.

    This is recoverable for RoleLens: jobs remain pending and the next
    scheduled run can resume without losing discovery state.
    """


class TemporaryProviderError(RemoteAPIError):
    """A transport, rate-limit, timeout, unavailable, or temporary 5xx failure."""

    def __init__(self, provider: str, message: str) -> None:
        super().__init__(message)
        self.provider = provider


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


@dataclasses.dataclass(frozen=True)
class Settings:
    home: Path
    db_path: Path
    profile_dir: Path
    secrets_path: Path
    lock_path: Path
    primary_provider: str
    fallback_provider: str
    search_terms: tuple[str, ...]
    location_terms: tuple[str, ...]
    include_unlocated_searches: bool
    search_limit: int
    query_delay_seconds: float
    max_candidates_per_run: int
    max_jobs_per_batch: int
    max_run_seconds: int
    max_prompt_chars: int
    max_job_description_chars: int
    max_notifications_per_run: int
    jobsearch_timeout_seconds: int
    gateway_timeout_seconds: int
    http_retries: int
    northern_exclusions: tuple[str, ...]
    preferred_locations: tuple[str, ...]
    high_signal_title_terms: tuple[str, ...]

    @classmethod
    def load(cls, home: Path) -> "Settings":
        config_path = home / "config.json"
        if not config_path.exists():
            raise ConfigurationError(f"Missing configuration: {config_path}")
        try:
            raw = json.loads(config_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ConfigurationError(f"Cannot read {config_path}: {exc}") from exc

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

        home = home.expanduser().resolve()
        return cls(
            home=home,
            db_path=home / str(raw.get("database", "data/rolelens.db")),
            profile_dir=home / str(raw.get("profile_dir", "profile")),
            secrets_path=home / str(raw.get("secrets_file", "secrets.env")),
            lock_path=home / str(raw.get("lock_file", "data/rolelens.lock")),
            primary_provider=str(raw.get("primary_provider", PRIMARY_PROVIDER)),
            fallback_provider=str(raw.get("fallback_provider", FALLBACK_PROVIDER)),
            search_terms=strings("search_terms"),
            location_terms=strings("location_terms"),
            include_unlocated_searches=bool(raw.get("include_unlocated_searches", True)),
            search_limit=min(100, positive_int("search_limit", 50)),
            query_delay_seconds=max(0.0, float(raw.get("query_delay_ms", 120)) / 1000.0),
            # Completeness beats punctuality: a run evaluates its whole frozen
            # snapshot. The two limits below are emergency valves, not throughput
            # caps - one bounds snapshot memory, the other bounds wall clock.
            max_candidates_per_run=min(500, positive_int("max_candidates_per_run", 300)),
            max_jobs_per_batch=min(10, positive_int("max_jobs_per_batch", 10)),
            max_run_seconds=positive_int("max_run_seconds", 3000),
            max_prompt_chars=positive_int("max_prompt_chars", 180_000),
            max_job_description_chars=positive_int("max_job_description_chars", 9_000),
            max_notifications_per_run=positive_int("max_notifications_per_run", 8),
            jobsearch_timeout_seconds=positive_int("jobsearch_timeout_seconds", 30),
            gateway_timeout_seconds=positive_int("gateway_timeout_seconds", 150),
            http_retries=min(8, positive_int("http_retries", 4)),
            northern_exclusions=tuple(x.casefold() for x in strings("northern_exclusions")),
            preferred_locations=strings("preferred_locations"),
            high_signal_title_terms=tuple(x.casefold() for x in strings("high_signal_title_terms")),
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
    queries_attempted: int = 0
    queries_succeeded: int = 0
    search_hits: int = 0
    unique_jobs: int = 0
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
    batches_processed: int = 0
    cleanup_batches: int = 0
    duplicates_suppressed: int = 0
    snapshot_size: int = 0
    unresolved: int = 0
    deferred: int = 0


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


class FileLock:
    """Non-blocking process lock. A second cron tick exits quietly."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self._fh: Any = None

    def __enter__(self) -> "FileLock":
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._fh = self.path.open("a+", encoding="utf-8")
        try:
            fcntl.flock(self._fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            self._fh.close()
            self._fh = None
            raise RoleLensError("Another RoleLens run is already active") from exc
        self._fh.seek(0)
        self._fh.truncate()
        self._fh.write(str(os.getpid()))
        self._fh.flush()
        return self

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        if self._fh is not None:
            with contextlib.suppress(OSError):
                fcntl.flock(self._fh.fileno(), fcntl.LOCK_UN)
            self._fh.close()


class HttpClient:
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
    ) -> dict[str, Any]:
        final_headers = {
            "Accept": "application/json",
            "User-Agent": self.user_agent,
            **(dict(headers or {})),
        }
        data: bytes | None = None
        if payload is not None:
            data = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
            final_headers.setdefault("Content-Type", "application/json")

        last_error: BaseException | None = None
        for attempt in range(self.retries + 1):
            request = urllib.request.Request(url, data=data, headers=final_headers, method=method)
            try:
                with urllib.request.urlopen(request, timeout=timeout) as response:
                    body = response.read()
                    if not body:
                        return {}
                    parsed = json.loads(body.decode("utf-8"))
                    if not isinstance(parsed, dict):
                        raise RemoteAPIError(f"Expected JSON object from {redact_url(url)}")
                    return parsed
            except urllib.error.HTTPError as exc:
                last_error = exc
                body = exc.read().decode("utf-8", errors="replace")[:1500]
                if exc.code not in RETRYABLE_HTTP_CODES or attempt >= self.retries:
                    message = f"HTTP {exc.code} from {redact_url(url)}: {body or exc.reason}"
                    if exc.code == 429:
                        raise RateLimitError(message) from exc
                    raise RemoteAPIError(message) from exc
                delay = retry_delay(attempt, exc.headers.get("Retry-After"))
                LOG.warning("HTTP %s, retrying in %.1fs: %s", exc.code, delay, redact_url(url))
                time.sleep(delay)
            except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
                last_error = exc
                if attempt >= self.retries:
                    raise RemoteAPIError(f"Request failed: {redact_url(url)}: {exc}") from exc
                delay = retry_delay(attempt, None)
                LOG.warning("Network/JSON error, retrying in %.1fs: %s", delay, exc)
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
        self._migrate()

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

                -- Repost bookkeeping. Additive and never deletes a source job,
                -- so an older build simply ignores this table.
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
                """
            )
            existing = self.conn.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()
            existing_version = existing["value"] if existing is not None else None
            if existing_version not in {None, "1", SCHEMA_VERSION}:
                raise ConfigurationError(
                    f"Unsupported database schema {existing_version}; expected 1 or {SCHEMA_VERSION}"
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

    def upsert_job(self, job: JobRecord) -> None:
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
                    END
                """,
                (
                    "platsbanken",
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
                  {live_clause}
                ORDER BY
                    j.discovery_score DESC,
                    COALESCE(j.published_at, j.first_seen_at) DESC,
                    j.id DESC
                LIMIT ?
                """,
                tuple(params),
            )
        )

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
              {live_clause}
            """,
            tuple(params),
        ).fetchone()
        return int(row[0])

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

    def unnotified(self, limit: int) -> list[sqlite3.Row]:
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
                ORDER BY e.opportunity_score DESC, e.career_fit DESC, e.evaluated_at ASC
                LIMIT ?
                """,
                (limit,),
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

    def status(self, profile_version: str | None = None) -> dict[str, Any]:
        def scalar(sql: str) -> int:
            return int(self.conn.execute(sql).fetchone()[0])

        live_since = self.get_meta("live_since")
        result: dict[str, Any] = {
            "jobs": scalar("SELECT COUNT(*) FROM jobs"),
            "evaluations": scalar("SELECT COUNT(*) FROM evaluations"),
            "notifications_emitted": scalar("SELECT COUNT(*) FROM notifications"),
            "runs": scalar("SELECT COUNT(*) FROM runs"),
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


class JobSearchClient:
    def __init__(self, http: HttpClient, settings: Settings) -> None:
        self.http = http
        self.settings = settings

    def search(self, query: str) -> list[dict[str, Any]]:
        params = urllib.parse.urlencode({"q": query, "limit": self.settings.search_limit})
        url = f"{JOBSEARCH_BASE_URL}/search?{params}"
        result = self.http.json_request(
            "GET",
            url,
            timeout=self.settings.jobsearch_timeout_seconds,
        )
        hits = result.get("hits", [])
        if not isinstance(hits, list):
            raise RemoteAPIError("JobSearch response did not contain a hits array")
        return [x for x in hits if isinstance(x, dict)]

    def ad(self, ad_id: str) -> dict[str, Any]:
        quoted = urllib.parse.quote(ad_id, safe="")
        return self.http.json_request(
            "GET",
            f"{JOBSEARCH_BASE_URL}/ad/{quoted}",
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


def semantic_system_prompt(swedish: LanguageLevel = UNKNOWN_LANGUAGE_LEVEL) -> str:
    return (
        "You are an evidence-disciplined semantic career matcher for Swedish job discovery. "
        "Evaluate what the person would actually do, not the advertised title. Understand English and Swedish. "
        "Use only candidate evidence supplied below; never invent experience or turn unknown facts into unmet facts. "
        "Score career_fit only for long-term role/content alignment. Practical constraints such as language, location, "
        "citizenship, clearance, or timing belong in opportunity_score and blockers, never career_fit. "
        + swedish_prompt_clause(swedish) +
        "A Swedish-language advertisement alone is not a Swedish-language requirement. "
        "Judge explicit mandatory fluent/professional/advanced Swedish against the candidate level stated above; preferred or optional "
        "Swedish is never a blocker. Ordinary background/security screening does not imply citizenship or clearance eligibility. "
        "If citizenship or security eligibility is explicitly required and candidate evidence does not resolve it, preserve UNKNOWN. "
        "Unknown years of experience are unknown or partial, not automatically unmet. Founder/CTO titles are not proof of "
        "staff-level seniority. Return exactly one evaluation for every supplied source_job_id, no duplicates and no other IDs. "
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
            raise TemporaryProviderError(provider, message) from exc
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
            accepted[item_id] = validate_evaluation(item, job=expected[item_id], swedish=swedish)
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
        # is a normal Gemini quirk and must never be read as a provider failure.
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
        system_prompt = semantic_system_prompt(self.swedish)
        if self.provider == PRIMARY_PROVIDER:
            request_payload = {
                "systemInstruction": {"parts": [{"text": system_prompt}]},
                "contents": [{"role": "user", "parts": [{"text": json.dumps(user_payload, ensure_ascii=False, separators=(",", ":"))}]}],
                "generationConfig": {
                    "maxOutputTokens": 12_000,
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
                "max_completion_tokens": 12_000,
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
            content, jobs, provider=self.provider, model=self.model, usage=usage, swedish=self.swedish
        )
        archive_model_response(self.settings, self.provider, content, failure=bool(result.error))
        return result


def evaluate_with_fallback(
    primary: ProviderMatcher,
    fallback: ProviderMatcher,
    jobs: Sequence[sqlite3.Row],
) -> tuple[ProviderBatchResult, bool]:
    """One primary call, and at most one fallback call for temporary transport failure."""
    try:
        return primary.evaluate(jobs), False
    except TemporaryProviderError as primary_error:
        LOG.warning("%s temporarily unavailable; trying %s once", primary.provider, fallback.provider)
        try:
            return fallback.evaluate(jobs), True
        except TemporaryProviderError as fallback_error:
            expected_ids = frozenset(str(row["source_job_id"]) for row in jobs)
            error = f"{primary.provider} temporary failure; {fallback.provider} temporary failure"
            LOG.warning("%s: %s / %s", error, primary_error, fallback_error)
            return ProviderBatchResult(
                provider=fallback.provider,
                model=fallback.model,
                evaluations=(),
                unresolved_ids=expected_ids,
                usage=empty_usage(),
                error=error,
                error_kind="transport",
            ), True


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

        result, used_fallback = evaluate_with_fallback(primary, fallback, batch)
        if cleanup:
            stats.cleanup_batches += 1
        else:
            stats.batches_processed += 1
        stats.fallback_calls += int(used_fallback)
        add_provider_usage(stats, result.usage)

        by_source_id = {str(row["source_job_id"]): row for row in batch}
        for evaluation in result.evaluations:
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
    parts = urllib.parse.urlsplit(url)
    return urllib.parse.urlunsplit((parts.scheme, parts.netloc, parts.path, "", ""))


def clean_text(value: Any) -> str:
    if value is None:
        return ""
    text = html.unescape(str(value))
    text = re.sub(r"<[^>]+>", " ", text)
    text = text.replace("\u00ad", "").replace("\xa0", " ")
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
    """Compatibility helper used by tests and callers interested in any remote signal."""
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


def should_prefilter(job: JobRecord, settings: Settings) -> tuple[bool, str]:
    if application_expired(job.application_deadline):
        return True, "expired"
    country = job.country.casefold()
    if country and country not in {"sverige", "sweden"}:
        return True, "outside_sweden"
    if not job.fully_remote:
        location = f"{job.municipality} {job.region}".casefold()
        if any(term in location for term in settings.northern_exclusions):
            return True, "excluded_northern_location"
    if not job.description:
        return True, "missing_description"
    return False, ""


def discovery_score(job: JobRecord, settings: Settings) -> int:
    score = min(20, 4 * len(job.matched_queries))
    title = job.title.casefold()
    score += 8 * sum(1 for term in settings.high_signal_title_terms if term in title)
    location = f"{job.municipality} {job.region}".casefold()
    for index, preferred in enumerate(settings.preferred_locations):
        if preferred.casefold() in location:
            score += max(1, 8 - index)
            break
    if job.remote:
        score += 2
    return score


def build_queries(settings: Settings) -> list[str]:
    queries: list[str] = []
    seen: set[str] = set()
    for term in settings.search_terms:
        variants = [term] if settings.include_unlocated_searches else []
        variants.extend(f"{term} {location}" for location in settings.location_terms)
        for query in variants:
            normalized = " ".join(query.split()).casefold()
            if normalized not in seen:
                seen.add(normalized)
                queries.append(" ".join(query.split()))
    return queries


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


def load_secrets(path: Path) -> dict[str, str]:
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
                raise ConfigurationError(f"Invalid secrets line in {path}: expected KEY=VALUE")
            key, value = line.split("=", 1)
            value = value.strip()
            if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
                value = value[1:-1]
            result[key.strip()] = value
    for key in (
        "VERTEX_GEMINI_API_KEY",
        "VERTEX_GEMINI_MODEL",
        "AZURE_OPENAI_API_KEY",
        "AZURE_OPENAI_BASE_URL",
        "AZURE_OPENAI_DEPLOYMENT",
        # Kept only for explicit, manual GLM benchmark scripts.
        "AI_GATEWAY_API_KEY",
    ):
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


def extract_completion_content(response: Mapping[str, Any]) -> str:
    choices = response.get("choices")
    if not isinstance(choices, list) or not choices or not isinstance(choices[0], dict):
        raise RemoteAPIError("AI Gateway response missing choices")
    message = choices[0].get("message")
    if not isinstance(message, dict):
        raise RemoteAPIError("AI Gateway response missing message")
    content = message.get("content")
    if not isinstance(content, str) or not content.strip():
        raise RemoteAPIError("AI Gateway returned empty content")
    return content.strip()


def parse_json_object(content: str) -> dict[str, Any]:
    try:
        value = json.loads(content)
    except json.JSONDecodeError:
        # Compatibility path for providers that wrap JSON in a markdown fence.
        match = re.search(r"```(?:json)?\s*(\{.*\})\s*```", content, flags=re.DOTALL | re.IGNORECASE)
        if not match:
            start, end = content.find("{"), content.rfind("}")
            if start < 0 or end <= start:
                raise RemoteAPIError("Model returned invalid JSON")
            candidate = content[start : end + 1]
        else:
            candidate = match.group(1)
        try:
            value = json.loads(candidate)
        except json.JSONDecodeError as exc:
            raise RemoteAPIError(f"Model returned invalid JSON: {exc}") from exc
    if not isinstance(value, dict):
        raise RemoteAPIError("Model response must be a JSON object")
    return value


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
    out: list[dict[str, str]] = []
    for item in value:
        out.append({str(k): clean_text(v) for k, v in item.items()})
    return tuple(out)


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


def normalize_evaluation_policy(
    item: Mapping[str, Any],
    job: Mapping[str, Any],
    *,
    swedish: LanguageLevel = UNKNOWN_LANGUAGE_LEVEL,
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
        swedish_rows = [x for x in must_haves if mentions(x.get("requirement"), ("swedish", "svenska"))]
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
        blockers = [x for x in blockers if not mentions(x.get("reason"), ("swedish", "svenska"))]
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
        must_haves = [x for x in must_haves if not mentions(x.get("requirement"), ("swedish", "svenska"))]
        blockers = [x for x in blockers if not mentions(x.get("reason"), ("swedish", "svenska"))]
        if optional_swedish:
            normalized["language_risk"] = "Swedish is optional/preferred and is not a blocker."
            changes.append("optional_swedish_not_blocking")

    explicit_citizenship = bool(CITIZENSHIP_PATTERN.search(description))
    explicit_clearance = bool(CLEARANCE_ELIGIBILITY_PATTERN.search(description))
    screening_only = bool(SCREENING_PATTERN.search(description)) and not explicit_citizenship and not explicit_clearance
    sensitive_terms = ("citizen", "citizenship", "medborg", "clearance", "security eligibility")
    if screening_only:
        must_haves = [x for x in must_haves if not mentions(x.get("requirement"), sensitive_terms)]
        blockers = [x for x in blockers if not mentions(x.get("reason"), sensitive_terms)]
        changes.append("screening_not_converted_to_eligibility")
    elif explicit_citizenship or explicit_clearance:
        label = "Citizenship eligibility" if explicit_citizenship else "Security-clearance eligibility"
        related = [x for x in must_haves if mentions(x.get("requirement"), sensitive_terms)]
        if related:
            for row in related:
                row["status"] = "unknown"
                row["reason"] = f"{label} is explicit in the ad but unresolved by candidate evidence."
        else:
            must_haves.append({
                "requirement": label,
                "status": "unknown",
                "reason": f"{label} is explicit in the ad but unresolved by candidate evidence.",
            })
        blockers = [x for x in blockers if not mentions(x.get("reason"), sensitive_terms)]
        blockers.append({"type": "unknown", "reason": f"{label} requires candidate verification."})
        changes.append("explicit_eligibility_preserved_unknown")

    # Lack of evidence for a numeric tenure claim is unknown, not proof of failure.
    for row in must_haves:
        combined = f"{clean_text(row.get('requirement'))} {clean_text(row.get('reason'))}"
        if re.search(r"\b\d+\+?\s*(?:years?|yrs?|år)\b", combined, flags=re.IGNORECASE) and mentions(
            row.get("reason"), ("no evidence", "not stated", "not specified", "not provided", "cannot confirm", "cannot verify", "unknown", "unclear")
        ):
            if clean_text(row.get("status")).casefold() == "unmet":
                row["status"] = "unknown"
                changes.append("unknown_tenure_preserved")

    # Any genuinely unmet item in the model's mandatory-requirement list is hard.
    for row in must_haves:
        if clean_text(row.get("status")).casefold() == "unmet":
            reason = f"Mandatory requirement unmet: {clean_text(row.get('requirement'))}"
            if not any(x.get("type") == "hard" and clean_text(x.get("reason")) == reason for x in blockers):
                blockers.append({"type": "hard", "reason": reason})

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
    if opportunity >= 60:
        return "notify_stretch"
    return "store_no_notify"


def validate_evaluation(
    item: Mapping[str, Any],
    *,
    job: Mapping[str, Any] | None = None,
    swedish: LanguageLevel = UNKNOWN_LANGUAGE_LEVEL,
) -> Evaluation:
    normalized = (
        normalize_evaluation_policy(item, job, swedish=swedish) if job is not None else dict(item)
    )
    item = normalized
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


def load_profile_bundle(settings: Settings) -> tuple[dict[str, Any], dict[str, Any], str]:
    matcher_profile = load_json_file(settings.profile_dir / "matcher_profile.json")
    matcher_rules = load_json_file(settings.profile_dir / "matcher_rules_v1_1.json")
    return matcher_profile, matcher_rules, profile_version(matcher_profile, matcher_rules)


def fetch_jobs(
    client: JobSearchClient,
    settings: Settings,
    stats: RunStats,
) -> dict[str, JobRecord]:
    merged: dict[str, JobRecord] = {}
    queries = build_queries(settings)
    LOG.info("Running %d discovery queries", len(queries))
    failures = 0
    for index, query in enumerate(queries, start=1):
        stats.queries_attempted += 1
        try:
            hits = client.search(query)
            stats.queries_succeeded += 1
        except RemoteAPIError as exc:
            failures += 1
            LOG.error("Query failed [%s]: %s", query, exc)
            continue
        stats.search_hits += len(hits)
        for hit in hits:
            try:
                job = normalize_job(hit)
            except (ValueError, TypeError) as exc:
                LOG.warning("Skipping malformed job: %s", exc)
                continue
            if not job.description:
                try:
                    full = client.ad(job.source_job_id)
                    job = normalize_job(full)
                except (RemoteAPIError, ValueError, TypeError) as exc:
                    LOG.warning("Could not hydrate ad %s: %s", job.source_job_id, exc)
            existing = merged.get(job.source_job_id)
            if existing is None:
                job.matched_queries.add(query)
                merged[job.source_job_id] = job
            else:
                existing.matched_queries.add(query)
        if settings.query_delay_seconds and index < len(queries):
            time.sleep(settings.query_delay_seconds)

    if stats.queries_succeeded == 0:
        raise RemoteAPIError(f"All {failures} JobSearch queries failed")
    stats.unique_jobs = len(merged)
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
            rendered = []
            for blocker in blockers[:2]:
                rendered.append(f"{blocker.get('type','?')}: {compact_sentence(blocker.get('reason',''), 120)}")
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


def unedited_templates(settings: Settings) -> list[str]:
    """Profile files that still look like the shipped examples."""
    still_template: list[str] = []
    for name in ("career_profile.json", "matcher_profile.json", "search_lenses.json"):
        path = settings.profile_dir / name
        try:
            text = path.read_text(encoding="utf-8")
        except OSError:
            continue
        if any(marker in text for marker in TEMPLATE_MARKERS):
            still_template.append(name)
    return still_template


def doctor(settings: Settings, *, require_key: bool = True) -> dict[str, Any]:
    settings.home.mkdir(parents=True, exist_ok=True)
    settings.db_path.parent.mkdir(parents=True, exist_ok=True)
    required = [
        settings.profile_dir / "career_profile.json",
        settings.profile_dir / "matcher_profile.json",
        settings.profile_dir / "search_lenses.json",
        settings.profile_dir / "matcher_rules_v1_1.json",
    ]
    missing = [str(path) for path in required if not path.exists()]
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
            hint = (
                " Also still unedited: " + ", ".join(templates)
                + " in " + str(settings.profile_dir) + "."
            )
        raise ConfigurationError(
            "Missing provider configuration in "
            f"{settings.secrets_path}: {', '.join(missing_keys)}.{hint}"
        )
    swedish = candidate_language_level(matcher_profile, "swedish")
    todo: list[str] = []
    if templates:
        todo.append(
            "Personalise " + ", ".join(templates) + " in " + str(settings.profile_dir)
            + " - they still contain the shipped example candidate."
        )
    if missing_keys:
        todo.append(
            "Add " + ", ".join(missing_keys) + " to " + str(settings.secrets_path)
            + " (chmod 600)."
        )
    if not swedish.known:
        todo.append(
            "Set constraints.swedish in matcher_profile.json - the Swedish level is "
            "unspecified, so language requirements stay UNKNOWN rather than judged."
        )

    return {
        "home": str(settings.home),
        "database": str(settings.db_path),
        "primary_provider": settings.primary_provider,
        "fallback_provider": settings.fallback_provider,
        "profile_version": version,
        "queries_per_run": len(build_queries(settings)),
        "provider_credentials_present": not missing_keys,
        "missing_credentials": missing_keys,
        "profile_keys": len(matcher_profile),
        "rules_keys": len(matcher_rules),
        "unedited_example_profiles": templates,
        "candidate_swedish_level": swedish.label,
        "max_candidates_per_run": settings.max_candidates_per_run,
        "max_jobs_per_batch": settings.max_jobs_per_batch,
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


def format_run_summary(
    evaluated: int,
    matches: int,
    queued: int = 0,
    *,
    failed: bool = False,
    ceiling_reached: bool = False,
    selected: int = 0,
) -> str:
    """One compact stats line per run.

    `evaluated` counts jobs the semantic matcher returned a usable result for,
    never Platsbanken search hits or database upserts. `queued` counts frozen
    snapshot jobs that ended the run without one and will be retried next time.
    """
    noun = "match" if matches == 1 else "matches"
    if failed:
        return (
            f"\u26a0\ufe0f RoleLens: {evaluated} jobs checked \u00b7 {matches} {noun} "
            f"\u00b7 {queued} pending after provider error."
        )
    if ceiling_reached:
        # The snapshot was truncated by the emergency ceiling, so this run is not a
        # complete picture of the market and must never be reported as one.
        return (
            f"\u26a0\ufe0f RoleLens: candidate safety ceiling reached, {selected} selected "
            f"\u00b7 {matches} {noun} \u00b7 additional jobs remain queued."
        )
    if evaluated == 0 and matches == 0 and queued == 0:
        # Only a genuinely empty tick reports nothing; a match carried over from
        # an earlier run must still be described accurately.
        return "\U0001f50e RoleLens: no new jobs found."
    icon = "\U0001f3af" if matches else "\U0001f50e"
    if queued:
        return (
            f"{icon} RoleLens: {evaluated} jobs checked \u00b7 {matches} {noun} "
            f"\u00b7 {queued} queued for next run."
        )
    return f"{icon} RoleLens: {evaluated} jobs checked \u00b7 {matches} {noun}."

def determine_run_status(partial_reasons: Sequence[str]) -> str:
    return "partial" if partial_reasons else "success"


def run_pipeline(settings: Settings, *, fetch_only: bool, evaluate_only: bool) -> int:
    info = doctor(settings, require_key=not fetch_only)
    LOG.info(
        "Profile %s, primary=%s fallback=%s",
        info["profile_version"],
        settings.primary_provider,
        settings.fallback_provider,
    )
    http = HttpClient(retries=settings.http_retries, user_agent=f"{APP_NAME}/{APP_VERSION} personal-job-search")
    db = Database(settings.db_path)
    stats = RunStats()
    run_id = db.start_run()
    partial_reasons: list[str] = []
    unresolved_count = 0
    try:
        if not evaluate_only:
            jobs = fetch_jobs(JobSearchClient(http, settings), settings, stats)
            persist_discovered_jobs(db, jobs.values(), settings, stats)
            LOG.info(
                "Discovery: %d hits, %d unique, %d stored, %d prefiltered",
                stats.search_hits,
                stats.unique_jobs,
                stats.jobs_upserted,
                stats.jobs_prefiltered,
            )

        ceiling_reached = False
        snapshot_selected = 0
        if not fetch_only:
            matcher_profile, matcher_rules, version = load_profile_bundle(settings)
            secrets = load_secrets(settings.secrets_path)

            # Freeze the candidate set for this run. It is taken once and never
            # re-queried, so a job discovered mid-run belongs to the next run and
            # this run stays auditable.
            ceiling = settings.max_candidates_per_run
            candidates = db.pending_jobs(version, ceiling)
            eligible_total = db.pending_count(version, respect_live_mode=True)
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

                outcome = run_batch_pass(
                    db, primary, fallback, batches,
                    profile_version=version, stats=stats,
                    deadline=deadline, reserve_seconds=reserve,
                )

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

            # Exactly one compact stats line per run, after any detailed matches.
            print(
                format_run_summary(
                    stats.evaluated,
                    stats.notified,
                    # Frozen-snapshot jobs that ended the run without a result.
                    max(0, stats.pending_selected - stats.evaluated),
                    failed=bool(partial_reasons),
                    ceiling_reached=ceiling_reached,
                    selected=snapshot_selected,
                ),
                flush=True,
            )
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


def make_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="RoleLens: semantic Swedish job discovery for script-only cron.",
    )
    parser.add_argument(
        "--home",
        type=Path,
        default=Path(os.getenv("ROLELENS_HOME", DEFAULT_HOME)),
        help="RoleLens home directory (default: ~/.rolelens)",
    )
    parser.add_argument("--verbose", action="store_true", help="Enable debug logs on stderr")
    sub = parser.add_subparsers(dest="command")
    sub.add_parser("run", help="Fetch, evaluate and emit new matches (default)")
    sub.add_parser("fetch", help="Fetch/store jobs only, no LLM call")
    sub.add_parser("evaluate", help="Evaluate already-stored pending jobs only")
    sub.add_parser("doctor", help="Validate local configuration without network calls")
    sub.add_parser("status", help="Show local database counters")
    sub.add_parser(
        "activate",
        help="Freeze the historical backlog and switch future runs to new/changed jobs only",
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
            matcher_profile, matcher_rules, version = load_profile_bundle(settings)
            del matcher_profile, matcher_rules
            db = Database(settings.db_path)
            try:
                print(json.dumps(db.status(version), indent=2, ensure_ascii=False))
            finally:
                db.close()
            return 0
        if command == "activate":
            db = Database(settings.db_path)
            try:
                before = db.get_meta("live_since")
                live_since = db.activate_live_mode()
                state = "already active" if before else "activated"
                print(
                    json.dumps(
                        {
                            "mode": "live",
                            "state": state,
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
