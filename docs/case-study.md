# Case study: building RoleLens

How a small scheduled script that reads job advertisements ended up being mostly
a lesson about not trusting model output.

---

## 1. The problem

Job board alerts match keywords. That fails in both directions at once.

The false positives are obvious and merely annoying: every vacancy containing
"engineer" arrives, including the ones for control-system specialists and iOS
developers. You learn to skim and discard, and the alert becomes noise you stop
reading.

The false negatives are the expensive failure. The most interesting role in a
search period is regularly the one advertised as *Forward Deployed Engineer*, or
*Implementation Engineer*, or *Systemutvecklare* — a title you did not think to
search for, describing work that is exactly what you do. Keyword alerts hide it,
and you never find out it existed.

Both problems have the same root cause: the title and the keyword list are not
the job. The description is the job.

## 2. Product goal

**Understand what the person would actually do, then decide.**

Read the advertisement, infer the day-to-day work, compare it against
evidence-backed capabilities, and separately assess whether this particular
vacancy is practically worth pursuing. Notify only when both hold. Never apply
to anything automatically — the output is a short ranked list of things worth
looking at, delivered once a day.

## 3. Constraints

The constraints were fixed before any code was written, and they eliminated most
of the architectures one might reach for by default:

- **Low operating cost.** A few cents per day. This is a personal tool competing
  with a free email alert; it has to justify itself.
- **A small always-on VPS.** No Kubernetes, no managed queue, no autoscaling.
- **No vector database.** The corpus is a few hundred fresh advertisements at a
  time, not a knowledge base. Semantic retrieval infrastructure would be pure
  overhead.
- **No unnecessary services.** Every additional moving part is something that can
  break silently at 08:30 while you are not watching.
- **Scheduled autonomous operation.** One unattended tick a day, delivering to a
  chat channel. Nobody is going to notice a stack trace.
- **No third-party Python packages at runtime.** Dependency drift on a VPS you
  touch once a month is a real failure mode.

The last constraint sounds like asceticism, but it is really about the second:
a script with no dependencies cannot break because something upstream released a
new major version.

## 4. Initial architecture

The first working version was almost embarrassingly plain:

```text
JobSearch API  →  in-memory list  →  one LLM call  →  print matches
```

Discovery against the Arbetsförmedlingen JobSearch API is free and consumes no
LLM tokens, so it could be aggressively over-inclusive: many small queries,
broad terms, Swedish and English, several geographies. Filtering conservatively
and letting the semantic layer decide was cheaper than trying to be clever about
retrieval.

That version worked exactly once per run and forgot everything afterwards. It
re-evaluated the same vacancies every day, paid for them every day, and notified
about them every day.

## 5. Why SQLite became the durable retry queue

The obvious fix for re-notification is a set of seen IDs. What actually got
built is a bit more than that, for reasons that only appear in production.

Four requirements arrived in quick succession:

1. **Idempotency.** A vacancy found by six different queries is one job.
2. **Change detection.** Employers *edit* advertisements. An advertisement that
   gains a mandatory Swedish requirement on Tuesday is a different job from the
   one evaluated on Monday.
3. **Profile versioning.** When the candidate profile changes, every stored
   evaluation is stale — but re-running discovery to get the jobs back would be
   absurd.
4. **Partial-failure recovery.** When a provider returns nine of ten
   evaluations, the tenth must not be lost, and the nine must not be paid for
   twice.

SQLite answers all four with keys rather than logic:

- `jobs UNIQUE(source, source_job_id)` gives idempotency.
- A content hash over the meaningful fields gives change detection.
- `evaluations UNIQUE(job_id, content_hash, profile_version)` gives both profile
  versioning and per-job evaluation memory.

The consequence is the design decision I would keep in any similar system:
**there is no `pending` column.** Pending is derived — a job is pending exactly
when no evaluation row exists for its current content hash and the current
profile version. A status column would need to be updated correctly on every
path, including the crash paths. A `LEFT JOIN ... WHERE e.id IS NULL` cannot
drift out of sync, and an interrupted run leaves the queue correct for free.

The retry queue is not a queue. It is the absence of a row.

## 6. `career_fit` versus `opportunity_score`

The single-score version of this tool produced outputs that were technically
accurate and practically useless. A role that is exactly your work but requires
fluent Swedish scores about 55. So does a mediocre role you could get tomorrow.
The number cannot distinguish "this is your job but there is a wall" from
"this is fine but unremarkable", and those demand completely different responses.

So the evaluation returns two scores:

