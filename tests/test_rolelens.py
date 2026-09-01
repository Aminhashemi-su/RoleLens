from __future__ import annotations

import contextlib
import datetime as dt
import importlib.util
import io
import json
import shutil
import sqlite3
import tempfile
import unittest
from pathlib import Path

MODULE_PATH = Path(__file__).resolve().parents[1] / "rolelens.py"
spec = importlib.util.spec_from_file_location("rolelens", MODULE_PATH)
assert spec and spec.loader
cs = importlib.util.module_from_spec(spec)
import sys
sys.modules[spec.name] = cs
spec.loader.exec_module(cs)


class RoleLensTests(unittest.TestCase):
    def test_truncate_middle_preserves_head_and_tail(self):
        text = "A" * 100 + "B" * 100
        out = cs.truncate_middle(text, 80)
        self.assertLessEqual(len(out), 80)
        self.assertTrue(out.startswith("A"))
        self.assertTrue(out.endswith("B"))
        self.assertIn("middle omitted", out)

    def test_application_expired(self):
        today = dt.date(2026, 8, 29)
        self.assertTrue(cs.application_expired("2026-08-28", today))
        self.assertFalse(cs.application_expired("2026-08-29", today))
        self.assertFalse(cs.application_expired("2026-09-01", today))
        self.assertFalse(cs.application_expired(None, today))

    def test_decision_classification(self):
        self.assertEqual(cs.classify_decision(90, 90, []), "notify_strong")
        self.assertEqual(cs.classify_decision(90, 75, []), "notify_good")
        self.assertEqual(cs.classify_decision(90, 62, []), "notify_stretch")
        # V1.5 narrows notify_verify to genuine citizenship/clearance unknowns,
        # so a bare unknown blocker no longer forces a verification notification.
        self.assertEqual(cs.classify_decision(92, 57, [{"type": "unknown"}]), "store_no_notify")
        self.assertEqual(
            cs.classify_decision(
                92,
                57,
                [{"type": "unknown", "reason": "Citizenship eligibility requires candidate verification."}],
            ),
            "notify_verify",
        )
        self.assertEqual(cs.classify_decision(99, 95, [{"type": "hard"}]), "store_no_notify")

    def test_normalize_job_prefers_application_url(self):
        raw = {
            "id": "abc123",
            "headline": "Product Engineer",
            "webpage_url": "https://platsbanken.example/abc123",
            "application_deadline": "2026-12-01",
            "description": {"text": "Build useful software with React and Python."},
            "employer": {"name": "Example AB"},
            "application_details": {"url": "https://jobs.example/abc123"},
            "workplace_address": {
                "municipality": "Linköping",
                "region": "Östergötlands län",
                "country": "Sverige",
            },
            "employment_type": {"label": "Tillsvidare"},
        }
        job = cs.normalize_job(raw)
        self.assertEqual(job.url, "https://jobs.example/abc123")
        self.assertEqual(job.company, "Example AB")
        self.assertEqual(job.municipality, "Linköping")
        self.assertIn("React", job.description)

    def test_remote_detection(self):
        raw = {"description": {"text": "Hybrid work is supported."}}
        description = cs.merge_description(raw)
        self.assertTrue(cs.detect_remote(raw, description))
        any_remote, fully_remote = cs.detect_work_mode(raw, description)
        self.assertTrue(any_remote)
        self.assertFalse(fully_remote)

    def test_fully_remote_detection(self):
        raw = {"description": {"text": "This role is fully remote anywhere in Sweden."}}
        any_remote, fully_remote = cs.detect_work_mode(raw, cs.merge_description(raw))
        self.assertTrue(any_remote)
        self.assertTrue(fully_remote)

    def test_profile_version_is_order_independent(self):
        one = cs.profile_version({"a": 1, "b": 2}, {"x": True})
        two = cs.profile_version({"b": 2, "a": 1}, {"x": True})
        self.assertEqual(one, two)

    def test_database_is_idempotent_and_requeues_changed_content(self):
        with tempfile.TemporaryDirectory() as td:
            db = cs.Database(Path(td) / "test.db")
            try:
                raw = {
                    "id": "1",
                    "headline": "Software Engineer",
                    "description": {"text": "Build APIs."},
                    "employer": {"name": "ACME"},
                    "workplace_address": {"municipality": "Stockholm", "country": "Sverige"},
                }
                job = cs.normalize_job(raw)
                job.matched_queries.add("software engineer Stockholm")
                job.discovery_score = 10
                db.upsert_job(job)
                db.upsert_job(job)
                self.assertEqual(db.status()["jobs"], 1)
                pending = db.pending_jobs("profile-v1", 10)
                self.assertEqual(len(pending), 1)

                item = {
                    "source_job_id": "1",
                    "career_fit": 80,
                    "opportunity_score": 75,
                    "confidence": 0.8,
                    "actual_role": "API engineer",
                    "why_fit": ["API experience"],
                    "candidate_evidence": ["FastAPI"],
                    "must_have_assessment": [],
                    "gaps": [],
                    "blockers": [],
                    "language_risk": "none",
                    "seniority_risk": "moderate",
                    "location_note": "preferred",
                }
                ev = cs.validate_evaluation(item)
                db.save_evaluation(pending[0], ev, profile_version="profile-v1", model="test")
                self.assertEqual(len(db.pending_jobs("profile-v1", 10)), 0)

                raw["description"]["text"] = "Build APIs and distributed systems."
                changed = cs.normalize_job(raw)
                changed.matched_queries.add("software engineer Stockholm")
                changed.discovery_score = 10
                db.upsert_job(changed)
                self.assertEqual(len(db.pending_jobs("profile-v1", 10)), 1)
            finally:
                db.close()

    def test_json_schema_has_required_evaluations(self):
        schema = cs.evaluation_schema()
        self.assertEqual(schema["type"], "object")
        self.assertIn("evaluations", schema["required"])



