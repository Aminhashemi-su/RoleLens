#!/usr/bin/env python3
"""Fail if a repository tree contains anything that must not be published.

Run this before every public commit. It checks two things:

  1. FORBIDDEN FILES  — databases, secrets, logs, backups, raw provider output,
     a frozen job corpus, and the private profile filenames.
  2. FORBIDDEN CONTENT — credential-shaped strings, bearer tokens, private keys,
     absolute home-directory paths, and any extra terms you supply.

Findings are printed with the file, the line number, and a MASKED excerpt. The
value of a suspected secret is never printed in full.

Usage
-----
    python3 tools/check_public_safety.py                # check the repo it lives in
    python3 tools/check_public_safety.py ../rolelens
    python3 tools/check_public_safety.py . --terms ~/.rolelens-private-terms

The `--terms` file holds one literal string per line — your VPS address, your
real name, a chat ID, an employer you do not want mentioned. Keep it OUTSIDE
any repository; lines starting with `#` are ignored. Nothing from it is printed
back, only the fact that it matched.

Exit codes: 0 clean, 1 findings, 2 usage error.
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path
from typing import Iterable, NamedTuple

# ---------------------------------------------------------------------------
# Forbidden files
# ---------------------------------------------------------------------------
FORBIDDEN_NAMES: frozenset[str] = frozenset({
    "secrets.env",
    ".env",
    ".netrc",
    "id_rsa",
    "id_ed25519",
    "config.json",                 # the real one; config.example.json is fine
    "career_profile.json",         # the real one; *.example.json is fine
    "matcher_profile.json",
    "search_lenses.json",
    "benchmark_provider.py",       # private tooling: reads the production database
    "prepare_targeted_benchmark.py",
    "run_targeted_benchmark.py",
    "finalize_targeted_results.py",
    "benchmark_cases_v1.json",
    "targeted_cases_v2.json",
    "calibration_set_v1.json",
})

FORBIDDEN_SUFFIXES: tuple[str, ...] = (
    ".db", ".db-shm", ".db-wal", ".sqlite", ".sqlite3",
    ".bak", ".backup", ".orig", ".rej",
    ".log", ".pem", ".key", ".p12", ".pfx", ".keystore",
)

FORBIDDEN_DIR_PARTS: frozenset[str] = frozenset({
    "benchmark_results", "targeted_phase1", "model_failures",
    "backups", "rolelens-rollback", "logs", "data", "raw",
})

FORBIDDEN_PREFIXES: tuple[str, ...] = (".env.", "last_", "secrets.")

# `secrets.env.example`, `config.example.json` and friends are shipped on
# purpose: they are templates, and they carry variable names without values.
EXAMPLE_MARKERS: tuple[str, ...] = (".example", ".sample", ".template", ".dist")

SKIP_DIR_PARTS: frozenset[str] = frozenset({".git", "__pycache__", ".venv", "venv", "node_modules"})

BINARY_SUFFIXES: frozenset[str] = frozenset({
    ".png", ".jpg", ".jpeg", ".gif", ".pdf", ".zip", ".gz", ".tar",
    ".ico", ".woff", ".woff2", ".ttf", ".pyc", ".so", ".dll", ".exe",
})


# ---------------------------------------------------------------------------
# Forbidden content
# ---------------------------------------------------------------------------
class Rule(NamedTuple):
    name: str
    pattern: re.Pattern[str]
    note: str


CONTENT_RULES: tuple[Rule, ...] = (
    Rule("google_api_key", re.compile(r"AIza[0-9A-Za-z_\-]{30,}"),
         "Google/Vertex API key"),
    Rule("openai_key", re.compile(r"\bsk-[A-Za-z0-9_\-]{20,}"),
         "OpenAI-style secret key"),
    Rule("anthropic_key", re.compile(r"\bsk-ant-[A-Za-z0-9_\-]{20,}"),
         "Anthropic API key"),
    Rule("aws_access_key", re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b"),
         "AWS access key id"),
    Rule("github_token", re.compile(r"\bgh[pousr]_[A-Za-z0-9]{30,}"),
         "GitHub token"),
    Rule("slack_token", re.compile(r"\bxox[abprs]-[A-Za-z0-9\-]{10,}"),
         "Slack token"),
    Rule("telegram_bot_token", re.compile(r"\b\d{8,12}:AA[A-Za-z0-9_\-]{30,}"),
         "Telegram bot token"),
    Rule("bearer_token", re.compile(r"\bBearer\s+[A-Za-z0-9._\-]{20,}"),
         "Hard-coded bearer token"),
    Rule("private_key_block", re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH |PGP )?PRIVATE KEY-----"),
         "Private key block"),
    Rule("azure_style_key", re.compile(r"\b[A-Za-z0-9]{32}\b(?=[\"'\s]*$)"),
         "32-character key-shaped literal at end of line"),
    # KEY=value where the value is present and not an obvious placeholder.
    # Case-sensitive on purpose: credential variables are uppercase by
    # convention, and matching lowercase turns ordinary Python
    # (`secrets = load_secrets(path)`) into a finding. The value character class
    # excludes brackets, parentheses and commas for the same reason.
    Rule("assigned_credential", re.compile(
        r"(?m)^[^\S\n]*(?:export\s+)?"
        r"([A-Z0-9_]*(?:API_KEY|APIKEY|SECRET|TOKEN|PASSWORD|PASSWD|CREDENTIAL)[A-Z0-9_]*)"
        r"\s*[:=]\s*[\"']?"
        r"(?!your|YOUR|xxx|XXX|placeholder|PLACEHOLDER|changeme|CHANGEME|example|EXAMPLE"
        r"|redacted|REDACTED|dummy|DUMMY|none|None|null|NULL)"
        r"([A-Za-z0-9_\-./:+]{12,})"),
        "Credential-shaped variable with a real-looking value"),
    Rule("home_path_unix", re.compile(r"/(?:home|Users)/(?!<|\$|\{|USER\b|user\b)[A-Za-z0-9._\-]{2,}/"),
         "Absolute home-directory path"),
    Rule("home_path_windows", re.compile(r"[A-Za-z]:\\+Users\\+(?!<|%)[A-Za-z0-9._\-]{2,}\\+"),
         "Absolute Windows user path"),
    Rule("ssh_target", re.compile(r"\bssh\s+(?:-\w+\s+)*[A-Za-z0-9._\-]+@[A-Za-z0-9.\-]+"),
         "SSH connection string"),
    Rule("public_ipv4", re.compile(
        r"(?<![\d.])(?!0\.)(?!10\.)(?!127\.)(?!169\.254\.)(?!192\.168\.)"
        r"(?!172\.(?:1[6-9]|2\d|3[01])\.)"
        r"(?:\d{1,3}\.){3}\d{1,3}(?![\d.])"),
         "Possible public IP address"),
)

# Lines carrying these are structurally exempt: they are documenting the rule,
# not leaking a value.
ALLOW_MARKERS: tuple[str, ...] = (
    "check_public_safety",       # this file's own rule table
    "noqa: safety",              # explicit, reviewed opt-out
)

# Version strings, schema versions and semver-like tuples are numerically valid
# IPv4 addresses ("2.0.0.1"), so the IP rule needs context to stay useful.  noqa: safety
IP_VERSION_CONTEXT = re.compile(
    r"(?i)\b(?:version|versions|schema|semver|release|revision|build|tag|v\d)\b|"
    r"__version__|APP_VERSION|SCHEMA_VERSION|schema_version"
)


class Finding(NamedTuple):
    kind: str
    path: str
    line: int
    rule: str
    excerpt: str


def mask(text: str, match: re.Match[str]) -> str:
    """Show the shape of a hit without revealing it."""
    raw = match.group(0)
    keep = 4 if len(raw) > 12 else 1
    masked = raw[:keep] + "*" * max(3, min(16, len(raw) - keep))
    line = text.strip()
    line = line.replace(raw, masked)
    return line[:140] + ("..." if len(line) > 140 else "")


def load_terms(path: Path | None) -> list[str]:
    if path is None:
        return []
    if not path.exists():
        raise SystemExit(f"terms file not found: {path}")
    terms = []
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = line.strip()
        if line and not line.startswith("#"):
            terms.append(line)
    return terms


def iter_files(root: Path) -> Iterable[Path]:
    for path in sorted(root.rglob("*")):
        if not path.is_file():
            continue
        parts = set(path.relative_to(root).parts)
        if parts & SKIP_DIR_PARTS:
            continue
        yield path


def is_example(name: str) -> bool:
    return any(marker in name.casefold() for marker in EXAMPLE_MARKERS)


def check_filename(root: Path, path: Path) -> Finding | None:
    rel = path.relative_to(root).as_posix()
    name = path.name
    parts = set(path.relative_to(root).parts[:-1])
    if is_example(name):
        return None
    if name in FORBIDDEN_NAMES:
        return Finding("file", rel, 0, "forbidden_name", f"{name} must never be published")
    if any(name.endswith(suffix) for suffix in FORBIDDEN_SUFFIXES):
        return Finding("file", rel, 0, "forbidden_extension", f"{name} has a forbidden extension")
    if any(name.startswith(prefix) for prefix in FORBIDDEN_PREFIXES):
        return Finding("file", rel, 0, "forbidden_prefix", f"{name} has a forbidden prefix")
    if parts & FORBIDDEN_DIR_PARTS:
        bad = ", ".join(sorted(parts & FORBIDDEN_DIR_PARTS))
        return Finding("file", rel, 0, "forbidden_directory", f"{rel} is inside: {bad}")
    return None


def check_content(root: Path, path: Path, terms: list[str], self_path: Path) -> list[Finding]:
    if path.suffix.lower() in BINARY_SUFFIXES:
        return []
    try:
        text = path.read_text(encoding="utf-8")
    except (UnicodeDecodeError, OSError):
        return []
    rel = path.relative_to(root).as_posix()
    is_self = path.resolve() == self_path.resolve()
    findings: list[Finding] = []

    for number, line in enumerate(text.splitlines(), start=1):
        if any(marker in line for marker in ALLOW_MARKERS):
            continue
        for rule in CONTENT_RULES:
            if is_self:
                continue  # this scanner necessarily contains its own patterns
            match = rule.pattern.search(line)
            if match is None:
                continue
            if rule.name == "public_ipv4" and IP_VERSION_CONTEXT.search(line):
                continue
            findings.append(Finding("content", rel, number, rule.name, mask(line, match)))
        lowered = line.casefold()
        for term in terms:
            if term.casefold() in lowered:
                findings.append(
                    Finding("content", rel, number, "private_term",
                            f"matched a private term ({len(term)} chars) — value not printed")
                )
    return findings


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("root", type=Path, nargs="?", default=Path(__file__).resolve().parents[1],
                        help="repository to scan (default: the repo this script lives in)")
    parser.add_argument("--terms", type=Path, default=None,
                        help="file of extra literal strings to forbid, one per line, kept outside the repo")
    parser.add_argument("--quiet", action="store_true", help="print findings only")
    args = parser.parse_args(argv)

    root = args.root.expanduser().resolve()
    if not root.is_dir():
        print(f"not a directory: {root}", file=sys.stderr)
        return 2

    terms = load_terms(args.terms)
    self_path = Path(__file__).resolve()

    file_findings: list[Finding] = []
    content_findings: list[Finding] = []
    scanned = 0

    for path in iter_files(root):
        scanned += 1
        hit = check_filename(root, path)
        if hit is not None:
            file_findings.append(hit)
            continue
        content_findings.extend(check_content(root, path, terms, self_path))

    if not args.quiet:
        print(f"scanned {scanned} file(s) under {root}")
        print(f"content rules: {len(CONTENT_RULES)}   extra private terms: {len(terms)}")
        print()

    if file_findings:
        print(f"FORBIDDEN FILES ({len(file_findings)}):")
        for finding in file_findings:
            print(f"  [X] {finding.path}  [{finding.rule}] {finding.excerpt}")
        print()

    if content_findings:
        print(f"FORBIDDEN CONTENT ({len(content_findings)}):")
        for finding in content_findings:
            print(f"  [X] {finding.path}:{finding.line}  [{finding.rule}]")
            print(f"      {finding.excerpt}")
        print()

    total = len(file_findings) + len(content_findings)
    if total:
        print(f"FAILED: {total} finding(s). Do not commit.")
        return 1

    print("PASSED: no forbidden files or credential patterns found.")
    if not terms:
        print("Note: no --terms file was supplied, so private literals "
              "(server address, real name, chat id) were not checked.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
