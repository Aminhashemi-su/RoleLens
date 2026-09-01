# Changelog

RoleLens was developed under an earlier internal name before this repository
existed, so V1.1–V1.4 have no individual Git commits. This file records the
known major versions instead. Version 1.5.1 is the first state captured in Git.

The format is loosely [Keep a Changelog](https://keepachangelog.com/en/1.1.0/);
this project does not follow strict semantic versioning.

## [1.5.1] — 2026-09-01

Production baseline captured in Git.

### Added
- Bounded multi-batch throughput: `max_batches_per_run` (default 4) and
  `max_run_seconds` (default 600). A run now evaluates up to
  `max_candidates_per_run` (default 40) jobs as several sequential batches of at
  most 10, instead of a single batch of 10.
- Repost suppression. A `job_fingerprints` table records an employer/title/
  location/body fingerprint per job. Reposts of an already-evaluated vacancy are
  dropped *before* they cost a model call, and a second guard prevents two
  notification cards for the same vacancy.
- `SWEDISH_NEGATION_PATTERNS` in the deterministic policy layer.

### Fixed
- Two Swedish-language policy bugs found by replaying benchmark responses
  through the real policy layer, not by reading model scores:
  - an explicit denial ("svenska krävs inte", "Swedish is not required") was
    still matched as a mandatory-Swedish requirement and produced a false hard
    blocker;
  - the word-order variant "tal och skrift på svenska" was not matched, so a
    genuinely mandatory Swedish requirement could be missed.

## [1.5.0] — 2026-08-31

### Added
- Vertex Gemini 3.7 Flash as the primary semantic evaluator, with native
  structured output (`responseJsonSchema`) and medium thinking level.
- Azure GPT-5 mini as a **transport-only** fallback: it is called at most once
  per batch, and only when the primary fails with a transport, timeout,
  rate-limit, unavailable, or temporary 5xx error. A malformed but successful
  HTTP 200 response never triggers the fallback.
- Deterministic decision/policy layer between the model and the notification.
  `normalize_evaluation_policy` applies narrow, auditable facts (mandatory vs
  optional Swedish, screening vs citizenship/clearance, unknown tenure) and
  `classify_decision` derives the notification decision from validated scores
  and blockers. The model's own `decision` field is never trusted.
- Structured-output completeness validation: every requested `source_job_id`
  must come back exactly once. Duplicate, unknown, and missing IDs are recorded;
  independently valid rows are kept and unresolved IDs stay pending.
- Compact one-line run summary on stdout after any match cards, so a healthy
  scheduled tick is visible instead of silent.
- Improved Swedish handling: an advertisement *written* in Swedish is not a
  Swedish-language requirement; only explicit mandatory fluent/professional
  Swedish is a hard blocker.

### Changed
- GLM/Vercel removed from automatic production routing. `ProviderMatcher`
  rejects any provider other than the fixed primary/fallback pair, and `doctor`
  fails if configuration tries to change the routing. GLM credentials remain
  usable by the manual benchmark tooling only.

## [1.4.0] — 2026-08

### Added
- Bounded HTTP retry with `Retry-After` support and jittered backoff over the
  retryable status set (408, 409, 425, 429, 500, 502, 503, 504).
- Explicit `RateLimitError` / `TemporaryProviderError` separation so a
  rate-limited run ends as a recoverable partial rather than a cron failure.
- Resilient pending queue: jobs that were selected but not evaluated (provider
  omissions, a failed batch, batches never started) remain pending for the next
  scheduled run.
- GLM via the Vercel AI Gateway as the production evaluator of this version.

## [1.3.0] — 2026-08

### Added
- Durable SQLite evaluation state: `jobs`, `evaluations`, `notifications`,
  `runs`, and `meta`, with `UNIQUE(source, source_job_id)` idempotency and a
  `UNIQUE(job_id, content_hash, profile_version)` evaluation key.
- Live/historical handling. `activate` freezes the discovery backlog and sets a
  `live_since` cutoff, after which only jobs first seen or content-changed at or
  after the cutoff are selected. Schema v2 adds `content_changed_at`, backfilled
  from `first_seen_at` for existing rows.
- Malformed / incomplete model-output recovery: partial batches keep their
  independently valid evaluations, unresolved IDs stay pending, and no semantic
  retry is attempted.

## [1.1.0] – [1.2.0] — 2026-08

Early iterations: Arbetsförmedlingen JobSearch discovery, job normalization,
the `career_fit` / `opportunity_score` two-score model, and the first
single-provider semantic matcher. Not individually recorded.