SWEDISH_NOT_REQUIRED = 'Vi bygger en modern plattform i Stockholm. Teamets arbetsspråk är engelska och ansökan ska lämnas på engelska. Kunskaper i svenska krävs inte.'
SWEDISH_MANDATORY_REVERSED = 'Vi söker en systemutvecklare. Du har god kommunikativ förmåga och kan uttrycka dig väl i tal och skrift på svenska och engelska.'


class _FakeMatcher:
    """Minimal stand-in for ProviderMatcher in fallback-orchestration tests."""

    def __init__(self, provider, model, outcome):
        self.provider = provider
        self.model = model
        self._outcome = outcome
        self.calls = 0

    def evaluate(self, jobs):
        self.calls += 1
        if isinstance(self._outcome, Exception):
            raise self._outcome
        return self._outcome


def _result(provider, model, error=None):
    return cs.ProviderBatchResult(provider, model, (), frozenset(), cs.empty_usage(), error)


class ProviderRoutingTests(unittest.TestCase):
    def test_gemini_is_primary_and_azure_is_the_only_fallback(self):
        self.assertEqual(cs.PRIMARY_PROVIDER, "vertex_gemini")
        self.assertEqual(cs.FALLBACK_PROVIDER, "azure_gpt5mini")

    def test_glm_is_not_reachable_from_automatic_routing(self):
        for provider in ("glm", "zai_glm", "ai_gateway"):
            with self.assertRaises(cs.ConfigurationError):
                cs.ProviderMatcher(
                    settings=None,
                    secrets={},
                    matcher_profile={},
                    matcher_rules={},
                    provider=provider,
                )

    def test_successful_primary_never_calls_the_fallback(self):
        primary = _FakeMatcher("vertex_gemini", "g", _result("vertex_gemini", "g"))
        fallback = _FakeMatcher("azure_gpt5mini", "a", _result("azure_gpt5mini", "a"))
        result, used_fallback = cs.evaluate_with_fallback(primary, fallback, [])
        self.assertFalse(used_fallback)
        self.assertEqual(fallback.calls, 0)
        self.assertEqual(result.provider, "vertex_gemini")

    def test_transport_failure_falls_back_exactly_once(self):
        primary = _FakeMatcher("vertex_gemini", "g", cs.TemporaryProviderError("vertex_gemini", "503"))
        fallback = _FakeMatcher("azure_gpt5mini", "a", _result("azure_gpt5mini", "a"))
        result, used_fallback = cs.evaluate_with_fallback(primary, fallback, [])
        self.assertTrue(used_fallback)
        self.assertEqual(fallback.calls, 1)
        self.assertEqual(result.provider, "azure_gpt5mini")

    def test_semantic_failure_does_not_trigger_the_transport_fallback(self):
        # A schema/semantic failure is returned as a result, never raised, so the
        # fallback must stay unused. Azure is a transport fallback only.
        primary = _FakeMatcher("vertex_gemini", "g", _result("vertex_gemini", "g", error="invalid JSON"))
        fallback = _FakeMatcher("azure_gpt5mini", "a", _result("azure_gpt5mini", "a"))
        result, used_fallback = cs.evaluate_with_fallback(primary, fallback, [])
        self.assertFalse(used_fallback)
        self.assertEqual(fallback.calls, 0)
        self.assertEqual(result.error, "invalid JSON")

    def test_both_providers_failing_reports_both(self):
        primary = _FakeMatcher("vertex_gemini", "g", cs.TemporaryProviderError("vertex_gemini", "503"))
        fallback = _FakeMatcher("azure_gpt5mini", "a", cs.TemporaryProviderError("azure_gpt5mini", "429"))
        result, used_fallback = cs.evaluate_with_fallback(primary, fallback, [])
        self.assertTrue(used_fallback)
        self.assertIn("vertex_gemini", result.error)
        self.assertIn("azure_gpt5mini", result.error)


