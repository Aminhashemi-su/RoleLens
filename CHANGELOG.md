# Changelog

RoleLens was developed under an earlier internal name before this repository
existed, so V1.1–V1.4 have no individual Git commits. This file records the
known major versions instead. Version 1.5.1 is the first state captured in
Git; 1.9.0 is the current release.

The format is loosely [Keep a Changelog](https://keepachangelog.com/en/1.1.0/);
this project does not follow strict semantic versioning.

## [1.9.0] — 2026-09-03

Work eligibility became a fact the engine can be told, rather than a question it
forwards to the candidate.

### Added
- **Four optional `constraints` fields in the matcher profile:**
  `swedish_citizenship`, `eu_citizenship`, `permanent_residence`, `work_permit`.
  While `swedish_citizenship` is absent the engine behaves exactly as before,
  preserving an explicit citizenship demand as UNKNOWN — turning "we do not
  know" into "you are rejected" silently loses real roles. Once the profile
  answers it, the engine decides instead of asking.
- `candidate_eligibility()` and `barred_statuses()`, which read that position,
  and `CITIZENSHIP_ALTERNATIVE_PATTERN`, which recognises a right-to-work route.

### Fixed
- **A vacancy demanding a nationality the candidate cannot hold is now a hard
  blocker, decided deterministically.** Asked to judge this itself, the evaluator
  hedged: on roles whose advertisement says citizenship *may* be required rather
  than *is*, it returned `notify_verify` rather than ruling them out. A
  conditional nationality demand cannot be satisfied either, so forwarding it as
  unknown just hands over a dead end. Whether someone holds a nationality is a
  stated fact rather than a judgement, so the policy layer settles it.
- **The right-to-work exception, which is the expensive half.** Advertisements
  word "Swedish citizen or valid EU work permits" and "svenskt medborgarskap
  krävs" almost identically and often in one sentence; only the second is a
  bar. An advertisement offering a permit route is never blocked when the profile
  says a permit is held. The pattern must match the plural — a real vacancy
  said "work permits" and was wrongly barred until it did.
- **The Swedish-language normaliser was deleting citizenship rows.** It matched
  on the word "swedish" alone, so "Swedish citizenship" was filtered out as a
  language requirement and could never become a blocker.
  `mentions_swedish_language()` now separates the two.
- **A vacancy is never delivered twice.** Notifications key on the evaluation, so
  re-evaluating a job — a new profile version, an edited advertisement —
  minted a fresh id and re-sent a vacancy already read. Any profile edit would
  have re-delivered a batch of them.

### Measured
Six real vacancies through the live prompt and policy layer: 6/6 correct. Roles
whose *content* fit is high but which demand an unattainable nationality are now
blocked on eligibility alone; advertisements offering a permit route still pass.

### Notes
- Changing any profile field changes `profile_version`, which re-queues every
  live job. An operator who does not want a re-scoring sweep can carry existing
  evaluations forward to the new version, so the new rule governs what arrives
  from then on without recomputing stored verdicts.
- 161 tests, up from 153.

## [1.8.0] — 2026-09-03

One prompt clause, and no code change beyond it. The evaluator was rating jobs
notifiable whose subject matter it had *already identified* as outside the
candidate's evidence.

### Fixed
- **A job whose core work is outside candidate evidence is now a hard blocker.**
  The failure was not comprehension. On an industrial inline-vision role the
  evaluator named the position accurately as a vision and inspection system
  ownership role, listed the specialist camera and calibration background as a
  gap — and then scored it notifiable anyway, because that gap was one bullet
  among three positives. The same shape appeared on a role built on a language
  the candidate had never used, and on an information-management role that was
  not software engineering at all. The prompt now asks for that judgement as a
  `hard` blocker, which `classify_decision` has always suppressed. No schema,
  database or policy change was needed.

### Measured
Four probe vacancies held constant while their batch neighbours varied, scored
through the real policy layer:
- The guard fired on every role that deserved it, and on **none** of ten stored
  strong/good matches, so it does not cost real opportunities.
- Decisions were correct in every probe evaluation across three identical runs.
- On one probe the blocker itself fired in two of three runs, but the decision
  was right regardless because the score also sat well below the threshold. A
  dedicated boolean field was stable in all three and remains the upgrade path if
  that flicker ever costs a decision; it was not shipped because it needs a
  schema and a database column to buy an outcome this already achieves.

### Notes — two findings that matter more than the fix
- **The score is noisy; the judgement is not.** Re-running a byte-identical batch
  moved individual scores by up to 20 points. Changing the batch neighbours moved
  them no further. **There is no evidence of a batch-context effect** — an
  earlier hypothesis that scores were "batch-context dependent" was never tested
  and is withdrawn. It is ordinary sampling variance, concentrated on ambiguous
  vacancies: clear ones returned identical scores run after run. Instability is
  itself a signal of low confidence.
- **Scores stored by the previous model read about 20 points high.** Rows written
  before 1.7.1 are not comparable with later ones, and `backfill-report` will
  surface matches today's evaluator would not raise.

## [1.7.1] — 2026-09-03

Primary evaluator moved from Gemini 3.7 Flash to **Gemini 3.8 Flash**. No code
change was needed: 3.8 accepts the same `responseJsonSchema` and
`thinkingConfig`, so this is a one-line `secrets.env` edit.

### Changed
- `VERTEX_GEMINI_MODEL` is now `gemini-3.8-flash` in the templates and in the
  documents that describe the current system. Documents that record *past*
  benchmarks (`docs/provider-benchmark.md`, `docs/case-study.md`, and the 1.5.x
  entries below) still name 3.7, because that is what those benchmarks ran on.

### Evidence
- Frozen error-class benchmark: 3.8 scored 10/10 exact against expectations
  written before either model saw the cases; 3.7 scored 9/10, over-rating
  ordinary background screening as a strong match. No false negatives either way.
- Ten known-strong vacancies: both models kept all ten above the notify
  threshold.
- Latency: 26.8-43.9 s per batch of ten on 3.8 against 50.0-68.1 s on 3.7.

### Known open question
Re-evaluating already-scored vacancies produced materially different scores on
**both** models. Whether that is batch context (the evaluator scoring a job
relative to its neighbours) or ordinary sampling variance is **not yet
established**, and the two imply different fixes. Until it is measured, treat a
stored score as a band rather than a number.

## [1.7.0] — 2026-09-01

Observability for provider failures and for recovery runs.

### Fixed
- **The transport fallback now records *which* failure triggered it.** The
  warning was the same regardless of cause, and the underlying error was logged
  only when *both* providers failed, so a rate limit, a 503 and a socket
  timeout were indistinguishable afterwards. `TemporaryProviderError` now
  carries the HTTP status, the warning includes it, and a `Retry-After` header
  is surfaced when the provider sends one.
- **`backfill` now writes a `runs` row.** A recovery run consumed provider
  tokens without being recorded, so token and cost accounting for it was
  missing and the run counter never moved. It is recorded like any other run,
  with a `status` of success/partial/error, and its summary line reports tokens
  and fallbacks.

### Added
- `TemporaryProviderError.reason`, a short bounded label such as `http_429` or
  `transport_TimeoutError`, deliberately capped so an arbitrary provider message
  can never become a dictionary key in a stored run record.
- `RunStats.fallback_reasons`, counting those labels, and `RunStats.mode`
  (`live` or `backfill`) so the two kinds of run are distinguishable in
  `stats_json`.
- `evaluate_with_fallback(..., stats=...)`, optional so existing callers are
  unaffected.
- 14 new tests (133 → 147): status capture through the HTTP path, permanent
  statuses staying non-temporary, reason counting, double-failure recording,
  bounded keys, and backfill run accounting for success, partial, dry-run and
  empty-backlog cases.

### Notes
- A `backfill --dry-run`, and a run with nothing left to evaluate, still record
  nothing. Only a run that actually consumes provider tokens is written.

## [1.6.0] — 2026-09-01

Historical recovery mode. The live pipeline, the live selector and scheduled
behaviour are unchanged.

Activating live mode freezes already-discovered but unevaluated jobs behind the
live cutoff: they are never assessed, and under live-mode selection never would
be. This release adds an isolated recovery path for them rather than weakening
or reusing live-mode behaviour.

### Added
- **`backfill`** — evaluates open historical jobs the live selector skips.
  Selects only jobs that are unevaluated for the current `profile_version`,
  historical relative to `live_since`, still open today in the market timezone,
  and pass the deterministic prefilter. Ordered by urgency: deadline ASC,
  discovery score DESC, published/first-seen DESC. Same evaluator, same policy
  layer, same batch size of 10, same primary/transport-fallback routing, same
  missing-ID semantics and one bounded cleanup pass, same
  `max_candidates_per_run` ceiling, same run lock.
  **It never writes notification state**, so a recovery run cannot consume a
  match that has not been delivered. `--dry-run` shows the selection, the
  urgency tail and cost estimates without a provider call or a write.
- **`backfill-status`** — a read-only, free breakdown of the historical
  backlog: unevaluated, still open, expired, closing today/tomorrow/3 days/7
  days, evaluated, and matches awaiting delivery.
- **`backfill-report`** — the delivery phase. Emits one bounded page of stored
  historical matches and marks **only the entries it actually printed**, so a
  truncated page is retried rather than silently lost. Repeat until it reports
  completion.
- `market_today()` and `MARKET_TIMEZONE`. Platsbanken deadlines are Swedish
  calendar dates, so comparing them against the UTC date keeps a vacancy that
  closed at midnight alive for the last hours of the UTC day. Falls back to UTC
  where no tz database exists, which is lenient rather than strict.
- `prefilter_reason()`, one deterministic exclusion predicate now shared by
  discovery and recovery. `should_prefilter` delegates to it; behaviour is
  identical.
- 24 new tests (109 → 133).

### Changed
- `Database.unnotified()` takes `historical=`, so live delivery and historical
  delivery draw from disjoint sets. The live selector never evaluates a
  historical job, so the live queue could only ever contain live rows; this
  matters the moment a recovery run starts producing historical evaluations,
  which must not leak into the daily alert and drown genuinely new vacancies.

### Notes
- `live_since` is never modified. Recovery reads around it; it does not move it.
- Expired historical jobs are never selected, so they are never paid for.
- Historical jobs are kept after recovery. They are useful evidence.

## [1.5.3] — 2026-09-01

Release hardening. No change to how vacancies are discovered, evaluated, ranked
or delivered.

### Fixed
- **Candidate language proficiency is read from the profile rather than
  hardcoded in the engine.** The system prompt and the deterministic policy
  layer contained a literal candidate language level. The level now comes from
  `matcher_profile.json` → `constraints.<language>`, and every explanation is
  generated from it. Configure a different level and the policy follows, with no
  code change.
- **Fresh-clone installation now works.** `install.sh` expected files that a
  clean clone does not ship, and failed with `install: cannot stat`. Missing
  configuration and profile files are now created from the shipped
  `*.example.json` templates.
- **Re-installing no longer overwrites your files.** Existing configuration,
  profile and `secrets.env` are kept. `--refresh-config` opts into overwriting
  config and profile; `secrets.env` is never touched either way. A missing
  template now fails loudly instead of creating empty configuration.

### Added
- Deterministic language-level normalisation: an ordered CEFR scale
  (`none < A1 < A2 < B1 < B2 < C1 < C2`) with aliases for
  beginner/intermediate/advanced/professional/fluent/native, case-insensitive
  and tolerant of surrounding prose. Unknown is a distinct state that never
  satisfies a requirement and is never treated as `none`.
- A documented threshold for what satisfies "professional/fluent" language
  requirements: **C1**. B2 is penalised with a strong blocker rather than hard
  blocked; B1 and below are hard blocked; unknown stays unknown.
- `doctor` distinguishes the onboarding states — file missing, example template
  still unedited, credentials missing, ready — and reports
  `candidate_swedish_level`, `unedited_example_profiles`, `missing_credentials`,
  `ready` and `next_steps`. It still makes no provider calls.
- `install.sh --refresh-config` and `--help`.
- 36 new tests (73 → 109) covering language-level parsing and ordering, policy
  outcome at every level, prompt generation, fresh-clone installation,
  non-overwrite on re-install, and the doctor states.

### Notes
- Swedish detection patterns are unchanged; they are application policy, not
  candidate configuration.
- The example profile still ships `"swedish": "A2, progressing"` so the sample
  benchmark keeps demonstrating a genuine language blocker. That value is
  example data — change it and the behaviour changes with it.

## [1.5.2] — 2026-09-01

Completeness over punctuality.

### Changed
- **Processes the complete frozen candidate snapshot before delivery.** The
  invariant moved from "process up to N jobs per run" to "evaluate the complete
  frozen relevant snapshot before reporting". The candidate set is taken once
  per run and never re-queried, so a vacancy discovered mid-run belongs to the
  next run and this run stays auditable.
- **Keeps semantic request batches at 10.** Batch size stays small and bounded
  because that is where provider completeness was validated; throughput now
  comes from running as many sequential batches as the snapshot needs.
- **Removes the per-run throughput cap.** `max_batches_per_run` is gone.
  `max_candidates_per_run` (default 300, cap 500) and `max_run_seconds`
  (default 3000) are emergency valves, not throughput caps: one bounds snapshot
  memory, the other bounds wall clock.
- **Globally ranks matches after all batches complete.** Delivery happens only
  once semantic processing has finished, so the strongest opportunity is
  reported first regardless of which batch produced it.
- **Separates healthy queued work from provider failures.** The run summary now
  distinguishes `N queued for next run` from `N pending after provider error`.
- **Removes the redundant match header** in favour of the single closing
  RoleLens summary line. The cards already speak for themselves.

### Added
- **One bounded cleanup pass** for IDs a provider omitted, run once after the
  normal pass in fresh batches. Never recursive, and skipped entirely if the
  provider already failed during this run.
- **Explicit candidate and runtime safety limits.** When the candidate ceiling
  truncates a snapshot the run says so in its summary, because a partial view of
  the market must never be reported as a complete one.
- Internal `error_kind`, separating three outcomes that were previously
  conflated: `output` (envelope unparseable), `completeness` (envelope fine,
  some requested IDs missing) and `transport` (both providers failed).
- 20 new tests (53 → 73) covering snapshot completeness, completeness gaps,
  transport failure, ranking and delivery ordering, the runtime budget and the
  safety ceiling.

### Fixed
- **Continues after incomplete but usable provider responses.** A short batch no
  longer aborts the batches after it: its valid rows are saved and processing
  continues. Only an unparseable envelope or both providers failing stops a pass.

### Upgrading
- Remove `max_batches_per_run` from your `config.json`; it is no longer read.
- Raise your scheduler's per-task timeout above `max_run_seconds` plus one
  `gateway_timeout_seconds` of reserve — at least 3600 seconds with the
  defaults. See the README.

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
