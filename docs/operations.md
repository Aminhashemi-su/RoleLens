# Operations

How to validate an installation, run RoleLens on a schedule, keep its sources
healthy, control cost, and recover the historical backlog. For how it works
inside, see [architecture.md](architecture.md).

Every command below is safe to run by hand; the ones that cost money say so.

## Validate in four stages

Run these in order after installing or changing configuration. Each stage adds
one capability, so a failure tells you which layer is wrong.

```bash
# 1. Local configuration only. No network, no cost.
python3 rolelens.py doctor

# 2. Discovery only. Zero model cost. Logs on stderr, nothing on stdout.
python3 rolelens.py --verbose fetch

# 3. Inspect local state. Free.
python3 rolelens.py status

# 4. Ranking and evaluation. The first step that calls a provider and costs money.
python3 rolelens.py --verbose evaluate
```

`doctor` fails loudly when configuration, profile files or provider credentials
are missing, and reports the discovery sources, ranking setup, first-read model,
catalogue years and budget it will use.

## Why stdout matters

- **stdout** carries only the user-facing report: cards, alert lines, and one
  summary line.
- A run with **no match and no problem prints nothing**. A scheduler that
  delivers stdout sends nothing; cron sends no mail.
- **stderr** carries operational logging. Redirect it to a file if you want a
  history.
- A **non-zero exit** means the run failed and should be surfaced.

## Scheduling

Any scheduler that can run a script works. Two to four runs a day is plenty;
JobStream picks up exactly where the last run stopped.

The scheduled time means *start discovery now*, not *deliver at exactly this
time*. Give the job a timeout above `max_run_seconds` plus
`gateway_timeout_seconds` (3600 seconds with the defaults).

**cron**

```cron
30 7,15 * * *  /usr/bin/python3 $HOME/.local/scripts/rolelens.py 2>>$HOME/rolelens.log
```

**systemd**

```ini
# ~/.config/systemd/user/rolelens.service
[Service]
Type=oneshot
TimeoutStartSec=3600
ExecStart=/usr/bin/python3 %h/.local/scripts/rolelens.py run

# ~/.config/systemd/user/rolelens.timer
[Timer]
OnCalendar=*-*-* 07,15:30:00
Persistent=true

[Install]
WantedBy=timers.target
```

**Hermes Agent**

Install with `ROLELENS_SCRIPTS_HOME="$HOME/.hermes" ./install.sh`, then:

```bash
hermes cron create "30 7,15 * * *" --no-agent --script rolelens.py --deliver telegram --name rolelens
hermes cron run <job_id>     # one run now
```

Hermes runs the script without arguments (RoleLens treats that as `run`), does
not pass it provider credentials (RoleLens reads its own `secrets.env`),
delivers non-empty stdout verbatim, sends nothing for empty stdout, and turns a
non-zero exit into an error alert. Its default script timeout of 3600 seconds
(`cron.script_timeout_seconds`) fits the default `max_run_seconds`. See the
README section *Running with Hermes Agent*.

Confirm the server's timezone before choosing hours.

## Live mode

A fresh installation is in **bootstrap** mode: everything discovered and
selected is eligible. After the first runs, freeze the backlog:

```bash
python3 rolelens.py activate
```