class SwedishLanguagePolicyTests(unittest.TestCase):
    """Regression guards for the deterministic language policy layer."""

    BASE = {
        "source_job_id": "T",
        "career_fit": 95,
        "opportunity_score": 92,
        "confidence": 0.9,
        "actual_role": "Product engineer",
        "why_fit": ["fit"],
        "candidate_evidence": ["evidence"],
        "must_have_assessment": [],
        "gaps": [],
        "blockers": [],
        "language_risk": "none",
        "seniority_risk": "low",
        "location_note": "Stockholm",
    }

    def policy(self, description, **overrides):
        item = dict(self.BASE)
        item.update(overrides)
        return cs.normalize_evaluation_policy(item, {"description": description})

    def test_explicit_swedish_not_required_is_not_a_blocker(self):
        # The ad states that knowledge of Swedish is explicitly NOT required.
        out = self.policy(SWEDISH_NOT_REQUIRED)
        self.assertNotIn("mandatory_swedish_enforced", out["_policy_changes"])
        self.assertEqual(out["opportunity_score"], 92)
        self.assertEqual([b for b in out["blockers"] if b["type"] == "hard"], [])
        self.assertEqual(
            cs.classify_decision(out["career_fit"], out["opportunity_score"], out["blockers"]),
            "notify_strong",
        )

    def test_mandatory_swedish_detected_when_written_as_tal_och_skrift(self):
        # This is the common Swedish public-sector phrasing for the requirement.
        out = self.policy(SWEDISH_MANDATORY_REVERSED, opportunity_score=64)
        self.assertIn("mandatory_swedish_enforced", out["_policy_changes"])
        self.assertLessEqual(out["opportunity_score"], 49)
        self.assertTrue(any(b["type"] == "hard" for b in out["blockers"]))
        self.assertEqual(
            cs.classify_decision(out["career_fit"], out["opportunity_score"], out["blockers"]),
            "store_no_notify",
        )

    def test_mandatory_swedish_survives_a_nearby_preferred_phrase(self):
        # The negation guard must not fire on "mandatory, not merely preferred".
        description = (
            "Fluent Swedish is required because you will run weekly sessions with "
            "Swedish-speaking customers. Swedish proficiency is mandatory, not merely preferred."
        )
        out = self.policy(description)
        self.assertIn("mandatory_swedish_enforced", out["_policy_changes"])
        self.assertTrue(any(b["type"] == "hard" for b in out["blockers"]))

    def test_optional_swedish_is_not_treated_as_mandatory(self):
        description = (
            "Hands-on software development and professional English are required. "
            "Swedish is considered a plus for some local conversations, but it is not mandatory."
        )
        out = self.policy(description)
        self.assertNotIn("mandatory_swedish_enforced", out["_policy_changes"])
        self.assertEqual([b for b in out["blockers"] if b["type"] == "hard"], [])


class RunSummaryTests(unittest.TestCase):
    """The one compact stats line the scheduler delivers per run."""

    def summary(self, evaluated, matches, pending_retry=0, partial=False):
        return cs.format_run_summary(evaluated, matches, pending_retry, partial=partial)

    def test_no_new_jobs_evaluated(self):
        self.assertEqual(
            self.summary(0, 0),
            "🔎 RoleLens: no new jobs found.",
        )

    def test_jobs_evaluated_without_matches(self):
        self.assertEqual(
            self.summary(7, 0),
            "🔎 RoleLens: 7 new jobs checked · 0 matches.",
        )

    def test_single_match_is_singular(self):
        self.assertEqual(
            self.summary(9, 1),
            "🎯 RoleLens: 9 new jobs checked · 1 match.",
        )

    def test_multiple_matches_are_plural(self):
        self.assertEqual(
            self.summary(10, 4),
            "🎯 RoleLens: 10 new jobs checked · 4 matches.",
        )

    def test_partial_run_reports_pending_retry(self):
        self.assertEqual(
            self.summary(6, 2, pending_retry=4, partial=True),
            "⚠️ RoleLens: 6 jobs checked · 2 matches · 4 pending retry.",
        )

    def test_partial_run_keeps_singular_match(self):
        self.assertEqual(
            self.summary(6, 1, pending_retry=5, partial=True),
            "⚠️ RoleLens: 6 jobs checked · 1 match · 5 pending retry.",
        )

    def test_partial_wins_over_the_zero_evaluated_wording(self):
        # A provider failure that returned nothing must not read as a quiet
        # 'no new jobs found' tick.
        self.assertEqual(
            self.summary(0, 0, pending_retry=10, partial=True),
            "⚠️ RoleLens: 0 jobs checked · 0 matches · 10 pending retry.",
        )

    def test_target_icon_only_when_there_are_matches(self):
        self.assertTrue(self.summary(5, 0).startswith("🔎"))
        self.assertTrue(self.summary(5, 1).startswith("🎯"))
        self.assertTrue(self.summary(0, 0).startswith("🔎"))
        self.assertTrue(self.summary(5, 1, 2, partial=True).startswith("⚠️"))

    def test_summary_counts_evaluations_not_discovery(self):
        # The count must come from the semantic matcher, so a run that upserted
        # thousands of jobs but evaluated ten reports ten.
        stats = cs.RunStats()
        stats.search_hits = 2412
        stats.unique_jobs = 1283
        stats.jobs_upserted = 1258
        stats.evaluated = 10
        stats.notified = 4
        self.assertEqual(
            cs.format_run_summary(stats.evaluated, stats.notified, 0, partial=False),
            "🎯 RoleLens: 10 new jobs checked · 4 matches.",
        )

    def test_detailed_notifications_are_unchanged_by_the_summary(self):
        # format_notifications still owns the detailed block; the summary is a
        # separate single line and must not appear inside it.
        self.assertEqual(cs.format_notifications([]), "")
        self.assertNotIn("pending retry", cs.format_notifications([]))