- **`career_fit`** — how well the actual work matches evidence-backed
  capabilities, deliberately *ignoring* practical blockers. Language, location,
  citizenship, clearance and seniority never touch it.
- **`opportunity_score`** — how worthwhile this specific vacancy is after the
  requirements in this specific advertisement.

`95/30` and `60/58` are both "do not notify", and they mean entirely different
things. The first says *this is your job, find out what the wall is*. The second
says *ignore this*.

Keeping the axes clean turned out to be surprisingly hard. Models want to blend
them: told that a role requires fluent Swedish, they quietly reduce the work-fit
score too. This became a tracked error class in the benchmark ("career fit
reduced because of a practical blocker"), and both benchmarked models still did
it on some cases. The separation is enforced in the prompt, in the rules file,
and — where it can be — in the deterministic layer.

## 7. Production failure modes

None of these were hypothetical.

**Malformed JSON.** Structured output is enforced natively by both providers.
Both still occasionally return content that does not parse, or an envelope with
no completion content at all. Every provider response is parsed defensively, and
raw content is archived to disk so a failure can be read afterwards rather than
guessed at.

**Missing job IDs.** The most consequential failure, because it is silent. A
provider returns HTTP 200, `finish_reason=stop`, valid JSON matching the schema —
and evaluations for eight of the ten jobs you asked about. Nothing errors. The
two missing vacancies simply never reach you. RoleLens therefore verifies
completeness independently of schema validation: every requested `source_job_id`
must come back exactly once, and duplicate, unknown or missing IDs are recorded
and left pending.

**Rate limiting.** A 429 is not a failure, it is a "later". It became a distinct
error type that produces a *partial* run: exit 0, jobs remain pending, the
summary line says how many are waiting. A cron alert for something that will fix
itself in an hour is worse than useless — it trains you to ignore alerts.

**Duplicate and reposted jobs.** Employers repost the same vacancy under a new
advertisement ID. ID-based deduplication does not catch it, so the same job gets
evaluated again and notified again. A fingerprint over normalized employer,
title, location and body catches reposts *before* they cost a model call, with a
second guard at notification time so one vacancy can never produce two cards.

**The silent tick.** Early versions printed nothing when there were no matches,
which is indistinguishable from a broken cron job. Now every evaluating run ends
with exactly one compact summary line. A quiet day says so out loud.

## 8. Provider benchmark

Three candidates were benchmarked on frozen real vacancies: Gemini 3.7 Flash,
Azure GPT-5 mini, and GLM via a gateway (the incumbent evaluator at the time).

**Round one — twenty jobs, two batches, no retries.**

GLM eliminated itself, and not on quality. Both HTTP calls returned 200 with no
error, and between them the two responses contained **3 of the 20 requested job
IDs**. Seventeen vacancies vanished into a successful-looking API response. That
is precisely the failure mode a job-discovery tool cannot tolerate: silent,
invisible, and indistinguishable from "nothing matched today". GLM was removed
from automatic routing.

Gemini and Azure both returned all 20 IDs across two structurally valid batches
with no duplicates. On aggregate weighted error, Azure edged Gemini (9 versus
10, weighting false negatives at 3× false positives). The margin was too small
to justify switching production on, and the two models failed in noticeably
different places — so instead of picking a winner, the outcome was a second,
targeted round.

**Round two — fourteen cases chosen to hit the error classes that matter.**

Seven frozen production snapshots plus seven controlled synthetic variants, in
two batches, with the expected label for every case frozen and hashed *before*
any provider was called. The classes: mandatory Swedish, Swedish preferred,
Swedish-language advertisement with no language requirement, background
screening, explicit citizenship requirement, specialist stack mismatch,
seniority mismatch.

Both providers again returned every requested ID in structurally valid batches.
On this set Gemini was clearly better on the error classes RoleLens actually
cares about:

| | Azure GPT-5 mini | Gemini 3.7 Flash |
|---|---:|---:|
| Deterministic false negatives | 2 | 1 |
| Deterministic weighted error | 8 | 5 |
| Hard-language-blocker recall | 0.67 | 1.00 |
| Mandatory-requirement extraction recall | 0.69 | 0.93 |
| Pairwise ranking accuracy | 0.78 | 0.93 |
| Mean latency per batch | **31.1 s** | 43.0 s |
| Estimated cost | **$0.018** | $0.049 |

Azure won latency and cost — by a wide margin — and lost on the thing that
matters: it missed a genuinely good role, and it mishandled an explicit
mandatory Swedish requirement, contradicting its own hard blocker in the final
decision. For a tool whose entire value is not hiding good roles, false
negatives are weighted at 3×, and that settled it.

The other conclusion from round two was negative and saved real complexity: **do
not add Azure as a verifier.** Both models made the *same* two consequential
false-positive judgments and both missed the same case after deterministic
thresholds. A verification pass would have doubled cost and latency while
correcting none of the observed errors. Azure stayed as a transport-only
fallback, which is what it is genuinely good for.

## 9. The lesson that mattered most

Model benchmarking alone was not enough. What the model *reports* is not what
the system *does*.

RoleLens does not trust the model's own `decision` field. It derives the
decision deterministically from validated scores and blockers, after a policy
layer that applies narrow, auditable rules to the model's output. So a benchmark
that scores raw model responses is measuring the wrong artefact.

Replaying the stored benchmark responses through the **real** policy layer —
the actual `normalize_evaluation_policy` and `classify_decision` functions,
not a reimplementation — changed the ranking of several cases. `reported →
deterministic` disagreements showed up on 4 of Azure's cases and 2 of Gemini's:
roles the model called `notify_good` that the system would correctly suppress,
and one Gemini `notify_verify` that fell below the verify threshold once its
career-fit score was validated.

More importantly, the replay found **two real bugs in the Swedish-language
rules**, before deployment, that no amount of reading model scores would have
revealed:

1. **Negation was not handled.** An advertisement stating `kunskaper i svenska
   krävs inte` — *Swedish is not required* — still matched a mandatory-Swedish
   pattern elsewhere in the text and produced a false hard blocker. The system
   was silently discarding roles that had explicitly said the blocker did not
   apply. Fixed by adding `SWEDISH_NEGATION_PATTERNS`, which override every
   mandatory pattern.

2. **A word-order variant was missing.** `svenska i tal och skrift` was matched;
   the equally common `tal och skrift på svenska` was not. A genuinely mandatory
   Swedish requirement written the second way slipped through as no requirement
   at all.

One bug caused false negatives, the other caused false positives, and both lived
in a layer that had passing unit tests. They were only visible when real model
output met the real policy code on real advertisement text.

The generalisable version: **if your system post-processes model output, your
evaluation harness must run that post-processing.** Benchmarking the model in
isolation measures a component you do not ship. Both bugs are now covered by
tests in `SwedishLanguagePolicyTests`, and the public benchmark runner scores
deterministic decisions rather than reported ones, on purpose.

## 10. Current architecture

```text
Scheduler (script-only, no agent)
        |
        v
rolelens.py
        |
        +--> Arbetsförmedlingen JobSearch      [discovery, $0, 0 tokens]
        |
        +--> SQLite                            [idempotency, history, pending state]
        |
        +--> Gemini 3.7 Flash                  [semantic evaluation, primary]
        |        |
        |        +--> Azure GPT-5 mini         [one transport-only fallback]
        |
        +--> deterministic policy layer        [the part that decides]
        |
        +--> stdout                            [cards + one summary line]
                |
                v
             chat delivery
```

A run evaluates its complete frozen snapshot in sequential batches of at most
ten, ranks globally, and delivers once. The batch size stays at ten because that
is where provider completeness was actually validated. How that came to be the
invariant is the next section.

## 11. Changing the invariant: completeness over punctuality

The version that came out of the benchmark work had a throughput design that
looked responsible: up to 40 candidates per run, in at most four sequential
batches of ten, inside a 600-second budget. Bounded, predictable, cheap. Every
number had a defensible reason.

Production data showed the reasoning was wrong, and in a way that only appears
once real volume arrives.

**The failure was ordinal, not numeric.** The pending queue is ordered by a
cheap deterministic discovery score, which is a decent proxy for relevance and
nothing more. On a busy day the queue exceeded the per-run cap, and everything
past it was deferred to tomorrow. That is fine if the deferred jobs are the weak
ones. They are not reliably: discovery score is computed before any model has
read the advertisement, so the single strongest opportunity in a day could sit
at position 45 and simply not be looked at. It would eventually be evaluated —
the durable queue guarantees that — but "eventually" is the wrong word for a job
posting with an application deadline.

The bug was not in any function. It was in the product invariant.

> **Old:** process up to N jobs per run.
> **New:** evaluate the complete frozen relevant snapshot before reporting.

Three things had to change together for that to be safe.

**A short batch had to stop being a failure.** The old pass treated any error
from a batch as a reason to stop the run, on the sensible-sounding grounds that
a misbehaving provider should not burn the remaining budget. But the most common
"error" was a schema-valid response that returned eight of ten requested IDs —
a normal quirk of constrained generation, not a broken provider. Conflating the
two meant one omitted ID could defer everything behind it. Splitting the outcome
into three (`completeness`, `output`, `transport`) let the run continue through
the first and preserve work only for the other two.

**Omitted IDs needed exactly one more attempt.** Not a retry loop — one bounded
cleanup pass over the unresolved IDs, re-batched, after the normal pass, and
skipped entirely if the provider had already failed. Bounded and non-recursive
was the constraint; anything else reintroduces the runaway-cost risk that the
original cap existed to prevent.

**Ranking had to move to the end.** Ranking per batch was invisible when a run
was one or two batches; across a whole snapshot it would have delivered "the
best of batch one" rather than "the best of today". Matches are now ranked
globally, after every batch including cleanup, and delivered once.

The old limits did not disappear — they were **demoted**. `max_candidates_per_run`
and `max_run_seconds` are still there, raised to 300 and 3000, but they are
emergency valves rather than throughput caps: one bounds snapshot memory, the
other bounds wall clock. The distinction is not cosmetic. A cap silently
truncates and reports success; a valve, when it trips, makes the run say so —
`⚠️ RoleLens: candidate safety ceiling reached … additional jobs remain queued`.
A partial view of the market must never be reported as a complete one.

The batch stayed at ten. Nothing in this change made bigger prompts safer, and
the benchmark evidence for completeness at that size was the one number worth
keeping.

The general lesson: **bounded execution and complete results are different
goals, and it is easy to write code that optimises the first while believing it
is protecting the second.** A per-run cap is an execution bound wearing the
costume of a safety limit. The honest version bounds the expensive unit — the
individual model call — and lets the number of units follow the work.

## 12. What remains imperfect

**Adjacent and specialist roles.** The weakest area by a distance. Both
benchmarked models over-notified on roles that are adjacent to the candidate's
profile — a solutions-engineering position requiring five years of unproven
pre-sales experience, a front-end role needing deep framework-internals
expertise. The model sees familiar technology names and overweights them. This
is currently absorbed by accepting some false positives, which is the right
trade for this tool but is not a fix.

**Unknown tenure evidence.** "Five years of X required" against a profile that
does not state years is genuinely unknown, and both models tend to resolve it as
*unmet*. The policy layer converts a subset of these back to unknown, keyed on
explicit no-evidence phrasing, but that heuristic is narrower than the problem.

**Deduplication tuning.** The repost fingerprint is strict — employer, title,
location and body must all match after normalization — so a reposted
advertisement with a lightly reworded body still gets re-evaluated and costs a
model call.

**The emergency valves are untested at their limits.** A snapshot large enough
to trip the 300-candidate ceiling or the 3000-second budget has not occurred in
production. The behaviour is covered by tests, but tests are not the same as
having seen it happen.

**The candidate's language level is in the code.** The Swedish CEFR level appears
in the system prompt and the policy layer rather than being read from the
profile. It works for one installation and is wrong as a general design.

## 13. Lessons learned

**Derive state, do not store it.** The absence of an evaluation row is a better
pending queue than a status column, because it cannot be updated incorrectly.

**A successful HTTP response is not a successful result.** The most dangerous
provider failure returned 200, valid JSON, matching schema — and 15% of the
requested data. Validate completeness against what you asked for, always,
independently of any schema the provider claims to enforce.

**Do not retry a semantic failure on a different provider.** A transport failure
is worth a fallback call. Malformed content is not: the second provider is
solving the same prompt and will likely fail the same way, at double the cost.
Separating "temporary transport failure" from "the model answered badly" removed
a whole class of expensive, useless retries.

**Two axes beat one number** when the axes answer different questions. The extra
field costs almost nothing and it is the difference between an output you act on
and an output you skim.

**Benchmark the system, not the model.** This is the one worth repeating. The
policy layer, the thresholds, the validation — that is what ships. Replaying
benchmark responses through the real deterministic code found two production
bugs that scoring model output alone would have carried into deployment.

**Make quiet runs loud.** A tool that prints nothing when nothing matched is
indistinguishable from a tool that is broken. One summary line per run is the
cheapest observability in the entire system.

**Do not warn about normal operation.** The same summary line originally
reported every unprocessed job as `pending retry`, warning icon included, on
runs where nothing had gone wrong at all. A warning that fires during healthy
operation is a warning that gets ignored, which costs you the one that matters.
Healthy queued work and provider failures now read differently on purpose.

**Bounded execution is not the same goal as complete results.** A per-run job
cap looks like a safety limit and behaves like an execution bound. Bound the
expensive unit — the individual model call — and let the number of units follow
the work.
