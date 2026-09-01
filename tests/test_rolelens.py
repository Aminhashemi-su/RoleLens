from __future__ import annotations

import contextlib
import datetime as dt
import importlib.util
import io
import json
import os
import shutil
import sqlite3
import subprocess
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

    def policy(self, description, level="A2", **overrides):
        """Run the policy layer for a candidate at `level`.

        The level is passed explicitly because the engine no longer assumes one;
        A2 is the default here only so these pre-existing guards keep testing the
        blocking case they were written for.
        """
        item = dict(self.BASE)
        item.update(overrides)
        return cs.normalize_evaluation_policy(
            item, {"description": description},
            swedish=cs.normalize_language_level(level),
        )

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



MANDATORY_PROFESSIONAL_SWEDISH = (
    "You will build an AI-assisted workflow product end to end. Professional English is required. "
    "Fluent Swedish is required because you will independently run weekly customer sessions in Swedish."
)
SWEDISH_AD_NO_REQUIREMENT = (
    "Vi bygger en AI-baserad produkt och söker en utvecklare som arbetar från behovsanalys till "
    "produktion. Du arbetar med React, TypeScript, Python och PostgreSQL i ett litet produktteam."
)


class LanguageLevelTests(unittest.TestCase):
    """The proficiency scale itself: parsing, ordering and unknown handling."""

    def test_cefr_tokens_parse_in_any_case_and_context(self):
        for raw, expected in [
            ("A2", "A2"), ("a2, progressing", "A2"), ("B1 (intermediate)", "B1"),
            ("Swedish: c1", "C1"), ("c2", "C2"), ("A1", "A1"),
        ]:
            self.assertEqual(cs.normalize_language_level(raw).label, expected, raw)

    def test_prose_aliases_map_onto_the_scale(self):
        for raw, rank_of in [
            ("fluent", "C1"), ("Flytande", "C1"), ("native speaker", "C2"),
            ("Native", "C2"), ("advanced", "C1"), ("professional working proficiency", "C1"),
            ("upper intermediate", "B2"), ("intermediate", "B1"),
            ("beginner", "A1"), ("none", "none"),
        ]:
            level = cs.normalize_language_level(raw)
            self.assertEqual(level.rank, cs.CEFR_SCALE.index(rank_of), raw)

    def test_an_explicit_cefr_token_beats_a_prose_alias(self):
        # "working towards fluent" must not be read as already fluent.
        self.assertEqual(cs.normalize_language_level("A2, working towards fluent").label, "A2")

    def test_missing_or_unrecognised_values_stay_unknown(self):
        for raw in ["", None, "unknown", "not specified", "n/a", 42, [], "qwerty level"]:
            level = cs.normalize_language_level(raw)
            self.assertFalse(level.known, repr(raw))
            self.assertIsNone(level.rank, repr(raw))

    def test_unknown_never_satisfies_and_is_not_treated_as_none(self):
        unknown = cs.normalize_language_level("")
        self.assertFalse(unknown.at_least("none"))
        self.assertFalse(unknown.at_least(cs.PROFESSIONAL_LANGUAGE_LEVEL))
        self.assertNotEqual(unknown, cs.normalize_language_level("none"))

    def test_scale_is_ordered(self):
        ranks = [cs.normalize_language_level(x).rank for x in cs.CEFR_SCALE]
        self.assertEqual(ranks, sorted(ranks))
        self.assertTrue(cs.normalize_language_level("C1").at_least("B2"))
        self.assertFalse(cs.normalize_language_level("B2").at_least("C1"))

    def test_level_is_read_from_the_matcher_profile_constraints(self):
        self.assertEqual(
            cs.candidate_language_level({"constraints": {"swedish": "B2"}}, "swedish").label, "B2"
        )
        self.assertEqual(
            cs.candidate_language_level({"languages": {"swedish": "fluent"}}, "swedish").label, "fluent"
        )
        self.assertFalse(cs.candidate_language_level({}, "swedish").known)
        self.assertFalse(cs.candidate_language_level({"constraints": {}}, "swedish").known)

    def test_the_shipped_profile_resolves_to_the_level_it_declares(self):
        """Production semantics are unchanged: the level comes from the profile."""
        path = MODULE_PATH.parent / "profile" / "matcher_profile.json"
        if not path.exists():                       # a public checkout ships the example only
            path = path.with_name("matcher_profile.example.json")
        profile = json.loads(path.read_text(encoding="utf-8"))
        declared = profile["constraints"]["swedish"]
        level = cs.candidate_language_level(profile, "swedish")
        self.assertTrue(level.known, declared)
        self.assertEqual(level, cs.normalize_language_level(declared))