class RunSummaryPipelineTests(unittest.TestCase):
    """Drives run_pipeline with a stubbed provider and reads real stdout."""

    REPO = Path(__file__).resolve().parents[1]

    @classmethod
    def repo_file(cls, *parts):
        """Prefer a real config/profile file, fall back to its .example variant.

        A working checkout has config.json and profile/*.json. A fresh clone of
        the published repository ships only the *.example.json templates, and
        the suite must still run there without asking anyone to build a profile
        first.
        """
        path = cls.REPO.joinpath(*parts)
        if path.exists():
            return path
        example = path.with_name(f"{path.stem}.example{path.suffix}")
        if example.exists():
            return example
        raise FileNotFoundError(f"Neither {path} nor {example} exists")

    def build_home(self, tmp, **overrides):
        home = Path(tmp)
        (home / "profile").mkdir(parents=True, exist_ok=True)
        (home / "data").mkdir(parents=True, exist_ok=True)
        shutil.copy2(self.repo_file("config.json"), home / "config.json")
        if overrides:
            config = json.loads((home / "config.json").read_text(encoding="utf-8"))
            config.update(overrides)
            (home / "config.json").write_text(json.dumps(config), encoding="utf-8")
        for name in (
            "career_profile.json", "matcher_profile.json",
            "search_lenses.json", "matcher_rules_v1_1.json",
        ):
            shutil.copy2(self.repo_file("profile", name), home / "profile" / name)
        (home / "secrets.env").write_text(
            "VERTEX_GEMINI_API_KEY=dummy\nVERTEX_GEMINI_MODEL=gemini-3.7-flash\n"
            "AZURE_OPENAI_API_KEY=dummy\nAZURE_OPENAI_BASE_URL=https://example.invalid\n"
            "AZURE_OPENAI_DEPLOYMENT=gpt-5-mini\n",
            encoding="utf-8",
        )
        # load_secrets refuses anything looser than 600.
        (home / "secrets.env").chmod(0o600)
        return cs.Settings.load(home)

    def seed(self, settings, count):
        db = cs.Database(settings.db_path)
        try:
            for i in range(1, count + 1):
                job = cs.normalize_job({
                    "id": f"job{i}",
                    "headline": f"AI Engineer {i}",
                    "employer": {"name": f"Company {i}"},
                    "webpage_url": f"https://example.invalid/{i}",
                    "workplace_address": {"municipality": "Stockholm", "region": "Stockholms lan", "country": "Sverige"},
                    "description": {"text": "Build AI products with Python and TypeScript."},
                    "application_deadline": "2027-01-01T23:59:59",
                    "publication_date": "2026-08-01T00:00:00",
                    "employment_type": {"label": "Vanlig anstallning"},
                    "scope_of_work": {"min": 100, "max": 100},
                })
                job.matched_queries.add("AI engineer Stockholm")
                job.discovery_score = 10
                db.upsert_job(job)
        finally:
            db.close()

    def evaluation(self, source_id, opportunity):
        return cs.validate_evaluation({
            "source_job_id": source_id, "career_fit": 90,
            "opportunity_score": opportunity, "confidence": 0.9,
            "actual_role": "AI Product Engineer", "why_fit": ["Strong overlap"],
            "candidate_evidence": ["Python"], "must_have_assessment": [],
            "gaps": [], "blockers": [], "language_risk": "none",
            "seniority_risk": "low", "location_note": "Stockholm",
        })

    def run_once(self, jobs, matches, unresolved=0, error=None):
        with tempfile.TemporaryDirectory() as tmp:
            settings = self.build_home(tmp)
            self.seed(settings, jobs)
            evaluated = jobs - unresolved

            class Stub:
                def __init__(self, *a, **k):
                    self.provider, self.model = "vertex_gemini", "gemini-3.7-flash"

            def fake_eval(primary, fallback, batch):
                ids = [str(r["source_job_id"]) for r in batch]
                evals = tuple(
                    self.evaluation(sid, 90 if i < matches else 30)
                    for i, sid in enumerate(ids[:evaluated])
                )
                return cs.ProviderBatchResult(
                    "vertex_gemini", "gemini-3.7-flash", evals,
                    frozenset(ids[evaluated:]), cs.empty_usage(), error,
                ), False

            original = (cs.ProviderMatcher, cs.evaluate_with_fallback)
            cs.ProviderMatcher, cs.evaluate_with_fallback = Stub, fake_eval
            buffer = io.StringIO()
            try:
                with contextlib.redirect_stdout(buffer):
                    cs.run_pipeline(settings, fetch_only=False, evaluate_only=True)
            finally:
                cs.ProviderMatcher, cs.evaluate_with_fallback = original
            return buffer.getvalue()

    def summary_lines(self, out):
        icons = ("🔎 RoleLens:", "🎯 RoleLens:", "⚠️ RoleLens:")
        return [ln for ln in out.splitlines() if ln.startswith(icons)]

    def test_no_new_jobs(self):
        out = self.run_once(0, 0)
        self.assertEqual(
            self.summary_lines(out), ["🔎 RoleLens: no new jobs found."]
        )

    def test_jobs_but_no_matches(self):
        out = self.run_once(8, 0)
        self.assertEqual(
            self.summary_lines(out), ["🔎 RoleLens: 8 new jobs checked · 0 matches."]
        )
        self.assertNotIn("RoleLens found", out)

    def test_single_match_keeps_the_detailed_block(self):
        out = self.run_once(9, 1)
        self.assertEqual(
            self.summary_lines(out), ["🎯 RoleLens: 9 new jobs checked · 1 match."]
        )
        self.assertIn("RoleLens found 1 new match", out)

    def test_multiple_matches(self):
        out = self.run_once(10, 4)
        self.assertEqual(
            self.summary_lines(out), ["🎯 RoleLens: 10 new jobs checked · 4 matches."]
        )
        self.assertIn("RoleLens found 4 new matches", out)

    def test_partial_run(self):
        out = self.run_once(10, 2, unresolved=4, error="provider returned invalid JSON")
        self.assertEqual(
            self.summary_lines(out),
            ["⚠️ RoleLens: 6 jobs checked · 2 matches · 4 pending retry."],
        )

    def test_exactly_one_summary_line_in_every_state(self):
        for jobs, matches, unresolved in ((0, 0, 0), (5, 0, 0), (5, 1, 0), (9, 3, 0), (9, 1, 3)):
            with self.subTest(jobs=jobs, matches=matches, unresolved=unresolved):
                error = "partial" if unresolved else None
                out = self.run_once(jobs, matches, unresolved, error)
                self.assertEqual(len(self.summary_lines(out)), 1)


