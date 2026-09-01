# Benchmark

A reusable provider benchmark for RoleLens, plus a synthetic ten-case sample
set.

The design choice that makes this useful: **it scores the deterministic decision,
not the model's self-reported one.** RoleLens never trusts the `decision`
field a model returns — it re-derives the decision from validated scores and
blockers after a policy layer runs. A benchmark that scores raw model output is
measuring a component that does not ship.

`benchmark_runner.py` imports `normalize_evaluation_policy` and
`classify_decision` directly from `rolelens.py`, so the benchmark cannot
drift away from production behaviour.

## Requirements

Python 3.11+ on Linux or macOS. `rolelens.py` imports `fcntl`, so the runner
inherits that constraint. Nothing to install.

## Commands

### `dry-run` — free, no network

```bash
python3 benchmark/benchmark_runner.py dry-run
```

Builds both provider payloads for every batch and prints the batch layout, the
system-prompt hash, and a canonical SHA-256 of each batch's semantic input. Run
this first: identical semantic-input hashes are how you prove both providers saw
exactly the same thing.

### `run` — one provider, one call per batch

```bash
python3 benchmark/benchmark_runner.py run --provider gemini --out results/
python3 benchmark/benchmark_runner.py run --provider azure  --out results/
```

Credentials are read from `~/.rolelens/secrets.env` by default
(`--secrets PATH` to override) and are never printed or written to output.

Each run writes:

```text
results/
├── gemini.json               parsed evaluations + deterministic decisions
├── gemini.score.json         scored against the frozen expectations
└── raw/gemini/
    ├── batch_01.json         raw model content, usage, latency
    └── batch_02.json
```

There are no semantic retries. A provider that returns incomplete output is
scored on that output — that is the point.

### `score` — compare stored results

```bash
python3 benchmark/benchmark_runner.py score results/gemini.json results/azure.json
```

Prints a metric comparison table and a per-case deterministic-decision matrix.
`--out FILE` also writes the full report as JSON.

### `replay` — re-derive decisions after a policy change

```bash
python3 benchmark/benchmark_runner.py replay results/gemini.json
```

Re-runs the stored raw responses through the **current** policy layer, with no
provider calls and no cost. This is the highest-value command in the tool: run
it after every change to `normalize_evaluation_policy` or `classify_decision`.
It found two real Swedish-language bugs before deployment — see
[../docs/case-study.md](../docs/case-study.md), section 9.

## Scoring

| Metric | Meaning |
|---|---|
| `cases_returned` | How many requested IDs came back valid. Silent omission is the failure this catches. |
| `batches_structurally_valid` | Batches with no missing, duplicate, unexpected or invalid rows. |
| `false_negatives` | Expected a notification, the deterministic decision was `store_no_notify`. |
| `false_positives` | Expected no notification, got one. |
| `weighted_error` | `3 × false_negatives + 1 × false_positives`. A hidden good role costs far more than a wasted click. |
| `exact_decision_matches` | Deterministic decision equal to the expected decision, not just the same notify/no-notify class. |
| `mean_career_fit_range_error` | Average distance outside the expected `career_fit` band. Zero inside the band. |
| `mean_opportunity_range_error` | Same, for `opportunity_score`. |
| `reported_decision_disagreements` | Cases where the model's own decision differed from the deterministic one. High values mean the model's conclusions do not follow from its own evidence. |

Expected values are **ranges**, deliberately. The honest claim is "a competent
evaluator lands in this band", not "the correct answer is 87".

## The sample cases

`sample_cases.json` contains ten synthetic vacancies. Every company, URL and
description is fictional and written for this repository — no real advertisement
is redistributed here. Each case isolates an error class that changes what you
actually see:

| Case | Class | Expected decision |
|---|---|---|
| `SC-AI-PRODUCT-01` | Excellent applied-AI product match | `notify_strong` |
| `SC-SV-MANDATORY-01` | Great fit, fluent Swedish explicitly mandatory | `store_no_notify` |
| `SC-SV-PREFERRED-01` | Swedish preferred but explicitly not required | `notify_good` |
| `SC-SV-AD-ONLY-01` | Ad written in Swedish, team works in English | `notify_strong` |
| `SC-SEC-SCREEN-01` | Ordinary background screening only | `notify_good` |
| `SC-CITIZEN-REQ-01` | Explicit citizenship requirement, status unknown | `notify_verify` |
| `SC-DEVOPS-ADJACENT-01` | Adjacent DevOps/platform role | `store_no_notify` |
| `SC-STACK-SPECIALIST-01` | Specialist stack mismatch, similar title | `store_no_notify` |
| `SC-SENIORITY-MISMATCH-01` | Principal-level requirement | `store_no_notify` |
| `SC-UNRELATED-01` | Obvious reject, unrelated profession | `store_no_notify` |

Critical checks are declared per case:

| Code | Failure |
|---|---|
| A | An unknown fact was converted into an unmet requirement. |
| B | An explicit mandatory Swedish requirement was ignored. |
| C | A Swedish-language ad, or optional Swedish, was treated as mandatory. |
| D | Background screening was converted into a citizenship/clearance requirement. |
| E | `career_fit` was reduced by a practical blocker that belongs in `opportunity_score`. |
| F | An obvious mandatory seniority or specialist-stack blocker was ignored. |

`example_results.json` is an **illustrative** scored report showing the output
shape. It was produced by running hand-written model evaluations through the
real policy layer, not by calling a provider, and its numbers are not a
published benchmark result. Real results are in
[../docs/provider-benchmark.md](../docs/provider-benchmark.md).

## Using your own cases

```bash
cp benchmark/sample_cases.json benchmark/my_cases.json
# replace the `job` objects; write `expected` ranges BEFORE running anything
python3 benchmark/benchmark_runner.py --cases benchmark/my_cases.json dry-run
```

Two rules worth keeping:

1. **Freeze expectations before you call a provider.** Writing them afterwards
   is not a benchmark, it is a description.
2. **Do not commit a third-party job corpus.** `benchmark_results/` and any
   `raw/` directory are gitignored for this reason. Job advertisement text
   belongs to whoever published it.
