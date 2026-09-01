#!/usr/bin/env python3
"""Provider benchmark for RoleLens, using the real deterministic policy layer.

The point of this runner is not to compare raw model scores. It is to compare
what RoleLens would actually *do* with each model's output, by replaying
every provider response through the same `normalize_evaluation_policy` and
`classify_decision` functions the production pipeline uses. Two providers can
report identical-looking scores and still produce different notifications.

Commands
--------
    benchmark_runner.py dry-run
        Build both provider payloads for every batch. No network, no cost.
        Prints the batch layout and a stable hash of the semantic input, so you
        can prove both providers saw exactly the same thing.

    benchmark_runner.py run --provider gemini --out results/
        Call one provider for every batch and store the raw response, the parsed
        evaluations, and the deterministic decisions.

    benchmark_runner.py score results/gemini.json [results/azure.json ...]
        Score stored results against the expected labels in sample_cases.json.

    benchmark_runner.py replay results/gemini.json
        Re-derive the deterministic decisions from stored evaluations without
        calling anything. Use this after changing the policy layer.

The sample cases in `sample_cases.json` are synthetic. Point `--cases` at your
own frozen set to benchmark against real vacancies you are allowed to store.
"""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import importlib.util
import json
import statistics
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CASES = Path(__file__).resolve().parent / "sample_cases.json"
NOTIFY_DECISIONS = {"notify_strong", "notify_good", "notify_stretch", "notify_verify"}
TRANSIENT_HTTP = {408, 425, 429, 500, 502, 503, 504}

# A missed good role costs far more than one unnecessary notification.
FALSE_NEGATIVE_WEIGHT = 3
FALSE_POSITIVE_WEIGHT = 1