class _PipelineHarness(unittest.TestCase):
    """Shared scaffolding: a real home, a real database, a stubbed provider."""

    REPO = Path(__file__).resolve().parents[1]

    @classmethod
    def repo_file(cls, *parts):
        """Prefer a real config/profile file, fall back to its .example variant.

        A working checkout has config.json and profile/*.json. A fresh clone of
        the published repository ships only the *.example.json templates, and
        the suite must still run there without asking anyone to build a profile
        first.
        """
        path = cls.REPO.joinpath(*parts)
        if path.exists():
            return path
        example = path.with_name(f"{path.stem}.example{path.suffix}")
        if example.exists():
            return example
        raise FileNotFoundError(f"Neither {path} nor {example} exists")

    def build_home(self, tmp, **overrides):
        home = Path(tmp)
        (home / "profile").mkdir(parents=True, exist_ok=True)
        (home / "data").mkdir(parents=True, exist_ok=True)
        shutil.copy2(self.repo_file("config.json"), home / "config.json")
        if overrides:
            config = json.loads((home / "config.json").read_text(encoding="utf-8"))
            config.update(overrides)
            (home / "config.json").write_text(json.dumps(config), encoding="utf-8")
        for name in (
            "career_profile.json", "matcher_profile.json",
            "search_lenses.json", "matcher_rules_v1_1.json",
        ):
            shutil.copy2(self.repo_file("profile", name), home / "profile" / name)
        (home / "secrets.env").write_text(
            "VERTEX_GEMINI_API_KEY=dummy\nVERTEX_GEMINI_MODEL=gemini-3.7-flash\n"
            "AZURE_OPENAI_API_KEY=dummy\nAZURE_OPENAI_BASE_URL=https://example.invalid\n"
            "AZURE_OPENAI_DEPLOYMENT=gpt-5-mini\n",
            encoding="utf-8",
        )
        (home / "secrets.env").chmod(0o600)
        return cs.Settings.load(home)

    def raw_job(self, source_id, title, company, municipality, description):
        return {
            "id": str(source_id),
            "headline": title,
            "employer": {"name": company},
            "webpage_url": f"https://example.invalid/{source_id}",
            "workplace_address": {
                "municipality": municipality, "region": "Stockholms lan", "country": "Sverige",
            },
            "description": {"text": description},
            "application_deadline": "2027-01-01T23:59:59",
            "publication_date": "2026-08-01T00:00:00",
            "employment_type": {"label": "Vanlig anstallning"},
            "scope_of_work": {"min": 100, "max": 100},
        }

    def seed(self, settings, specs):
        db = cs.Database(settings.db_path)
        try:
            for spec in specs:
                job = cs.normalize_job(self.raw_job(*spec))
                job.matched_queries.add("AI engineer Stockholm")
                job.discovery_score = 10
                db.upsert_job(job)
        finally:
            db.close()

    def distinct(self, count):
        return [
            (f"job{i}", f"AI Engineer {i}", f"Company {i}", "Stockholm",
             f"Role {i}: build AI products with Python and TypeScript.")
            for i in range(1, count + 1)
        ]

    def evaluation(self, source_id, opportunity):
        return cs.validate_evaluation({
            "source_job_id": str(source_id), "career_fit": 90,
            "opportunity_score": opportunity, "confidence": 0.9,
            "actual_role": "AI Product Engineer", "why_fit": ["Strong overlap"],
            "candidate_evidence": ["Python"], "must_have_assessment": [],
            "gaps": [], "blockers": [], "language_risk": "none",
            "seniority_risk": "low", "location_note": "Stockholm",
        })

    def drive(self, settings, matches=0, fail_on_batch=None, unresolved_per_batch=0):
        """Run the real pipeline. Returns (stdout, batch_sizes)."""
        sizes = []

        class Stub:
            def __init__(self, *a, **k):
                self.provider, self.model = "vertex_gemini", "gemini-3.7-flash"

        state = {"matches_left": matches}

        def fake_eval(primary, fallback, batch):
            sizes.append(len(batch))
            ids = [str(r["source_job_id"]) for r in batch]
            if fail_on_batch is not None and len(sizes) == fail_on_batch:
                return cs.ProviderBatchResult(
                    "vertex_gemini", "gemini-3.7-flash", (), frozenset(ids),
                    cs.empty_usage(), "provider returned invalid JSON"), False
            keep = ids[: len(ids) - unresolved_per_batch] if unresolved_per_batch else ids
            evals = []
            for sid in keep:
                if state["matches_left"] > 0:
                    evals.append(self.evaluation(sid, 90))
                    state["matches_left"] -= 1
                else:
                    evals.append(self.evaluation(sid, 30))
            return cs.ProviderBatchResult(
                "vertex_gemini", "gemini-3.7-flash", tuple(evals),
                frozenset(ids[len(keep):]), cs.empty_usage(), None), False

        original = (cs.ProviderMatcher, cs.evaluate_with_fallback)
        cs.ProviderMatcher, cs.evaluate_with_fallback = Stub, fake_eval
        buffer = io.StringIO()
        try:
            with contextlib.redirect_stdout(buffer):
                cs.run_pipeline(settings, fetch_only=False, evaluate_only=True)
        finally:
            cs.ProviderMatcher, cs.evaluate_with_fallback = original
        return buffer.getvalue(), sizes

    def summary_lines(self, out):
        icons = ("%s RoleLens:" % "🔎", "%s RoleLens:" % "🎯", "%s RoleLens:" % "⚠️")
        return [ln for ln in out.splitlines() if ln.startswith(icons)]