Future runs select only ads first seen or changed from then on. Jobs discovered
before the cutoff are not lost; [historical recovery](#historical-recovery)
reaches them.

## Keeping sources healthy

`status` shows `jobs_by_source`; a run's `stats_json` in the `runs` table
records stream entries, JobSearch hits, career sites read and failed, and
postings stored.

| Symptom | What to do |
|---|---|
| `⚠️ RoleLens: discovery failed for career site <name>` | Run `--verbose fetch` and read that site's error. A 404 usually means the board id or URL changed; a parse failure on SuccessFactors means the site was redesigned. Fix or set `"enabled": false`. |
| `discovery failed for JobStream` | Usually a passing outage. The cursor did not move, so the next run replays the window. |
| A career site stores nothing | Its postings may all be outside Sweden, duplicates of Platsbanken ads, or waiting for detail requests within `career_site_max_details`. `--verbose fetch` shows posting counts. |
| Cursor clamped warning | The server was down longer than `jobstream_max_window_hours`. Ads changed in the gap were not replayed; JobSearch queries can cover part of it. |

## Ranking and cost controls

Cost is bounded by configuration, not by trust in the provider:

- `evaluate_top_share`, `explore_share` — how much of the market reaches a model.
  Raising the share trades money for recall.
- `triage_model` — the first read settles clear rejections cheaply. Empty sends
  every selected ad to the judge.
- `monthly_budget_usd` — judging pauses at the estimated month-to-date spend.
  `status` shows the estimate. Ranking keeps running; nothing queued is lost.
- `max_jobs_per_batch` — jobs per judge call; ten is where completeness was
  validated.
- `max_candidates_per_run`, `max_run_seconds` — emergency valves, not throughput
  limits. When one bites, the run says so.
- `max_notifications_per_run` — caps report size; undelivered matches wait.

`fetch`, `status`, `doctor`, `backfill-status` and any `--dry-run` never call a
provider.

Changing `matcher_profile.json`, `role_vocabulary.json` or the embedding model
re-ranks the recent window (a few cents). Changing the matcher profile, rules or
catalogue also changes `profile_version`, which re-queues every selected live ad
for judging.

## Failure semantics

| Situation | Behaviour |
|---|---|
| Provider omits some requested IDs | Valid results are saved; missing IDs get one bounded cleanup pass, then stay pending |
| Provider transport failure or account refusal | One fallback attempt for that batch |
| Both providers fail | Run stops cleanly; the batch and everything after it stay pending |
| Unparseable provider response | Run stops; the batch and everything after it stay pending |
| Embeddings unavailable | Ranking falls back to the vocabulary at the wider share; alert |
| Emergency runtime budget reached | Remaining jobs stay pending and are reported as queued |
| Candidate safety ceiling reached | Run is explicitly reported as incomplete, remainder queued |

Nothing is ever deleted or silently marked evaluated.

## Candidate language level and eligibility

The Swedish level and the eligibility answers are read from
`matcher_profile.json` → `constraints`, never from code. Set them to your real
situation; the policy layer uses them to decide whether a stated requirement is
a hard blocker, and every explanation is generated from them. Leaving
`swedish_citizenship` out keeps citizenship demands as *unknown*.

## Historical recovery

`activate` freezes the pre-cutoff backlog, and live selection will never reach
it. Recovery is an isolated path. It does not move `live_since`, does not change
live selection, and does not alter scheduled behaviour. Only ads that ranking
selected are recovered.

### Phase A — evaluate

```bash
python3 rolelens.py backfill-status        # free, read-only breakdown
python3 rolelens.py backfill --dry-run     # free, shows selection and cost estimate
python3 rolelens.py --verbose backfill     # one snapshot, costs money
```

Repeat `backfill` until `backfill-status` reports
`historical_still_open_pending: 0`. Each run takes the run lock, so run it
between scheduled ticks. **Nothing is delivered in this phase.**

### Phase B — deliver

```bash
python3 rolelens.py backfill-report
```

Emits one bounded page of stored historical matches and marks **only what it
printed**. Repeat until it reports completion.

| | |
|---|---|
| Move `live_since` | never |
| Select a live job | never |
| Select or pay for an expired job | never |
| Write notification state during `backfill` | never |
| Mark more delivered than it printed | never |

## Upgrading from 1.x

1. Pull, then re-run `./install.sh`. It adds `role_vocabulary.json` and
   `knowledge_catalogue.json` from the examples without touching your files.
   Replace both with your own, or delete the catalogue.
2. Merge the new keys from `config.example.json` into `config.json`.
3. `doctor`, `--verbose fetch`, `status`, `--verbose evaluate`.

The database migrates in place on first open. Rows older than
`ranking_reference_days` are never ranked and so never selected, which means a
live installation does not re-judge its history.

## Tests

```bash
python3 -m unittest discover -s tests
```

216 tests, no network and no paid API calls, on Linux, macOS and Windows. The
installer tests need a POSIX shell and a filesystem that honours `chmod`; they
skip themselves otherwise.

Before publishing a change, also run the safety scanner, ideally with a file of
private literals kept outside the repository:

```bash
python3 tools/check_public_safety.py . --terms ~/.rolelens-private-terms
```
