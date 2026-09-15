# RoleLens

**Find work that fits, beyond the job title.**

RoleLens is a lightweight semantic job-discovery agent for the Swedish market.
It reads every new vacancy on Platsbanken and on the company career sites you
choose, ranks them against your profile, and lets an LLM judge only the ones
worth reading — then a deterministic policy layer decides what reaches you.

Keyword alerts fail in both directions. They flood you with vacancies that
happen to contain the word "engineer", and they hide the role that describes
exactly your job under a title you never thought to search for. RoleLens reads
the advertisement instead of matching its title.

```text
Platsbanken (JobStream + JobSearch)      Company career sites
                  \                         /
                   fetch, normalise, store (SQLite)
                              ↓
          rank every new ad against your profile   ← no model tokens spent
          (role vocabulary + embeddings + JobTech enrichment)
                              ↓
          top share only → deterministic screening  ← no model tokens spent
                              ↓
          optional cheap first read → LLM judge
                              ↓
          deterministic policy layer → ranked notifications
```

It runs as one scheduled Python script on a small VPS. No agent loop, no
browser automation, no vector database, no Redis, no Docker, no queue broker,
and no third-party Python package at runtime.

---

## What it looks like

RoleLens evaluates the actual work behind a vacancy, explains why it may fit,
highlights gaps and uncertainties, and sends the strongest opportunities to your
preferred channel. These are real cards, delivered to Telegram by
[Hermes Agent](#running-with-hermes-agent).

### Strong match

<img src="docs/images/rolelens-strong-match.jpg" width="420">

Both scores high: the work itself fits, and nothing in the advertisement stands
in the way.

### Fit with gaps and uncertainty

<img src="docs/images/rolelens-nuanced-match.jpg" width="420">

The same shape of output, judged honestly. The work still fits, but the gaps are
named and an eligibility question is flagged as *unknown* rather than guessed at
— which is why `opportunity_score` sits below `career_fit`.

---

## Two scores, not one

A single "match score" cannot answer the only two questions that matter, because
they have different answers.

**`career_fit` — is this the kind of work I want?**
How well the actual day-to-day work matches your evidence-backed capabilities,
judged independently of whether you can practically get this particular job.
Language requirements, location, citizenship, clearance and seniority never
reduce `career_fit`.

**`opportunity_score` — is this specific vacancy worth pursuing?**
What is left after the practical reality of *this* advertisement: mandatory
language requirements, seniority, specialist stack gaps, location, security
eligibility, deadlines.

Keeping them apart is what makes the output useful. A role with
`career_fit 95 / opportunity_score 30` tells you something precise and
actionable — this is exactly your work, but something in the advertisement
blocks you — and that is information a single blended number destroys.

---

## How a job becomes a notification

1. **Discovery — free.** Three sources, each optional:
   - **JobStream** returns every Platsbanken ad added or changed since the last
     run, so no vacancy is missed for using a word you did not search for.
   - **JobSearch** keyword queries reach the open backlog a fresh installation
     has never seen.
   - **Career sites.** Built-in collectors read the public feeds of Teamtailor,
     Varbi, Greenhouse, Lever, Ashby, SmartRecruiters, Workday and SAP
     SuccessFactors sites. Many employers publish vacancies there that never
     reach Platsbanken. See [docs/career-sites.md](docs/career-sites.md).

   A failing source is reported and the others still run.
2. **Normalisation and persistence.** Each ad becomes a job record, upserted
   into SQLite under a `UNIQUE(source, source_job_id)` key. A SHA-256 hash of
   the meaningful fields decides whether an ad has genuinely changed. A
   career-site posting already stored from Platsbanken is left out.
3. **Ranking — no model tokens.** Every new or edited ad is scored three ways:
   a weighted role vocabulary built from your profile, the embedding similarity
   between the ad and the closest section of your matcher profile, and the
   competencies Arbetsförmedlingen's JobAd Enrichments API finds the ad
   requesting. Reciprocal rank fusion combines them, and only the top share of
   recent ads (15% by default) goes on — plus a small random sample of the rest,
   so a ranking miss is still found and visible. If embeddings fail, ranking
   falls back to the vocabulary with a wider share and says so.
4. **Snapshot selection.** Selected ads without an evaluation for their current
   content and profile version are pending. The run takes them **once**, as a
   frozen snapshot, and suppresses reposts of vacancies it has already judged.
5. **Deterministic screening — no model tokens.** Ads the rules would block
   anyway — a management title or years of experience far beyond your dated
   roles, a student role in a full-time search, a nationality you do not hold,
   mandatory Swedish above your level — are stored with that reason.
6. **First read (optional).** A cheap model reads the rest with a compact
   candidate card and settles only confident rejections well below a match.
   Anything it passes, doubts or fails to answer goes to the judge, so it can
   save money but never cost a match.
7. **Semantic evaluation.** The judge evaluates in batches of at most 10 with a
   JSON-schema-constrained output contract. Every requested ID must come back
   exactly once; a short batch is a completeness gap with one bounded cleanup
   pass. An ad whose score lands near the card line is judged a second time and
   decided on the mean, which halves run-to-run noise where it matters.
8. **Deterministic policy layer.** The model's own `decision` field is never
   trusted. Narrow, auditable rules are applied to the model output and the
   decision is derived from validated scores and blockers.
9. **Notification.** Only after every batch has completed are matches ranked
   **globally**, deduplicated and printed to stdout, followed by one compact
   summary line. A run with no match and no problem prints nothing at all.
   Operational logs go to stderr.

There is no auto-apply. RoleLens tells you what to look at; you decide what to
do about it.

---

## Quick start

Requires Python 3.11+. There is nothing to `pip install`. The installer needs a
POSIX shell (Linux, macOS, WSL); the script itself also runs on Windows.

```bash
git clone https://github.com/Aminhashemi-su/RoleLens.git
cd RoleLens
```

**1. Install.** This creates `~/.rolelens/` and fills in anything that is
missing from the shipped examples. It never overwrites a file you already have.

```bash
./install.sh
```

```text
~/.rolelens/
  config.json                        <- sources, career sites, limits
  secrets.env                        <- provider credentials, mode 600
  profile/matcher_profile.json       <- sent to the model; its sections rank jobs
  profile/role_vocabulary.json       <- weighted role terms that rank jobs
  profile/knowledge_catalogue.json   <- dated roles and skill levels (optional)
  profile/career_profile.json        <- your local evidence base, never sent anywhere
  profile/search_lenses.json         <- documents your search intent
  profile/matcher_rules_v1_1.json    <- the scoring contract
  data/
~/.local/scripts/rolelens.py
```

Override either location with `ROLELENS_HOME` and `ROLELENS_SCRIPTS_HOME`.
Re-running the installer refreshes `rolelens.py` and leaves everything else
alone; `./install.sh --refresh-config` opts into overwriting config and profile
from the checkout, and still never touches `secrets.env`.

**2. Make the profile yours.** Everything installed above describes a fictional
example candidate, so RoleLens will happily match jobs for someone who does not
exist until you edit it.

- `matcher_profile.json` matters most. It is sent to the model on every run, and
  its descriptive sections (`candidate_core`, `strong_capabilities`,
  `role_families_to_recognize_semantically`, …) are what ads are ranked against.
  List work you do not want under `out_of_scope_work`.
- `role_vocabulary.json` holds weighted terms — English and Swedish — for the
  work you do. Build it from your own profile, not from ads you have already
  judged. Negative weights mark professions you cannot do.
- `knowledge_catalogue.json` is optional. Its dated `professional_experience`
  entries let the engine settle years of experience and seniority itself; skills
  marked `not_evidenced` are treated as absent.
- Set your Swedish level and, if you want the engine to decide nationality
  requirements, your eligibility — see [Language proficiency](#language-proficiency)
  and [Work eligibility](#work-eligibility).

**3. Add provider credentials** to `~/.rolelens/secrets.env` (mode 600). The
Vertex key pays for the judge, the embeddings and the optional first read.

**4. Check the installation, then spend money deliberately.**

```bash
python3 ~/.local/scripts/rolelens.py doctor             # config only, no network, no cost
python3 ~/.local/scripts/rolelens.py --verbose fetch    # discovery only, zero model cost
python3 ~/.local/scripts/rolelens.py status             # local counters, per source
python3 ~/.local/scripts/rolelens.py --verbose evaluate # first step that calls a provider
```

`doctor` reports `unedited_example_profiles` while any profile file is still the
shipped example, `missing_credentials` until `secrets.env` is filled in, the
discovery sources and ranking it will use, and `ready: true` once both are done.

**5. Switch from backlog to live.** The first runs store a backlog. When you are
ready to see only new or changed vacancies:

```bash
python3 ~/.local/scripts/rolelens.py activate
```

---

## Running on a schedule

RoleLens is a plain script: a scheduler runs it, and whatever it prints is the
report. Run with no arguments it does a full `run`. Two to four runs a day is
plenty.

```cron
30 7,15 * * *  /usr/bin/python3 $HOME/.local/scripts/rolelens.py 2>>$HOME/rolelens.log
```

A quiet run prints nothing, so cron sends no mail and a chat scheduler sends no
message. Matches, alerts (a failing source, a provider refusing the account, the
monthly budget) and a non-zero exit always produce output.

The scheduler's per-task timeout must exceed `max_run_seconds` plus one
`gateway_timeout_seconds` — at least 3600 seconds with the defaults. A run killed
from outside never marks its notifications, so the work is simply repeated.

### Running with Hermes Agent

[Hermes Agent](https://hermes-agent.nousresearch.com/) has a cron scheduler with
[script-only jobs](https://hermes-agent.nousresearch.com/docs/guides/cron-script-only):
no LLM is involved, the script runs on a timer, and its stdout is delivered
verbatim to Telegram, Discord, Slack or Signal. That is exactly RoleLens's
contract, and it is how the cards above were delivered.

**1. Install into the Hermes scripts folder.** Hermes only runs cron scripts
from `~/.hermes/scripts/`:

```bash
ROLELENS_SCRIPTS_HOME="$HOME/.hermes" ./install.sh
```

Configure `~/.rolelens/` as in the quick start, and check it by hand:

```bash
python3 ~/.hermes/scripts/rolelens.py doctor
python3 ~/.hermes/scripts/rolelens.py --verbose run
```

**2. Create the job.**

```bash
hermes cron create "30 7,15 * * *" --no-agent --script rolelens.py --deliver telegram --name rolelens
```

`--deliver telegram` sends to your Telegram home channel; use
`telegram:<chat_id>` for a specific chat or group.

**3. Operate it.**

```bash
hermes cron list            # find the job id
hermes cron run <job_id>    # one run now, to see it deliver
hermes cron pause <job_id>
hermes cron resume <job_id>
```

What makes this work reliably:

- **No arguments.** Hermes runs the script without arguments, which RoleLens
  treats as `run`.
- **Its own credentials.** Hermes does not pass provider credentials to cron
  scripts. RoleLens reads `~/.rolelens/secrets.env` itself.
- **Silence is a feature.** Hermes delivers nothing for empty stdout, and
  RoleLens prints nothing when there is no match and no problem.
- **Failures are loud.** A non-zero exit becomes a Hermes error alert; partial
  provider failures exit 0 and say so in the summary line instead.
- **Timeout.** Hermes allows script jobs 3600 seconds by default
  (`cron.script_timeout_seconds`), which fits the default `max_run_seconds` of
  3000. Raise both together if you raise one.
- **One run at a time.** A run that overlaps a slow previous one exits quietly on
  the run lock.

Historical recovery (`backfill`, `backfill-report`) is meant to be run by hand,
not scheduled.

---

## CLI

| Command | What it does |
|---|---|
| `run` | Discover, rank, evaluate, emit new matches. The default. |
| `fetch` | Discovery and persistence only. No model call, no cost. |
| `evaluate` | Rank and evaluate already-stored jobs only. |
| `doctor` | Validate local configuration and credentials without network calls. |
| `status` | Database counters: jobs per source, ranking state, pending, month-to-date cost. |
| `activate` | Freeze the historical backlog; future runs see only new/changed jobs. |
| `backfill`, `backfill-status`, `backfill-report` | Historical recovery — see [docs/operations.md](docs/operations.md). |

Global flags: `--home PATH` (default `~/.rolelens`, or `ROLELENS_HOME`),
`--verbose`, `--version`.

Exit codes: `0` success or recoverable partial, `2` expected operational failure
(bad configuration, permanent provider failure, every discovery source failed),
`1` unexpected failure, `130` interrupted.

---

## Configuration

`config.json` — see `config.example.json` for a working starting point. Unknown
keys are ignored; invalid values fail `doctor` with a message naming the key.

**Discovery**

| Key | Meaning |
|---|---|
| `use_jobstream` | Read the complete Platsbanken change feed. Default `true`. |
| `jobstream_lookback_hours`, `jobstream_max_window_hours` | First-run replay window, and the widest window ever requested after downtime. |
| `search_terms`, `location_terms`, `include_unlocated_searches` | JobSearch keyword queries, cross-producted. Empty `search_terms` turns JobSearch off. |
| `search_limit`, `query_delay_ms` | Hits per query (max 100), and the pause between requests. |
| `career_sites` | Company career sites to read. See [docs/career-sites.md](docs/career-sites.md). |
| `career_site_max_details` | Detail pages fetched per site per run, on platforms whose listing has no description. |
| `preferred_locations`, `northern_exclusions` | Tie-break order, and locations dropped unless the role is fully remote. |

**Ranking and screening**

| Key | Meaning |
|---|---|
| `evaluate_top_share` | Share of recent ads the evaluator reads (default 0.15). |
| `degraded_top_share` | Share used when embeddings were unavailable (default 0.30). |
| `explore_share` | Random share of the rest judged anyway (default 0.03). |
| `use_enrichment` | Use JobTech's enrichment as a third ranking order. |
| `ranking_reference_days` | Window of recent ads a percentile is taken over. |
| `embedding_model`, `embedding_batch_size` | Vertex embedding model and request size. |
| `exclude_student_roles` | Treat internships, theses and student jobs as out of scope. |

**Judging, cost and limits**

| Key | Meaning |
|---|---|
| `triage_model` | Cheap first-read model, e.g. `gemini-2.5-flash-lite`. Empty sends every ad to the judge. |
| `triage_thinking_budget`, `triage_batch_size` | First-read thinking tokens (0 or 512–24576) and ads per call. |
| `monthly_budget_usd` | Estimated spend per UTC month after which judging pauses; ranking and delivery continue. |
| `max_candidates_per_run` | **Emergency ceiling** on snapshot size (default 300, cap 500). |
| `max_jobs_per_batch` | Jobs per judge call (default and cap 10). |
| `max_run_seconds` | **Emergency** wall-clock budget (default 3000). |
| `max_notifications_per_run` | Cards emitted per run; the rest wait for the next. |
| `*_timeout_seconds`, `http_retries` | Transport limits. |

`max_candidates_per_run` and `max_run_seconds` are **emergency valves, not
throughput caps**. A normal run reaches neither; when one bites, the run says so
explicitly and the remainder stays queued.

### Language proficiency

The candidate's proficiency lives in `matcher_profile.json`, never in the code:

```json
"constraints": { "swedish": "A2, progressing" }
```

Accepted values are CEFR levels (`none`, `A1`–`C2`) or plain words that map onto
them: `beginner`/`basic` → A1, `intermediate` → B1, `upper intermediate` → B2,
`advanced`/`professional`/`fluent` → C1, `native` → C2. An explicit CEFR token
always wins, so `"A2, working towards fluent"` is read as A2.

When an advertisement **explicitly requires** professional, fluent or advanced
Swedish, the policy layer resolves it against your configured level:

| Your level | Outcome |
|---|---|
| C1, C2, fluent, native | **met** — no blocker |
| B2 | **partial** — strong blocker, `opportunity_score` capped at 69 |
| B1, A2, A1, none | **unmet** — hard blocker, `opportunity_score` capped at 49 |
| missing or unparseable | **unknown** — no blocker, no cap, never "unmet" |

None of this applies to a merely *preferred* Swedish requirement, or to an
advertisement that simply happens to be written in Swedish.

### Work eligibility

Four optional fields let the policy layer tell a nationality requirement from a
right-to-work requirement an existing permit already satisfies:

```json
"constraints": {
  "swedish_citizenship": "no",
  "eu_citizenship": "yes",
  "permanent_residence": "no",
  "work_permit": "yes"
}
```

**Leaving `swedish_citizenship` out is a real choice.** While it is absent, an
explicit citizenship demand is preserved as `unknown` and the role is still
delivered for you to check. Once you answer, the engine decides:

| Advertisement says | With the profile above |
|---|---|
| "requires Swedish citizenship" | **hard blocker** |
| "citizenship may be required for vetting" | **hard blocker** |
| "Swedish citizen **or** valid EU work permit" | **met** — a permit route is offered |
| "we cannot offer visa sponsorship" | **met** — the permit is already held |

Ordinary background screening is never treated as a citizenship requirement.

### Seniority from the knowledge catalogue

With a dated `knowledge_catalogue.json`, the engine counts your years of
experience (overlapping roles once) and reads the advertisement's own words:

| Advertisement | Outcome |
|---|---|
| Requires 2+ more years of experience than you have | hard blocker |
| Requires a little more than you have | capped at a stretch |
| Senior, lead, principal or architect title, under 5 years | capped at a stretch |
| Head of, director, chief or team-leader title, under 5 years | hard blocker |
| Years demanded in one named field you have not worked in | capped at a stretch, never removed |
| Junior, graduate or "early career" wording | none of the above |

Without a catalogue, nothing here decides anything.

---

## Architecture

Full detail is in [docs/architecture.md](docs/architecture.md); day-to-day
operation, scheduling and historical recovery are in
[docs/operations.md](docs/operations.md). The short version:

- **Retrieval** — JobStream, JobSearch and career-site collectors; no model.
- **Persistence** — one SQLite file: `jobs` (with ranking columns), `evaluations`,
  `notifications`, `runs`, `job_fingerprints`, `profile_embeddings`, `meta`.
- **Ranking** — role vocabulary, profile embeddings and JobTech enrichment fused
  with reciprocal rank fusion; the profile is embedded once per version.
- **Provider routing** — Gemini is primary. Azure is called at most once per
  batch when Gemini is unreachable, rate-limited, temporarily failing or refuses
  the account. A successful HTTP 200 with malformed content never triggers it.
- **Deterministic policy layer** — the part that decides.
- **Completeness** — a run evaluates its whole frozen snapshot, ranks globally
  and delivers once; pending-ness is derived from state, so an interrupted run
  leaves the queue correct.

### Why the model does not get the last word

The model returns scores and blockers. RoleLens then applies narrow, auditable
rules before deriving the decision itself:

- An advertisement *written* in Swedish is not a Swedish-language requirement;
  "Swedish is a merit" is not a blocker; an explicit "Swedish is not required"
  overrides any mandatory-sounding phrase.
- Background screening is not a citizenship or clearance requirement, and an
  unresolved explicit requirement stays **unknown** — never silently *unmet*.
- Nationality and seniority are settled from the profile and the catalogue.
- Any genuinely unmet mandatory requirement becomes a hard blocker, and a hard
  blocker always suppresses notification.

| Decision | Condition |
|---|---|
| `store_no_notify` | any hard blocker, or nothing else matches |
| `notify_verify` | a genuine unknown eligibility blocker, `career_fit ≥ 85`, `opportunity_score ≥ 55` |
| `notify_strong` | `opportunity_score ≥ 85` |
| `notify_good` | `opportunity_score ≥ 70` |
| `notify_stretch` | `opportunity_score ≥ 60` and `career_fit ≥ 75` |

---

## Cost

Discovery is free. Ranking embeds each new ad once (gemini-embedding-001, about
ten cents per thousand ads) and the matcher profile once per version. The optional
first read on Flash-Lite costs a fraction of a cent per ad and, in calibration,
settled about three quarters of the ads that reached it without losing a match.
The judge reads what is left.

`status` and every run record carry an estimated month-to-date spend, priced
from recorded tokens at the dearer provider's rates so it errs high. When it
reaches `monthly_budget_usd`, judging pauses and says so; ranked jobs wait and
nothing is lost.

---

## Tests

```bash
python3 -m unittest discover -s tests
```

216 tests. No network, no API key, no cost. They cover the decision classifier,
the language, citizenship and seniority rules, provider routing and fallback,
snapshot completeness and cleanup passes, ranking maths and the ranking
pipeline, embedding and enrichment clients, the monthly budget, each career-site
collector against recorded payload shapes, discovery failure isolation, repost
suppression, historical recovery, fresh-clone installation, and `doctor`.
[GitHub Actions](.github/workflows/tests.yml) runs them on Linux, macOS and
Windows; the installer tests need a POSIX shell and skip themselves elsewhere.

---

## Benchmark

[`benchmark/`](benchmark/) contains a reusable provider benchmark and a synthetic
ten-case sample set covering the error classes that matter here. The runner
scores the **deterministic decision**, not the model's self-reported one, by
replaying every provider response through the same policy functions production
uses. Methodology and results are in
[docs/provider-benchmark.md](docs/provider-benchmark.md); how that approach found
two real bugs is in [docs/case-study.md](docs/case-study.md).

---

## Security and privacy

- **Nothing personal is committed.** Your `config.json`, profile files, the
  database and `secrets.env` are all in `.gitignore`. The repository ships only
  `*.example.json` templates describing a fictional candidate.
- **Credentials live in one mode-600 file**, are sent in request headers, never
  in a URL, and never logged; URLs are logged without their query string.
  RoleLens refuses a secrets file other users can read.
- **Your CV never leaves your machine.** `career_profile.json` is never read by
  the pipeline. `matcher_profile.json` (and the catalogue, without its
  `candidate` identity section) is sent to your model provider; its descriptive
  sections are also sent to the Vertex embedding endpoint and, with
  `use_enrichment`, to Arbetsförmedlingen's public JobAd Enrichments API. You
  choose what goes in it.
- **Job data goes to a third-party model.** Vacancy text is sent to your
  configured providers. That is the whole design; mind their data-retention terms.
- **Public sources only.** Discovery reads public APIs and public career-site
  feeds over HTTPS, one site at a time with a pause between detail pages, and
  never logs in, submits forms or bypasses access controls.
- **Provider responses are archived locally** under `data/` for debugging. They
  are gitignored; delete them if you do not want them.
- **No auto-apply, no outbound messages to employers.** RoleLens prints to stdout.

---

## Upgrading from 1.x

Version 2 reuses your home directory and database; the schema migrates in place.

1. Pull and re-run `./install.sh`. It adds `role_vocabulary.json` and
   `knowledge_catalogue.json` from the examples without touching your files —
   **replace both with your own** (or delete the catalogue).
2. Merge the new keys from `config.example.json` into your `config.json`
   (`use_jobstream`, `career_sites`, ranking and budget keys). Existing
   `search_terms` keep working; `high_signal_title_terms` is no longer read.
3. Run `doctor`, then `--verbose fetch`, then `--verbose evaluate`.

Behaviour that changed: a run with no match and no problem now prints nothing;
a stretch card needs `career_fit ≥ 75`; only ranked-and-selected ads are
evaluated; the Azure fallback also answers when Vertex refuses the account.
See [CHANGELOG.md](CHANGELOG.md).

---

## Project status

Version **2.0.0**. It grew out of a personal job search in Sweden and runs
daily in production for that search. It is published because the design is
more broadly interesting, not as a product.

It is deliberately narrow:

- One market (Sweden) and one candidate profile per installation.
- One primary provider and one fallback; routing is fixed in code.
- Ranking constants and first-read thresholds were calibrated on one profile's
  data; they are sensible defaults, not universal truths.
- Adjacent and specialist roles are still the weakest judgment area.
- Output is stdout only; delivery is whatever your scheduler does with it.

Contributions and forks are welcome under the MIT licence. See
[CHANGELOG.md](CHANGELOG.md) for version history.

## Licence

MIT — see [LICENSE](LICENSE). The licence covers this code, its documentation
and its synthetic example data. It does not grant rights to job advertisement
content retrieved at runtime, which belongs to its respective owners.