class MandatoryLanguagePolicyTests(unittest.TestCase):
    """Policy outcome as a function of the configured level, not of the code."""

    BASE = dict(SwedishLanguagePolicyTests.BASE)

    def policy(self, description, level):
        return cs.normalize_evaluation_policy(
            dict(self.BASE), {"description": description},
            swedish=cs.normalize_language_level(level),
        )

    def swedish_rows(self, out):
        return [r for r in out["must_have_assessment"]
                if "swedish" in r["requirement"].casefold()]

    def test_a2_against_mandatory_professional_swedish_is_unmet_and_hard(self):
        out = self.policy(MANDATORY_PROFESSIONAL_SWEDISH, "A2")
        self.assertEqual([r["status"] for r in self.swedish_rows(out)], ["unmet"])
        self.assertTrue(any(b["type"] == "hard" for b in out["blockers"]))
        self.assertLessEqual(out["opportunity_score"], 49)
        self.assertEqual(
            cs.classify_decision(out["career_fit"], out["opportunity_score"], out["blockers"]),
            "store_no_notify",
        )

    def test_c1_is_sufficient_for_mandatory_professional_swedish(self):
        out = self.policy(MANDATORY_PROFESSIONAL_SWEDISH, "C1")
        self.assertEqual([r["status"] for r in self.swedish_rows(out)], ["met"])
        self.assertEqual([b for b in out["blockers"] if b["type"] == "hard"], [])
        self.assertEqual(out["opportunity_score"], self.BASE["opportunity_score"])
        self.assertEqual(
            cs.classify_decision(out["career_fit"], out["opportunity_score"], out["blockers"]),
            "notify_strong",
        )

    def test_c2_fluent_and_native_are_all_sufficient(self):
        for level in ["C2", "fluent", "native", "native speaker", "flytande"]:
            out = self.policy(MANDATORY_PROFESSIONAL_SWEDISH, level)
            self.assertEqual([r["status"] for r in self.swedish_rows(out)], ["met"], level)
            self.assertEqual([b for b in out["blockers"] if b["type"] == "hard"], [], level)

    def test_b2_is_a_documented_middle_ground_penalised_not_hard_blocked(self):
        out = self.policy(MANDATORY_PROFESSIONAL_SWEDISH, "B2")
        self.assertEqual([r["status"] for r in self.swedish_rows(out)], ["partial"])
        self.assertEqual([b for b in out["blockers"] if b["type"] == "hard"], [])
        self.assertTrue(any(b["type"] == "strong" for b in out["blockers"]))
        self.assertLessEqual(out["opportunity_score"], 69)

    def test_b1_and_below_are_hard_blocked(self):
        for level in ["B1", "A1", "none"]:
            out = self.policy(MANDATORY_PROFESSIONAL_SWEDISH, level)
            self.assertEqual([r["status"] for r in self.swedish_rows(out)], ["unmet"], level)
            self.assertTrue(any(b["type"] == "hard" for b in out["blockers"]), level)

    def test_unknown_level_stays_unknown_and_is_never_fabricated_as_unmet(self):
        for level in ["", "unknown", None, "not specified"]:
            out = self.policy(MANDATORY_PROFESSIONAL_SWEDISH, level)
            statuses = [r["status"] for r in self.swedish_rows(out)]
            self.assertEqual(statuses, ["unknown"], repr(level))
            self.assertEqual([b for b in out["blockers"] if b["type"] == "hard"], [], repr(level))
            self.assertEqual(out["opportunity_score"], self.BASE["opportunity_score"], repr(level))

    def test_optional_swedish_never_blocks_at_any_level(self):
        description = (
            "Hands-on development and professional English are required. "
            "Swedish is meriterande and considered a plus, but it is not mandatory."
        )
        for level in ["A1", "A2", "B2", "C2", "", "native"]:
            out = self.policy(description, level)
            self.assertEqual([b for b in out["blockers"] if b["type"] == "hard"], [], level)
            self.assertEqual(self.swedish_rows(out), [], level)

    def test_a_swedish_language_ad_alone_infers_no_requirement(self):
        for level in ["A2", "C1", ""]:
            out = self.policy(SWEDISH_AD_NO_REQUIREMENT, level)
            self.assertEqual(self.swedish_rows(out), [], level)
            self.assertEqual([b for b in out["blockers"] if b["type"] == "hard"], [], level)

    def test_negation_vetoes_mandatory_detection_at_every_level(self):
        for level in ["A1", "A2", "C1", ""]:
            out = self.policy(SWEDISH_NOT_REQUIRED, level)
            self.assertNotIn("mandatory_swedish_enforced", out["_policy_changes"], level)
            self.assertEqual([b for b in out["blockers"] if b["type"] == "hard"], [], level)

    def test_tal_och_skrift_wording_is_still_mandatory_detection(self):
        out = self.policy(SWEDISH_MANDATORY_REVERSED, "A2")
        self.assertIn("mandatory_swedish_enforced", out["_policy_changes"])
        self.assertTrue(any(b["type"] == "hard" for b in out["blockers"]))

    def test_explanations_quote_the_configured_level_and_no_other(self):
        """Reasons are generated from the configured level, not from a literal."""
        for level in ["A2", "C1", "B2"]:
            out = self.policy(MANDATORY_PROFESSIONAL_SWEDISH, level)
            blob = json.dumps(out, ensure_ascii=False)
            self.assertIn(level, blob, level)
            for other in {"A2", "C1", "B2"} - {level}:
                self.assertNotIn(other, blob, "%s leaked %s" % (level, other))


class SystemPromptLanguageTests(unittest.TestCase):
    """The prompt describes the configured level and never assumes one."""

    def test_prompt_states_the_configured_level(self):
        for level in ["A2", "B2", "C1", "C2"]:
            prompt = cs.semantic_system_prompt(cs.normalize_language_level(level))
            self.assertIn("Candidate Swedish is %s" % level, prompt)

    def test_prompt_says_unknown_rather_than_inventing_a_level(self):
        prompt = cs.semantic_system_prompt(cs.UNKNOWN_LANGUAGE_LEVEL)
        self.assertIn("not specified", prompt)
        self.assertIn("UNKNOWN", prompt)
        for level in cs.CEFR_SCALE[1:]:
            self.assertNotIn("Candidate Swedish is %s" % level, prompt)

    def test_default_prompt_carries_no_candidate_level(self):
        prompt = cs.semantic_system_prompt()
        for level in cs.CEFR_SCALE[1:]:
            self.assertNotIn("Candidate Swedish is %s" % level, prompt)

    def test_engine_source_contains_no_candidate_specific_level(self):
        """Guard against the hardcoding regressing into the engine."""
        source = MODULE_PATH.read_text(encoding="utf-8")
        for phrase in ["level is A2", "unmet at A2", "above A2", "Swedish is A2 and progressing"]:
            self.assertNotIn(phrase, source, phrase)


class RunSummaryTests(unittest.TestCase):
    """The one compact stats line the scheduler delivers per run."""

    def summary(self, evaluated, matches, queued=0, failed=False):
        return cs.format_run_summary(evaluated, matches, queued, failed=failed)

    def test_no_new_jobs_evaluated(self):
        self.assertEqual(
            self.summary(0, 0),
            "🔎 RoleLens: no new jobs found.",
        )

    def test_jobs_evaluated_without_matches(self):
        self.assertEqual(
            self.summary(7, 0),
            "🔎 RoleLens: 7 jobs checked · 0 matches.",
        )

    def test_single_match_is_singular(self):
        self.assertEqual(
            self.summary(9, 1),
            "🎯 RoleLens: 9 jobs checked · 1 match.",
        )

    def test_multiple_matches_are_plural(self):
        self.assertEqual(
            self.summary(10, 4),
            "🎯 RoleLens: 10 jobs checked · 4 matches.",
        )

    def test_partial_run_reports_pending_retry(self):
        self.assertEqual(
            self.summary(6, 2, queued=4, failed=True),
            "⚠️ RoleLens: 6 jobs checked · 2 matches · 4 pending after provider error.",
        )

    def test_partial_run_keeps_singular_match(self):
        self.assertEqual(
            self.summary(6, 1, queued=5, failed=True),
            "⚠️ RoleLens: 6 jobs checked · 1 match · 5 pending after provider error.",
        )

    def test_partial_wins_over_the_zero_evaluated_wording(self):
        # A provider failure that returned nothing must not read as a quiet
        # 'no new jobs found' tick.
        self.assertEqual(
            self.summary(0, 0, queued=10, failed=True),
            "⚠️ RoleLens: 0 jobs checked · 0 matches · 10 pending after provider error.",
        )

    def test_target_icon_only_when_there_are_matches(self):
        self.assertTrue(self.summary(5, 0).startswith("🔎"))
        self.assertTrue(self.summary(5, 1).startswith("🎯"))
        self.assertTrue(self.summary(0, 0).startswith("🔎"))
        self.assertTrue(self.summary(5, 1, 2, failed=True).startswith("⚠️"))

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
            cs.format_run_summary(stats.evaluated, stats.notified, 0),
            "🎯 RoleLens: 10 jobs checked · 4 matches.",
        )

    def test_detailed_notifications_are_unchanged_by_the_summary(self):
        # format_notifications still owns the detailed block; the summary is a
        # separate single line and must not appear inside it.
        self.assertEqual(cs.format_notifications([]), "")
        self.assertNotIn("pending retry", cs.format_notifications([]))