class MultiBatchThroughputTests(_PipelineHarness):
    def test_forty_candidates_become_four_batches_of_at_most_ten(self):
        with tempfile.TemporaryDirectory() as tmp:
            settings = self.build_home(tmp)
            self.assertEqual(settings.max_candidates_per_run, 40)
            self.assertEqual(settings.max_jobs_per_batch, 10)
            self.assertEqual(settings.max_batches_per_run, 4)
            self.seed(settings, self.distinct(40))
            out, sizes = self.drive(settings)
            self.assertEqual(sizes, [10, 10, 10, 10])
            self.assertEqual(len(sizes), 4)
            self.assertTrue(all(n <= 10 for n in sizes))
            self.assertEqual(
                self.summary_lines(out),
                ["%s RoleLens: 40 new jobs checked %s 0 matches." % ("🔎", "·")],
            )

    def test_batch_cap_bounds_model_calls_independently_of_candidates(self):
        # 40 candidates are available but only 2 batches are permitted, so the cap
        # alone must stop the run at 20 jobs.
        with tempfile.TemporaryDirectory() as tmp:
            settings = self.build_home(tmp, max_batches_per_run=2)
            self.seed(settings, self.distinct(40))
            out, sizes = self.drive(settings)
            self.assertEqual(sizes, [10, 10], "max_batches_per_run must bound the model calls")
            self.assertEqual(sum(sizes), 20)
            self.assertEqual(
                self.summary_lines(out),
                ["🔎 RoleLens: 20 new jobs checked · 0 matches."],
            )
            db = cs.Database(settings.db_path)
            try:
                _, _, version = cs.load_profile_bundle(settings)
                self.assertEqual(db.pending_count(version, respect_live_mode=False), 20)
            finally:
                db.close()

    def test_runtime_budget_stops_cleanly_as_partial(self):
        # A one-second budget cannot fit a second batch's worst case, so the run
        # must stop after the first batch and report the rest as pending.
        with tempfile.TemporaryDirectory() as tmp:
            settings = self.build_home(tmp, max_run_seconds=1)
            self.seed(settings, self.distinct(40))
            out, sizes = self.drive(settings)
            self.assertEqual(len(sizes), 1, "only the first batch fits the budget")
            self.assertEqual(
                self.summary_lines(out),
                ["⚠️ RoleLens: 10 jobs checked · 0 matches · 30 pending retry."],
            )
            db = cs.Database(settings.db_path)
            try:
                _, _, version = cs.load_profile_bundle(settings)
                self.assertEqual(db.pending_count(version, respect_live_mode=False), 30)
            finally:
                db.close()

    def test_summary_aggregates_across_batches(self):
        with tempfile.TemporaryDirectory() as tmp:
            settings = self.build_home(tmp)
            self.seed(settings, self.distinct(37))
            out, sizes = self.drive(settings, matches=4)
            self.assertEqual(sum(sizes), 37)
            self.assertEqual(
                self.summary_lines(out),
                ["%s RoleLens: 37 new jobs checked %s 4 matches." % ("🎯", "·")],
            )

    def test_one_summary_line_not_one_per_batch(self):
        with tempfile.TemporaryDirectory() as tmp:
            settings = self.build_home(tmp)
            self.seed(settings, self.distinct(40))
            out, sizes = self.drive(settings, matches=2)
            self.assertEqual(len(sizes), 4)
            self.assertEqual(len(self.summary_lines(out)), 1)

    def test_provider_failure_in_a_later_batch_leaves_the_rest_pending(self):
        with tempfile.TemporaryDirectory() as tmp:
            settings = self.build_home(tmp)
            self.seed(settings, self.distinct(40))
            out, sizes = self.drive(settings, fail_on_batch=3)
            # Batch 4 must never be attempted.
            self.assertEqual(len(sizes), 3)
            db = cs.Database(settings.db_path)
            try:
                _, _, version = cs.load_profile_bundle(settings)
                pending = db.pending_count(version, respect_live_mode=False)
            finally:
                db.close()
            # 20 evaluated; 10 unresolved from the failed batch, 10 never started.
            self.assertEqual(pending, 20)
            self.assertEqual(
                self.summary_lines(out),
                ["%s RoleLens: 20 jobs checked %s 0 matches %s 20 pending retry." % ("⚠️", "·", "·")],
            )