# --------------------------------------------------------------------------
# Load the production module so the benchmark cannot drift from the real policy
# --------------------------------------------------------------------------
def load_rolelens(root: Path = REPO_ROOT) -> Any:
    module_path = root / "rolelens.py"
    if not module_path.exists():
        raise SystemExit(f"Cannot find {module_path}. Run this from the repository.")
    spec = importlib.util.spec_from_file_location("rolelens", module_path)
    if spec is None or spec.loader is None:  # pragma: no cover
        raise SystemExit(f"Cannot import {module_path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


cs = load_rolelens()


# --------------------------------------------------------------------------
# Inputs
# --------------------------------------------------------------------------
def read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def load_secrets(path: Path) -> dict[str, str]:
    """Read KEY=VALUE lines. Values are never printed or stored by this tool."""
    if not path.exists():
        raise SystemExit(f"Missing secrets file: {path}")
    secrets: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        secrets[key.strip()] = value.strip().strip('"').strip("'")
    return secrets


def load_cases(path: Path) -> dict[str, Any]:
    data = read_json(path)
    by_id = {case["source_job_id"]: case for case in data["cases"]}
    if len(by_id) != len(data["cases"]):
        raise SystemExit("Duplicate source_job_id in the case file")
    batches: list[list[dict[str, Any]]] = []
    for batch in data["batches"]:
        rows = []
        for case_id in batch:
            if case_id not in by_id:
                raise SystemExit(f"Batch references unknown case {case_id}")
            rows.append(by_id[case_id])
        batches.append(rows)
    listed = {cid for batch in data["batches"] for cid in batch}
    missing = set(by_id) - listed
    if missing:
        raise SystemExit(f"Cases not assigned to any batch: {sorted(missing)}")
    data["_by_id"] = by_id
    data["_batches"] = batches
    return data


def profile_bundle(root: Path, cases: Mapping[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    candidate = read_json(root / cases.get("candidate", "profile/matcher_profile.example.json"))
    rules = read_json(root / cases.get("rules", "profile/matcher_rules_v1_1.json"))
    return candidate, rules


# --------------------------------------------------------------------------
# Payload construction — identical semantic input for every provider
# --------------------------------------------------------------------------
def semantic_input(candidate: Mapping[str, Any], rules: Mapping[str, Any], batch: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    return {
        "candidate": candidate,
        "matching_rules": rules,
        "jobs": [dict(case["job"]) for case in batch],
    }


def canonical_hash(value: Any) -> str:
    blob = json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def gemini_payload(system_prompt: str, user_payload: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "systemInstruction": {"parts": [{"text": system_prompt}]},
        "contents": [
            {
                "role": "user",
                "parts": [{"text": json.dumps(user_payload, ensure_ascii=False, separators=(",", ":"))}],
            }
        ],
        "generationConfig": {
            "maxOutputTokens": 12_000,
            "responseMimeType": "application/json",
            "responseJsonSchema": cs.evaluation_schema(),
            "thinkingConfig": {"thinkingLevel": "MEDIUM"},
        },
    }


def azure_payload(model: str, system_prompt: str, user_payload: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "model": model,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": json.dumps(user_payload, ensure_ascii=False, separators=(",", ":"))},
        ],
        "stream": False,
        "response_format": {
            "type": "json_schema",
            "json_schema": {"name": "career_job_evaluations", "strict": True, "schema": cs.evaluation_schema()},
        },
        "reasoning_effort": "low",
        "max_completion_tokens": 12_000,
    }


def provider_spec(provider: str, secrets: Mapping[str, str]) -> dict[str, Any]:
    if provider == "gemini":
        model = secrets.get("VERTEX_GEMINI_MODEL") or ""
        key = secrets.get("VERTEX_GEMINI_API_KEY") or ""
        if not model or not key:
            raise SystemExit("VERTEX_GEMINI_API_KEY and VERTEX_GEMINI_MODEL are required")
        quoted = urllib.parse.quote(model, safe="")
        return {
            "model": model,
            "url": f"{cs.GEMINI_BASE_URL}/{quoted}:generateContent",
            "headers": {"x-goog-api-key": key},
        }
    if provider == "azure":
        model = secrets.get("AZURE_OPENAI_DEPLOYMENT") or ""
        key = secrets.get("AZURE_OPENAI_API_KEY") or ""
        base = secrets.get("AZURE_OPENAI_BASE_URL") or ""
        if not model or not key or not base:
            raise SystemExit("AZURE_OPENAI_API_KEY, AZURE_OPENAI_BASE_URL and AZURE_OPENAI_DEPLOYMENT are required")
        return {"model": model, "url": cs.azure_chat_url(base), "headers": {"api-key": key}}
    raise SystemExit(f"Unknown provider {provider!r}")


def build_payload(provider: str, model: str, system_prompt: str, user_payload: Mapping[str, Any]) -> dict[str, Any]:
    if provider == "gemini":
        return gemini_payload(system_prompt, user_payload)
    return azure_payload(model, system_prompt, user_payload)


# --------------------------------------------------------------------------
# Transport
# --------------------------------------------------------------------------
def call_provider(spec: Mapping[str, Any], payload: Mapping[str, Any], timeout: int) -> tuple[dict[str, Any], float]:
    body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    request = urllib.request.Request(
        spec["url"],
        data=body,
        headers={
            "Accept": "application/json",
            "Content-Type": "application/json",
            "User-Agent": "rolelens-benchmark/1.0",
            **dict(spec["headers"]),
        },
        method="POST",
    )
    started = time.monotonic()
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            raw = response.read()
    except urllib.error.HTTPError as exc:
        kind = "transient" if exc.code in TRANSIENT_HTTP else "permanent"
        raise SystemExit(f"Provider returned HTTP {exc.code} ({kind})") from exc
    except (urllib.error.URLError, TimeoutError, ConnectionError) as exc:
        raise SystemExit(f"Transport failure: {type(exc).__name__}") from exc
    latency = time.monotonic() - started
    return json.loads(raw.decode("utf-8")), latency


def extract(provider: str, response: Mapping[str, Any]) -> tuple[str, dict[str, int]]:
    if provider == "gemini":
        return cs.extract_gemini_response(response)
    return cs.extract_azure_response(response)


# --------------------------------------------------------------------------
# The part that matters: replay through the real policy layer
# --------------------------------------------------------------------------
def apply_policy(item: Mapping[str, Any], job: Mapping[str, Any], swedish: Any) -> dict[str, Any]:
    """Run one raw model evaluation through RoleLens's deterministic layer."""
    normalized = cs.normalize_evaluation_policy(item, job, swedish=swedish)
    decision = cs.classify_decision(
        cs.bounded_int(normalized.get("career_fit"), "career_fit"),
        cs.bounded_int(normalized.get("opportunity_score"), "opportunity_score"),
        normalized.get("blockers") or [],
    )
    return {
        "source_job_id": cs.clean_text(normalized.get("source_job_id")),
        "career_fit": cs.bounded_int(normalized.get("career_fit"), "career_fit"),
        "opportunity_score": cs.bounded_int(normalized.get("opportunity_score"), "opportunity_score"),
        "reported_decision": cs.clean_text(item.get("decision")) or None,
        "deterministic_decision": decision,
        "policy_changes": list(normalized.get("_policy_changes") or []),
        "must_have_assessment": normalized.get("must_have_assessment") or [],
        "blockers": normalized.get("blockers") or [],
        "language_risk": cs.clean_text(normalized.get("language_risk")),
        "seniority_risk": cs.clean_text(normalized.get("seniority_risk")),
        "actual_role": cs.clean_text(normalized.get("actual_role")),
    }


def parse_batch(content: str, batch: Sequence[Mapping[str, Any]], swedish: Any) -> dict[str, Any]:
    """Structural validation first, then the policy layer. Mirrors production."""
    expected = {case["source_job_id"]: case for case in batch}
    out: dict[str, Any] = {"evaluations": {}, "errors": [], "missing": [], "unexpected": [], "duplicates": []}
    try:
        parsed = json.loads(content)
    except json.JSONDecodeError as exc:
        out["errors"].append(f"invalid JSON: {exc}")
        out["missing"] = sorted(expected)
        return out
    items = parsed.get("evaluations") if isinstance(parsed, dict) else None
    if not isinstance(items, list):
        out["errors"].append("response missing evaluations array")
        out["missing"] = sorted(expected)
        return out

    seen: dict[str, int] = {}
    for item in items:
        if isinstance(item, dict):
            key = cs.clean_text(item.get("source_job_id"))
            if key:
                seen[key] = seen.get(key, 0) + 1
    out["duplicates"] = sorted(k for k, n in seen.items() if n > 1)

    for item in items:
        if not isinstance(item, dict):
            out["errors"].append("non-object evaluation")
            continue
        key = cs.clean_text(item.get("source_job_id"))
        if not key:
            out["errors"].append("evaluation missing source_job_id")
            continue
        if key not in expected:
            out["unexpected"].append(key)
            continue
        if key in out["duplicates"]:
            continue
        try:
            out["evaluations"][key] = apply_policy(item, expected[key]["job"], swedish)
        except Exception as exc:  # noqa: BLE001 - benchmark records, never crashes
            out["errors"].append(f"{key}: {exc}")
    out["missing"] = sorted(set(expected) - set(out["evaluations"]))
    out["unexpected"] = sorted(set(out["unexpected"]))
    return out


# --------------------------------------------------------------------------
# Scoring
# --------------------------------------------------------------------------
def range_error(value: int, bounds: Sequence[int]) -> int:
    low, high = int(bounds[0]), int(bounds[1])
    if value < low:
        return low - value
    if value > high:
        return value - high
    return 0


def score_results(cases: Mapping[str, Any], result: Mapping[str, Any]) -> dict[str, Any]:
    by_id = cases["_by_id"]
    evaluations: dict[str, Any] = result["evaluations"]
    rows: list[dict[str, Any]] = []
    false_negatives = 0
    false_positives = 0
    decision_matches = 0
    career_errors: list[int] = []
    opportunity_errors: list[int] = []
    reported_vs_deterministic = 0

    for case_id, case in by_id.items():
        expected = case["expected"]
        got = evaluations.get(case_id)
        if got is None:
            rows.append({"case": case_id, "status": "missing", "expected_decision": expected["expected_decision"]})
            if expected["expected_decision"] in NOTIFY_DECISIONS:
                false_negatives += 1
            continue
        want_notify = expected["expected_decision"] in NOTIFY_DECISIONS
        got_notify = got["deterministic_decision"] in NOTIFY_DECISIONS
        if want_notify and not got_notify:
            false_negatives += 1
        if got_notify and not want_notify:
            false_positives += 1
        if got["deterministic_decision"] == expected["expected_decision"]:
            decision_matches += 1
        ce = range_error(got["career_fit"], expected["career_fit_range"])
        oe = range_error(got["opportunity_score"], expected["opportunity_score_range"])
        career_errors.append(ce)
        opportunity_errors.append(oe)
        if got["reported_decision"] and got["reported_decision"] != got["deterministic_decision"]:
            reported_vs_deterministic += 1
        rows.append(
            {
                "case": case_id,
                "status": "ok",
                "label": case["label"],
                "expected_decision": expected["expected_decision"],
                "reported_decision": got["reported_decision"],
                "deterministic_decision": got["deterministic_decision"],
                "career_fit": got["career_fit"],
                "career_fit_range": expected["career_fit_range"],
                "career_fit_range_error": ce,
                "opportunity_score": got["opportunity_score"],
                "opportunity_score_range": expected["opportunity_score_range"],
                "opportunity_range_error": oe,
                "policy_changes": got["policy_changes"],
            }
        )

    evaluated = len(evaluations)
    return {
        "provider": result.get("provider"),
        "model": result.get("model"),
        "cases_total": len(by_id),
        "cases_returned": evaluated,
        "batches_structurally_valid": result.get("batches_valid"),
        "batches_total": result.get("batches_total"),
        "missing_ids": result.get("missing", []),
        "unexpected_ids": result.get("unexpected", []),
        "duplicate_ids": result.get("duplicates", []),
        "false_negatives": false_negatives,
        "false_positives": false_positives,
        "weighted_error": FALSE_NEGATIVE_WEIGHT * false_negatives + FALSE_POSITIVE_WEIGHT * false_positives,
        "exact_decision_matches": decision_matches,
        "mean_career_fit_range_error": round(statistics.fmean(career_errors), 3) if career_errors else None,
        "mean_opportunity_range_error": round(statistics.fmean(opportunity_errors), 3) if opportunity_errors else None,
        "reported_decision_disagreements": reported_vs_deterministic,
        "usage": result.get("usage"),
        "mean_latency_seconds": result.get("mean_latency_seconds"),
        "cases": rows,
    }


# --------------------------------------------------------------------------
# Commands
# --------------------------------------------------------------------------
def cmd_dry_run(args: argparse.Namespace) -> int:
    cases = load_cases(args.cases)
    candidate, rules = profile_bundle(args.root, cases)
    swedish = cs.candidate_language_level(candidate, "swedish")
    prompt = cs.semantic_system_prompt(swedish)
    print(f"cases file      : {args.cases}")
    print(f"candidate       : {cases.get('candidate')}")
    print(f"rules           : {cases.get('rules')}")
    print(f"candidate swedish: {swedish.label}")
    print(f"system prompt   : {len(prompt)} chars, sha256 {hashlib.sha256(prompt.encode()).hexdigest()[:16]}")
    print(f"batches         : {len(cases['_batches'])}")
    total_chars = 0
    for index, batch in enumerate(cases["_batches"], start=1):
        user_payload = semantic_input(candidate, rules, batch)
        blob = json.dumps(user_payload, ensure_ascii=False, separators=(",", ":"))
        total_chars += len(blob)
        gem = json.dumps(gemini_payload(prompt, user_payload), ensure_ascii=False)
        azu = json.dumps(azure_payload("<deployment>", prompt, user_payload), ensure_ascii=False)
        print()
        print(f"  batch {index}: {len(batch)} case(s)")
        print(f"    semantic input sha256 : {canonical_hash(user_payload)}")
        print(f"    semantic input chars  : {len(blob)}")
        print(f"    gemini envelope chars : {len(gem)}")
        print(f"    azure envelope chars  : {len(azu)}")
        for case in batch:
            print(f"      - {case['source_job_id']:<28} {case['label']}")
    print()
    print(f"total semantic characters: {total_chars}")
    print("no network calls were made")
    return 0


def cmd_run(args: argparse.Namespace) -> int:
    cases = load_cases(args.cases)
    candidate, rules = profile_bundle(args.root, cases)
    secrets = load_secrets(args.secrets)
    spec = provider_spec(args.provider, secrets)
    swedish = cs.candidate_language_level(candidate, "swedish")
    prompt = cs.semantic_system_prompt(swedish)

    merged: dict[str, Any] = {}
    errors: list[str] = []
    missing: list[str] = []
    unexpected: list[str] = []
    duplicates: list[str] = []
    usage_total = {"prompt_tokens": 0, "completion_tokens": 0, "reasoning_tokens": 0, "total_tokens": 0}
    latencies: list[float] = []
    valid_batches = 0

    out_dir = args.out
    raw_dir = out_dir / "raw" / args.provider
    for index, batch in enumerate(cases["_batches"], start=1):
        user_payload = semantic_input(candidate, rules, batch)
        payload = build_payload(args.provider, spec["model"], prompt, user_payload)
        response, latency = call_provider(spec, payload, args.timeout)
        latencies.append(latency)
        content, usage = extract(args.provider, response)
        write_json(raw_dir / f"batch_{index:02d}.json", {"content": content, "usage": usage, "latency_seconds": round(latency, 3)})
        for key in usage_total:
            usage_total[key] += int(usage.get(key) or 0)
        parsed = parse_batch(content, batch, swedish)
        if not parsed["errors"] and not parsed["missing"] and not parsed["unexpected"] and not parsed["duplicates"]:
            valid_batches += 1
        merged.update(parsed["evaluations"])
        errors.extend(parsed["errors"])
        missing.extend(parsed["missing"])
        unexpected.extend(parsed["unexpected"])
        duplicates.extend(parsed["duplicates"])
        print(f"batch {index}: {len(parsed['evaluations'])}/{len(batch)} evaluations, {latency:.1f}s", file=sys.stderr)

    result = {
        "schema_version": 1,
        "generated_at": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
        "provider": args.provider,
        "model": spec["model"],
        "candidate_swedish_level": swedish.label,
        "cases_file": str(args.cases),
        "batches_total": len(cases["_batches"]),
        "batches_valid": valid_batches,
        "evaluations": merged,
        "errors": errors,
        "missing": sorted(set(missing)),
        "unexpected": sorted(set(unexpected)),
        "duplicates": sorted(set(duplicates)),
        "usage": usage_total,
        "mean_latency_seconds": round(statistics.fmean(latencies), 3) if latencies else None,
    }
    target = out_dir / f"{args.provider}.json"
    write_json(target, result)
    print(f"wrote {target}")
    write_json(out_dir / f"{args.provider}.score.json", score_results(cases, result))
    print(f"wrote {out_dir / (args.provider + '.score.json')}")
    return 0


def cmd_replay(args: argparse.Namespace) -> int:
    """Re-derive decisions from stored raw content, using the current policy layer."""
    cases = load_cases(args.cases)
    candidate, _rules = profile_bundle(args.root, cases)
    swedish = cs.candidate_language_level(candidate, "swedish")
    stored = read_json(args.result)
    raw_dir = args.result.parent / "raw" / str(stored["provider"])
    if not raw_dir.exists():
        raise SystemExit(f"No stored raw responses at {raw_dir}")
    merged: dict[str, Any] = {}
    errors: list[str] = []
    missing: list[str] = []
    valid = 0
    for index, batch in enumerate(cases["_batches"], start=1):
        path = raw_dir / f"batch_{index:02d}.json"
        if not path.exists():
            errors.append(f"missing raw batch {index}")
            missing.extend(case["source_job_id"] for case in batch)
            continue
        parsed = parse_batch(read_json(path)["content"], batch, swedish)
        if not parsed["errors"] and not parsed["missing"]:
            valid += 1
        merged.update(parsed["evaluations"])
        errors.extend(parsed["errors"])
        missing.extend(parsed["missing"])
    replayed = dict(stored)
    replayed.update(
        {
            "evaluations": merged,
            "errors": errors,
            "missing": sorted(set(missing)),
            "batches_valid": valid,
            "replayed_at": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
        }
    )
    scored = score_results(cases, replayed)
    print(json.dumps(scored, ensure_ascii=False, indent=2))
    changed = [row for row in scored["cases"] if row.get("status") == "ok" and row["reported_decision"] and row["reported_decision"] != row["deterministic_decision"]]
    print(f"\n{len(changed)} case(s) where the policy layer changed the model's own decision", file=sys.stderr)
    return 0


def cmd_score(args: argparse.Namespace) -> int:
    cases = load_cases(args.cases)
    reports = [score_results(cases, read_json(path)) for path in args.results]
    metrics = [
        ("cases returned", "cases_returned", False),
        ("structurally valid batches", "batches_structurally_valid", False),
        ("false negatives", "false_negatives", True),
        ("false positives", "false_positives", True),
        ("weighted error", "weighted_error", True),
        ("exact decision matches", "exact_decision_matches", False),
        ("career-fit range error", "mean_career_fit_range_error", True),
        ("opportunity range error", "mean_opportunity_range_error", True),
        ("reported vs deterministic", "reported_decision_disagreements", True),
        ("mean latency (s)", "mean_latency_seconds", True),
    ]
    names = [str(r["provider"]) for r in reports]
    width = max([len(m[0]) for m in metrics] + [24])
    print("| " + "Metric".ljust(width) + " | " + " | ".join(n.rjust(12) for n in names) + " |")
    print("|" + "-" * (width + 2) + "|" + "|".join("-" * 14 for _ in names) + "|")
    for title, key, _lower_is_better in metrics:
        cells = []
        for report in reports:
            value = report.get(key)
            cells.append("-" if value is None else str(value))
        print("| " + title.ljust(width) + " | " + " | ".join(c.rjust(12) for c in cells) + " |")

    print("\nPer-case deterministic decisions\n")
    header = "| Case                         | Expected          | " + " | ".join(n.ljust(17) for n in names) + " |"
    print(header)
    print("|" + "-" * 30 + "|" + "-" * 19 + "|" + "|".join("-" * 19 for _ in names) + "|")
    for case_id in cases["_by_id"]:
        expected = cases["_by_id"][case_id]["expected"]["expected_decision"]
        cells = []
        for report in reports:
            row = next((r for r in report["cases"] if r["case"] == case_id), None)
            cells.append("missing" if row is None or row["status"] != "ok" else str(row["deterministic_decision"]))
        print(f"| {case_id.ljust(28)} | {expected.ljust(17)} | " + " | ".join(c.ljust(17) for c in cells) + " |")

    if args.out:
        write_json(args.out, {"reports": reports})
        print(f"\nwrote {args.out}")
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--cases", type=Path, default=DEFAULT_CASES, help="case file (default: sample_cases.json)")
    parser.add_argument("--root", type=Path, default=REPO_ROOT, help="repository root, for profile paths")
    sub = parser.add_subparsers(dest="command", required=True)

    p_dry = sub.add_parser("dry-run", help="build payloads without calling anything")
    p_dry.set_defaults(func=cmd_dry_run)

    p_run = sub.add_parser("run", help="call one provider and store results")
    p_run.add_argument("--provider", choices=("gemini", "azure"), required=True)
    p_run.add_argument("--secrets", type=Path, default=Path.home() / ".rolelens" / "secrets.env")
    p_run.add_argument("--out", type=Path, default=Path("benchmark_results"))
    p_run.add_argument("--timeout", type=int, default=150)
    p_run.set_defaults(func=cmd_run)

    p_score = sub.add_parser("score", help="score stored result files")
    p_score.add_argument("results", type=Path, nargs="+")
    p_score.add_argument("--out", type=Path, default=None)
    p_score.set_defaults(func=cmd_score)

    p_replay = sub.add_parser("replay", help="re-derive decisions from stored raw responses")
    p_replay.add_argument("result", type=Path)
    p_replay.set_defaults(func=cmd_replay)

    args = parser.parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())