class _PipelineHarness(unittest.TestCase):
    """Real pipeline, real SQLite, scripted provider.

    Only ProviderMatcher is replaced, so evaluate_with_fallback - and therefore
    the Azure fallback rule - is exercised for real.
    """

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
            (f"job{i:04d}", f"AI Engineer {i}", f"Company {i}", "Stockholm",
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

    def drive(self, settings, script=None, scores=None):
        """Run the pipeline. `script(call_index, ids, provider)` returns an action.

        Actions: "ok", ("omit", n), "output", "raise".
        Returns (stdout, calls) where calls is [(provider, (ids...)), ...].
        """
        calls: list[tuple[str, tuple[str, ...]]] = []
        test = self

        def action_for(index, ids, provider):
            if script is None:
                return "ok"
            if callable(script):
                return script(index, ids, provider)
            return script[index] if index < len(script) else "ok"

        class Stub:
            def __init__(self, settings_, secrets, profile, rules, provider):
                self.provider = provider
                self.model = "gemini-3.7-flash" if provider == cs.PRIMARY_PROVIDER else "gpt-5-mini"

            def evaluate(self, jobs):
                ids = [str(r["source_job_id"]) for r in jobs]
                index = len(calls)
                calls.append((self.provider, tuple(ids)))
                action = action_for(index, ids, self.provider)
                if action == "raise":
                    raise cs.TemporaryProviderError(self.provider, "503 upstream unavailable")
                if action == "output":
                    return cs.ProviderBatchResult(
                        self.provider, self.model, (), frozenset(ids),
                        cs.empty_usage(), "invalid JSON: boom", "output")
                omit = action[1] if isinstance(action, tuple) and action[0] == "omit" else 0
                keep = ids[: len(ids) - omit] if omit else ids
                evals = tuple(
                    test.evaluation(sid, (scores or {}).get(sid, 30)) for sid in keep
                )
                missing = frozenset(ids[len(keep):])
                return cs.ProviderBatchResult(
                    self.provider, self.model, evals, missing, cs.empty_usage(),
                    ("missing/invalid IDs: " + ", ".join(sorted(missing))) if missing else None,
                    "completeness" if missing else None,
                )

        original = cs.ProviderMatcher
        cs.ProviderMatcher = Stub
        buffer = io.StringIO()
        try:
            with contextlib.redirect_stdout(buffer):
                cs.run_pipeline(settings, fetch_only=False, evaluate_only=True)
        finally:
            cs.ProviderMatcher = original
        return buffer.getvalue(), calls

    def summary_lines(self, out):
        icons = ("\U0001f50e RoleLens:", "\U0001f3af RoleLens:", "\u26a0\ufe0f RoleLens:")
        return [ln for ln in out.splitlines() if ln.startswith(icons)]

    def pending(self, settings):
        db = cs.Database(settings.db_path)
        try:
            _, _, version = cs.load_profile_bundle(settings)
            return db.pending_count(version, respect_live_mode=False)
        finally:
            db.close()


class SnapshotCompletenessTests(_PipelineHarness):
    """Every frozen candidate is attempted exactly once, whatever the count."""

    def check(self, count):
        with tempfile.TemporaryDirectory() as tmp:
            settings = self.build_home(tmp)
            self.seed(settings, self.distinct(count))
            out, calls = self.drive(settings)
            sizes = [len(ids) for _, ids in calls]
            attempted = [i for _, ids in calls for i in ids]
            self.assertEqual(len(attempted), count, "every candidate attempted")
            self.assertEqual(len(set(attempted)), count, "no candidate attempted twice")
            self.assertEqual(sum(sizes), count)
            self.assertTrue(all(n <= 10 for n in sizes), f"batch larger than 10: {sizes}")
            self.assertEqual(len(sizes), -(-count // 10) if count else 0)
            self.assertEqual(self.pending(settings), 0, "nothing left pending")
            return out, sizes

    def test_zero_candidates(self):
        out, sizes = self.check(0)
        self.assertEqual(sizes, [])
        self.assertEqual(self.summary_lines(out), ["\U0001f50e RoleLens: no new jobs found."])

    def test_one_candidate(self):
        out, sizes = self.check(1)
        self.assertEqual(sizes, [1])
        self.assertEqual(self.summary_lines(out), ["\U0001f50e RoleLens: 1 jobs checked \u00b7 0 matches."])

    def test_nine_candidates(self):
        self.assertEqual(self.check(9)[1], [9])

    def test_ten_candidates(self):
        self.assertEqual(self.check(10)[1], [10])

    def test_eleven_candidates(self):
        self.assertEqual(self.check(11)[1], [10, 1])

    def test_thirty_candidates(self):
        self.assertEqual(self.check(30)[1], [10, 10, 10])

    def test_forty_candidates(self):
        self.assertEqual(self.check(40)[1], [10, 10, 10, 10])

    def test_forty_one_candidates_no_longer_truncate(self):
        # The old build capped at 40; candidate 41 must now be evaluated too.
        out, sizes = self.check(41)
        self.assertEqual(sizes, [10, 10, 10, 10, 1])
        self.assertEqual(self.summary_lines(out), ["\U0001f50e RoleLens: 41 jobs checked \u00b7 0 matches."])

    def test_sixty_five_candidates(self):
        out, sizes = self.check(65)
        self.assertEqual(sizes, [10, 10, 10, 10, 10, 10, 5])
        self.assertEqual(self.summary_lines(out), ["\U0001f50e RoleLens: 65 jobs checked \u00b7 0 matches."])

    def test_one_hundred_three_candidates(self):
        out, sizes = self.check(103)
        self.assertEqual(sizes, [10] * 10 + [3])
        self.assertEqual(self.summary_lines(out), ["\U0001f50e RoleLens: 103 jobs checked \u00b7 0 matches."])

    def test_snapshot_is_frozen_not_re_queried(self):
        # A job that appears after the snapshot is taken belongs to the next run.
        with tempfile.TemporaryDirectory() as tmp:
            settings = self.build_home(tmp)
            self.seed(settings, self.distinct(12))
            inserted = {"done": False}
            test = self

            def script(index, ids, provider):
                if not inserted["done"]:
                    test.seed(settings, [("late-1", "Late Job", "Late AB", "Stockholm", "Arrived mid-run.")])
                    inserted["done"] = True
                return "ok"

            out, calls = self.drive(settings, script=script)
            attempted = [i for _, ids in calls for i in ids]
            self.assertNotIn("late-1", attempted, "mid-run arrival must not join this snapshot")
            self.assertEqual(len(attempted), 12)


class CompletenessGapTests(_PipelineHarness):
    """A short batch is a gap, not a failure, and gets exactly one cleanup pass."""

    def test_nine_of_ten_does_not_stop_later_batches(self):
        with tempfile.TemporaryDirectory() as tmp:
            settings = self.build_home(tmp)
            self.seed(settings, self.distinct(30))
            out, calls = self.drive(settings, script=[("omit", 1)])
            normal = [ids for _, ids in calls][:3]
            self.assertEqual([len(x) for x in normal], [10, 10, 10], "all normal batches ran")
            self.assertGreaterEqual(len(calls), 4, "a cleanup call must follow")

    def test_omitted_ids_are_collected_into_one_cleanup_pass(self):
        with tempfile.TemporaryDirectory() as tmp:
            settings = self.build_home(tmp)
            self.seed(settings, self.distinct(30))
            _, calls = self.drive(settings, script=[("omit", 1), ("omit", 1), "ok"])
            self.assertEqual(len(calls), 4, "3 normal batches + exactly 1 cleanup batch")
            cleanup_ids = set(calls[3][1])
            self.assertEqual(len(cleanup_ids), 2, "both omitted IDs retried together")
            first_two = set(calls[0][1]) | set(calls[1][1])
            self.assertTrue(cleanup_ids <= first_two)

    def test_cleanup_success_reaches_full_completeness(self):
        with tempfile.TemporaryDirectory() as tmp:
            settings = self.build_home(tmp)
            self.seed(settings, self.distinct(65))
            out, calls = self.drive(settings, script=[("omit", 1)])
            self.assertEqual(len(calls), 8, "7 normal batches + 1 cleanup")
            self.assertEqual(
                self.summary_lines(out),
                ["\U0001f50e RoleLens: 65 jobs checked \u00b7 0 matches."],
            )
            self.assertEqual(self.pending(settings), 0)

    def test_cleanup_failure_leaves_only_the_unresolved_id_pending(self):
        with tempfile.TemporaryDirectory() as tmp:
            settings = self.build_home(tmp)
            self.seed(settings, self.distinct(65))
            out, calls = self.drive(
                settings, script=lambda i, ids, p: ("omit", 1) if i in (0, 7) else "ok")
            self.assertEqual(
                self.summary_lines(out),
                ["\U0001f50e RoleLens: 64 jobs checked \u00b7 0 matches \u00b7 1 queued for next run."],
            )
            self.assertEqual(self.pending(settings), 1)

    def test_cleanup_is_attempted_at_most_once(self):
        with tempfile.TemporaryDirectory() as tmp:
            settings = self.build_home(tmp)
            self.seed(settings, self.distinct(20))
            _, calls = self.drive(settings, script=lambda i, ids, p: ("omit", 1))
            self.assertEqual(len(calls), 3, "2 normal + 1 cleanup, never a retry tree")
            attempts = {}
            for _, ids in calls:
                for i in ids:
                    attempts[i] = attempts.get(i, 0) + 1
            self.assertLessEqual(max(attempts.values()), 2, "no ID attempted more than twice")

    def test_missing_ids_never_call_azure(self):
        with tempfile.TemporaryDirectory() as tmp:
            settings = self.build_home(tmp)
            self.seed(settings, self.distinct(20))
            _, calls = self.drive(settings, script=lambda i, ids, p: ("omit", 2))
            providers = {p for p, _ in calls}
            self.assertEqual(providers, {cs.PRIMARY_PROVIDER},
                             "a completeness gap is not a transport failure")


class TransportFailureTests(_PipelineHarness):
    def test_azure_still_covers_a_genuine_gemini_transport_failure(self):
        with tempfile.TemporaryDirectory() as tmp:
            settings = self.build_home(tmp)
            self.seed(settings, self.distinct(20))
            # Only the first Gemini call fails; Azure answers it.
            out, calls = self.drive(
                settings,
                script=lambda i, ids, p: "raise" if (i == 0 and p == cs.PRIMARY_PROVIDER) else "ok",
            )
            self.assertEqual(calls[0][0], cs.PRIMARY_PROVIDER)
            self.assertEqual(calls[1][0], cs.FALLBACK_PROVIDER, "Azure took the failed batch")
            self.assertEqual(calls[1][1], calls[0][1], "same batch, one retry")
            self.assertEqual(self.pending(settings), 0)
            self.assertEqual(
                self.summary_lines(out),
                ["\U0001f50e RoleLens: 20 jobs checked \u00b7 0 matches."],
            )

    def test_both_providers_failing_stops_cleanly_and_keeps_jobs(self):
        with tempfile.TemporaryDirectory() as tmp:
            settings = self.build_home(tmp)
            self.seed(settings, self.distinct(40))
            out, calls = self.drive(
                settings, script=lambda i, ids, p: "raise" if i in (2, 3) else "ok")
            # Batches 1-2 succeed; batch 3 fails on both providers and stops the run.
            self.assertEqual(self.pending(settings), 20, "nothing lost")
            line = self.summary_lines(out)[0]
            self.assertTrue(line.startswith("\u26a0\ufe0f RoleLens:"), line)
            self.assertIn("pending after provider error", line)
            self.assertIn("20 jobs checked", line)

    def test_unparseable_envelope_stops_the_run_as_a_failure(self):
        with tempfile.TemporaryDirectory() as tmp:
            settings = self.build_home(tmp)
            self.seed(settings, self.distinct(30))
            out, calls = self.drive(settings, script=["ok", "output"])
            self.assertEqual(len(calls), 2, "no cleanup pass after a provider failure")
            line = self.summary_lines(out)[0]
            self.assertIn("pending after provider error", line)
            self.assertEqual(self.pending(settings), 20)


class RankingAndDeliveryTests(_PipelineHarness):
    def test_ranking_is_global_across_every_batch(self):
        with tempfile.TemporaryDirectory() as tmp:
            settings = self.build_home(tmp)
            self.seed(settings, self.distinct(30))
            # The strongest opportunity sits in the final batch.
            scores = {"job0002": 80, "job0015": 88, "job0029": 95}
            out, calls = self.drive(settings, scores=scores)
            order = [ln for ln in out.splitlines() if ln.startswith(("\U0001f7e2", "\U0001f7e1"))]
            self.assertEqual(len(order), 3, out)
            self.assertIn("AI Engineer 29", order[0], "highest opportunity ranked first")
            self.assertIn("AI Engineer 15", order[1])
            self.assertIn("AI Engineer 2 ", order[2] + " ")
            self.assertEqual(
                self.summary_lines(out),
                ["\U0001f3af RoleLens: 30 jobs checked \u00b7 3 matches."],
            )

    def test_notifications_wait_until_semantic_processing_finishes(self):
        with tempfile.TemporaryDirectory() as tmp:
            settings = self.build_home(tmp)
            self.seed(settings, self.distinct(30))
            seen_stdout_during_calls = []
            test = self

            def script(index, ids, provider):
                # Nothing may be delivered while batches are still running.
                seen_stdout_during_calls.append(sys.stdout.getvalue())
                return "ok"

            scores = {"job0001": 90, "job0030": 92}
            out, calls = self.drive(settings, script=script, scores=scores)
            self.assertEqual(len(calls), 3)
            self.assertTrue(all(x == "" for x in seen_stdout_during_calls),
                            "output appeared before the run finished")
            # A match from the first and the last batch arrive in the same report.
            self.assertIn("AI Engineer 1 ", out + " ")
            self.assertIn("AI Engineer 30", out)
            self.assertEqual(
                self.summary_lines(out),
                ["\U0001f3af RoleLens: 30 jobs checked \u00b7 2 matches."],
            )

    def test_summary_counts_aggregate_the_whole_frozen_run(self):
        with tempfile.TemporaryDirectory() as tmp:
            settings = self.build_home(tmp)
            self.seed(settings, self.distinct(65))
            scores = {f"job{i:04d}": 90 for i in (3, 24, 41, 60, 65)}
            out, calls = self.drive(settings, scores=scores)
            self.assertEqual(len(calls), 7)
            self.assertEqual(
                self.summary_lines(out),
                ["\U0001f3af RoleLens: 65 jobs checked \u00b7 5 matches."],
            )


class RuntimeBudgetTests(_PipelineHarness):
    def test_emergency_limit_exits_safely_without_losing_jobs(self):
        with tempfile.TemporaryDirectory() as tmp:
            settings = self.build_home(tmp, max_run_seconds=1)
            self.seed(settings, self.distinct(65))
            out, calls = self.drive(settings)
            self.assertEqual(len(calls), 1, "only the first batch fits a 1s budget")
            self.assertEqual(self.pending(settings), 55, "every unevaluated job kept")
            self.assertEqual(
                self.summary_lines(out),
                ["\U0001f50e RoleLens: 10 jobs checked \u00b7 0 matches \u00b7 55 queued for next run."],
            )

    def test_budget_deferral_is_not_reported_as_a_provider_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            settings = self.build_home(tmp, max_run_seconds=1)
            self.seed(settings, self.distinct(30))
            out, _ = self.drive(settings)
            line = self.summary_lines(out)[0]
            self.assertNotIn("provider error", line)
            self.assertIn("queued for next run", line)


class DuplicateSuppressionTests(_PipelineHarness):
    BODY = "We are hiring a platform engineer to build and operate internal developer tooling."

    def test_identical_repost_with_a_new_source_id_is_suppressed(self):
        with tempfile.TemporaryDirectory() as tmp:
            settings = self.build_home(tmp)
            self.seed(settings, [
                ("orig-1", "Platform Engineer", "Acme AB", "Stockholm", self.BODY),
                ("repost-2", "Platform Engineer", "Acme AB", "Stockholm", self.BODY),
            ])
            out, calls = self.drive(settings)
            self.assertEqual(sum(len(ids) for _, ids in calls), 1, "the repost must not reach the model")

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
                self.assertEqual(db.conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0], 2,
                                 "both source rows survive")
            finally:
                db.close()
            self.assertEqual(len(rows), 1)
            self.assertIn("Repost of job", rows[0]["reason"])
            self.assertNotEqual(rows[0]["job_id"], rows[0]["canonical_job_id"])

    def test_different_description_is_not_suppressed(self):
        with tempfile.TemporaryDirectory() as tmp:
            settings = self.build_home(tmp)
            self.seed(settings, [
                ("a-1", "Member of Technical Staff", "Northwind Labs AB", "Stockholm",
                 "You will build end-to-end product features across the full stack."),
                ("a-2", "Member of Technical Staff", "Northwind Labs AB", "Stockholm",
                 "You will lead architecture for a senior platform team and mentor engineers."),
            ])
            _, calls = self.drive(settings)
            self.assertEqual(sum(len(ids) for _, ids in calls), 2)

    def test_different_location_is_not_suppressed(self):
        with tempfile.TemporaryDirectory() as tmp:
            settings = self.build_home(tmp)
            self.seed(settings, [
                ("loc-1", "Sourcing Manager", "Baltic Energy AB", "Solna", self.BODY),
                ("loc-2", "Sourcing Manager", "Baltic Energy AB", "Goteborg", self.BODY),
            ])
            _, calls = self.drive(settings)
            self.assertEqual(sum(len(ids) for _, ids in calls), 2)

    def test_duplicate_matches_do_not_produce_duplicate_cards(self):
        with tempfile.TemporaryDirectory() as tmp:
            settings = self.build_home(tmp)
            self.seed(settings, [
                ("dup-1", "Senior Software Engineer", "DataJob AB", "Stockholm", self.BODY),
                ("dup-2", "Senior Software Engineer", "DataJob AB", "Stockholm", self.BODY),
            ])
            db = cs.Database(settings.db_path)
            try:
                _, _, version = cs.load_profile_bundle(settings)
                for row in db.pending_jobs(version, 10, respect_live_mode=False):
                    db.save_evaluation(row, self.evaluation(row["source_job_id"], 90),
                                       profile_version=version, model="test")
            finally:
                db.close()
            out, _ = self.drive(settings)
            self.assertEqual(out.count("Senior Software Engineer"), 1, "one vacancy, one card")
            self.assertEqual(
                self.summary_lines(out),
                ["\U0001f3af RoleLens: 0 jobs checked \u00b7 1 match."],
            )


class DuplicateFingerprintTests(unittest.TestCase):
    BODY = "Build and operate internal developer tooling for a product team."

    def fp(self, company="Acme AB", title="Platform Engineer", location="Stockholm", body=None):
        return cs.duplicate_fingerprint(company, title, location, self.BODY if body is None else body)

    def test_formatting_noise_does_not_change_the_fingerprint(self):
        self.assertEqual(
            self.fp(),
            self.fp(company="  ACME   ab ", title="Platform   Engineer!",
                    body="  Build and, operate internal developer tooling for a product team.  "),
        )

    def test_employer_title_location_and_body_each_change_it(self):
        base = self.fp()
        self.assertNotEqual(base, self.fp(company="Other AB"))
        self.assertNotEqual(base, self.fp(title="Senior Platform Engineer"))
        self.assertNotEqual(base, self.fp(location="Goteborg"))
        self.assertNotEqual(base, self.fp(body="A completely different role description entirely."))


class EmptyRunHeartbeatTests(_PipelineHarness):
    """A quiet scheduled run must still report in, without contradicting itself."""

    BODY = "Build internal tooling with Python, TypeScript and cloud services."

    def deferred_match(self, settings):
        db = cs.Database(settings.db_path)
        try:
            _, _, version = cs.load_profile_bundle(settings)
            for row in db.pending_jobs(version, 10, respect_live_mode=False):
                db.save_evaluation(row, self.evaluation(row["source_job_id"], 90),
                                   profile_version=version, model="test")
        finally:
            db.close()

    def test_empty_scheduled_run_emits_the_heartbeat(self):
        with tempfile.TemporaryDirectory() as tmp:
            settings = self.build_home(tmp)
            out, calls = self.drive(settings)
            self.assertEqual(calls, [], "an empty run must not call the model")
            self.assertEqual(self.summary_lines(out), ["\U0001f50e RoleLens: no new jobs found."])
            self.assertNotIn("RoleLens found", out)

    def test_heartbeat_is_not_used_while_a_deferred_card_is_delivered(self):
        with tempfile.TemporaryDirectory() as tmp:
            settings = self.build_home(tmp)
            self.seed(settings, [("carry-1", "Platform Engineer", "Acme AB", "Stockholm", self.BODY)])
            self.deferred_match(settings)
            out, calls = self.drive(settings)
            self.assertEqual(calls, [])
            self.assertEqual(
                self.summary_lines(out),
                ["\U0001f3af RoleLens: 0 jobs checked \u00b7 1 match."],
            )
            self.assertNotIn("no new jobs found", out)

    def test_heartbeat_returns_once_the_deferred_card_has_been_sent(self):
        with tempfile.TemporaryDirectory() as tmp:
            settings = self.build_home(tmp)
            self.seed(settings, [("carry-1", "Platform Engineer", "Acme AB", "Stockholm", self.BODY)])
            self.deferred_match(settings)
            self.drive(settings)
            second, _ = self.drive(settings)
            self.assertEqual(self.summary_lines(second), ["\U0001f50e RoleLens: no new jobs found."])

    def test_heartbeat_never_accompanies_a_match_at_the_formatter(self):
        for matches in (1, 2, 5):
            with self.subTest(matches=matches):
                self.assertNotIn("no new jobs found", cs.format_run_summary(0, matches, 0))
        self.assertEqual(cs.format_run_summary(0, 0, 0), "\U0001f50e RoleLens: no new jobs found.")



class SafetyCeilingTests(_PipelineHarness):
    """The 300-candidate ceiling is emergency protection, never a silent cap."""

    def test_ceiling_run_is_reported_as_incomplete(self):
        with tempfile.TemporaryDirectory() as tmp:
            settings = self.build_home(tmp, max_candidates_per_run=20)
            self.seed(settings, self.distinct(35))
            out, calls = self.drive(settings)
            attempted = [i for _, ids in calls for i in ids]
            self.assertEqual(len(attempted), 20, "only the ceiling is processed")
            line = self.summary_lines(out)[0]
            self.assertEqual(
                line,
                "\u26a0\ufe0f RoleLens: candidate safety ceiling reached, 20 selected "
                "\u00b7 0 matches \u00b7 additional jobs remain queued.",
            )
            self.assertEqual(self.pending(settings), 15, "the remainder stays queued")

    def test_ceiling_report_still_names_the_matches(self):
        with tempfile.TemporaryDirectory() as tmp:
            settings = self.build_home(tmp, max_candidates_per_run=20)
            self.seed(settings, self.distinct(35))
            scores = {"job0035": 90, "job0034": 92}
            out, _ = self.drive(settings, scores=scores)
            line = self.summary_lines(out)[0]
            self.assertIn("candidate safety ceiling reached, 20 selected", line)
            self.assertIn("2 matches", line)

    def test_exactly_at_the_ceiling_is_not_flagged(self):
        # 20 eligible and a ceiling of 20 is a complete snapshot, not a truncation.
        with tempfile.TemporaryDirectory() as tmp:
            settings = self.build_home(tmp, max_candidates_per_run=20)
            self.seed(settings, self.distinct(20))
            out, _ = self.drive(settings)
            self.assertEqual(
                self.summary_lines(out),
                ["\U0001f50e RoleLens: 20 jobs checked \u00b7 0 matches."],
            )
            self.assertEqual(self.pending(settings), 0)

    def test_normal_run_never_reports_the_ceiling(self):
        with tempfile.TemporaryDirectory() as tmp:
            settings = self.build_home(tmp)
            self.seed(settings, self.distinct(65))
            out, _ = self.drive(settings)
            self.assertNotIn("safety ceiling", out)

    def test_ceiling_wording_at_the_formatter(self):
        self.assertEqual(
            cs.format_run_summary(300, 4, 0, ceiling_reached=True, selected=300),
            "\u26a0\ufe0f RoleLens: candidate safety ceiling reached, 300 selected "
            "\u00b7 4 matches \u00b7 additional jobs remain queued.",
        )
        # A genuine provider failure outranks the ceiling notice.
        self.assertIn(
            "pending after provider error",
            cs.format_run_summary(10, 0, 290, failed=True, ceiling_reached=True, selected=300),
        )


class NotificationHeaderTests(_PipelineHarness):
    """Cards are emitted directly; only the final summary states totals."""

    def test_no_header_line_precedes_the_cards(self):
        with tempfile.TemporaryDirectory() as tmp:
            settings = self.build_home(tmp)
            self.seed(settings, self.distinct(20))
            scores = {"job0003": 90, "job0011": 92}
            out, _ = self.drive(settings, scores=scores)
            self.assertNotIn("new matches", out)
            self.assertNotIn("new match", out)
            # Assert on the removed header's shape, not the bare brand: the
            # closing summary line legitimately contains the product name.
            self.assertNotIn("RoleLens found", out)
            self.assertNotIn("RoleLens found", out)

    def test_output_starts_with_a_card_and_ends_with_one_summary(self):
        with tempfile.TemporaryDirectory() as tmp:
            settings = self.build_home(tmp)
            self.seed(settings, self.distinct(20))
            scores = {"job0003": 90, "job0011": 92}
            out, _ = self.drive(settings, scores=scores)
            body = [ln for ln in out.splitlines() if ln.strip()]
            self.assertTrue(body[0].startswith("\U0001f7e2"), body[0])
            self.assertEqual(
                body[-1],
                "\U0001f3af RoleLens: 20 jobs checked \u00b7 2 matches.",
            )
            self.assertEqual(len(self.summary_lines(out)), 1)

    def test_formatter_emits_no_header_for_a_single_row(self):
        with tempfile.TemporaryDirectory() as tmp:
            settings = self.build_home(tmp)
            self.seed(settings, self.distinct(5))
            scores = {"job0002": 95}
            out, _ = self.drive(settings, scores=scores)
            self.assertNotIn("1 new match", out)
            self.assertTrue(out.lstrip().startswith("\U0001f7e2"), out[:80])

class FreshCloneInstallTests(unittest.TestCase):
    """install.sh must work from a checkout that ships only *.example files.

    The suite runs on Linux and macOS. On a filesystem that cannot chmod, the
    permission assertions are skipped rather than reported as failures; the
    install logic itself is still exercised.
    """

    REPO = MODULE_PATH.parent
    SCRIPT = MODULE_PATH.parent / "install.sh"

    @classmethod
    def setUpClass(cls):
        if shutil.which("bash") is None:
            raise unittest.SkipTest("bash is not available")
        if not cls.SCRIPT.exists():
            raise unittest.SkipTest("install.sh is not in this checkout")

    @staticmethod
    def chmod_is_meaningful(directory):
        probe = Path(directory) / ".chmod_probe"
        probe.write_text("x", encoding="utf-8")
        try:
            probe.chmod(0o600)
            return (probe.stat().st_mode & 0o777) == 0o600
        except OSError:
            return False
        finally:
            probe.unlink(missing_ok=True)

    def fresh_checkout(self, root):
        """A checkout carrying only what a public clone ships."""
        src = Path(root) / "checkout"
        (src / "profile").mkdir(parents=True)
        shutil.copy2(self.SCRIPT, src / "install.sh")
        shutil.copy2(MODULE_PATH, src / MODULE_PATH.name)

        def place(name, *candidates):
            for candidate in candidates:
                origin = self.REPO / candidate
                if origin.exists():
                    shutil.copy2(origin, src / name)
                    return
            self.fail("no source for %s" % name)

        place("config.example.json", "config.example.json", "config.json")
        place("secrets.env.example", "secrets.env.example")
        place("profile/matcher_rules_v1_1.json", "profile/matcher_rules_v1_1.json")
        for stem in ("career_profile", "matcher_profile", "search_lenses"):
            place("profile/%s.example.json" % stem,
                  "profile/%s.example.json" % stem,
                  "public/profile/%s.example.json" % stem,
                  "profile/%s.json" % stem)
        return src

    def run_install(self, src, home, scripts, *args):
        env = dict(os.environ)
        env["ROLELENS_HOME"] = str(home)
        env["ROLELENS_SCRIPTS_HOME"] = str(scripts)
        return subprocess.run(
            ["bash", str(src / "install.sh"), *args],
            capture_output=True, text=True, env=env, cwd=str(src),
        )

    def test_fresh_clone_installs_from_examples(self):
        with tempfile.TemporaryDirectory() as tmp:
            src = self.fresh_checkout(tmp)
            home = Path(tmp) / "home"
            result = self.run_install(src, home, Path(tmp) / "scripts")
            self.assertEqual(result.returncode, 0, result.stderr)

            for rel in ("config.json", "secrets.env",
                        "profile/career_profile.json", "profile/matcher_profile.json",
                        "profile/search_lenses.json", "profile/matcher_rules_v1_1.json"):
                self.assertTrue((home / rel).is_file(), rel)
            self.assertIn("Created", result.stdout)
            self.assertIn("config.json", result.stdout)

    def test_installed_files_are_valid_json_the_app_can_load(self):
        with tempfile.TemporaryDirectory() as tmp:
            src = self.fresh_checkout(tmp)
            home = Path(tmp) / "home"
            self.run_install(src, home, Path(tmp) / "scripts")
            for rel in ("config.json", "profile/matcher_profile.json",
                        "profile/matcher_rules_v1_1.json"):
                json.loads((home / rel).read_text(encoding="utf-8"))
            settings = cs.Settings.load(home)
            self.assertEqual(settings.primary_provider, cs.PRIMARY_PROVIDER)

    def test_rerunning_does_not_overwrite_user_edits(self):
        with tempfile.TemporaryDirectory() as tmp:
            src = self.fresh_checkout(tmp)
            home = Path(tmp) / "home"
            scripts = Path(tmp) / "scripts"
            self.run_install(src, home, scripts)

            edited = json.loads((home / "config.json").read_text(encoding="utf-8"))
            edited["search_terms"] = ["my own term"]
            (home / "config.json").write_text(json.dumps(edited), encoding="utf-8")
            (home / "secrets.env").write_text("VERTEX_GEMINI_API_KEY=mine\n", encoding="utf-8")
            profile = home / "profile" / "matcher_profile.json"
            mine = json.loads(profile.read_text(encoding="utf-8"))
            mine["constraints"]["swedish"] = "C1"
            profile.write_text(json.dumps(mine), encoding="utf-8")

            result = self.run_install(src, home, scripts)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(
                json.loads((home / "config.json").read_text(encoding="utf-8"))["search_terms"],
                ["my own term"],
            )
            self.assertEqual((home / "secrets.env").read_text(encoding="utf-8"),
                             "VERTEX_GEMINI_API_KEY=mine\n")
            self.assertEqual(
                json.loads(profile.read_text(encoding="utf-8"))["constraints"]["swedish"], "C1"
            )
            self.assertIn("Kept your existing files", result.stdout)

    def test_refresh_config_overwrites_config_but_never_secrets(self):
        with tempfile.TemporaryDirectory() as tmp:
            src = self.fresh_checkout(tmp)
            home = Path(tmp) / "home"
            scripts = Path(tmp) / "scripts"
            self.run_install(src, home, scripts)
            (home / "config.json").write_text('{"broken": true}', encoding="utf-8")
            (home / "secrets.env").write_text("VERTEX_GEMINI_API_KEY=mine\n", encoding="utf-8")

            result = self.run_install(src, home, scripts, "--refresh-config")
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("search_terms",
                          json.loads((home / "config.json").read_text(encoding="utf-8")))
            self.assertEqual((home / "secrets.env").read_text(encoding="utf-8"),
                             "VERTEX_GEMINI_API_KEY=mine\n")

    def test_missing_template_fails_loudly_and_creates_nothing_empty(self):
        with tempfile.TemporaryDirectory() as tmp:
            src = self.fresh_checkout(tmp)
            (src / "config.example.json").unlink()
            home = Path(tmp) / "home"
            result = self.run_install(src, home, Path(tmp) / "scripts")
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("cannot install config.json", result.stderr)
            self.assertFalse((home / "config.json").exists())

    def test_secrets_permissions_are_restrictive(self):
        with tempfile.TemporaryDirectory() as tmp:
            if not self.chmod_is_meaningful(tmp):
                self.skipTest("filesystem does not honour chmod")
            src = self.fresh_checkout(tmp)
            home = Path(tmp) / "home"
            self.run_install(src, home, Path(tmp) / "scripts")
            self.assertEqual((home / "secrets.env").stat().st_mode & 0o777, 0o600)
            self.assertEqual((home / "config.json").stat().st_mode & 0o777, 0o600)
            self.assertEqual(home.stat().st_mode & 0o777, 0o700)

    def test_unknown_argument_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            src = self.fresh_checkout(tmp)
            result = self.run_install(src, Path(tmp) / "home", Path(tmp) / "scripts", "--wat")
            self.assertEqual(result.returncode, 2)
            self.assertIn("unknown argument", result.stderr)


class DoctorOnboardingTests(unittest.TestCase):
    """doctor distinguishes missing / template / no-credentials / ready."""

    REPO = MODULE_PATH.parent

    def build(self, tmp, *, credentials=True, personalise=False):
        home = Path(tmp)
        (home / "profile").mkdir(parents=True, exist_ok=True)
        (home / "data").mkdir(parents=True, exist_ok=True)

        def pick(*candidates):
            for candidate in candidates:
                path = self.REPO / candidate
                if path.exists():
                    return path
            self.fail("no source for %s" % (candidates,))

        shutil.copy2(pick("config.example.json", "config.json"), home / "config.json")
        shutil.copy2(pick("profile/matcher_rules_v1_1.json"),
                     home / "profile" / "matcher_rules_v1_1.json")
        for stem in ("career_profile", "matcher_profile", "search_lenses"):
            shutil.copy2(
                pick("public/profile/%s.example.json" % stem,
                     "profile/%s.example.json" % stem),
                home / "profile" / ("%s.json" % stem),
            )
        if personalise:
            for stem in ("career_profile", "matcher_profile", "search_lenses"):
                path = home / "profile" / ("%s.json" % stem)
                data = json.loads(path.read_text(encoding="utf-8"))
                data.pop("profile_status", None)
                blob = json.dumps(data, ensure_ascii=False)
                for marker in cs.TEMPLATE_MARKERS:
                    blob = blob.replace(marker.replace('\\"', '"'), "personalised")
                path.write_text(blob, encoding="utf-8")
        secrets = (
            "VERTEX_GEMINI_API_KEY=k\nVERTEX_GEMINI_MODEL=gemini-3.7-flash\n"
            "AZURE_OPENAI_API_KEY=k\nAZURE_OPENAI_BASE_URL=https://example.invalid\n"
            "AZURE_OPENAI_DEPLOYMENT=d\n"
        ) if credentials else "VERTEX_GEMINI_MODEL=gemini-3.7-flash\n"
        (home / "secrets.env").write_text(secrets, encoding="utf-8")
        return cs.Settings.load(home)

    def test_missing_profile_file_names_the_installer(self):
        with tempfile.TemporaryDirectory() as tmp:
            settings = self.build(tmp)
            (Path(tmp) / "profile" / "matcher_profile.json").unlink()
            with self.assertRaises(cs.ConfigurationError) as caught:
                cs.doctor(settings, require_key=False)
            self.assertIn("install.sh", str(caught.exception))

    def test_unedited_templates_are_reported_not_fatal(self):
        with tempfile.TemporaryDirectory() as tmp:
            info = cs.doctor(self.build(tmp), require_key=True)
            self.assertTrue(info["unedited_example_profiles"])
            self.assertFalse(info["ready"])
            self.assertTrue(any("Personalise" in step for step in info["next_steps"]))

    def test_missing_credentials_are_reported_with_the_template_state(self):
        with tempfile.TemporaryDirectory() as tmp:
            settings = self.build(tmp, credentials=False)
            with self.assertRaises(cs.ConfigurationError) as caught:
                cs.doctor(settings, require_key=True)
            message = str(caught.exception)
            self.assertIn("VERTEX_GEMINI_API_KEY", message)
            self.assertIn("still unedited", message)
            info = cs.doctor(settings, require_key=False)
            self.assertIn("VERTEX_GEMINI_API_KEY", info["missing_credentials"])
            self.assertFalse(info["ready"])

    def test_a_personalised_installation_reports_ready(self):
        with tempfile.TemporaryDirectory() as tmp:
            info = cs.doctor(self.build(tmp, personalise=True), require_key=True)
            self.assertEqual(info["unedited_example_profiles"], [])
            self.assertEqual(info["missing_credentials"], [])
            self.assertTrue(info["ready"])
            self.assertEqual(info["next_steps"], [])

    def test_doctor_surfaces_the_configured_language_level(self):
        with tempfile.TemporaryDirectory() as tmp:
            settings = self.build(tmp)
            profile = Path(tmp) / "profile" / "matcher_profile.json"
            data = json.loads(profile.read_text(encoding="utf-8"))
            data["constraints"]["swedish"] = "C1"
            profile.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
            self.assertEqual(cs.doctor(settings, require_key=True)["candidate_swedish_level"], "C1")

    def test_an_unspecified_language_level_is_called_out(self):
        with tempfile.TemporaryDirectory() as tmp:
            settings = self.build(tmp)
            profile = Path(tmp) / "profile" / "matcher_profile.json"
            data = json.loads(profile.read_text(encoding="utf-8"))
            data["constraints"].pop("swedish", None)
            profile.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
            info = cs.doctor(settings, require_key=True)
            self.assertEqual(info["candidate_swedish_level"], "unknown")
            self.assertTrue(any("constraints.swedish" in step for step in info["next_steps"]))

if __name__ == "__main__":
    unittest.main()