class DuplicateSuppressionTests(_PipelineHarness):
    BODY = "We are hiring a platform engineer to build and operate internal developer tooling."

    def test_identical_repost_with_a_new_source_id_is_suppressed(self):
        with tempfile.TemporaryDirectory() as tmp:
            settings = self.build_home(tmp)
            self.seed(settings, [
                ("orig-1", "Platform Engineer", "Acme AB", "Stockholm", self.BODY),
                ("repost-2", "Platform Engineer", "Acme AB", "Stockholm", self.BODY),
            ])
            out, sizes = self.drive(settings)
            self.assertEqual(sum(sizes), 1, "the repost must not reach the model")
            self.assertEqual(
                self.summary_lines(out),
                ["%s RoleLens: 1 new jobs checked %s 0 matches." % ("🔎", "·")],
            )

    def test_repost_relationship_is_recorded_for_audit(self):
        with tempfile.TemporaryDirectory() as tmp:
            settings = self.build_home(tmp)
            self.seed(settings, [
                ("orig-1", "Platform Engineer", "Acme AB", "Stockholm", self.BODY),
                ("repost-2", "Platform Engineer", "Acme AB", "Stockholm", self.BODY),
            ])
            self.drive(settings)
            db = cs.Database(settings.db_path)
            try:
                rows = list(db.conn.execute(
                    "SELECT job_id, canonical_job_id, reason FROM job_fingerprints "
                    "WHERE canonical_job_id IS NOT NULL"))
                jobs = {r["id"]: r["source_job_id"] for r in db.conn.execute("SELECT id, source_job_id FROM jobs")}
            finally:
                db.close()
            self.assertEqual(len(rows), 1)
            # Selection orders newest first, so the fresher posting becomes canonical
            # and the older one is recorded as its repost.
            self.assertEqual(jobs[rows[0]["job_id"]], "orig-1")
            self.assertEqual(jobs[rows[0]["canonical_job_id"]], "repost-2")
            self.assertIn("Repost of job", rows[0]["reason"])
            self.assertNotEqual(rows[0]["job_id"], rows[0]["canonical_job_id"])

    def test_both_source_rows_survive_in_the_database(self):
        with tempfile.TemporaryDirectory() as tmp:
            settings = self.build_home(tmp)
            self.seed(settings, [
                ("orig-1", "Platform Engineer", "Acme AB", "Stockholm", self.BODY),
                ("repost-2", "Platform Engineer", "Acme AB", "Stockholm", self.BODY),
            ])
            self.drive(settings)
            db = cs.Database(settings.db_path)
            try:
                self.assertEqual(db.conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0], 2)
            finally:
                db.close()

    def test_same_title_and_company_but_different_description_is_not_suppressed(self):
        with tempfile.TemporaryDirectory() as tmp:
            settings = self.build_home(tmp)
            self.seed(settings, [
                ("a-1", "Member of Technical Staff", "Northwind Labs AB", "Stockholm",
                 "You will build end-to-end product features across the full stack."),
                ("a-2", "Member of Technical Staff", "Northwind Labs AB", "Stockholm",
                 "You will lead architecture for a senior platform team and mentor engineers."),
            ])
            _, sizes = self.drive(settings)
            self.assertEqual(sum(sizes), 2, "materially different ads are distinct vacancies")

    def test_same_title_and_company_in_a_different_location_is_not_suppressed(self):
        with tempfile.TemporaryDirectory() as tmp:
            settings = self.build_home(tmp)
            self.seed(settings, [
                ("loc-1", "Sourcing Manager", "Baltic Energy AB", "Solna", self.BODY),
                ("loc-2", "Sourcing Manager", "Baltic Energy AB", "Goteborg", self.BODY),
            ])
            _, sizes = self.drive(settings)
            self.assertEqual(sum(sizes), 2, "a different city is a different vacancy")

    def test_duplicate_matches_do_not_produce_duplicate_cards(self):
        with tempfile.TemporaryDirectory() as tmp:
            settings = self.build_home(tmp)
            self.seed(settings, [
                ("dup-1", "Senior Software Engineer", "DataJob AB", "Stockholm", self.BODY),
                ("dup-2", "Senior Software Engineer", "DataJob AB", "Stockholm", self.BODY),
            ])
            # Evaluate both directly, as if they predate suppression.
            db = cs.Database(settings.db_path)
            try:
                _, _, version = cs.load_profile_bundle(settings)
                for row in db.pending_jobs(version, 10, respect_live_mode=False):
                    db.save_evaluation(
                        row, self.evaluation(row["source_job_id"], 90),
                        profile_version=version, model="test",
                    )
                self.assertEqual(db.conn.execute("SELECT COUNT(*) FROM evaluations").fetchone()[0], 2)
            finally:
                db.close()

            out, _ = self.drive(settings)
            self.assertEqual(out.count("Senior Software Engineer"), 1, "one vacancy, one card")
            self.assertIn("RoleLens found 1 new match", out)
            self.assertEqual(
                self.summary_lines(out),
                ["%s RoleLens: 0 new jobs checked %s 1 match." % ("🎯", "·")],
            )
            # The suppressed card must be retired, not left to resurface.
            db = cs.Database(settings.db_path)
            try:
                self.assertEqual(db.conn.execute("SELECT COUNT(*) FROM notifications").fetchone()[0], 2)
            finally:
                db.close()


