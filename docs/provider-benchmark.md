# Provider benchmark

How RoleLens chose its evaluator, what the numbers were, and how to run the
same benchmark yourself.

> **On the data.** The benchmark that produced these numbers ran against frozen
> real Swedish job advertisements. That corpus is third-party content and is not
> redistributed in this repository — cases are referred to by their error class,
> not by employer. The reusable runner and a ten-case **synthetic** sample set
> are in [`benchmark/`](../benchmark/), and the methodology below is exactly what
> the runner implements.

---

## What is being measured

Not "which model is smartest". The question is narrower and more useful:

**Which provider produces the fewest costly mistakes after RoleLens's
deterministic policy layer has had its say?**

That framing has three consequences that shape the whole methodology.

### 1. Errors are weighted asymmetrically

A false positive costs one wasted click. A false negative means a genuinely good
role was hidden and you never learned it existed. False negatives are therefore
weighted **3×**:

```text
weighted_error = 3 × false_negatives + 1 × false_positives
```

### 2. The scored artefact is the deterministic decision

RoleLens never trusts the model's own `decision` field. It re-derives the
decision from validated scores and blockers after a policy layer runs. So the
benchmark scores what the *system* would do, by replaying every provider
response through the real `normalize_evaluation_policy` and `classify_decision`
functions — imported from `rolelens.py`, not reimplemented.

Both the reported and the deterministic decision are recorded, and the
disagreement count between them is itself a reported metric.

### 3. Expected labels are frozen before any call

For each case, the expected `career_fit` range, `opportunity_score` range and
decision are written down and hashed *before* a provider is contacted. Ranges,
not point values — the honest claim is "a competent evaluator lands in this
band", not "the correct answer is 87".

---

## Error classes

Six critical checks, chosen because each one is a mistake that changes what you
see:

| Code | Failure |
|---|---|
| **A** | An unknown fact was converted into an unmet requirement. |
| **B** | An explicit mandatory Swedish requirement was ignored. |
| **C** | A Swedish-language advertisement, or optional Swedish, was treated as mandatory. |
| **D** | Ordinary background screening was converted into a citizenship or clearance requirement. |
| **E** | `career_fit` was reduced because of a practical blocker that belongs in `opportunity_score`. |
| **F** | An obvious mandatory seniority or specialist-stack blocker was ignored. |

A and D are the "unknown discipline" checks: a model that resolves uncertainty
into rejection quietly hides roles. C is the inverse: a model that treats a
Swedish-language advertisement as a Swedish-language *requirement* rejects
almost every Swedish vacancy. E is the two-score separation. B and F are the
opposite failure — notifying about something genuinely blocked.

---

## Fairness rules

Both providers received:

- The **same semantic input**, verified by hashing the serialized
  `{candidate, matching_rules, jobs}` payload per batch and comparing hashes
  across providers before any call.
- The **same system prompt**.
- **Native structured output** for their own API: `responseJsonSchema` for
  Gemini, `strict` `json_schema` response format for Azure.
- The **same batch composition** and the same batch size.
- **No semantic retries and no repair.** One primary call per batch, plus at
  most one retry for a transient transport failure. A model that returns
  incomplete output is scored on that output.

The dry-run stage verifies all of this without spending anything, and it is the
first thing the public runner does:

```bash
python3 benchmark/benchmark_runner.py dry-run
```

---

## Round one: broad comparison

Twenty frozen vacancies, two batches of ten, three providers: Gemini 3.7 Flash,
Azure GPT-5 mini, and GLM via a gateway (the incumbent evaluator at the time).

| Provider | IDs returned | Valid batches | False neg. | False pos. | Weighted error | Ranking accuracy | Latency | Est. cost |
|---|---:|---:|---:|---:|---:|---:|---:|---:|
| Gemini 3.7 Flash | 20/20 | 2/2 | 2 | 4 | 10 | 0.874 | 93.5 s | $0.069 |
| Azure GPT-5 mini | 20/20 | 2/2 | 1 | 6 | **9** | 0.888 | **68.3 s** | **$0.021** |
| GLM (gateway) | **3/20** | 0/2 | 4 | 0 | 12 | – | 28.7 s | $0.005 |

### The GLM result is the important one

GLM did not lose on judgment quality. It lost on reliability, in the worst
possible way:

- Both HTTP calls returned **200**.
- Both responses had `finish_reason=stop`.
- Both were valid JSON conforming to the schema.
- Between them they contained **3 of the 20 requested job IDs**.

Seventeen vacancies disappeared into a successful-looking API response with no
error anywhere in the stack. For a job-discovery tool this is the single worst
failure mode available: silent, invisible, and indistinguishable from "nothing
matched today". GLM was removed from automatic routing.

Its ranking and explanation figures are computed over only the three rows it
returned and are not comparable to the complete providers.

### Gemini versus Azure was inconclusive

Azure edged the aggregate (9 vs 10) with better ranking, richer explanations,
2.4× lower latency and 3.3× lower cost. But the margin was one weighted point,
and the two models failed in different places — Azure mishandled a mandatory
Swedish requirement that Gemini got right; Gemini suppressed work-fit too hard
on the same case. A one-point aggregate difference is not a basis for changing
production routing.

So round one produced a decision to run round two, not a winner.

---

## Round two: targeted calibration

