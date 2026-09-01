# Operations

How to validate an installation, run RoleLens on a schedule, and recover the
historical backlog. For architecture see [architecture.md](architecture.md).

RoleLens is a single script with no third-party runtime dependencies. Every
command below is safe to run by hand; the ones that cost money say so.

## Validate in four stages

Run these in order after installing. Each stage adds one capability, so a
failure tells you exactly which layer is wrong.

```bash
# 1. Local configuration only. No network, no cost.
python3 rolelens.py doctor

# 2. Discovery only. Zero LLM cost. Logs on stderr, nothing on stdout.
python3 rolelens.py --verbose fetch

# 3. Inspect local state. Free.
python3 rolelens.py status

# 4. Semantic evaluation. The first step that calls a provider and costs money.
python3 rolelens.py --verbose evaluate
```

`doctor` fails loudly when configuration, profile files or provider credentials
are missing or incomplete. It never contacts a provider.

## Why stdout matters

RoleLens separates the two streams deliberately, so it can be driven by any
scheduler that captures output:

- **stdout** carries only the user-facing report: match cards, then exactly one
  summary line.
- **stderr** carries operational logging.
- A non-zero exit means the run failed and should be surfaced, rather than a
  silent loss of jobs.

Every evaluating run ends with one summary line, so a healthy run is visible
rather than silent.

## Live mode

A fresh installation is in **bootstrap** mode: everything discovered is
eligible for evaluation. Once you are satisfied with the configuration:

```bash
python3 rolelens.py activate
```

This freezes the historical backlog behind a cutoff (`live_since`) and switches
future runs to new or changed vacancies only. It is what stops a first
scheduled run from evaluating months of accumulated postings at once.

Jobs discovered before that cutoff are not lost. They are simply outside live
selection, and [historical recovery](#historical-recovery) exists to reach them.

## Scheduling

RoleLens is a plain script, so any scheduler works — cron, systemd timers, or a
task runner that captures stdout. Two runs a day is a reasonable starting
point.

The scheduled time means *start discovery now*, not *deliver at exactly this
time*. A run evaluates its whole frozen candidate snapshot before ranking and
reporting, so a large snapshot takes longer and that is intended. Give the job
a generous timeout — well above the longest run you expect — and set
`max_run_seconds` in `config.json` **below** that external timeout so RoleLens
exits cleanly and still delivers its report instead of being killed mid-run.

## Failure semantics

| Situation | Behaviour |
|---|---|
| Provider omits some requested IDs | Valid results are saved; missing IDs get one bounded cleanup pass, then stay pending |
| Provider transport failure (timeout, 429, 5xx) | One fallback attempt for that batch, then stop cleanly |
| Unparseable provider response | Run stops; the batch and everything after it stay pending |
| Emergency runtime budget reached | Remaining jobs stay pending and are reported as queued |
| Candidate safety ceiling reached | Run is explicitly reported as incomplete, remainder queued |

Nothing is ever deleted or silently marked evaluated. A job without a valid
evaluation stays in the queue for the next run.

## Token controls

Cost is bounded by configuration, not by trust in the provider:

- `max_jobs_per_batch` — jobs per provider call. Ten is validated; raising it
  trades completeness reliability for fewer calls.
- `max_candidates_per_run` — an emergency ceiling on one run's snapshot, not a
  throughput limit. If it is reached while more candidates are eligible, the
  run says so rather than presenting a truncated snapshot as complete.
- `max_run_seconds` — emergency wall-clock budget.
- `max_notifications_per_run` — caps report size; undelivered matches wait.

`fetch`, `status`, `doctor`, `backfill-status` and any `--dry-run` never call a
provider and never cost anything.

## Candidate language level

Language proficiency is read from `matcher_profile.json` under
`constraints.<language>`, not hardcoded. Set it to your real level: the
deterministic policy layer uses it to decide whether a stated language
requirement is a hard blocker, and every explanation is generated from it.

## Historical recovery

`activate` freezes the pre-cutoff backlog, and live selection will never reach
those jobs. Recovery is an isolated path for them. It does not move
`live_since`, does not change live selection, and does not alter scheduled
behaviour.

It is deliberately two phases, so an interactive session can never consume a
match that was never delivered.

### Phase A — evaluate

```bash
python3 rolelens.py backfill-status        # free, read-only breakdown
python3 rolelens.py backfill --dry-run     # free, shows selection and cost estimate
python3 rolelens.py --verbose backfill     # one snapshot, costs money
```

Repeat `backfill` until `backfill-status` reports
`historical_still_open_pending: 0`. Each run takes the run lock, so run it
between scheduled ticks rather than against one.

**Nothing is delivered in this phase.** Evaluations are stored and matches
wait. Selection covers only jobs that are unevaluated for the current
`profile_version`, historical relative to `live_since`, still open today in the
market timezone, and pass the deterministic prefilter — ordered by urgency
(deadline first).

### Phase B — deliver

```bash
python3 rolelens.py backfill-report
```

Emits one bounded page of stored historical matches and marks **only what it
printed**, so a truncated page is retried rather than lost. Repeat until it
reports completion.

For a large backlog, reading the stored matches directly out of SQLite is
faster than paging them. Both are safe; the paging exists so a scheduled path
stays bounded.

### What recovery will not do

| | |
|---|---|
| Move `live_since` | never |
| Select a live job | never — the selector excludes them structurally |
| Select an expired job | never — checked against the market-timezone date |
| Pay for an expired ad | never |
| Write notification state during `backfill` | never |
| Mark more delivered than it printed | never |
| Change scheduled behaviour | never |

## Tests

```bash
python3 -m unittest discover -s tests -v
```

147 tests, no network and no paid API calls. Requires Linux or macOS
(`fcntl`). One permission test skips itself on filesystems that cannot honour
`chmod`.