class DuplicateFingerprintTests(unittest.TestCase):
    BODY = "Build and operate internal developer tooling for a product team."

    def fp(self, company="Acme AB", title="Platform Engineer", location="Stockholm", body=None):
        return cs.duplicate_fingerprint(company, title, location, self.BODY if body is None else body)

    def test_formatting_noise_does_not_change_the_fingerprint(self):
        self.assertEqual(
            self.fp(),
            self.fp(company="  ACME   ab ", title="Platform   Engineer!", body="  Build and, operate internal developer tooling for a product team.  "),
        )

    def test_employer_title_location_and_body_each_change_it(self):
        base = self.fp()
        self.assertNotEqual(base, self.fp(company="Other AB"))
        self.assertNotEqual(base, self.fp(title="Senior Platform Engineer"))
        self.assertNotEqual(base, self.fp(location="Goteborg"))
        self.assertNotEqual(base, self.fp(body="A completely different role description entirely."))


class EmptyRunHeartbeatTests(_PipelineHarness):
    """A quiet scheduled run must still say something, but never contradict itself."""

    BODY = "Build internal tooling with Python, TypeScript and cloud services."

    def deferred_match(self, settings):
        """Leave one evaluated, un-notified match and nothing pending."""
        db = cs.Database(settings.db_path)
        try:
            _, _, version = cs.load_profile_bundle(settings)
            for row in db.pending_jobs(version, 10, respect_live_mode=False):
                db.save_evaluation(
                    row, self.evaluation(row["source_job_id"], 90),
                    profile_version=version, model="test",
                )
            self.assertEqual(db.pending_count(version, respect_live_mode=False), 0)
        finally:
            db.close()

    def test_empty_scheduled_run_emits_the_heartbeat(self):
        # Nothing discovered, nothing deferred: the run must still report in.
        with tempfile.TemporaryDirectory() as tmp:
            settings = self.build_home(tmp)
            out, sizes = self.drive(settings)
            self.assertEqual(sizes, [], "an empty run must not call the model")
            self.assertEqual(
                self.summary_lines(out),
                ["%s RoleLens: no new jobs found." % "🔎"],
            )
            self.assertNotIn("RoleLens found", out)

    def test_heartbeat_is_not_used_while_a_deferred_card_is_delivered(self):
        # Zero new jobs, but a match carried over from an earlier run is being
        # sent: claiming "no new jobs found" alongside a card would contradict it.
        with tempfile.TemporaryDirectory() as tmp:
            settings = self.build_home(tmp)
            self.seed(settings, [("carry-1", "Platform Engineer", "Acme AB", "Stockholm", self.BODY)])
            self.deferred_match(settings)
            out, sizes = self.drive(settings)
            self.assertEqual(sizes, [], "nothing was pending, so no model call")
            self.assertIn("RoleLens found 1 new match", out)
            self.assertEqual(
                self.summary_lines(out),
                ["%s RoleLens: 0 new jobs checked %s 1 match." % ("🎯", "·")],
            )
            self.assertNotIn("no new jobs found", out)

    def test_heartbeat_returns_once_the_deferred_card_has_been_sent(self):
        # The suppression is scoped to the run that actually delivers the card.
        with tempfile.TemporaryDirectory() as tmp:
            settings = self.build_home(tmp)
            self.seed(settings, [("carry-1", "Platform Engineer", "Acme AB", "Stockholm", self.BODY)])
            self.deferred_match(settings)
            first, _ = self.drive(settings)
            self.assertIn("RoleLens found 1 new match", first)
            second, sizes = self.drive(settings)
            self.assertEqual(sizes, [])
            self.assertNotIn("RoleLens found", second)
            self.assertEqual(
                self.summary_lines(second),
                ["%s RoleLens: no new jobs found." % "🔎"],
            )

    def test_heartbeat_never_accompanies_a_match_at_the_formatter(self):
        for matches in (1, 2, 5):
            with self.subTest(matches=matches):
                self.assertNotIn(
                    "no new jobs found",
                    cs.format_run_summary(0, matches, 0, partial=False),
                )
        self.assertEqual(
            cs.format_run_summary(0, 0, 0, partial=False),
            "%s RoleLens: no new jobs found." % "🔎",
        )

if __name__ == "__main__":
    unittest.main()