Fourteen cases, two balanced batches of seven, Gemini and Azure only. Seven
frozen production snapshots plus seven **controlled synthetic variants** written
to isolate a single error class each. Expected labels frozen and SHA-256 hashed
before any call.

Case composition:

| Class | Cases | Checks |
|---|---:|---|
| Mandatory Swedish, high work fit | 2 | B, E |
| Swedish-language ad, English-speaking team | 1 | C |
| Swedish preferred / optional | 1 | C |
| Background screening only | 1 | A, D |
| Explicit citizenship or clearance | 3 | A, D, E |
| Specialist stack mismatch | 2 | D, F |
| Seniority mismatch | 1 | A, F |
| Adjacent customer-facing role | 1 | A, E, F |
| Strong match, English-only | 2 | – |
| Unresolved tenure claims | 1 | A, B, E, F |

### Results

| Metric | Azure GPT-5 mini | Gemini 3.7 Flash | Better |
|---|---:|---:|---|
| Valid requested IDs | 14/14 | 14/14 | tie |
| Structurally valid batches | 2/2 | 2/2 | tie |
| Deterministic false negatives | 2 | **1** | Gemini |
| Deterministic false positives | 2 | 2 | tie |
| **Deterministic weighted error** | 8 | **5** | **Gemini** |
| Career-fit range error | 4.21 | **2.21** | Gemini |
| Opportunity range error | 6.00 | **3.07** | Gemini |
| Pairwise ranking accuracy | 0.780 | **0.929** | Gemini |
| Hard-language-blocker recall | 0.667 | **1.000** | Gemini |
| Mandatory-requirement extraction recall | 0.689 | **0.933** | Gemini |
| Mandatory-requirement state accuracy | 0.556 | **0.756** | Gemini |
| Unknown-state preservation | **0.500** | 0.429 | Azure |
| Reported vs deterministic disagreements | 4 | **2** | Gemini |
| Mean latency per batch | **31.1 s** | 43.0 s | Azure |
| Total tokens | **20 751** | 25 302 | Azure |
| Estimated cost | **$0.018** | $0.049 | Azure |

### Reading the result honestly

Azure is faster and 2.7× cheaper, and it preserved unknown states slightly
better. It also:

- produced **two** deterministic false negatives to Gemini's one, including a
  strong forward-deployed engineering role it rejected on a title-based
  seniority inference;
- reached hard-language-blocker recall of only 0.67, contradicting its own hard
  blocker in the final decision on a mandatory-Swedish case;
- disagreed with its own deterministic outcome twice as often, i.e. its reported
  decisions were less consistent with its reported evidence;
- emitted a nonsensical `unknown` blocker labelled `None` on one case.

With false negatives weighted 3×, Gemini wins on the metric that expresses what
this tool is for. **Gemini 3.7 Flash became the primary evaluator**, at roughly
2.7× the cost of the alternative — which, at a few cents per day, is not a
constraint worth optimising against recall.

### The negative result that saved complexity

The obvious next idea is to run both models and use the second as a verifier.
The data says no:

- Both models made the **same two** consequential false-positive judgments.
- Both missed the **same** case once deterministic thresholds were applied.
- Azure did not correct Gemini's weaknesses; it shared them.

A verification pass would have added cost and latency while correcting none of
the observed routing errors. Azure remained a **transport-only** fallback:
called at most once per batch, and only when the primary fails with a transport,
timeout, rate-limit, unavailable or temporary 5xx error. It is never used to
second-guess a semantic answer.

---

## What both models still get wrong

Neither is good at these, and this is where the remaining error budget sits:

- **Adjacent and specialist roles.** Both over-notified on positions that share
  technology names with the candidate's profile but require depth the profile
  does not evidence — a solutions role wanting five years of unproven pre-sales
  experience, a front-end position needing framework-internals expertise.
- **Unknown tenure.** "Five or more years of X" against a profile that never
  states years is genuinely unknown. Both models frequently resolve it as
  *unmet*. The policy layer converts a narrow subset back to unknown; the
  heuristic is narrower than the problem.
- **Score-axis bleed (check E).** Both reduced `career_fit` in response to
  practical blockers on the same three cases, despite explicit instructions in
  the prompt and the rules file. Model output does not expose its internal
  decomposition, so this is an observed numeric violation rather than proof of
  cause.

---

## Reproducing this

The public runner implements the same methodology against the synthetic sample
set:

```bash
# Verify both providers would receive identical semantic input. No network.
python3 benchmark/benchmark_runner.py dry-run

# One provider, one call per batch, raw responses stored.
python3 benchmark/benchmark_runner.py run --provider gemini --out results/
python3 benchmark/benchmark_runner.py run --provider azure  --out results/

# Compare deterministic decisions against the frozen expectations.
python3 benchmark/benchmark_runner.py score results/gemini.json results/azure.json

# After changing the policy layer: re-derive decisions from stored responses.
python3 benchmark/benchmark_runner.py replay results/gemini.json
```

`replay` is the command that matters. It re-runs stored provider output through
the current policy code, with no provider calls and no cost. Running it after a
change to `normalize_evaluation_policy` is how two Swedish-language bugs were
caught before deployment — see [case-study.md](case-study.md), section 9.

To benchmark against your own vacancies, copy `benchmark/sample_cases.json`,
replace the `job` objects with advertisements you are entitled to store, write
expected ranges *before* running anything, and pass `--cases`. Do not commit a
third-party job corpus.
