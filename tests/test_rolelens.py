"""Offline tests for rolelens.py. No network, no API key, no cost.

Fixtures use fictional employers and example.invalid URLs.
"""

from __future__ import annotations

import array
import contextlib
import dataclasses
import datetime as dt
import importlib.util
import io
import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import types
import unittest
import unittest.mock
import urllib.error
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
MODULE_PATH = REPO / "rolelens.py"
spec = importlib.util.spec_from_file_location("rolelens", MODULE_PATH)
assert spec and spec.loader
rl = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = rl
spec.loader.exec_module(rl)


def mark_selected(db, ranking_key="test-ranking"):
    """Stand in for the ranking step: every stored job is ranked and selected."""
    with db.conn:
        db.conn.execute(
            "UPDATE jobs SET selection_state='selected', rank_content_hash=content_hash, "
            "rank_profile_key=?, vocabulary_score=0, ranked_at=?",
            (ranking_key, rl.iso_now()),
        )


def repo_file(*parts):
    """A real config/profile file when present, otherwise its .example variant.

    A fresh clone ships only the *.example.json templates, and the suite must
    run there without anyone building a profile first.
    """
    path = REPO.joinpath(*parts)
    if path.exists():
        return path
    example = path.with_name(f"{path.stem}.example{path.suffix}")
    if example.exists():
        return example
    raise FileNotFoundError(f"Neither {path} nor {example} exists")


PROFILE_FILES = (
    "career_profile.json", "matcher_profile.json", "search_lenses.json",
    "matcher_rules_v1_1.json", "role_vocabulary.json", "knowledge_catalogue.json",
)
TEST_SECRETS = (
    "VERTEX_GEMINI_API_KEY=dummy\nVERTEX_GEMINI_MODEL=gemini-3.8-flash\n"
    "AZURE_OPENAI_API_KEY=dummy\nAZURE_OPENAI_BASE_URL=https://example.invalid\n"
    "AZURE_OPENAI_DEPLOYMENT=gpt-5-mini\n"
)


class _NoNetworkEmbeddingClient:
    """Installed for every pipeline test, so none can reach Vertex by accident."""

    def __init__(self, settings, secrets, http=None):
        self.calls = 0
        self.tokens = 0
        self.quota_pauses = 0

    def embed(self, texts, task_type):
        raise AssertionError("a test reached for the embedding API without a fake")


class _NoNetworkEnrichmentClient:
    """Installed for every pipeline test, so none can reach JobTech by accident."""

    def __init__(self, http, *, timeout=120):
        self.calls = 0

    def enrich(self, documents):
        raise AssertionError("a test reached for the enrichment API without a fake")


def evaluation_item(source_id, opportunity=75, career_fit=80, **overrides):
    item = {
        "source_job_id": str(source_id), "career_fit": career_fit, "opportunity_score": opportunity,
        "confidence": 0.8, "actual_role": "API engineer", "why_fit": ["API experience"],
        "candidate_evidence": ["FastAPI"], "must_have_assessment": [], "gaps": [], "blockers": [],
        "language_risk": "none", "seniority_risk": "moderate", "location_note": "preferred",
    }
    item.update(overrides)
    return item


class CoreTests(unittest.TestCase):
    def test_truncate_middle_preserves_head_and_tail(self):
        out = rl.truncate_middle("A" * 100 + "B" * 100, 80)
        self.assertLessEqual(len(out), 80)
        self.assertTrue(out.startswith("A"))
        self.assertTrue(out.endswith("B"))
        self.assertIn("middle omitted", out)

    def test_application_expired(self):
        today = dt.date(2026, 8, 29)
        self.assertTrue(rl.application_expired("2026-08-28", today))
        self.assertFalse(rl.application_expired("2026-08-29", today))
        self.assertFalse(rl.application_expired("2026-09-01", today))
        self.assertFalse(rl.application_expired(None, today))

    def test_decision_classification(self):
        self.assertEqual(rl.classify_decision(90, 90, []), "notify_strong")
        self.assertEqual(rl.classify_decision(90, 75, []), "notify_good")
        self.assertEqual(rl.classify_decision(90, 62, []), "notify_stretch")
        # A stretch card must still be a strong career fit.
        self.assertEqual(rl.classify_decision(70, 62, []), "store_no_notify")
        # A bare unknown blocker does not force a verification card; only a
        # genuine citizenship or clearance unknown does.
        self.assertEqual(rl.classify_decision(92, 57, [{"type": "unknown"}]), "store_no_notify")
        self.assertEqual(
            rl.classify_decision(92, 57, [{"type": "unknown", "reason": "Citizenship eligibility requires candidate verification."}]),
            "notify_verify",
        )
        self.assertEqual(rl.classify_decision(99, 95, [{"type": "hard"}]), "store_no_notify")

    def test_normalize_job_prefers_application_url(self):
        job = rl.normalize_job({
            "id": "abc123",
            "headline": "Product Engineer",
            "webpage_url": "https://platsbanken.example.invalid/abc123",
            "application_deadline": "2026-12-01",
            "description": {"text": "Build useful software with React and Python."},
            "employer": {"name": "Example AB"},
            "application_details": {"url": "https://jobs.example.invalid/abc123"},
            "workplace_address": {"municipality": "Linköping", "region": "Östergötlands län", "country": "Sverige"},
            "employment_type": {"label": "Tillsvidare"},
        })
        self.assertEqual(job.url, "https://jobs.example.invalid/abc123")
        self.assertEqual(job.company, "Example AB")
        self.assertEqual(job.municipality, "Linköping")
        self.assertIn("React", job.description)

    def test_remote_detection(self):
        raw = {"description": {"text": "Hybrid work is supported."}}
        description = rl.merge_description(raw)
        self.assertTrue(rl.detect_remote(raw, description))
        self.assertEqual(rl.detect_work_mode(raw, description), (True, False))

    def test_fully_remote_detection(self):
        raw = {"description": {"text": "This role is fully remote anywhere in Sweden."}}
        self.assertEqual(rl.detect_work_mode(raw, rl.merge_description(raw)), (True, True))

    def test_profile_version_is_order_independent(self):
        self.assertEqual(rl.profile_version({"a": 1, "b": 2}, {"x": True}),
                         rl.profile_version({"b": 2, "a": 1}, {"x": True}))

    def test_urls_are_logged_without_their_query_string(self):
        self.assertEqual(rl.redact_url("https://api.example.invalid/v1/x?key=secret&q=1"),
                         "https://api.example.invalid/v1/x")

    def test_database_is_idempotent_and_requeues_changed_content(self):
        with tempfile.TemporaryDirectory() as td:
            db = rl.Database(Path(td) / "test.db")
            try:
                raw = {
                    "id": "1", "headline": "Software Engineer", "description": {"text": "Build APIs."},
                    "employer": {"name": "ACME"}, "workplace_address": {"municipality": "Stockholm", "country": "Sverige"},
                }
                db.upsert_job(rl.normalize_job(raw))
                db.upsert_job(rl.normalize_job(raw))
                self.assertEqual(db.status()["jobs"], 1)
                self.assertEqual(db.status()["jobs_by_source"], {"platsbanken": 1})
                mark_selected(db)
                pending = db.pending_jobs("profile-v1", 10)
                self.assertEqual(len(pending), 1)
                db.save_evaluation(pending[0], rl.validate_evaluation(evaluation_item("1")),
                                   profile_version="profile-v1", model="test")
                self.assertEqual(len(db.pending_jobs("profile-v1", 10)), 0)

                raw["description"]["text"] = "Build APIs and distributed systems."
                db.upsert_job(rl.normalize_job(raw))
                self.assertEqual(db.pending_jobs("profile-v1", 10), [], "an edited ad waits to be ranked again")
                mark_selected(db)
                self.assertEqual(len(db.pending_jobs("profile-v1", 10)), 1)
            finally:
                db.close()

    def test_json_schema_has_required_evaluations(self):
        schema = rl.evaluation_schema()
        self.assertEqual(schema["type"], "object")
        self.assertIn("evaluations", schema["required"])

    def test_a_vacancy_is_never_delivered_twice(self):
        """Notifications key on the evaluation, so re-evaluating a job - a new
        profile version, an edited ad - mints a fresh id. Without this the
        candidate is sent a vacancy they have already read."""
        with tempfile.TemporaryDirectory() as td:
            db = rl.Database(Path(td) / "test.db")
            try:
                db.upsert_job(rl.normalize_job({
                    "id": "1", "headline": "AI Engineer", "description": {"text": "Build LLM applications."},
                    "employer": {"name": "ACME"}, "workplace_address": {"municipality": "Stockholm", "country": "Sverige"},
                }))
                mark_selected(db)
                item = evaluation_item("1", opportunity=90, career_fit=90)
                pending = db.pending_jobs("profile-v1", 10)
                db.save_evaluation(pending[0], rl.validate_evaluation(item), profile_version="profile-v1", model="test")
                first = db.unnotified(10)
                self.assertEqual(len(first), 1, "the first evaluation is delivered")
                db.mark_notifications_emitted([first[0]["evaluation_id"]])

                pending = db.pending_jobs("profile-v2", 10)
                self.assertEqual(len(pending), 1, "a new profile version re-queues the job")
                db.save_evaluation(pending[0], rl.validate_evaluation(item), profile_version="profile-v2", model="test")
                self.assertEqual(db.unnotified(10), [], "but it is not delivered a second time")
            finally:
                db.close()

    def test_a_second_run_is_refused_by_the_lock(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "data" / "rolelens.lock"
            with rl.FileLock(path):
                with self.assertRaises(rl.RoleLensError):
                    with rl.FileLock(path):
                        pass
            with rl.FileLock(path):
                pass  # released, so a later run gets it

    def test_the_version_is_two_point_something(self):
        self.assertTrue(rl.APP_VERSION.startswith("2."))


SWEDISH_NOT_REQUIRED = 'Vi bygger en modern plattform i Stockholm. Teamets arbetsspråk är engelska och ansökan ska lämnas på engelska. Kunskaper i svenska krävs inte.'
SWEDISH_MANDATORY_REVERSED = 'Vi söker en systemutvecklare. Du har god kommunikativ förmåga och kan uttrycka dig väl i tal och skrift på svenska och engelska.'
MANDATORY_PROFESSIONAL_SWEDISH = (
    "You will build an AI-assisted workflow product end to end. Professional English is required. "
    "Fluent Swedish is required because you will independently run weekly customer sessions in Swedish."
)
SWEDISH_AD_NO_REQUIREMENT = (
    "Vi bygger en AI-baserad produkt och söker en utvecklare som arbetar från behovsanalys till "
    "produktion. Du arbetar med React, TypeScript, Python och PostgreSQL i ett litet produktteam."
)


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
    return rl.ProviderBatchResult(provider, model, (), frozenset(), rl.empty_usage(), error)


class ProviderRoutingTests(unittest.TestCase):
    def test_gemini_is_primary_and_azure_is_the_only_fallback(self):
        self.assertEqual(rl.PRIMARY_PROVIDER, "vertex_gemini")
        self.assertEqual(rl.FALLBACK_PROVIDER, "azure_gpt5mini")

    def test_other_providers_are_not_reachable_from_automatic_routing(self):
        for provider in ("glm", "zai_glm", "ai_gateway"):
            with self.assertRaises(rl.ConfigurationError):
                rl.ProviderMatcher(settings=None, secrets={}, matcher_profile={}, matcher_rules={}, provider=provider)

    def test_successful_primary_never_calls_the_fallback(self):
        primary = _FakeMatcher("vertex_gemini", "g", _result("vertex_gemini", "g"))
        fallback = _FakeMatcher("azure_gpt5mini", "a", _result("azure_gpt5mini", "a"))
        result, used_fallback = rl.evaluate_with_fallback(primary, fallback, [])
        self.assertFalse(used_fallback)
        self.assertEqual(fallback.calls, 0)
        self.assertEqual(result.provider, "vertex_gemini")

    def test_transport_failure_falls_back_exactly_once(self):
        primary = _FakeMatcher("vertex_gemini", "g", rl.TemporaryProviderError("vertex_gemini", "503"))
        fallback = _FakeMatcher("azure_gpt5mini", "a", _result("azure_gpt5mini", "a"))
        result, used_fallback = rl.evaluate_with_fallback(primary, fallback, [])
        self.assertTrue(used_fallback)
        self.assertEqual(fallback.calls, 1)
        self.assertEqual(result.provider, "azure_gpt5mini")

    def test_semantic_failure_does_not_trigger_the_transport_fallback(self):
        primary = _FakeMatcher("vertex_gemini", "g", _result("vertex_gemini", "g", error="invalid JSON"))
        fallback = _FakeMatcher("azure_gpt5mini", "a", _result("azure_gpt5mini", "a"))
        result, used_fallback = rl.evaluate_with_fallback(primary, fallback, [])
        self.assertFalse(used_fallback)
        self.assertEqual(fallback.calls, 0)
        self.assertEqual(result.error, "invalid JSON")

    def test_both_providers_failing_reports_both(self):
        primary = _FakeMatcher("vertex_gemini", "g", rl.TemporaryProviderError("vertex_gemini", "503"))
        fallback = _FakeMatcher("azure_gpt5mini", "a", rl.TemporaryProviderError("azure_gpt5mini", "429"))
        result, used_fallback = rl.evaluate_with_fallback(primary, fallback, [])
        self.assertTrue(used_fallback)
        self.assertIn("vertex_gemini", result.error)
        self.assertIn("azure_gpt5mini", result.error)

    def test_a_refused_account_falls_back_exactly_once(self):
        primary = _FakeMatcher("vertex_gemini", "g", rl.ProviderAccessError("vertex_gemini", "HTTP 403", status=403))
        fallback = _FakeMatcher("azure_gpt5mini", "a", _result("azure_gpt5mini", "a"))
        stats = rl.RunStats()
        result, used_fallback = rl.evaluate_with_fallback(primary, fallback, [], stats=stats)
        self.assertTrue(used_fallback)
        self.assertEqual((fallback.calls, result.provider), (1, "azure_gpt5mini"))
        self.assertEqual(stats.fallback_reasons, {"access_http_403": 1})

    def test_a_refusal_on_both_sides_keeps_the_jobs_instead_of_crashing(self):
        primary = _FakeMatcher("vertex_gemini", "g", rl.ProviderAccessError("vertex_gemini", "403", status=403))
        fallback = _FakeMatcher("azure_gpt5mini", "a", rl.ProviderAccessError("azure_gpt5mini", "401", status=401))
        result, _ = rl.evaluate_with_fallback(primary, fallback, [])
        self.assertEqual(result.error_kind, "transport")
        self.assertIn("refused access (access_http_401)", result.error)


class SwedishLanguagePolicyTests(unittest.TestCase):
    """Regression guards for the deterministic language policy layer."""

    BASE = evaluation_item("T", opportunity=92, career_fit=95, why_fit=["fit"], candidate_evidence=["evidence"])

    def policy(self, description, level="A2", **overrides):
        item = dict(self.BASE)
        item.update(overrides)
        return rl.normalize_evaluation_policy(item, {"description": description},
                                              swedish=rl.normalize_language_level(level))

    def test_explicit_swedish_not_required_is_not_a_blocker(self):
        out = self.policy(SWEDISH_NOT_REQUIRED)
        self.assertNotIn("mandatory_swedish_enforced", out["_policy_changes"])
        self.assertEqual(out["opportunity_score"], 92)
        self.assertEqual([b for b in out["blockers"] if b["type"] == "hard"], [])
        self.assertEqual(rl.classify_decision(out["career_fit"], out["opportunity_score"], out["blockers"]), "notify_strong")

    def test_mandatory_swedish_detected_when_written_as_tal_och_skrift(self):
        out = self.policy(SWEDISH_MANDATORY_REVERSED, opportunity_score=64)
        self.assertIn("mandatory_swedish_enforced", out["_policy_changes"])
        self.assertLessEqual(out["opportunity_score"], 49)
        self.assertTrue(any(b["type"] == "hard" for b in out["blockers"]))

    def test_mandatory_swedish_survives_a_nearby_preferred_phrase(self):
        out = self.policy(
            "Fluent Swedish is required because you will run weekly sessions with "
            "Swedish-speaking customers. Swedish proficiency is mandatory, not merely preferred.")
        self.assertIn("mandatory_swedish_enforced", out["_policy_changes"])
        self.assertTrue(any(b["type"] == "hard" for b in out["blockers"]))

    def test_optional_swedish_is_not_treated_as_mandatory(self):
        out = self.policy(
            "Hands-on software development and professional English are required. "
            "Swedish is considered a plus for some local conversations, but it is not mandatory.")
        self.assertNotIn("mandatory_swedish_enforced", out["_policy_changes"])
        self.assertEqual([b for b in out["blockers"] if b["type"] == "hard"], [])


class LanguageLevelTests(unittest.TestCase):
    """The proficiency scale itself: parsing, ordering and unknown handling."""

    def test_cefr_tokens_parse_in_any_case_and_context(self):
        for raw, expected in [("A2", "A2"), ("a2, progressing", "A2"), ("B1 (intermediate)", "B1"),
                              ("Swedish: c1", "C1"), ("c2", "C2"), ("A1", "A1")]:
            self.assertEqual(rl.normalize_language_level(raw).label, expected, raw)

    def test_prose_aliases_map_onto_the_scale(self):
        for raw, rank_of in [("fluent", "C1"), ("Flytande", "C1"), ("native speaker", "C2"), ("Native", "C2"),
                             ("advanced", "C1"), ("professional working proficiency", "C1"),
                             ("upper intermediate", "B2"), ("intermediate", "B1"), ("beginner", "A1"), ("none", "none")]:
            self.assertEqual(rl.normalize_language_level(raw).rank, rl.CEFR_SCALE.index(rank_of), raw)

    def test_an_explicit_cefr_token_beats_a_prose_alias(self):
        self.assertEqual(rl.normalize_language_level("A2, working towards fluent").label, "A2")

    def test_missing_or_unrecognised_values_stay_unknown(self):
        for raw in ["", None, "unknown", "not specified", "n/a", 42, [], "qwerty level"]:
            level = rl.normalize_language_level(raw)
            self.assertFalse(level.known, repr(raw))
            self.assertIsNone(level.rank, repr(raw))

    def test_unknown_never_satisfies_and_is_not_treated_as_none(self):
        unknown = rl.normalize_language_level("")
        self.assertFalse(unknown.at_least("none"))
        self.assertFalse(unknown.at_least(rl.PROFESSIONAL_LANGUAGE_LEVEL))
        self.assertNotEqual(unknown, rl.normalize_language_level("none"))

    def test_scale_is_ordered(self):
        ranks = [rl.normalize_language_level(x).rank for x in rl.CEFR_SCALE]
        self.assertEqual(ranks, sorted(ranks))
        self.assertTrue(rl.normalize_language_level("C1").at_least("B2"))
        self.assertFalse(rl.normalize_language_level("B2").at_least("C1"))

    def test_level_is_read_from_the_matcher_profile_constraints(self):
        self.assertEqual(rl.candidate_language_level({"constraints": {"swedish": "B2"}}, "swedish").label, "B2")
        self.assertEqual(rl.candidate_language_level({"languages": {"swedish": "fluent"}}, "swedish").label, "fluent")
        self.assertFalse(rl.candidate_language_level({}, "swedish").known)
        self.assertFalse(rl.candidate_language_level({"constraints": {}}, "swedish").known)

    def test_the_shipped_profile_resolves_to_the_level_it_declares(self):
        profile = json.loads(repo_file("profile", "matcher_profile.json").read_text(encoding="utf-8"))
        declared = profile["constraints"]["swedish"]
        level = rl.candidate_language_level(profile, "swedish")
        self.assertTrue(level.known, declared)
        self.assertEqual(level, rl.normalize_language_level(declared))


class MandatoryLanguagePolicyTests(unittest.TestCase):
    """Policy outcome as a function of the configured level, not of the code."""

    BASE = dict(SwedishLanguagePolicyTests.BASE)

    def policy(self, description, level):
        return rl.normalize_evaluation_policy(dict(self.BASE), {"description": description},
                                              swedish=rl.normalize_language_level(level))

    def swedish_rows(self, out):
        return [r for r in out["must_have_assessment"] if "swedish" in r["requirement"].casefold()]

    def test_a2_against_mandatory_professional_swedish_is_unmet_and_hard(self):
        out = self.policy(MANDATORY_PROFESSIONAL_SWEDISH, "A2")
        self.assertEqual([r["status"] for r in self.swedish_rows(out)], ["unmet"])
        self.assertTrue(any(b["type"] == "hard" for b in out["blockers"]))
        self.assertLessEqual(out["opportunity_score"], 49)

    def test_c1_is_sufficient_for_mandatory_professional_swedish(self):
        out = self.policy(MANDATORY_PROFESSIONAL_SWEDISH, "C1")
        self.assertEqual([r["status"] for r in self.swedish_rows(out)], ["met"])
        self.assertEqual([b for b in out["blockers"] if b["type"] == "hard"], [])
        self.assertEqual(out["opportunity_score"], self.BASE["opportunity_score"])

    def test_c2_fluent_and_native_are_all_sufficient(self):
        for level in ["C2", "fluent", "native", "native speaker", "flytande"]:
            out = self.policy(MANDATORY_PROFESSIONAL_SWEDISH, level)
            self.assertEqual([r["status"] for r in self.swedish_rows(out)], ["met"], level)

    def test_b2_is_a_documented_middle_ground_penalised_not_hard_blocked(self):
        out = self.policy(MANDATORY_PROFESSIONAL_SWEDISH, "B2")
        self.assertEqual([r["status"] for r in self.swedish_rows(out)], ["partial"])
        self.assertEqual([b for b in out["blockers"] if b["type"] == "hard"], [])
        self.assertTrue(any(b["type"] == "strong" for b in out["blockers"]))
        self.assertLessEqual(out["opportunity_score"], 69)

    def test_b1_and_below_are_hard_blocked(self):
        for level in ["B1", "A1", "none"]:
            out = self.policy(MANDATORY_PROFESSIONAL_SWEDISH, level)
            self.assertTrue(any(b["type"] == "hard" for b in out["blockers"]), level)

    def test_unknown_level_stays_unknown_and_is_never_fabricated_as_unmet(self):
        for level in ["", "unknown", None, "not specified"]:
            out = self.policy(MANDATORY_PROFESSIONAL_SWEDISH, level)
            self.assertEqual([r["status"] for r in self.swedish_rows(out)], ["unknown"], repr(level))
            self.assertEqual([b for b in out["blockers"] if b["type"] == "hard"], [], repr(level))
            self.assertEqual(out["opportunity_score"], self.BASE["opportunity_score"], repr(level))

    def test_optional_swedish_never_blocks_at_any_level(self):
        description = ("Hands-on development and professional English are required. "
                       "Swedish is meriterande and considered a plus, but it is not mandatory.")
        for level in ["A1", "A2", "B2", "C2", "", "native"]:
            out = self.policy(description, level)
            self.assertEqual([b for b in out["blockers"] if b["type"] == "hard"], [], level)
            self.assertEqual(self.swedish_rows(out), [], level)

    def test_a_swedish_language_ad_alone_infers_no_requirement(self):
        for level in ["A2", "C1", ""]:
            out = self.policy(SWEDISH_AD_NO_REQUIREMENT, level)
            self.assertEqual(self.swedish_rows(out), [], level)

    def test_negation_vetoes_mandatory_detection_at_every_level(self):
        for level in ["A1", "A2", "C1", ""]:
            out = self.policy(SWEDISH_NOT_REQUIRED, level)
            self.assertNotIn("mandatory_swedish_enforced", out["_policy_changes"], level)

    def test_explanations_quote_the_configured_level_and_no_other(self):
        for level in ["A2", "C1", "B2"]:
            blob = json.dumps(self.policy(MANDATORY_PROFESSIONAL_SWEDISH, level), ensure_ascii=False)
            self.assertIn(level, blob, level)
            for other in {"A2", "C1", "B2"} - {level}:
                self.assertNotIn(other, blob, f"{level} leaked {other}")


class SystemPromptTests(unittest.TestCase):
    """The prompts describe the configured candidate and never assume one."""

    NON_EU = {"constraints": {"swedish_citizenship": "no", "eu_citizenship": "no",
                              "permanent_residence": "no", "work_permit": "yes"}}

    def test_prompt_states_the_configured_level(self):
        for level in ["A2", "B2", "C1", "C2"]:
            self.assertIn(f"Candidate Swedish is {level}", rl.semantic_system_prompt(rl.normalize_language_level(level)))

    def test_prompt_says_unknown_rather_than_inventing_a_level(self):
        prompt = rl.semantic_system_prompt(rl.UNKNOWN_LANGUAGE_LEVEL)
        self.assertIn("not specified", prompt)
        self.assertIn("UNKNOWN", prompt)
        for level in rl.CEFR_SCALE[1:]:
            self.assertNotIn(f"Candidate Swedish is {level}", prompt)

    def test_prompt_bars_every_status_the_candidate_lacks(self):
        prompt = rl.semantic_system_prompt(rl.UNKNOWN_LANGUAGE_LEVEL, self.NON_EU)
        for phrase in ["Swedish citizenship", "EU/EEA citizenship", "a permanent residence permit", "hard blocker"]:
            self.assertIn(phrase, prompt, phrase)
        self.assertIn("no employer sponsorship", prompt)
        self.assertIn("never a blocker", prompt)

    def test_a_candidate_with_eu_citizenship_is_not_barred_on_it(self):
        profile = {"constraints": {"swedish_citizenship": "no", "eu_citizenship": "yes"}}
        prompt = rl.semantic_system_prompt(rl.UNKNOWN_LANGUAGE_LEVEL, profile)
        self.assertIn("Swedish citizenship", prompt)
        self.assertNotIn("EU/EEA citizenship", prompt)

    def test_a_swedish_citizen_prompt_bars_nothing(self):
        prompt = rl.semantic_system_prompt(rl.UNKNOWN_LANGUAGE_LEVEL, {"constraints": {"swedish_citizenship": "yes"}})
        self.assertIn("nationality requirements are met", prompt)
        self.assertNotIn("hard blocker with the reason", prompt)

    def test_prompt_says_nothing_about_citizenship_when_the_profile_is_silent(self):
        self.assertNotIn("Swedish citizen", rl.semantic_system_prompt(rl.UNKNOWN_LANGUAGE_LEVEL, {}))
        self.assertNotIn("Swedish citizen", rl.semantic_system_prompt())

    def test_prompt_asks_for_a_hard_blocker_on_core_work_and_not_on_peripheral_gaps(self):
        prompt = rl.semantic_system_prompt()
        self.assertIn("Record a hard blocker", prompt)
        self.assertIn("centre of the job", prompt)
        self.assertIn("never a hard blocker", prompt)

    def test_prompt_asks_for_the_years_in_field_row_and_not_for_a_lower_score(self):
        prompt = rl.semantic_system_prompt()
        self.assertIn('"Years in field:"', prompt)
        self.assertIn("Do not lower career_fit or opportunity_score", prompt)

    def test_the_first_read_prompt_names_no_candidate(self):
        """Everything about the candidate comes from the card, built from the profile."""
        for exclude in (True, False):
            prompt = rl.triage_system_prompt(exclude)
            for phrase in ("Java", "SAP", "WordPress", "health care", "A2"):
                self.assertNotIn(phrase, prompt, phrase)
        self.assertIn("internship", rl.triage_system_prompt(True))
        self.assertNotIn("internship", rl.triage_system_prompt(False))

    def test_the_engine_carries_no_candidate_specific_facts(self):
        """Levels and nationality answers belong in the profile, and delivery
        belongs to the scheduler: none of them may be baked into the engine."""
        source = MODULE_PATH.read_text(encoding="utf-8")
        for phrase in ["level is A2", "unmet at A2", "Swedish is A2 and progressing",
                       'swedish_citizenship": "no"', "Hermes", "Telegram"]:
            self.assertNotIn(phrase, source, phrase)


class CitizenshipResolutionTests(unittest.TestCase):
    """An unstated citizenship stays UNKNOWN, because turning "we do not know"
    into "you are rejected" silently loses real roles. Once the profile answers
    it, the engine decides."""

    AD = "Krav på svenskt medborgarskap."
    NON_EU_PROFILE = SystemPromptTests.NON_EU

    def normalize(self, ad=None, profile=None):
        item = {"source_job_id": "1", "career_fit": 90, "opportunity_score": 88,
                "must_have_assessment": [], "blockers": [], "gaps": []}
        return rl.normalize_evaluation_policy(item, {"description": ad or self.AD},
                                              eligibility=rl.candidate_eligibility(profile if profile is not None else {}))

    def test_silent_profile_still_preserves_unknown(self):
        self.assertEqual({b["type"] for b in self.normalize(profile={})["blockers"]}, {"unknown"})

    def test_a_stated_non_citizen_is_barred_deterministically(self):
        out = self.normalize(profile=self.NON_EU_PROFILE)
        self.assertIn("hard", {b["type"] for b in out["blockers"]})
        reason = next(b["reason"] for b in out["blockers"] if b["type"] == "hard")
        for status in ("Swedish citizenship", "EU/EEA citizenship", "permanent residence"):
            self.assertIn(status, reason)

    def test_a_conditional_demand_is_still_a_dead_end(self):
        out = self.normalize(ad="I samband med detta kan krav på visst medborgarskap förekomma.",
                             profile=self.NON_EU_PROFILE)
        self.assertIn("hard", {b["type"] for b in out["blockers"]})

    def test_a_work_permit_route_is_never_barred(self):
        out = self.normalize(ad="You are either a Swedish citizen or hold a valid EU work permit.",
                             profile=self.NON_EU_PROFILE)
        self.assertNotIn("hard", {b["type"] for b in out["blockers"]})

    def test_clearance_alone_is_still_preserved_unknown(self):
        out = self.normalize(ad="Security clearance is required for this position.", profile=self.NON_EU_PROFILE)
        self.assertIn("unknown", {b["type"] for b in out["blockers"]})

    def test_profile_reader_accepts_only_a_real_answer(self):
        for value, resolved in (("no", True), ("yes", True), ("", False), ("maybe", False)):
            got = rl.candidate_eligibility({"constraints": {"swedish_citizenship": value}})
            self.assertEqual(got is not None, resolved, value)
        self.assertIsNone(rl.candidate_eligibility({}))

    def test_barred_statuses_follow_the_profile(self):
        self.assertEqual(rl.barred_statuses(rl.candidate_eligibility({"constraints": {"swedish_citizenship": "yes"}})), [])
        self.assertEqual(
            rl.barred_statuses(rl.candidate_eligibility({"constraints": {"swedish_citizenship": "no", "eu_citizenship": "yes"}})),
            ["Swedish citizenship"])

    def test_a_swedish_language_requirement_is_not_a_citizenship_one(self):
        self.assertTrue(rl.mentions_swedish_language("Fluent Swedish required"))
        self.assertTrue(rl.mentions_swedish_language("Flytande svenska"))
        self.assertFalse(rl.mentions_swedish_language("Swedish citizenship"))
        self.assertFalse(rl.mentions_swedish_language("Svenskt medborgarskap"))

    def test_a_core_work_blocker_suppresses_notification_and_an_unknown_gap_does_not(self):
        self.assertEqual(rl.classify_decision(84, 76, [{"type": "hard", "reason": "Core job is firmware."}]),
                         "store_no_notify")
        self.assertEqual(rl.classify_decision(84, 76, [{"type": "unknown", "reason": "Years not stated."}]),
                         "notify_good")


class RunSummaryTests(unittest.TestCase):
    """The one compact line a scheduler delivers after the cards."""

    def summary(self, evaluated, matches, queued=0, failed=False):
        return rl.format_run_summary(evaluated, matches, queued, failed=failed)

    def test_a_run_without_matches_or_problems_prints_nothing(self):
        self.assertEqual(self.summary(0, 0), "")
        self.assertEqual(self.summary(7, 0), "")
        self.assertEqual(self.summary(64, 0, queued=1), "")

    def test_matches_are_counted_in_singular_and_plural(self):
        self.assertEqual(self.summary(9, 1), "🎯 RoleLens: 9 jobs checked · 1 match.")
        self.assertEqual(self.summary(10, 4), "🎯 RoleLens: 10 jobs checked · 4 matches.")
        self.assertEqual(self.summary(10, 4, queued=2), "🎯 RoleLens: 10 jobs checked · 4 matches · 2 queued for next run.")

    def test_a_provider_failure_is_always_reported(self):
        self.assertEqual(self.summary(6, 2, queued=4, failed=True),
                         "⚠️ RoleLens: 6 jobs checked · 2 matches · 4 pending after provider error.")
        self.assertEqual(self.summary(0, 0, queued=10, failed=True),
                         "⚠️ RoleLens: 0 jobs checked · 0 matches · 10 pending after provider error.")

    def test_the_ceiling_is_reported_and_a_failure_outranks_it(self):
        self.assertEqual(
            rl.format_run_summary(300, 4, 0, ceiling_reached=True, selected=300),
            "⚠️ RoleLens: candidate safety ceiling reached, 300 selected · 4 matches · additional jobs remain queued.")
        self.assertIn("pending after provider error",
                      rl.format_run_summary(10, 0, 290, failed=True, ceiling_reached=True, selected=300))

    def test_the_card_formatter_is_empty_without_rows(self):
        self.assertEqual(rl.format_notifications([]), "")


class _PipelineHarness(unittest.TestCase):
    """Real pipeline, real SQLite, scripted provider.

    Only ProviderMatcher is replaced, so evaluate_with_fallback - and therefore
    the Azure fallback rule - is exercised for real.
    """

    def setUp(self):
        super().setUp()
        for name, fake in (("EmbeddingClient", _NoNetworkEmbeddingClient),
                           ("EnrichmentClient", _NoNetworkEnrichmentClient)):
            self.addCleanup(setattr, rl, name, getattr(rl, name))
            setattr(rl, name, fake)

    def build_home(self, tmp, **overrides):
        # No test reads the network or calls a first-read model unless it installs a fake.
        overrides.setdefault("triage_model", "")
        overrides.setdefault("search_terms", [])
        overrides.setdefault("career_sites", [])
        overrides.setdefault("query_delay_ms", 0)
        home = Path(tmp)
        (home / "profile").mkdir(parents=True, exist_ok=True)
        (home / "data").mkdir(parents=True, exist_ok=True)
        config = json.loads(repo_file("config.json").read_text(encoding="utf-8"))
        config.update(overrides)
        (home / "config.json").write_text(json.dumps(config, ensure_ascii=False), encoding="utf-8")
        for name in PROFILE_FILES:
            shutil.copy2(repo_file("profile", name), home / "profile" / name)
        (home / "secrets.env").write_text(TEST_SECRETS, encoding="utf-8")
        (home / "secrets.env").chmod(0o600)
        return rl.Settings.load(home)

    def raw_job(self, source_id, title, company, municipality, description):
        return {
            "id": str(source_id),
            "headline": title,
            "employer": {"name": company},
            "webpage_url": f"https://example.invalid/{source_id}",
            "workplace_address": {"municipality": municipality, "region": "Stockholms lan", "country": "Sverige"},
            "description": {"text": description},
            "application_deadline": "2027-01-01T23:59:59",
            "publication_date": "2026-08-01T00:00:00",
            "employment_type": {"label": "Vanlig anstallning"},
            "scope_of_work": {"min": 100, "max": 100},
        }

    def seed(self, settings, specs):
        db = rl.Database(settings.db_path)
        try:
            for item in specs:
                db.upsert_job(rl.normalize_job(self.raw_job(*item)))
            mark_selected(db, rl.RankingContext.load(settings).ranking_key)
        finally:
            db.close()

    def distinct(self, count):
        return [
            (f"job{i:04d}", f"AI Engineer {i}", f"Company {i}", "Stockholm",
             f"Role {i}: build AI products with Python and TypeScript.")
            for i in range(1, count + 1)
        ]

    def evaluation(self, source_id, opportunity):
        return rl.validate_evaluation(evaluation_item(
            source_id, opportunity=opportunity, career_fit=90, actual_role="AI Product Engineer",
            why_fit=["Strong overlap"], candidate_evidence=["Python"], seniority_risk="low",
        ))

    def drive(self, settings, script=None, scores=None):
        """Run the evaluation pipeline. `script(call_index, ids, provider)` returns an action.

        Actions: "ok", ("omit", n), "output", "raise", "refuse".
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
                self.model = "gemini-3.8-flash" if provider == rl.PRIMARY_PROVIDER else "gpt-5-mini"

            def evaluate(self, jobs):
                ids = [str(r["source_job_id"]) for r in jobs]
                index = len(calls)
                calls.append((self.provider, tuple(ids)))
                action = action_for(index, ids, self.provider)
                if action == "raise":
                    raise rl.TemporaryProviderError(self.provider, "503 upstream unavailable")
                if action == "refuse":
                    raise rl.ProviderAccessError(self.provider, "HTTP 403 billing disabled", status=403)
                if action == "output":
                    return rl.ProviderBatchResult(self.provider, self.model, (), frozenset(ids),
                                                  rl.empty_usage(), "invalid JSON: boom", "output")
                omit = action[1] if isinstance(action, tuple) and action[0] == "omit" else 0
                keep = ids[: len(ids) - omit] if omit else ids
                evals = tuple(
                    test.evaluation(sid, scores(index, sid) if callable(scores) else (scores or {}).get(sid, 30))
                    for sid in keep
                )
                missing = frozenset(ids[len(keep):])
                return rl.ProviderBatchResult(
                    self.provider, self.model, evals, missing, rl.empty_usage(),
                    ("missing/invalid IDs: " + ", ".join(sorted(missing))) if missing else None,
                    "completeness" if missing else None,
                )

        original = rl.ProviderMatcher
        rl.ProviderMatcher = Stub
        buffer = io.StringIO()
        try:
            with contextlib.redirect_stdout(buffer):
                rl.run_pipeline(settings, fetch_only=False, evaluate_only=True)
        finally:
            rl.ProviderMatcher = original
        return buffer.getvalue(), calls

    def summary_lines(self, out):
        return [ln for ln in out.splitlines() if ln.startswith(("\U0001f3af RoleLens:", "⚠️ RoleLens: ")) and
                ("jobs checked" in ln or "safety ceiling" in ln)]

    def pending(self, settings):
        db = rl.Database(settings.db_path)
        try:
            return db.pending_count(rl.load_profile_bundle(settings)[2], respect_live_mode=False)
        finally:
            db.close()

    def last_run(self, settings):
        db = rl.Database(settings.db_path)
        try:
            return json.loads(db.conn.execute("SELECT stats_json FROM runs ORDER BY id DESC LIMIT 1").fetchone()[0])
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
            self.assertTrue(all(n <= 10 for n in sizes), f"batch larger than 10: {sizes}")
            self.assertEqual(len(sizes), -(-count // 10) if count else 0)
            self.assertEqual(self.pending(settings), 0, "nothing left pending")
            self.assertEqual(self.summary_lines(out), [], "no match, no problem: nothing to say")
            return sizes

    def test_zero_candidates(self):
        self.assertEqual(self.check(0), [])

    def test_one_candidate(self):
        self.assertEqual(self.check(1), [1])

    def test_ten_and_eleven_candidates(self):
        self.assertEqual(self.check(10), [10])
        self.assertEqual(self.check(11), [10, 1])

    def test_sixty_five_candidates(self):
        self.assertEqual(self.check(65), [10, 10, 10, 10, 10, 10, 5])

    def test_one_hundred_three_candidates(self):
        self.assertEqual(self.check(103), [10] * 10 + [3])

    def test_snapshot_is_frozen_not_re_queried(self):
        with tempfile.TemporaryDirectory() as tmp:
            settings = self.build_home(tmp)
            self.seed(settings, self.distinct(12))
            inserted = {"done": False}

            def script(index, ids, provider):
                if not inserted["done"]:
                    self.seed(settings, [("late-1", "Late Job", "Late AB", "Stockholm", "Arrived mid-run.")])
                    inserted["done"] = True
                return "ok"

            _, calls = self.drive(settings, script=script)
            attempted = [i for _, ids in calls for i in ids]
            self.assertNotIn("late-1", attempted, "a mid-run arrival must not join this snapshot")
            self.assertEqual(len(attempted), 12)


class CompletenessGapTests(_PipelineHarness):
    """A short batch is a gap, not a failure, and gets exactly one cleanup pass."""

    def test_omitted_ids_are_collected_into_one_cleanup_pass(self):
        with tempfile.TemporaryDirectory() as tmp:
            settings = self.build_home(tmp)
            self.seed(settings, self.distinct(30))
            _, calls = self.drive(settings, script=[("omit", 1), ("omit", 1), "ok"])
            self.assertEqual(len(calls), 4, "3 normal batches + exactly 1 cleanup batch")
            cleanup_ids = set(calls[3][1])
            self.assertEqual(len(cleanup_ids), 2, "both omitted IDs retried together")
            self.assertTrue(cleanup_ids <= set(calls[0][1]) | set(calls[1][1]))

    def test_cleanup_failure_leaves_only_the_unresolved_id_pending(self):
        with tempfile.TemporaryDirectory() as tmp:
            settings = self.build_home(tmp)
            self.seed(settings, self.distinct(65))
            self.drive(settings, script=lambda i, ids, p: ("omit", 1) if i in (0, 7) else "ok")
            self.assertEqual(self.pending(settings), 1)

    def test_cleanup_is_attempted_at_most_once(self):
        with tempfile.TemporaryDirectory() as tmp:
            settings = self.build_home(tmp)
            self.seed(settings, self.distinct(20))
            _, calls = self.drive(settings, script=lambda i, ids, p: ("omit", 1))
            self.assertEqual(len(calls), 3, "2 normal + 1 cleanup, never a retry tree")

    def test_missing_ids_never_call_azure(self):
        with tempfile.TemporaryDirectory() as tmp:
            settings = self.build_home(tmp)
            self.seed(settings, self.distinct(20))
            _, calls = self.drive(settings, script=lambda i, ids, p: ("omit", 2))
            self.assertEqual({p for p, _ in calls}, {rl.PRIMARY_PROVIDER}, "a completeness gap is not a transport failure")


class TransportFailureTests(_PipelineHarness):
    def test_azure_still_covers_a_genuine_gemini_transport_failure(self):
        with tempfile.TemporaryDirectory() as tmp:
            settings = self.build_home(tmp)
            self.seed(settings, self.distinct(20))
            _, calls = self.drive(
                settings, script=lambda i, ids, p: "raise" if (i == 0 and p == rl.PRIMARY_PROVIDER) else "ok")
            self.assertEqual(calls[1][0], rl.FALLBACK_PROVIDER, "Azure took the failed batch")
            self.assertEqual(calls[1][1], calls[0][1], "same batch, one retry")
            self.assertEqual(self.pending(settings), 0)

    def test_both_providers_failing_stops_cleanly_and_keeps_jobs(self):
        with tempfile.TemporaryDirectory() as tmp:
            settings = self.build_home(tmp)
            self.seed(settings, self.distinct(40))
            out, _ = self.drive(settings, script=lambda i, ids, p: "raise" if i in (2, 3) else "ok")
            self.assertEqual(self.pending(settings), 20, "nothing lost")
            self.assertEqual(self.summary_lines(out),
                             ["⚠️ RoleLens: 20 jobs checked · 0 matches · 20 pending after provider error."])

    def test_unparseable_envelope_stops_the_run_as_a_failure(self):
        with tempfile.TemporaryDirectory() as tmp:
            settings = self.build_home(tmp)
            self.seed(settings, self.distinct(30))
            out, calls = self.drive(settings, script=["ok", "output"])
            self.assertEqual(len(calls), 2, "no cleanup pass after a provider failure")
            self.assertIn("pending after provider error", self.summary_lines(out)[0])
            self.assertEqual(self.pending(settings), 20)


class RankingAndDeliveryTests(_PipelineHarness):
    def test_ranking_is_global_across_every_batch(self):
        with tempfile.TemporaryDirectory() as tmp:
            settings = self.build_home(tmp)
            self.seed(settings, self.distinct(30))
            out, _ = self.drive(settings, scores={"job0002": 80, "job0015": 88, "job0029": 95})
            order = [ln for ln in out.splitlines() if ln.startswith(("\U0001f7e2", "\U0001f7e1"))]
            self.assertEqual(len(order), 3, out)
            self.assertIn("AI Engineer 29", order[0], "highest opportunity ranked first")
            self.assertIn("AI Engineer 15", order[1])
            self.assertIn("AI Engineer 2 ", order[2] + " ")
            self.assertEqual(self.summary_lines(out), ["🎯 RoleLens: 30 jobs checked · 3 matches."])

    def test_notifications_wait_until_semantic_processing_finishes(self):
        with tempfile.TemporaryDirectory() as tmp:
            settings = self.build_home(tmp)
            self.seed(settings, self.distinct(30))
            seen_during_calls = []

            def script(index, ids, provider):
                seen_during_calls.append(sys.stdout.getvalue())
                return "ok"

            out, calls = self.drive(settings, script=script, scores={"job0001": 90, "job0030": 92})
            self.assertEqual(len(calls), 3)
            self.assertTrue(all(x == "" for x in seen_during_calls), "output appeared before the run finished")
            self.assertIn("AI Engineer 30", out)
            body = [ln for ln in out.splitlines() if ln.strip()]
            self.assertTrue(body[0].startswith("\U0001f7e2"), "cards come first, with no header")
            self.assertEqual(body[-1], "🎯 RoleLens: 30 jobs checked · 2 matches.")


class RuntimeBudgetTests(_PipelineHarness):
    def test_emergency_limit_exits_safely_without_losing_jobs(self):
        with tempfile.TemporaryDirectory() as tmp:
            settings = self.build_home(tmp, max_run_seconds=1)
            self.seed(settings, self.distinct(65))
            out, calls = self.drive(settings)
            self.assertEqual(len(calls), 1, "only the first batch fits a 1s budget")
            self.assertEqual(self.pending(settings), 55, "every unevaluated job kept")
            self.assertNotIn("provider error", out)


class DuplicateSuppressionTests(_PipelineHarness):
    BODY = "We are hiring a platform engineer to build and operate internal developer tooling."

    def test_identical_repost_with_a_new_source_id_is_suppressed_and_recorded(self):
        with tempfile.TemporaryDirectory() as tmp:
            settings = self.build_home(tmp)
            self.seed(settings, [
                ("orig-1", "Platform Engineer", "Acme AB", "Stockholm", self.BODY),
                ("repost-2", "Platform Engineer", "Acme AB", "Stockholm", self.BODY),
            ])
            _, calls = self.drive(settings)
            self.assertEqual(sum(len(ids) for _, ids in calls), 1, "the repost must not reach the model")
            db = rl.Database(settings.db_path)
            try:
                rows = db.conn.execute(
                    "SELECT job_id, canonical_job_id, reason FROM job_fingerprints WHERE canonical_job_id IS NOT NULL"
                ).fetchall()
                self.assertEqual(db.conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0], 2, "both rows survive")
            finally:
                db.close()
            self.assertEqual(len(rows), 1)
            self.assertIn("Repost of job", rows[0]["reason"])

    def test_different_description_or_location_is_not_suppressed(self):
        with tempfile.TemporaryDirectory() as tmp:
            settings = self.build_home(tmp)
            self.seed(settings, [
                ("a-1", "Member of Technical Staff", "Northwind Labs AB", "Stockholm", "You will build product features."),
                ("a-2", "Member of Technical Staff", "Northwind Labs AB", "Stockholm", "You will mentor a platform team."),
                ("loc-1", "Platform Engineer", "Baltic Energy AB", "Solna", self.BODY),
                ("loc-2", "Platform Engineer", "Baltic Energy AB", "Goteborg", self.BODY),
            ])
            _, calls = self.drive(settings)
            self.assertEqual(sum(len(ids) for _, ids in calls), 4)

    def test_duplicate_matches_do_not_produce_duplicate_cards(self):
        with tempfile.TemporaryDirectory() as tmp:
            settings = self.build_home(tmp)
            self.seed(settings, [
                ("dup-1", "Senior Software Engineer", "DataJob AB", "Stockholm", self.BODY),
                ("dup-2", "Senior Software Engineer", "DataJob AB", "Stockholm", self.BODY),
            ])
            db = rl.Database(settings.db_path)
            try:
                version = rl.load_profile_bundle(settings)[2]
                for row in db.pending_jobs(version, 10, respect_live_mode=False):
                    db.save_evaluation(row, self.evaluation(row["source_job_id"], 90), profile_version=version, model="test")
            finally:
                db.close()
            out, _ = self.drive(settings)
            self.assertEqual(out.count("Senior Software Engineer"), 1, "one vacancy, one card")
            self.assertEqual(self.summary_lines(out), ["🎯 RoleLens: 0 jobs checked · 1 match."])

    def test_formatting_noise_does_not_change_the_fingerprint(self):
        base = rl.duplicate_fingerprint("Acme AB", "Platform Engineer", "Stockholm", self.BODY)
        self.assertEqual(base, rl.duplicate_fingerprint("  ACME   ab ", "Platform   Engineer!", "Stockholm",
                                                        "  We are hiring a platform engineer, to build and operate "
                                                        "internal developer tooling.  "))
        for changed in (("Other AB", "Platform Engineer", "Stockholm", self.BODY),
                        ("Acme AB", "Senior Platform Engineer", "Stockholm", self.BODY),
                        ("Acme AB", "Platform Engineer", "Goteborg", self.BODY),
                        ("Acme AB", "Platform Engineer", "Stockholm", "A different role entirely.")):
            self.assertNotEqual(base, rl.duplicate_fingerprint(*changed))


class QuietRunTests(_PipelineHarness):
    """A quiet scheduled run prints nothing, so a scheduler that delivers stdout
    sends nothing; a run that delivers a card still says so in one line."""

    def test_an_empty_run_prints_nothing_and_calls_no_model(self):
        with tempfile.TemporaryDirectory() as tmp:
            settings = self.build_home(tmp)
            out, calls = self.drive(settings)
            self.assertEqual((out, calls), ("", []))

    def test_a_carried_over_card_is_delivered_once_with_its_summary(self):
        with tempfile.TemporaryDirectory() as tmp:
            settings = self.build_home(tmp)
            self.seed(settings, [("carry-1", "Platform Engineer", "Acme AB", "Stockholm", "Build internal tools.")])
            db = rl.Database(settings.db_path)
            try:
                version = rl.load_profile_bundle(settings)[2]
                for row in db.pending_jobs(version, 10, respect_live_mode=False):
                    db.save_evaluation(row, self.evaluation(row["source_job_id"], 90), profile_version=version, model="test")
            finally:
                db.close()
            first, _ = self.drive(settings)
            self.assertEqual(self.summary_lines(first), ["🎯 RoleLens: 0 jobs checked · 1 match."])
            second, _ = self.drive(settings)
            self.assertEqual(second, "")


class SafetyCeilingTests(_PipelineHarness):
    """The candidate ceiling is emergency protection, never a silent cap."""

    def test_ceiling_run_is_reported_as_incomplete(self):
        with tempfile.TemporaryDirectory() as tmp:
            settings = self.build_home(tmp, max_candidates_per_run=20)
            self.seed(settings, self.distinct(35))
            out, calls = self.drive(settings, scores={"job0035": 90, "job0034": 92})
            self.assertEqual(sum(len(ids) for _, ids in calls), 20, "only the ceiling is processed")
            self.assertEqual(self.summary_lines(out), [
                "⚠️ RoleLens: candidate safety ceiling reached, 20 selected · 2 matches · additional jobs remain queued."])
            self.assertEqual(self.pending(settings), 15, "the remainder stays queued")

    def test_exactly_at_the_ceiling_is_not_flagged(self):
        with tempfile.TemporaryDirectory() as tmp:
            settings = self.build_home(tmp, max_candidates_per_run=20)
            self.seed(settings, self.distinct(20))
            out, _ = self.drive(settings)
            self.assertNotIn("safety ceiling", out)
            self.assertEqual(self.pending(settings), 0)


class JobStreamDiscoveryTests(_PipelineHarness):
    """JobStream returns every ad added, changed or unpublished since a cursor."""

    NOW = dt.datetime(2026, 9, 10, 12, 0, 0)

    class FakeStream:
        def __init__(self, ads=None, error=None):
            self.ads = ads or []
            self.error = error
            self.requested = []

        def stream(self, updated_after):
            self.requested.append(updated_after)
            if self.error:
                raise self.error
            return list(self.ads)

    def ad(self, source_id, title="Verksamhetsutvecklare IT", **extra):
        ad = self.raw_job(source_id, title, "Company", "Stockholm",
                          "Lead digital change and process improvement across the organisation.")
        ad.update(extra)
        return ad

    def fetch(self, settings, stream):
        db = rl.Database(settings.db_path)
        try:
            stats = rl.RunStats()
            jobs, cursor = rl.fetch_jobstream(stream, db, settings, stats, now=self.NOW)
            return jobs, cursor, stats
        finally:
            db.close()

    def store_cursor(self, settings, value):
        db = rl.Database(settings.db_path)
        try:
            db.set_meta(rl.JOBSTREAM_CURSOR_KEY, value)
        finally:
            db.close()

    def run_fetch_only(self, settings, stream):
        original = rl.JobStreamClient
        rl.JobStreamClient = lambda http, settings_: stream
        try:
            with contextlib.redirect_stdout(io.StringIO()):
                rl.run_pipeline(settings, fetch_only=True, evaluate_only=False)
        finally:
            rl.JobStreamClient = original

    def test_live_ads_are_kept_and_unpublished_or_malformed_ones_skipped(self):
        with tempfile.TemporaryDirectory() as tmp:
            settings = self.build_home(tmp)
            stream = self.FakeStream([self.ad("a1"), self.ad("a2", removed=True),
                                      {"headline": "an ad without an id"}, "not an object"])
            jobs, cursor, stats = self.fetch(settings, stream)
            self.assertEqual(sorted(jobs), ["a1"])
            self.assertEqual((stats.stream_entries, stats.stream_removed, stats.stream_malformed, stats.unique_jobs),
                             (4, 1, 1, 1))
            self.assertEqual(cursor, self.NOW.isoformat())

    def test_cold_start_replays_the_lookback_window_with_one_second_overlap(self):
        with tempfile.TemporaryDirectory() as tmp:
            settings = self.build_home(tmp, jobstream_lookback_hours=24)
            stream = self.FakeStream()
            self.fetch(settings, stream)
            self.assertEqual(stream.requested, [self.NOW - dt.timedelta(hours=24, seconds=1)])

    def test_an_existing_cursor_is_resumed_and_a_stale_one_clamped(self):
        with tempfile.TemporaryDirectory() as tmp:
            settings = self.build_home(tmp, jobstream_max_window_hours=72)
            self.store_cursor(settings, "2026-09-10T04:00:00")
            stream = self.FakeStream()
            _, _, stats = self.fetch(settings, stream)
            self.assertEqual(stream.requested, [dt.datetime(2026, 9, 10, 3, 59, 59)])
            self.assertFalse(stats.stream_window_clamped)
            self.store_cursor(settings, "2026-08-01T00:00:00")
            stream = self.FakeStream()
            _, _, stats = self.fetch(settings, stream)
            self.assertEqual(stream.requested, [self.NOW - dt.timedelta(hours=72, seconds=1)])
            self.assertTrue(stats.stream_window_clamped)

    def test_the_cursor_advances_only_after_jobs_are_stored(self):
        with tempfile.TemporaryDirectory() as tmp:
            settings = self.build_home(tmp)
            self.run_fetch_only(settings, self.FakeStream([self.ad("b1"), self.ad("b2")]))
            db = rl.Database(settings.db_path)
            try:
                self.assertIsNotNone(db.get_meta(rl.JOBSTREAM_CURSOR_KEY))
                self.assertEqual(db.status()["jobs"], 2)
            finally:
                db.close()

    def test_a_failed_stream_leaves_the_cursor_untouched(self):
        with tempfile.TemporaryDirectory() as tmp:
            settings = self.build_home(tmp)
            self.store_cursor(settings, "2026-09-10T04:00:00")
            with self.assertRaises(rl.RemoteAPIError):
                self.run_fetch_only(settings, self.FakeStream(error=rl.RemoteAPIError("stream down")))
            db = rl.Database(settings.db_path)
            try:
                self.assertEqual(db.get_meta(rl.JOBSTREAM_CURSOR_KEY), "2026-09-10T04:00:00")
            finally:
                db.close()

    def test_discovery_order_ignores_title_words(self):
        with tempfile.TemporaryDirectory() as tmp:
            settings = self.build_home(tmp)
            plain = rl.normalize_job(self.ad("t1", title="Verksamhetsutvecklare"))
            keyword = rl.normalize_job(self.ad("t2", title="Senior AI Engineer, product engineer, full stack"))
            self.assertEqual(rl.discovery_score(plain, settings), rl.discovery_score(keyword, settings))


class JobSearchDiscoveryTests(_PipelineHarness):
    """Keyword queries reach the open backlog JobStream does not replay."""

    class FakeSearch:
        def __init__(self, hits_by_query, failing=(), ads=None):
            self.hits_by_query = hits_by_query
            self.failing = set(failing)
            self.ads = ads or {}
            self.queries = []

        def search(self, query):
            self.queries.append(query)
            if query in self.failing or "*" in self.failing:
                raise rl.RemoteAPIError(f"HTTP 503 for {query}")
            return self.hits_by_query.get(query, [])

        def ad(self, ad_id):
            return self.ads[ad_id]

    def test_queries_cross_terms_and_locations_without_duplicates(self):
        with tempfile.TemporaryDirectory() as tmp:
            settings = self.build_home(tmp, search_terms=["AI engineer", "ai  ENGINEER", "Systemutvecklare"],
                                       location_terms=["Stockholm"])
            self.assertEqual(rl.build_queries(settings),
                             ["AI engineer", "AI engineer Stockholm", "Systemutvecklare", "Systemutvecklare Stockholm"])
            located = dataclasses.replace(settings, include_unlocated_searches=False)
            self.assertEqual(rl.build_queries(located), ["AI engineer Stockholm", "Systemutvecklare Stockholm"])

    def test_hits_merge_across_queries_and_a_bare_hit_loads_the_full_ad(self):
        with tempfile.TemporaryDirectory() as tmp:
            settings = self.build_home(tmp, search_terms=["AI engineer"], location_terms=["Stockholm"])
            full = self.raw_job("b1", "Backend Developer", "Acme AB", "Stockholm", "Build Python services.")
            bare = dict(full, description={})
            shared = self.raw_job("a1", "AI Engineer", "Acme AB", "Stockholm", "Build LLM features.")
            fake = self.FakeSearch({"AI engineer": [shared, bare], "AI engineer Stockholm": [shared]}, ads={"b1": full})
            stats = rl.RunStats()
            found = rl.fetch_jobsearch(fake, settings, stats)
            self.assertEqual(found["a1"].matched_queries, {"AI engineer", "AI engineer Stockholm"})
            self.assertIn("Build Python services.", found["b1"].description)
            self.assertEqual((stats.queries_attempted, stats.queries_succeeded, stats.search_hits), (2, 2, 3))

    def test_one_failing_query_is_tolerated_and_all_failing_raises(self):
        with tempfile.TemporaryDirectory() as tmp:
            settings = self.build_home(tmp, search_terms=["AI engineer"], location_terms=["Stockholm"])
            hit = self.raw_job("a1", "AI Engineer", "Acme AB", "Stockholm", "Build LLM features.")
            found = rl.fetch_jobsearch(self.FakeSearch({"AI engineer": [hit]}, failing={"AI engineer Stockholm"}),
                                       settings, rl.RunStats())
            self.assertEqual(sorted(found), ["a1"])
            with self.assertRaises(rl.RemoteAPIError):
                rl.fetch_jobsearch(self.FakeSearch({}, failing={"*"}), settings, rl.RunStats())


class DiscoveryIsolationTests(_PipelineHarness):
    """One failing source is reported; only every source failing stops a run."""

    SITE = {"platform": "teamtailor", "name": "acme", "url": "https://acme.example.invalid", "company": "Acme AB"}

    def site_job(self, settings):
        site = settings.career_sites[0]
        return rl.career_site_job(site, key="42", title="Platform Developer", url="https://acme.example.invalid/42",
                                  description="Build internal tools with Python and TypeScript for operations.",
                                  city="Stockholm")

    def fetch_only(self, settings, *, stream_error=None, site_error=None):
        class Stream:
            def __init__(self, http, settings_):
                pass

            def stream(self, updated_after):
                if stream_error:
                    raise stream_error
                return []

        def collector(http, site, settings_, known):
            if site_error:
                raise site_error
            return [self.site_job(settings)]

        with unittest.mock.patch.object(rl, "JobStreamClient", Stream), \
                unittest.mock.patch.dict(rl.CAREER_SITE_COLLECTORS, {"teamtailor": collector}), \
                contextlib.redirect_stdout(io.StringIO()):
            rl.run_pipeline(settings, fetch_only=True, evaluate_only=False)

    def test_a_failing_source_is_reported_and_the_others_still_run(self):
        with tempfile.TemporaryDirectory() as tmp:
            settings = self.build_home(tmp, career_sites=[self.SITE])
            self.fetch_only(settings, stream_error=rl.RemoteAPIError("HTTP 503"))
            db = rl.Database(settings.db_path)
            try:
                self.assertEqual(db.status()["jobs_by_source"], {rl.CAREER_SITE_SOURCE: 1})
            finally:
                db.close()
            stats = self.last_run(settings)
            self.assertEqual(stats["discovery_failures"], ["JobStream"])
            self.assertEqual((stats["career_sites_read"], stats["career_site_jobs_stored"]), (1, 1))

    def test_every_source_failing_fails_the_run(self):
        with tempfile.TemporaryDirectory() as tmp:
            settings = self.build_home(tmp, career_sites=[self.SITE])
            with self.assertRaises(rl.RemoteAPIError):
                self.fetch_only(settings, stream_error=rl.RemoteAPIError("HTTP 503"),
                                site_error=rl.RemoteAPIError("HTTP 404"))

    def test_a_broken_site_is_isolated_even_when_its_parser_crashes(self):
        with tempfile.TemporaryDirectory() as tmp:
            settings = self.build_home(tmp, career_sites=[self.SITE])
            with self.assertLogs(rl.LOG, level="ERROR"):
                self.fetch_only(settings, site_error=AttributeError("'list' object has no attribute 'get'"))
            self.assertEqual(self.last_run(settings)["discovery_failures"], ["career site acme"])

    def test_the_alert_names_the_failed_sources(self):
        self.assertEqual(rl.format_run_alerts(rl.RunStats()), [])
        alert = rl.format_run_alerts(rl.RunStats(discovery_failures=["JobStream", "career site acme"]))[0]
        self.assertEqual(alert, "⚠️ RoleLens: discovery failed for JobStream, career site acme; "
                                "the other sources were still read.")


class JsonRequestShapeTests(unittest.TestCase):
    """JobStream and some career sites answer with an array, most endpoints with an object."""

    def call(self, body, expect):
        with unittest.mock.patch.object(rl.urllib.request, "urlopen", lambda request, timeout=None: io.BytesIO(body)):
            return rl.HttpClient(retries=0, user_agent="test").json_request(
                "GET", "https://example.invalid/x", expect=expect)

    def test_an_array_endpoint_returns_a_list(self):
        self.assertEqual(self.call(b'[{"id": "1"}]', list), [{"id": "1"}])

    def test_the_wrong_shape_is_refused(self):
        with self.assertRaises(rl.RemoteAPIError):
            self.call(b"[1, 2]", dict)

    def test_objects_remain_the_default(self):
        self.assertEqual(self.call(b'{"ok": true}', dict), {"ok": True})


class ScreeningTests(_PipelineHarness):
    """What needs no model is settled before the judge is paid."""

    def test_ads_the_rules_block_are_stored_without_a_model_call(self):
        with tempfile.TemporaryDirectory() as tmp:
            settings = self.build_home(tmp)
            (settings.profile_dir / "knowledge_catalogue.json").write_text(json.dumps(
                {"professional_experience": [{"role": "Engineer", "period": "2025-01 to 2025-12"}]}), encoding="utf-8")
            self.seed(settings, [
                ("s1", "Head of Backend Engineering", "Company A", "Stockholm", "Build services."),
                ("s2", "Backend Engineer", "Company B", "Stockholm", "Minimum of 10 years of experience in backend work."),
                ("s3", "Backend Engineer", "Company C", "Stockholm", "Build APIs with Python."),
                ("s4", "Senior Backend Engineer", "Company D", "Stockholm", "Build services."),
                ("s5", "Sommarjobb: Developer", "Company E", "Stockholm", "Build services over the summer."),
            ])
            _, calls = self.drive(settings)
            # A senior grade goes to the judge as a stretch; a management title does not.
            self.assertEqual({sid for _, ids in calls for sid in ids}, {"s3", "s4"})
            self.assertEqual(self.last_run(settings)["rules_screened"], 3)
            self.assertEqual(self.pending(settings), 0, "settled ads leave the queue")


class SeniorityAndScopeTests(unittest.TestCase):
    """Matches are checked against the knowledge catalogue: its dated roles
    settle years and seniority, and the ad's own words say what is required."""

    CATALOGUE = {
        "candidate": {"name": "Example Person"},
        "professional_experience": [
            {"role": "Engineer", "period": "2024-present"},
            {"role": "Volunteer developer", "period": "2026-01 to 2026-06"},
            {"role": "Intern", "period": "2025-04 to 2025-08"},
        ],
    }

    def findings(self, title, description, years=2.75, students=True):
        return rl.seniority_and_scope_findings(title, description, years, students)

    def test_overlapping_roles_are_counted_once(self):
        self.assertAlmostEqual(rl.catalogue_experience_years(self.CATALOGUE, today=dt.date(2026, 9, 11)), 2.75, delta=0.01)
        self.assertIsNone(rl.catalogue_experience_years({"professional_experience": [{"role": "undated"}]}))
        self.assertIsNone(rl.catalogue_experience_years(None))

    def test_the_shipped_example_catalogue_is_dated(self):
        catalogue = json.loads(repo_file("profile", "knowledge_catalogue.json").read_text(encoding="utf-8"))
        self.assertIsNotNone(rl.catalogue_experience_years(catalogue))

    def test_required_years_come_only_from_requirement_sentences(self):
        cases = {
            "Minimum of 5 years of experience in business analysis or related roles": 5,
            "● 5+ years in developing modern Web applications": 5,
            "Du har minst fem års erfarenhet av systemutveckling.": 5,
            "You bring 3-5 years of experience with React.": 3,
            "Du har flera års erfarenhet av agilt arbete.": 3,
            "Experience with Terraform for 5 years is a plus.": None,
            "Founded 25 years ago, we have grown across Europe.": None,
            "Anställningen är ett projekt på 2 år.": None,
        }
        for text, expected in cases.items():
            with self.subTest(text=text):
                self.assertEqual(rl.required_experience_years(text)[0], expected)

    def test_a_senior_grade_is_a_stretch_and_a_management_title_is_not(self):
        for title in ("Senior Software Engineer", "Sr. Fullstack Engineer – Backend", "Lead Developer"):
            with self.subTest(title=title):
                self.assertEqual(self.findings(title, "Build and run production services.")[:2], ([], rl.STRETCH_CAP))
        for title in ("Head of Data, Analytics and AI", "AI Tech Director", "DevOps Teamleader"):
            with self.subTest(title=title):
                blockers, _, _ = self.findings(title, "Build and run production services.")
                self.assertEqual([blocker["type"] for blocker in blockers], ["hard"])

    def test_realistic_ads_pass_and_an_architect_title_is_a_stretch(self):
        for title, description in [
            ("Software Application Developer", "Proven experience in software engineering and Large Language Models."),
            ("Program Lead", "You are a few years into your career and good at improving how organisations work."),
            ("Member of Technical Staff", "Build and ship model-powered products."),
        ]:
            with self.subTest(title=title):
                self.assertEqual(self.findings(title, description)[:2], ([], None))
        self.assertEqual(self.findings("Business Architect", "Relevant degree or equivalent experience.")[:2],
                         ([], rl.STRETCH_CAP))

    def test_an_internship_is_out_of_scope_only_when_the_search_says_so(self):
        description = "Description:\nVi söker en driven praktikant som vill hjälpa oss att öka vår synlighet."
        self.assertTrue(self.findings("SEO & AI Search", description)[0])
        self.assertFalse(self.findings("SEO & AI Search", description, students=False)[0])

    def test_without_a_dated_catalogue_nothing_is_decided_about_seniority(self):
        self.assertEqual(self.findings("Senior Business Analyst", "Minimum of 5 years of experience.",
                                       years=None, students=False)[:2], ([], None))

    def test_the_policy_turns_a_finding_into_the_decision(self):
        item = evaluation_item("1", opportunity=80, career_fit=85)
        blocked = rl.validate_evaluation(item, job={"title": "Senior Business Analyst",
                                                    "description": "Minimum of 5 years of experience in business analysis."},
                                         experience_years=2.75)
        self.assertEqual(blocked.decision, "store_no_notify")
        stretched = rl.validate_evaluation(item, job={"title": "Business Architect",
                                                      "description": "Map processes with stakeholders."},
                                           experience_years=2.75)
        self.assertEqual((stretched.opportunity_score, stretched.decision), (rl.STRETCH_CAP, "notify_stretch"))


class YearsInFieldTests(unittest.TestCase):
    """Years demanded in one named field cap an ad at a stretch and never remove a card."""

    JOB = {"title": "QA/Tester", "description": "Test web applications and APIs in a product team."}

    def judge(self, fit, opportunity, status, job=None):
        item = evaluation_item("1", opportunity=opportunity, career_fit=fit, must_have_assessment=[{
            "requirement": "Years in field: 2+ years of experience in QA and software testing",
            "status": status, "reason": "Testing is part of the candidate's engineering work, not a QA role.",
        }])
        return rl.validate_evaluation(item, job=job or self.JOB, experience_years=2.75)

    def test_a_field_gap_demotes_a_good_card_to_a_stretch(self):
        evaluation = self.judge(84, 80, "partial")
        self.assertEqual((evaluation.opportunity_score, evaluation.decision), (rl.STRETCH_CAP, "notify_stretch"))

    def test_a_field_gap_never_removes_or_makes_a_card(self):
        kept = self.judge(70, 76, "unmet")
        self.assertEqual(kept.decision, "notify_stretch")
        self.assertFalse([blocker for blocker in kept.blockers if blocker.get("type") == "hard"])
        self.assertEqual(self.judge(84, 50, "unmet").decision, "store_no_notify")

    def test_other_hard_blockers_still_win_and_a_met_requirement_costs_nothing(self):
        job = {"title": "QA/Tester", "description": "Minimum of 5 years of experience in software testing."}
        self.assertEqual(self.judge(84, 80, "partial", job=job).decision, "store_no_notify")
        met = self.judge(84, 80, "met")
        self.assertEqual((met.opportunity_score, met.decision), (80, "notify_good"))


class RoleVocabularyTests(unittest.TestCase):
    """The vocabulary reads the ad word-aware, exactly as it was calibrated."""

    def score(self, terms, text):
        return rl.RoleVocabulary.from_terms(terms).score(text)

    def test_short_and_padded_terms_never_fire_inside_other_words(self):
        self.assertEqual(self.score({" rag ": 3}, "Ett spännande uppdrag"), 0)
        self.assertEqual(self.score({" erp": 2}, "Enterprise-lösningar"), 0)
        self.assertEqual(self.score({" lean ": 1}, "Cleaning services"), 0)

    def test_short_and_padded_terms_still_match_whole_words(self):
        self.assertEqual(self.score({" crm": 2}, "Microsoft Dynamics CRM-system"), 2)
        self.assertEqual(self.score({" rag ": 3}, "Vi bygger RAG-lösningar"), 3)
        self.assertEqual(self.score({"rag-": 3}, "rag-baserad sökning"), 3)

    def test_long_terms_match_as_stems_whatever_the_case_or_diacritics(self):
        self.assertEqual(self.score({"förändringsled": 3}, "FÖRÄNDRINGSLEDARE till IT"), 3)

    def test_a_term_counts_once_and_negative_weights_subtract(self):
        terms = {"processutveckl": 2, "lokalvård": -2}
        self.assertEqual(self.score(terms, "processutveckling och mer processutveckling"), 2)
        self.assertEqual(self.score(terms, "Processutveckling inom lokalvård"), 0)

    def test_a_phrase_does_not_match_across_a_line_break(self):
        self.assertEqual(self.score({"large language model": 3}, "large language\nmodel"), 0)
        self.assertEqual(self.score({"large language model": 3}, "large language  model"), 3)

    def test_the_shipped_vocabulary_loads(self):
        self.assertGreater(len(rl.RoleVocabulary.load(repo_file("profile", "role_vocabulary.json")).terms), 100)

    def test_a_term_without_a_numeric_weight_is_refused(self):
        with self.assertRaises(rl.ConfigurationError):
            rl.RoleVocabulary.from_terms({"verksamhetsutveckl": "high"})


class ProfileFacetTests(_PipelineHarness):
    """What of the matcher profile is embedded, and how it is cut."""

    def with_profile(self, tmp, matcher):
        settings = self.build_home(tmp)
        (settings.profile_dir / "matcher_profile.json").write_text(json.dumps(matcher, ensure_ascii=False), encoding="utf-8")
        return settings

    def test_who_the_candidate_is_is_embedded_and_how_to_judge_is_not(self):
        with tempfile.TemporaryDirectory() as tmp:
            settings = self.with_profile(tmp, {
                "candidate_core": "Förändringsledare", "strong_capabilities": ["process design"],
                "constraints": {"swedish": "B1"}, "important_cautions": ["no clearance"]})
            self.assertEqual(rl.profile_facets(settings), (
                ("candidate_core", "Förändringsledare"), ("strong_capabilities", '["process design"]')))

    def test_the_career_profile_never_reaches_a_ranking_service(self):
        with tempfile.TemporaryDirectory() as tmp:
            settings = self.build_home(tmp)
            (settings.profile_dir / "career_profile.json").write_text(
                json.dumps({"professional_identity": "PRIVATE CV TEXT"}), encoding="utf-8")
            self.assertNotIn("PRIVATE CV TEXT", json.dumps(rl.profile_facets(settings)))

    def test_a_section_too_long_for_one_embedding_is_split_by_its_keys(self):
        with tempfile.TemporaryDirectory() as tmp:
            part = "x" * 4000
            settings = self.with_profile(tmp, {"candidate_core": "core", "differentiators": {"digital": part, "ai": part}})
            self.assertEqual([name for name, _ in rl.profile_facets(settings)],
                             ["candidate_core", "differentiators.digital", "differentiators.ai"])

    def test_a_profile_without_any_ranking_section_is_refused(self):
        with tempfile.TemporaryDirectory() as tmp:
            settings = self.with_profile(tmp, {"constraints": {}})
            with self.assertRaises(rl.ConfigurationError):
                rl.profile_facets(settings)


class EmbeddingClientTests(unittest.TestCase):
    class FakeHttp:
        def __init__(self, responder):
            self.responder = responder
            self.requests = []

        def json_request(self, method, url, *, headers=None, payload=None, timeout=30, expect=dict):
            self.requests.append((url, dict(headers or {}), payload))
            return self.responder(payload)

    @staticmethod
    def prediction(scale=3.0, tokens=7):
        values = [0.0] * rl.EMBEDDING_DIMENSIONS
        values[0] = scale
        return {"embeddings": {"values": values, "statistics": {"token_count": tokens, "truncated": False}}}

    def client(self, responder, batch_size=2):
        settings = types.SimpleNamespace(embedding_model="gemini-embedding-001", http_retries=0,
                                         embedding_timeout_seconds=5, embedding_batch_size=batch_size)
        http = self.FakeHttp(responder)
        return rl.EmbeddingClient(settings, {"VERTEX_GEMINI_API_KEY": "k"}, http), http

    def test_requests_are_batched_normalised_and_counted(self):
        client, http = self.client(lambda payload: {"predictions": [self.prediction() for _ in payload["instances"]]})
        vectors = client.embed(["a", "b", "c"], "RETRIEVAL_DOCUMENT")
        self.assertEqual([len(payload["instances"]) for _, _, payload in http.requests], [2, 1])
        url, headers, payload = http.requests[0]
        self.assertTrue(url.endswith("/gemini-embedding-001:predict"))
        self.assertEqual(headers, {"x-goog-api-key": "k"}, "the key travels in a header, never the URL")
        self.assertEqual(payload["parameters"], {"outputDimensionality": 768})
        self.assertAlmostEqual(vectors[0][0], 1.0, places=6)
        self.assertEqual((client.calls, client.tokens), (2, 21))

    def test_a_rejected_batch_is_halved_and_a_rejected_ad_gets_no_vector(self):
        def responder(payload):
            if len(payload["instances"]) > 1 or payload["instances"][0]["content"] == "bad":
                raise rl.HttpStatusError("HTTP 400: input too long", status=400)
            return {"predictions": [self.prediction()]}

        client, _ = self.client(responder)
        vectors = client.embed(["good", "bad", "fine"], "RETRIEVAL_DOCUMENT")
        self.assertEqual([vector is None for vector in vectors], [False, True, False])
        self.assertEqual(client.batch_size, 1)

    def test_an_invalid_key_is_a_failure_not_a_rejected_ad(self):
        def responder(payload):
            raise rl.HttpStatusError("HTTP 400: API key not valid. Please pass a valid API key.", status=400)

        client, http = self.client(responder)
        with self.assertRaises(rl.RemoteAPIError):
            client.embed(["a", "b"], "RETRIEVAL_DOCUMENT")
        self.assertEqual(len(http.requests), 1)

    def test_a_malformed_vector_is_a_failure(self):
        client, _ = self.client(lambda payload: {"predictions": [{"embeddings": {"values": [1.0, 2.0]}}]}, batch_size=1)
        with self.assertRaises(rl.RemoteAPIError):
            client.embed(["a"], "RETRIEVAL_QUERY")

    def test_the_per_minute_quota_is_waited_out_within_a_bound(self):
        refusals = [rl.RateLimitError("HTTP 429: quota exceeded")]

        def responder(payload):
            if refusals:
                raise refusals.pop()
            return {"predictions": [self.prediction() for _ in payload["instances"]]}

        client, http = self.client(responder)
        pauses = []
        client.sleep = pauses.append
        self.assertEqual(len(client.embed(["a", "b"], "RETRIEVAL_DOCUMENT")), 2)
        self.assertEqual(pauses, [rl.EMBEDDING_QUOTA_PAUSE_SECONDS])

        def always_limited(payload):
            raise rl.RateLimitError("HTTP 429: quota exceeded")

        client, _ = self.client(always_limited)
        client.sleep = lambda seconds: None
        with self.assertRaises(rl.RemoteAPIError):
            client.embed(["a"], "RETRIEVAL_DOCUMENT")
        self.assertEqual(client.quota_pauses, rl.EMBEDDING_QUOTA_MAX_PAUSES)


class EnrichmentClientTests(unittest.TestCase):
    class FakeHttp:
        def __init__(self, responder):
            self.responder = responder
            self.requests = []

        def json_request(self, method, url, *, headers=None, payload=None, timeout=30, expect=dict):
            self.requests.append(payload)
            return self.responder(payload)

    @staticmethod
    def answer(payload):
        return [{"doc_id": doc["doc_id"],
                 "enriched_candidates": {"competencies": [{"concept_label": "Python", "prediction": 0.9}]}}
                for doc in payload["documents_input"]]

    def test_documents_are_sent_ten_at_a_time(self):
        http = self.FakeHttp(self.answer)
        found = rl.EnrichmentClient(http).enrich([(str(n), "title", "text") for n in range(23)])
        self.assertEqual([len(p["documents_input"]) for p in http.requests], [10, 10, 3])
        self.assertEqual(found["22"], {"comp:python": 0.9})

    def test_a_rejected_batch_is_retried_one_ad_at_a_time(self):
        def responder(payload):
            documents = payload["documents_input"]
            if len(documents) > 1 or documents[0]["doc_id"] == "bad":
                raise rl.HttpStatusError("HTTP 400", status=400)
            return self.answer(payload)

        found = rl.EnrichmentClient(self.FakeHttp(responder)).enrich([("good", "t", "x"), ("bad", "t", "x")])
        self.assertEqual(sorted(found), ["good"])

    def test_an_outage_is_a_failure(self):
        def responder(payload):
            raise rl.RemoteAPIError("down")

        with self.assertRaises(rl.RemoteAPIError):
            rl.EnrichmentClient(self.FakeHttp(responder)).enrich([("a", "t", "x")])


class SelectionMathTests(unittest.TestCase):
    def test_tied_values_share_their_average_rank(self):
        self.assertEqual(rl.fractional_ranks([5, 9, 5, 1]), [2.5, 1.0, 2.5, 4.0])

    def test_agreement_between_both_orders_ranks_first(self):
        result = rl.selection_percentiles([(1, 9.0, 0.9), (2, 9.0, 0.1), (3, 0.0, 0.9), (4, 0.0, 0.1)])
        self.assertEqual(result[1], (0.25, True))
        self.assertEqual(result[2], result[3])
        self.assertEqual(result[4], (1.0, True))

    def test_a_tied_block_takes_its_average_position_not_its_best(self):
        result = rl.selection_percentiles([(1, 2.0, None), (2, 0.0, None), (3, 0.0, None), (4, 0.0, None)])
        self.assertEqual(result[1], (0.25, False))
        self.assertEqual(result[2], (0.75, False))

    def test_an_ad_without_embeddings_is_placed_by_vocabulary_among_the_whole_pool(self):
        result = rl.selection_percentiles([(1, 5.0, 0.9), (2, 1.0, 0.8), (3, 3.0, None), (4, 0.0, 0.1)])
        self.assertEqual(result[3], (0.5, False))
        self.assertTrue(all(result[job_id][1] for job_id in (1, 2, 4)))

    def test_enrichment_breaks_ties_the_other_orders_leave(self):
        result = rl.selection_percentiles([(1, 0.0, 0.5, 0.0), (2, 0.0, 0.5, 3.0), (3, 0.0, 0.9, 0.0), (4, 0.0, 0.1, None)])
        self.assertLess(result[2][0], result[1][0], "same embedding, but ad 2 requests the profile's skills")
        self.assertEqual(result[4], (1.0, True))

    def test_only_requested_concepts_count_and_occupations_count_double(self):
        concepts = rl.requested_concepts({
            "competencies": [{"concept_label": "Python", "prediction": 0.9}, {"concept_label": "Excel", "prediction": 0.3}],
            "occupations": [{"concept_label": "Systemutvecklare", "prediction": 0.8}],
        })
        self.assertEqual(concepts, {"comp:python": 0.9, "occ:systemutvecklare": 0.8})
        self.assertAlmostEqual(rl.enrichment_score(concepts, {"comp:python": 1.0, "occ:systemutvecklare": 1.0}), 0.9 + 1.6)
        self.assertEqual(rl.enrichment_score(concepts, {"comp:java": 1.0}), 0.0)

    def test_the_random_check_gives_an_ad_the_same_answer_every_time(self):
        picks = [rl.explored(f"ad{i}", "hash", 0.3) for i in range(200)]
        self.assertEqual(picks, [rl.explored(f"ad{i}", "hash", 0.3) for i in range(200)])
        self.assertTrue(30 <= sum(picks) <= 90, sum(picks))
        self.assertFalse(any(rl.explored(f"ad{i}", "hash", 0) for i in range(200)))


class AccessRefusalTests(_PipelineHarness):
    """A provider that refuses the account must not stop delivery, and must be heard about."""

    def test_a_vertex_refusal_is_judged_by_azure_and_announced(self):
        with tempfile.TemporaryDirectory() as tmp:
            settings = self.build_home(tmp)
            self.seed(settings, self.distinct(3))
            out, calls = self.drive(settings, script=lambda i, ids, p: "refuse" if p == rl.PRIMARY_PROVIDER else "ok")
            self.assertEqual([provider for provider, _ in calls], [rl.PRIMARY_PROVIDER, rl.FALLBACK_PROVIDER])
            self.assertEqual(self.pending(settings), 0, "Azure judged every job")
            self.assertIn("refused access (access_http_403)", out)

    def test_alerts_name_the_cause_and_stay_silent_on_a_clean_run(self):
        stats = rl.RunStats(fallback_reasons={"http_503": 1, "both_failed:access_http_401": 1},
                            embedding_failure="http_403")
        alerts = rl.format_run_alerts(stats)
        self.assertEqual(len(alerts), 2)
        self.assertIn("(access_http_401)", alerts[0])
        self.assertNotIn("http_503", alerts[0], "a passing outage is not an access problem")
        self.assertIn("embeddings unavailable (http_403)", alerts[1])


class MonthlyBudgetTests(_PipelineHarness):
    """Judging stops for the month at the budget, and nothing queued is lost."""

    def record_spend(self, settings, started_at, **tokens):
        db = rl.Database(settings.db_path)
        try:
            with db.conn:
                db.conn.execute("INSERT INTO runs(started_at, finished_at, status, stats_json) VALUES(?,?,?,?)",
                                (started_at, started_at, "success", json.dumps(tokens)))
        finally:
            db.close()

    def test_a_run_is_priced_from_its_tokens_and_errs_high(self):
        cost = rl.estimated_run_cost_usd({"prompt_tokens": 1_000_000, "completion_tokens": 100_000,
                                          "reasoning_tokens": 100_000, "embedding_tokens": 1_000_000})
        self.assertAlmostEqual(cost, 0.75 + 0.2 * 3.75 + 0.15)
        self.assertEqual(rl.estimated_run_cost_usd({"prompt_tokens": -5, "completion_tokens": True}), 0.0)
        self.assertAlmostEqual(rl.estimated_run_cost_usd({"triage_prompt_tokens": 1_000_000,
                                                          "triage_completion_tokens": 500_000,
                                                          "triage_reasoning_tokens": 500_000}), 0.10 + 0.40)

    def test_only_this_months_runs_count(self):
        with tempfile.TemporaryDirectory() as tmp:
            settings = self.build_home(tmp)
            self.record_spend(settings, "2026-08-31T23:59:59.000000+00:00", prompt_tokens=4_000_000)
            self.record_spend(settings, "2026-09-01T00:00:01.000000+00:00", prompt_tokens=2_000_000)
            db = rl.Database(settings.db_path)
            try:
                self.assertAlmostEqual(db.month_to_date_cost_usd(dt.datetime(2026, 9, 11, tzinfo=dt.timezone.utc)), 1.5)
            finally:
                db.close()

    def test_reaching_the_budget_pauses_judging_without_losing_jobs(self):
        with tempfile.TemporaryDirectory() as tmp:
            settings = self.build_home(tmp, monthly_budget_usd=1)
            self.seed(settings, self.distinct(3))
            self.record_spend(settings, rl.iso_now(), prompt_tokens=2_000_000)
            out, calls = self.drive(settings)
            self.assertEqual(calls, [], "no job is sent to a model")
            self.assertEqual(self.pending(settings), 3, "every job is still queued")
            self.assertIn("monthly budget reached ($1.50 of $1.00)", out)
            self.assertIn("3 jobs waiting", out)

    def test_the_budget_must_be_a_positive_amount(self):
        for bad in (0, -5, True, "100"):
            with self.subTest(bad=bad), tempfile.TemporaryDirectory() as tmp:
                with self.assertRaises(rl.ConfigurationError):
                    self.build_home(tmp, monthly_budget_usd=bad)


class RankingPipelineTests(_PipelineHarness):
    """Discovery stores every ad; ranking decides which ones the evaluator reads."""

    def build_home(self, tmp, **overrides):
        # Exact selections below assume no random check and no enrichment order
        # unless a test asks for them.
        overrides.setdefault("explore_share", 0)
        overrides.setdefault("use_enrichment", False)
        return super().build_home(tmp, **overrides)

    def fake_enrichment(self, concepts=None, fail=False):
        seen = {"documents": 0}
        mapping = concepts or {}

        class Fake:
            def __init__(self, http, *, timeout=120):
                self.calls = 0

            def enrich(self, documents):
                if fail:
                    raise rl.RemoteAPIError("HTTP 503 enrichment down")
                seen["documents"] += len(documents)
                return {doc_id: {k: v for phrase, found in mapping.items() if phrase in f"{headline} {text}"
                                 for k, v in found.items()}
                        for doc_id, headline, text in documents}

        rl.EnrichmentClient = Fake
        return seen

    def fake_embeddings(self, fail=False):
        """An ad's similarity to the profile is the number after 'relevance' in its text."""
        embedded = {"RETRIEVAL_QUERY": 0, "RETRIEVAL_DOCUMENT": 0}

        class Fake:
            def __init__(self, settings, secrets, http=None):
                self.calls = self.tokens = self.quota_pauses = 0

            def embed(self, texts, task_type):
                self.calls += 1
                if fail:
                    raise rl.RemoteAPIError("HTTP 403 billing disabled")
                embedded[task_type] += len(texts)
                vectors = []
                for text in texts:
                    found = re.search(r"relevance (0\.\d+)", text)
                    similarity = float(found.group(1)) if found else 1.0
                    vector = array.array("f", [0.0] * rl.EMBEDDING_DIMENSIONS)
                    vector[0] = similarity
                    vector[1] = (1.0 - similarity * similarity) ** 0.5
                    vectors.append(vector)
                return vectors

        rl.EmbeddingClient = Fake
        return embedded

    def use_vocabulary(self, settings, terms):
        (settings.profile_dir / "role_vocabulary.json").write_text(json.dumps({"terms": terms}), encoding="utf-8")

    def store(self, settings, ads):
        db = rl.Database(settings.db_path)
        try:
            for source_id, description in ads:
                db.upsert_job(rl.normalize_job(self.raw_job(source_id, f"Position {source_id}", "Company", "Stockholm",
                                                            description)))
        finally:
            db.close()

    def market(self, count):
        return [(f"ad{i:04d}", f"Everyday work, relevance {i / 1000:.3f}") for i in range(1, count + 1)]

    def evaluated(self, calls):
        return {source_id for _, ids in calls for source_id in ids}

    def state_of(self, settings, source_id):
        db = rl.Database(settings.db_path)
        try:
            return db.conn.execute("SELECT selection_state FROM jobs WHERE source_job_id=?", (source_id,)).fetchone()[0]
        finally:
            db.close()

    def test_only_the_top_share_reaches_the_evaluator(self):
        with tempfile.TemporaryDirectory() as tmp:
            settings = self.build_home(tmp, evaluate_top_share=0.134)
            self.use_vocabulary(settings, {"generativ ai": 3})
            self.fake_embeddings()
            self.store(settings, self.market(250))
            _, calls = self.drive(settings)
            # 13.4% of 250 is 33.5, so the 33 most relevant ads are read.
            self.assertEqual(self.evaluated(calls), {f"ad{i:04d}" for i in range(218, 251)})
            stats = self.last_run(settings)
            self.assertEqual((stats["jobs_ranked"], stats["jobs_selected"], stats["ranking_pool"]), (250, 33, 250))
            self.assertFalse(stats["ranking_degraded"])
            self.assertEqual(self.state_of(settings, "ad0001"), "not_selected", "kept, with its rank")

    def test_an_edited_ad_competes_again(self):
        with tempfile.TemporaryDirectory() as tmp:
            settings = self.build_home(tmp)
            self.use_vocabulary(settings, {"generativ ai": 3})
            self.fake_embeddings()
            self.store(settings, self.market(250))
            self.drive(settings)
            self.store(settings, [("ad0001", "Everyday work, relevance 0.999")])
            self.assertIsNone(self.state_of(settings, "ad0001"), "an edit clears the old decision")
            _, calls = self.drive(settings)
            self.assertEqual(self.evaluated(calls), {"ad0001"})

    def test_the_profile_is_embedded_once(self):
        with tempfile.TemporaryDirectory() as tmp:
            settings = self.build_home(tmp)
            self.use_vocabulary(settings, {"generativ ai": 3})
            embedded = self.fake_embeddings()
            self.store(settings, self.market(250))
            self.drive(settings)
            sections = len(rl.RankingContext.load(settings).facets)
            self.store(settings, [("new1", "Everyday work, relevance 0.500")])
            self.drive(settings)
            self.assertEqual(embedded, {"RETRIEVAL_QUERY": sections, "RETRIEVAL_DOCUMENT": 251})

    def test_changing_the_vocabulary_ranks_the_window_again(self):
        with tempfile.TemporaryDirectory() as tmp:
            settings = self.build_home(tmp)
            self.use_vocabulary(settings, {"generativ ai": 3})
            self.fake_embeddings()
            self.store(settings, self.market(250))
            self.drive(settings)
            self.use_vocabulary(settings, {"everyday": 1})
            self.drive(settings)
            self.assertEqual(self.last_run(settings)["jobs_ranked"], 250)

    def test_failed_embeddings_rank_on_the_vocabulary_and_are_flagged(self):
        with tempfile.TemporaryDirectory() as tmp:
            settings = self.build_home(tmp, degraded_top_share=0.30)
            self.use_vocabulary(settings, {"generativ ai": 3})
            self.fake_embeddings(fail=True)
            strong = [(f"s{i:02d}", "Generativ AI i vardagen") for i in range(20)]
            self.store(settings, strong + self.market(230))
            out, calls = self.drive(settings)
            self.assertEqual(self.evaluated(calls), {source_id for source_id, _ in strong})
            self.assertTrue(self.last_run(settings)["ranking_degraded"])
            self.assertIn("embeddings unavailable (RemoteAPIError)", out)

    def test_ads_passed_over_while_degraded_are_ranked_again_when_embeddings_return(self):
        with tempfile.TemporaryDirectory() as tmp:
            settings = self.build_home(tmp, degraded_top_share=0.30)
            self.use_vocabulary(settings, {"generativ ai": 3})
            self.fake_embeddings(fail=True)
            strong = [(f"s{i:02d}", "Generativ AI i vardagen") for i in range(20)]
            self.store(settings, strong + self.market(229) + [("hidden", "Everyday work, relevance 0.999")])
            self.drive(settings)
            self.assertEqual(self.state_of(settings, "hidden"), "not_selected")
            self.fake_embeddings()
            _, calls = self.drive(settings)
            self.assertIn("hidden", self.evaluated(calls))
            self.assertFalse(self.evaluated(calls) & {source_id for source_id, _ in strong},
                             "ads already read are not read again")

    def test_a_window_too_small_for_a_percentile_selects_everything(self):
        with tempfile.TemporaryDirectory() as tmp:
            settings = self.build_home(tmp)
            self.use_vocabulary(settings, {"generativ ai": 3})
            self.fake_embeddings()
            self.store(settings, self.market(5))
            _, calls = self.drive(settings)
            self.assertEqual(len(self.evaluated(calls)), 5)
            self.assertTrue(self.last_run(settings)["ranking_fail_open"])

    def test_selection_shares_must_be_proper_fractions(self):
        for key, bad in (("evaluate_top_share", 0), ("evaluate_top_share", 1.5), ("evaluate_top_share", True),
                         ("explore_share", -0.1), ("explore_share", 1.5), ("use_enrichment", "yes")):
            with self.subTest(key=key, bad=bad), tempfile.TemporaryDirectory() as tmp:
                with self.assertRaises(rl.ConfigurationError):
                    self.build_home(tmp, **{key: bad})

    def test_a_random_sample_of_skipped_ads_is_judged_too(self):
        with tempfile.TemporaryDirectory() as tmp:
            settings = self.build_home(tmp, evaluate_top_share=0.134, explore_share=0.2)
            self.use_vocabulary(settings, {"generativ ai": 3})
            self.fake_embeddings()
            self.store(settings, self.market(250))
            _, calls = self.drive(settings)
            ranked = {f"ad{i:04d}" for i in range(218, 251)}
            db = rl.Database(settings.db_path)
            try:
                explored = {row["source_job_id"] for row in db.conn.execute(
                    "SELECT source_job_id FROM jobs WHERE selection_reason = 'explore'")}
            finally:
                db.close()
            self.assertFalse(explored & ranked, "the sample only draws from ads the cut left out")
            self.assertTrue(20 <= len(explored) <= 70, len(explored))
            self.assertEqual(self.evaluated(calls), ranked | explored)

    def test_requested_skills_the_profile_names_lift_an_ad(self):
        with tempfile.TemporaryDirectory() as tmp:
            settings = self.build_home(tmp, use_enrichment=True)
            self.use_vocabulary(settings, {"generativ ai": 3})
            self.fake_embeddings()
            seen = self.fake_enrichment({"candidate_core": {"comp:python": 0.9},
                                         "Specialist python": {"comp:python": 0.95}})
            market = self.market(250)
            market[99] = ("ad0100", "Specialist python work, relevance 0.100")
            self.store(settings, market)
            _, calls = self.drive(settings)
            self.assertIn("ad0100", self.evaluated(calls))
            stats = self.last_run(settings)
            self.assertEqual((stats["jobs_enriched"], stats["enrichment_failure"]), (250, None))
            sections = len(rl.RankingContext.load(settings).facets)
            self.assertEqual(seen["documents"], sections + 250, "the profile is enriched once")

    def test_an_enrichment_outage_ranks_on_vocabulary_and_embeddings(self):
        with tempfile.TemporaryDirectory() as tmp:
            settings = self.build_home(tmp, use_enrichment=True, evaluate_top_share=0.134)
            self.use_vocabulary(settings, {"generativ ai": 3})
            self.fake_embeddings()
            self.fake_enrichment(fail=True)
            self.store(settings, self.market(250))
            _, calls = self.drive(settings)
            self.assertEqual(self.evaluated(calls), {f"ad{i:04d}" for i in range(218, 251)})
            stats = self.last_run(settings)
            self.assertEqual((stats["enrichment_failure"], stats["jobs_enriched"]), ("RemoteAPIError", 0))


class RankingDatabaseTests(unittest.TestCase):
    V2_DATABASE = """
        CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
        CREATE TABLE jobs (
            id INTEGER PRIMARY KEY, source TEXT NOT NULL, source_job_id TEXT NOT NULL,
            content_hash TEXT NOT NULL, title TEXT NOT NULL, company TEXT NOT NULL, url TEXT NOT NULL,
            municipality TEXT NOT NULL, region TEXT NOT NULL, country TEXT NOT NULL,
            remote INTEGER NOT NULL, fully_remote INTEGER NOT NULL, application_deadline TEXT,
            published_at TEXT, employment_type TEXT NOT NULL, scope TEXT NOT NULL,
            description TEXT NOT NULL, matched_queries_json TEXT NOT NULL,
            discovery_score INTEGER NOT NULL DEFAULT 0, raw_json TEXT NOT NULL,
            first_seen_at TEXT NOT NULL, last_seen_at TEXT NOT NULL, content_changed_at TEXT NOT NULL,
            UNIQUE(source, source_job_id)
        );
        INSERT INTO meta VALUES ('schema_version', '2');
        INSERT INTO jobs VALUES (1, 'platsbanken', 'old1', 'h', 'T', 'C', 'u', 'Stockholm', '', 'Sverige',
            0, 0, NULL, NULL, '', '', 'd', '[]', 0, '{}', '2026-09-01', '2026-09-01', '2026-09-01');
    """

    def job(self, source_id):
        return rl.normalize_job({"id": source_id, "headline": source_id, "description": {"text": "x"}})

    def test_a_version_1_database_upgrades_in_place_without_losing_rows(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "v2.db"
            with contextlib.closing(sqlite3.connect(path)) as conn:
                conn.executescript(self.V2_DATABASE)
            db = rl.Database(path)
            try:
                columns = {row["name"] for row in db.conn.execute("PRAGMA table_info(jobs)")}
                self.assertTrue({name for name, _ in rl.RANKING_COLUMNS} <= columns)
                self.assertEqual(db.get_meta("schema_version"), "3")
                self.assertEqual(db.status()["jobs"], 1)
            finally:
                db.close()

    def test_a_newer_schema_is_refused(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "future.db"
            rl.Database(path).close()
            with contextlib.closing(sqlite3.connect(path)) as conn:
                conn.execute("UPDATE meta SET value='4' WHERE key='schema_version'")
                conn.commit()
            with self.assertRaises(rl.ConfigurationError):
                rl.Database(path)

    def test_unselected_ads_never_enter_the_queue_and_the_best_ranked_come_first(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = rl.Database(Path(tmp) / "queue.db")
            try:
                ranked = (("mid", 0.10), ("best", 0.01), ("edge", 0.13))
                for source_id, _ in ranked:
                    db.upsert_job(self.job(source_id))
                self.assertEqual(db.pending_count("v", respect_live_mode=False), 0, "unranked")
                mark_selected(db)
                with db.conn:
                    for source_id, percentile in ranked:
                        db.conn.execute("UPDATE jobs SET rank_percentile=? WHERE source_job_id=?", (percentile, source_id))
                self.assertEqual([row["source_job_id"] for row in db.pending_jobs("v", 10)], ["best", "mid", "edge"])
                with db.conn:
                    db.conn.execute("UPDATE jobs SET selection_state='not_selected'")
                self.assertEqual(db.pending_count("v", respect_live_mode=False), 0, "not selected")
            finally:
                db.close()


# ---------------------------------------------------------------------------
# Career sites
# ---------------------------------------------------------------------------
class FakeHttp:
    """Answers json_request and fetch from (url fragment, answer) routes, in order."""

    def __init__(self, routes):
        self.routes = routes
        self.requests = []

    def _answer(self, method, url, payload=None):
        self.requests.append((method, url, payload))
        for fragment, answer in self.routes:
            if fragment in url:
                if isinstance(answer, Exception):
                    raise answer
                return answer(payload) if callable(answer) else answer
        raise AssertionError(f"unexpected request: {method} {url}")

    def json_request(self, method, url, *, headers=None, payload=None, timeout=30, expect=dict):
        return self._answer(method, url, payload)

    def fetch(self, url, *, timeout=30, accept="*/*"):
        answer = self._answer("GET", url)
        return answer.encode("utf-8") if isinstance(answer, str) else answer


LONG_TEXT = "You will build internal tools with Python and TypeScript for our operations team in Stockholm."


class CareerSiteConfigTests(_PipelineHarness):
    def test_sites_are_validated_and_a_disabled_one_is_skipped(self):
        sites = rl.parse_career_sites([
            {"platform": "Teamtailor", "name": "acme", "url": "https://acme.teamtailor.com/", "company": "Acme AB"},
            {"platform": "workday", "name": "big.co", "url": "https://big.wd3.myworkdayjobs.com", "tenant": "big",
             "site": "Careers", "applied_facets": {"locationCountry": ["x"]}},
            {"platform": "greenhouse", "name": "off", "board": "off", "enabled": False},
        ])
        self.assertEqual([(s.name, s.platform, s.url) for s in sites],
                         [("acme", "teamtailor", "https://acme.teamtailor.com"),
                          ("big.co", "workday", "https://big.wd3.myworkdayjobs.com")])
        self.assertEqual(sites[1].option("applied_facets"), {"locationCountry": ["x"]})
        self.assertEqual(sites[1].option("locale", "en-US"), "en-US")

    def test_invalid_sites_are_refused_with_a_reason(self):
        cases = [
            ("not a list", "career_sites must be a list"),
            ([{"platform": "monster", "name": "x"}], "platform must be one of"),
            ([{"platform": "greenhouse", "name": "Has Spaces", "board": "b"}], "name must be"),
            ([{"platform": "greenhouse", "name": "a", "board": "b"}, {"platform": "lever", "name": "a", "board": "b"}],
             "used by another"),
            ([{"platform": "workday", "name": "w", "url": "https://x.example.invalid", "tenant": "t"}], "'site'"),
            ([{"platform": "teamtailor", "name": "t", "url": "http://insecure.example.invalid"}], "https://"),
            ([{"platform": "greenhouse", "name": "g", "board": "b", "enabled": "no"}], "enabled"),
        ]
        for value, message in cases:
            with self.subTest(message=message):
                with self.assertRaises(rl.ConfigurationError) as caught:
                    rl.parse_career_sites(value)
                self.assertIn(message, str(caught.exception))

    def test_every_platform_has_a_collector(self):
        self.assertEqual(set(rl.CAREER_SITE_COLLECTORS), set(rl.CAREER_SITE_REQUIRED))

    def test_a_configuration_without_any_source_is_refused(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(rl.ConfigurationError):
                self.build_home(tmp, use_jobstream=False, search_terms=[], career_sites=[])

    def test_the_shipped_example_configuration_loads(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            shutil.copy2(repo_file("config.json"), home / "config.json")
            settings = rl.Settings.load(home)
            self.assertTrue(settings.use_jobstream)
            self.assertTrue(settings.career_sites)


class CareerSiteCollectorTests(_PipelineHarness):
    """Each collector turns one platform's public feed into JobRecords."""

    def settings(self, tmp, **overrides):
        overrides.setdefault("career_site_max_details", 2)
        return self.build_home(tmp, **overrides)

    def site(self, **entry):
        return rl.parse_career_sites([entry])[0]

    def test_teamtailor_reads_the_jobposting_and_skips_postings_without_text(self):
        feed = {"title": "Acme Group", "items": [
            {"id": "u1", "url": "https://acme.teamtailor.com/jobs/u1", "title": "ignored",
             "_jobposting": {"title": "Backend Developer", "description": f"<p>{LONG_TEXT}</p><ul><li>Python</li></ul>",
                             "datePosted": "2026-09-01T08:00:00+02:00", "validThrough": "2026-10-01",
                             "employmentType": ["FULL_TIME"], "hiringOrganization": {"name": "Acme AB"},
                             "jobLocation": [{"address": {"addressLocality": "Stockholm", "addressRegion": "Sweden",
                                                          "addressCountry": "SE"}}]}},
            {"id": "u2", "url": "https://acme.teamtailor.com/jobs/u2", "title": "Title Only"},
        ]}
        with tempfile.TemporaryDirectory() as tmp:
            site = self.site(platform="teamtailor", name="acme", url="https://acme.teamtailor.com")
            [job] = rl.collect_teamtailor(FakeHttp([("/jobs.json", feed)]), site, self.settings(tmp), set())
        self.assertEqual((job.source_job_id, job.title, job.company), ("acme:u1", "Backend Developer", "Acme AB"))
        self.assertEqual((job.municipality, job.region, job.country), ("Stockholm", "", "Sverige"))
        self.assertEqual((job.published_at, job.application_deadline, job.employment_type),
                         ("2026-09-01", "2026-10-01", "FULL_TIME"))
        self.assertIn("\n- Python", job.description)

    def test_varbi_reads_the_rss_feed_with_the_configured_place(self):
        feed = f"""<?xml version="1.0" encoding="UTF-8"?><rss version="2.0"><channel>
            <title>Nya lediga jobb hos Exempeluniversitetet</title>
            <item><title>Systemutvecklare</title><link>https://ex.varbi.com/sv/what:job/jobID:123/</link>
                  <description>&lt;p&gt;{LONG_TEXT}&lt;/p&gt;</description><pubDate>Mon, 14 Sep 2026 14:22:46 +0200</pubDate></item>
            <item><title>No id</title><link>https://ex.varbi.com/sv/</link><description>{LONG_TEXT}</description></item>
        </channel></rss>"""
        with tempfile.TemporaryDirectory() as tmp:
            site = self.site(platform="varbi", name="ex", url="https://ex.varbi.com", city="Uppsala", region="Uppsala län")
            [job] = rl.collect_varbi(FakeHttp([("what:rssfeed", feed)]), site, self.settings(tmp), set())
        self.assertEqual((job.source_job_id, job.company, job.municipality, job.region, job.published_at),
                         ("ex:123", "Exempeluniversitetet", "Uppsala", "Uppsala län", "2026-09-14"))
        self.assertTrue(job.description.startswith("You will build"))

    def test_a_malformed_varbi_feed_is_a_remote_failure(self):
        with tempfile.TemporaryDirectory() as tmp:
            site = self.site(platform="varbi", name="ex", url="https://ex.varbi.com")
            with self.assertRaises(rl.RemoteAPIError):
                rl.collect_varbi(FakeHttp([("what:rssfeed", "<rss><channel>")]), site, self.settings(tmp), set())

    def test_greenhouse_unescapes_its_html_and_marks_foreign_offices(self):
        payload = {"jobs": [
            {"id": 7, "title": "Product Engineer", "absolute_url": "https://job-boards.greenhouse.io/acme/jobs/7",
             "content": "&lt;p&gt;" + LONG_TEXT + "&lt;/p&gt;", "location": {"name": "Stockholm, Sweden"},
             "first_published": "2026-09-02T10:00:00Z"},
            {"id": 8, "title": "Product Engineer", "absolute_url": "https://job-boards.greenhouse.io/acme/jobs/8",
             "content": LONG_TEXT, "location": {"name": "Berlin"}},
        ]}
        with tempfile.TemporaryDirectory() as tmp:
            settings = self.settings(tmp)
            site = self.site(platform="greenhouse", name="acme", board="acme", company="Acme AB")
            http = FakeHttp([("boards-api.greenhouse.io/v1/boards/acme/jobs?content=true", payload)])
            stockholm, berlin = rl.collect_greenhouse(http, site, settings, set())
            self.assertNotIn("<p>", stockholm.description)
            self.assertEqual((stockholm.municipality, stockholm.country, stockholm.published_at),
                             ("Stockholm", "Sverige", "2026-09-02"))
            self.assertEqual(berlin.country, "Outside Sweden")
            self.assertEqual(rl.prefilter_reason(berlin, settings), "outside_sweden")

    def test_lever_joins_its_sections_and_uses_the_eu_instance_when_asked(self):
        postings = [{"id": "p1", "text": "Solutions Engineer", "hostedUrl": "https://jobs.eu.lever.co/acme/p1",
                     "descriptionPlain": LONG_TEXT, "lists": [{"text": "Requirements", "content": "<li>SQL</li>"}],
                     "additionalPlain": "Apply in English.", "categories": {"location": "Göteborg", "commitment": "Full-time"},
                     "createdAt": 1788000000000}]
        with tempfile.TemporaryDirectory() as tmp:
            site = self.site(platform="lever", name="acme", board="acme", instance="eu")
            http = FakeHttp([("api.eu.lever.co/v0/postings/acme", postings)])
            [job] = rl.collect_lever(http, site, self.settings(tmp), set())
        self.assertIn("Requirements\n- SQL", job.description)
        self.assertEqual((job.municipality, job.country, job.employment_type), ("Göteborg", "Sverige", "Full-time"))
        self.assertEqual(job.published_at, "2026-08-29")

    def test_ashby_skips_unlisted_postings_and_reads_the_postal_address(self):
        payload = {"name": "Acme", "jobs": [
            {"id": "a1", "title": "Data Engineer", "jobUrl": "https://jobs.ashbyhq.com/acme/a1", "descriptionHtml": LONG_TEXT,
             "address": {"postalAddress": {"addressLocality": "Malmö", "addressCountry": "Sweden"}},
             "isRemote": True, "publishedAt": "2026-09-03T00:00:00Z", "employmentType": "FullTime"},
            {"id": "a2", "title": "Hidden", "isListed": False, "descriptionHtml": LONG_TEXT},
        ]}
        with tempfile.TemporaryDirectory() as tmp:
            site = self.site(platform="ashby", name="acme", board="acme")
            [job] = rl.collect_ashby(FakeHttp([("posting-api/job-board/acme", payload)]), site, self.settings(tmp), set())
        self.assertEqual((job.company, job.municipality, job.country, job.remote), ("Acme", "Malmö", "Sverige", True))

    def test_smartrecruiters_fetches_details_only_for_new_postings_within_the_budget(self):
        listing = {"content": [
            {"id": "1", "name": "Known", "location": {"city": "Stockholm", "country": "se"}},
            {"id": "2", "name": "Integration Developer", "location": {"city": "Solna", "country": "se"},
             "releasedDate": "2026-09-04T09:00:00.000Z", "typeOfEmployment": {"label": "Full-time"},
             "company": {"name": "Acme Sverige AB"}},
            {"id": "3", "name": "Over Budget", "location": {"city": "Solna", "country": "se"}},
        ]}
        detail = {"postingUrl": "https://jobs.smartrecruiters.com/acme/2",
                  "jobAd": {"sections": {"jobDescription": {"text": f"<p>{LONG_TEXT}</p>"},
                                         "qualifications": {"text": "<p>REST APIs</p>"}}}}
        with tempfile.TemporaryDirectory() as tmp:
            site = self.site(platform="smartrecruiters", name="acme", board="acme")
            http = FakeHttp([("/postings/2", detail), ("/postings?", listing)])
            jobs = rl.collect_smartrecruiters(http, site, self.settings(tmp, career_site_max_details=1), {"1"})
        self.assertEqual([job.source_job_id for job in jobs], ["acme:2"])
        self.assertIn("country=se", http.requests[0][1])
        self.assertEqual(sum("/postings/" in url for _, url, _ in http.requests), 1, "one detail within the budget")
        self.assertEqual((jobs[0].country, jobs[0].published_at), ("Sverige", "2026-09-04"))
        self.assertIn("REST APIs", jobs[0].description)

    def test_workday_pages_by_total_sends_the_facets_and_skips_known_postings(self):
        pages = [
            {"total": 21, "jobPostings": [{"title": f"Job {n}", "externalPath": f"/job/Stockholm/Job_{n}"} for n in range(20)]},
            {"jobPostings": [{"title": "Job 20", "externalPath": "/job/Stockholm/Job_20"}]},
        ]
        detail = {"jobPostingInfo": {"title": "Job 1", "jobDescription": f"<p>{LONG_TEXT}</p>",
                                     "location": "Stockholm, Sweden", "country": {"descriptor": "Sweden"},
                                     "timeType": "Full time"}}
        with tempfile.TemporaryDirectory() as tmp:
            site = self.site(platform="workday", name="big", url="https://big.wd3.myworkdayjobs.com", tenant="big",
                             site="Careers", applied_facets={"locationCountry": ["se-id"]})
            http = FakeHttp([("/jobs", lambda payload: pages[payload["offset"] // 20]), ("/job/", detail)])
            jobs = rl.collect_workday(http, site, self.settings(tmp), {"/job/Stockholm/Job_0"})
        posts = [payload for method, _, payload in http.requests if method == "POST"]
        self.assertEqual([p["offset"] for p in posts], [0, 20])
        self.assertEqual(posts[0]["appliedFacets"], {"locationCountry": ["se-id"]})
        self.assertEqual([job.source_job_id for job in jobs], ["big:/job/Stockholm/Job_1", "big:/job/Stockholm/Job_2"])
        self.assertEqual((jobs[0].country, jobs[0].url), ("Sverige", "https://big.wd3.myworkdayjobs.com/en-US/Careers/job/Stockholm/Job_1"))

    def test_successfactors_reads_the_rendered_search_and_job_pages(self):
        search = ('<a class="jobTitle-link" href="/job/Stockholm-Data-Engineer/111/">Data Engineer</a>'
                  '<a class="jobTitle-link" href="/job/Stockholm-Data-Engineer/111/">Data Engineer</a>'
                  '<a class="jobTitle-link" href="/job/G%C3%B6teborg-Tester/222/">Tester &amp; QA</a>')
        page = (f'<span class="jobLocation">Stockholm, SE</span><div><span class="jobdescription"><p>{LONG_TEXT}</p>'
                "</span>\n</div>")
        with tempfile.TemporaryDirectory() as tmp:
            site = self.site(platform="successfactors", name="big", url="https://jobs.big.example.invalid", max_pages=1)
            http = FakeHttp([("/search/", search), ("/job/", page)])
            jobs = rl.collect_successfactors(http, site, self.settings(tmp), set())
        self.assertIn("locationsearch=Sweden", http.requests[0][1])
        self.assertEqual([(job.source_job_id, job.title) for job in jobs], [("big:111", "Data Engineer"), ("big:222", "Tester & QA")])
        self.assertEqual((jobs[0].municipality, jobs[0].country), ("Stockholm", "Sverige"))

    def test_postings_are_stored_once_and_platsbanken_copies_stay_out(self):
        with tempfile.TemporaryDirectory() as tmp:
            settings = self.build_home(tmp, career_sites=[
                {"platform": "teamtailor", "name": "acme", "url": "https://acme.example.invalid", "company": "Acme AB"}])
            self.seed(settings, [("pb1", "AI Engineer", "Acme AB", "Stockholm", "Found on Platsbanken first.")])
            site = settings.career_sites[0]

            def collector(http, site_, settings_, known):
                return [job for job in (
                    rl.career_site_job(site, key="1", title="Platform Developer", url="https://acme.example.invalid/1",
                                       description=LONG_TEXT, city="Stockholm"),
                    rl.career_site_job(site, key="2", title="AI Engineer", url="https://acme.example.invalid/2",
                                       description=LONG_TEXT, city="Stockholm"),
                    rl.career_site_job(site, key="3", title="Platform Developer", url="https://acme.example.invalid/3",
                                       description=LONG_TEXT, city="Oslo"),
                )]

            db = rl.Database(settings.db_path)
            try:
                with unittest.mock.patch.dict(rl.CAREER_SITE_COLLECTORS, {"teamtailor": collector}):
                    first, second = rl.RunStats(), rl.RunStats()
                    self.assertEqual(rl.collect_career_sites(None, db, settings, first), [])
                    rl.collect_career_sites(None, db, settings, second)
                stored = [tuple(row) for row in db.conn.execute(
                    "SELECT source_job_id, title FROM jobs WHERE source=?", (rl.CAREER_SITE_SOURCE,))]
            finally:
                db.close()
            self.assertEqual(stored, [("acme:1", "Platform Developer")])
            self.assertEqual((first.career_site_jobs_seen, first.career_site_jobs_stored, first.career_site_jobs_known,
                              first.jobs_prefiltered), (3, 1, 1, 1))
            self.assertEqual(second.career_site_jobs_stored, 0, "an unchanged posting is not written again")

    def test_place_and_text_helpers(self):
        self.assertEqual(rl.place_country("Kista, Stockholm"), "Sverige")
        self.assertEqual(rl.place_country("London, United Kingdom"), "Outside Sweden")
        self.assertEqual(rl.place_country("Hybrid - HQ"), "")
        self.assertEqual(rl.place_country("Lusaka"), "", "a foreign token only counts as a whole word")
        self.assertEqual(rl.country_name("se"), "Sverige")
        self.assertEqual(rl.parse_date("Mon, 14 Sep 2026 14:22:46 +0200"), "2026-09-14")
        self.assertIsNone(rl.parse_date("2026-02-31"))
        self.assertEqual(rl.html_to_text("<script>x()</script><p>A&amp;B</p><ul><li>one</li></ul>"), "A&B\n\n- one")


class FreshCloneInstallTests(unittest.TestCase):
    """install.sh must work from a checkout that ships only *.example files."""

    SCRIPT = REPO / "install.sh"

    @classmethod
    def setUpClass(cls):
        if os.name != "posix" or shutil.which("bash") is None:
            raise unittest.SkipTest("install.sh needs a POSIX shell")

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
        src = Path(root) / "checkout"
        (src / "profile").mkdir(parents=True)
        for name in ("install.sh", "rolelens.py", "config.example.json", "secrets.env.example",
                     "profile/matcher_rules_v1_1.json"):
            shutil.copy2(REPO / name, src / name)
        for stem in ("career_profile", "matcher_profile", "search_lenses", "role_vocabulary", "knowledge_catalogue"):
            shutil.copy2(REPO / "profile" / f"{stem}.example.json", src / "profile" / f"{stem}.example.json")
        return src

    def run_install(self, src, home, scripts, *args):
        env = dict(os.environ, ROLELENS_HOME=str(home), ROLELENS_SCRIPTS_HOME=str(scripts))
        return subprocess.run(["bash", str(src / "install.sh"), *args],
                              capture_output=True, text=True, env=env, cwd=str(src))

    def test_fresh_clone_installs_from_examples_and_the_app_loads_it(self):
        with tempfile.TemporaryDirectory() as tmp:
            src = self.fresh_checkout(tmp)
            home, scripts = Path(tmp) / "home", Path(tmp) / "scripts"
            result = self.run_install(src, home, scripts)
            self.assertEqual(result.returncode, 0, result.stderr)
            for rel in ("config.json", "secrets.env", *(f"profile/{name}" for name in PROFILE_FILES)):
                self.assertTrue((home / rel).is_file(), rel)
            self.assertTrue((scripts / "scripts" / "rolelens.py").is_file())
            self.assertEqual(rl.Settings.load(home).primary_provider, rl.PRIMARY_PROVIDER)

    def test_rerunning_does_not_overwrite_user_edits_and_refresh_never_touches_secrets(self):
        with tempfile.TemporaryDirectory() as tmp:
            src = self.fresh_checkout(tmp)
            home, scripts = Path(tmp) / "home", Path(tmp) / "scripts"
            self.run_install(src, home, scripts)
            (home / "config.json").write_text('{"preferred_locations": ["My own town"]}', encoding="utf-8")
            (home / "secrets.env").write_text("VERTEX_GEMINI_API_KEY=mine\n", encoding="utf-8")

            result = self.run_install(src, home, scripts)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("My own town", (home / "config.json").read_text(encoding="utf-8"))
            self.assertIn("Kept your existing files", result.stdout)

            result = self.run_install(src, home, scripts, "--refresh-config")
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("career_sites", json.loads((home / "config.json").read_text(encoding="utf-8")))
            self.assertEqual((home / "secrets.env").read_text(encoding="utf-8"), "VERTEX_GEMINI_API_KEY=mine\n")

    def test_missing_template_fails_loudly_and_an_unknown_argument_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            src = self.fresh_checkout(tmp)
            (src / "config.example.json").unlink()
            result = self.run_install(src, Path(tmp) / "home", Path(tmp) / "scripts")
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("cannot install config.json", result.stderr)
            result = self.run_install(src, Path(tmp) / "home", Path(tmp) / "scripts", "--wat")
            self.assertEqual(result.returncode, 2)

    def test_secrets_permissions_are_restrictive(self):
        with tempfile.TemporaryDirectory() as tmp:
            if not self.chmod_is_meaningful(tmp):
                self.skipTest("filesystem does not honour chmod")
            src = self.fresh_checkout(tmp)
            home = Path(tmp) / "home"
            self.run_install(src, home, Path(tmp) / "scripts")
            self.assertEqual((home / "secrets.env").stat().st_mode & 0o777, 0o600)
            self.assertEqual(home.stat().st_mode & 0o777, 0o700)


class DoctorOnboardingTests(_PipelineHarness):
    """doctor distinguishes missing / template / no-credentials / ready."""

    def build(self, tmp, *, credentials=True, personalise=False):
        settings = self.build_home(tmp)
        if personalise:
            for name in rl.PERSONAL_PROFILE_FILES:
                path = settings.profile_dir / name
                data = json.loads(path.read_text(encoding="utf-8"))
                data.pop("profile_status", None)
                blob = json.dumps(data, ensure_ascii=False)
                for marker in rl.TEMPLATE_MARKERS:
                    blob = blob.replace(marker.replace('\\"', '"'), "personalised")
                path.write_text(blob, encoding="utf-8")
        if not credentials:
            (settings.home / "secrets.env").write_text("VERTEX_GEMINI_MODEL=gemini-3.8-flash\n", encoding="utf-8")
            (settings.home / "secrets.env").chmod(0o600)
        return settings

    def test_missing_profile_files_name_the_installer(self):
        for name in ("matcher_profile.json", "role_vocabulary.json"):
            with self.subTest(name=name), tempfile.TemporaryDirectory() as tmp:
                settings = self.build(tmp)
                (settings.profile_dir / name).unlink()
                with self.assertRaises(rl.ConfigurationError) as caught:
                    rl.doctor(settings, require_key=False)
                self.assertIn("install.sh", str(caught.exception))

    def test_unedited_templates_are_reported_not_fatal(self):
        with tempfile.TemporaryDirectory() as tmp:
            info = rl.doctor(self.build(tmp), require_key=True)
            self.assertIn("role_vocabulary.json", info["unedited_example_profiles"])
            self.assertFalse(info["ready"])
            self.assertTrue(any("Personalise" in step for step in info["next_steps"]))

    def test_missing_credentials_are_reported_with_the_template_state(self):
        with tempfile.TemporaryDirectory() as tmp:
            settings = self.build(tmp, credentials=False)
            with self.assertRaises(rl.ConfigurationError) as caught:
                rl.doctor(settings, require_key=True)
            self.assertIn("VERTEX_GEMINI_API_KEY", str(caught.exception))
            self.assertIn("still unedited", str(caught.exception))
            self.assertIn("VERTEX_GEMINI_API_KEY", rl.doctor(settings, require_key=False)["missing_credentials"])

    def test_a_personalised_installation_reports_ready(self):
        with tempfile.TemporaryDirectory() as tmp:
            info = rl.doctor(self.build(tmp, personalise=True), require_key=True)
            self.assertEqual((info["unedited_example_profiles"], info["missing_credentials"], info["ready"],
                              info["next_steps"]), ([], [], True, []))

    def test_doctor_reports_language_level_sources_and_ranking(self):
        with tempfile.TemporaryDirectory() as tmp:
            settings = self.build_home(tmp, search_terms=["AI engineer"], location_terms=["Stockholm"], career_sites=[
                {"platform": "greenhouse", "name": "acme", "board": "acme"}])
            profile = settings.profile_dir / "matcher_profile.json"
            data = json.loads(profile.read_text(encoding="utf-8"))
            data["constraints"].pop("swedish")
            profile.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
            info = rl.doctor(settings, require_key=True)
            self.assertEqual(info["candidate_swedish_level"], "unknown")
            self.assertTrue(any("constraints.swedish" in step for step in info["next_steps"]))
            self.assertEqual(info["discovery"]["jobsearch_queries"], 2)
            self.assertEqual(info["discovery"]["career_sites"], {"acme": "greenhouse"})
            self.assertGreater(info["ranking"]["vocabulary_terms"], 0)
            self.assertEqual(info["ranking"]["embedding_model"], "gemini-embedding-001")


class _BackfillHarness(_PipelineHarness):
    """A home whose database already carries a frozen historical backlog."""

    CUTOFF = "2026-08-29T19:08:23.284604+00:00"
    TODAY = dt.date(2026, 9, 1)
    TEXT = "Build AI products end to end with Python, TypeScript and PostgreSQL."

    def setUp(self):
        super().setUp()
        # run_backfill reads the market date itself. Pin it to TODAY, the date these
        # fixtures were written against, so their deadlines never expire on the calendar.
        real = rl.market_today
        rl.market_today = lambda now=None: self.TODAY if now is None else real(now)
        self.addCleanup(setattr, rl, "market_today", real)

    def seed_history(self, settings, rows):
        """rows: (source_id, title, deadline, discovery_score, historical)."""
        db = rl.Database(settings.db_path)
        try:
            for source_id, title, deadline, score, historical in rows:
                job = rl.normalize_job(self.raw_job(source_id, title, "Acme AB", "Stockholm", self.TEXT))
                job.application_deadline = deadline
                job.discovery_score = score
                db.upsert_job(job)
                stamp = "2026-08-20T09:00:00+00:00" if historical else "2026-08-31T09:00:00+00:00"
                db.conn.execute("UPDATE jobs SET content_changed_at=?, first_seen_at=? WHERE source_job_id=?",
                                (stamp, stamp, str(source_id)))
            db.conn.commit()
            mark_selected(db, rl.RankingContext.load(settings).ranking_key)
            db.set_meta("live_since", self.CUTOFF)
        finally:
            db.close()

    def version_of(self, settings):
        return rl.load_profile_bundle(settings)[2]

    def select(self, settings, limit=300):
        db = rl.Database(settings.db_path)
        try:
            scoped = dataclasses.replace(settings, max_candidates_per_run=limit)
            kept, excluded = rl.select_historical_candidates(db, scoped, self.version_of(settings), today=self.TODAY)
            return [r["source_job_id"] for r in kept], excluded
        finally:
            db.close()

    def counts(self, settings):
        db = rl.Database(settings.db_path)
        try:
            return db.historical_counts(self.version_of(settings), today=self.TODAY)
        finally:
            db.close()

    def scalar(self, settings, sql):
        db = rl.Database(settings.db_path)
        try:
            return int(db.conn.execute(sql).fetchone()[0])
        finally:
            db.close()

    @contextlib.contextmanager
    def provider(self, scores, omit=(), action=None):
        test = self
        calls: list[tuple[str, tuple[str, ...]]] = []

        class Stub:
            def __init__(self, settings_, secrets, profile, rules, provider):
                self.provider = provider
                self.model = "gemini-3.8-flash" if provider == rl.PRIMARY_PROVIDER else "gpt-5-mini"

            def evaluate(self, jobs):
                ids = [str(r["source_job_id"]) for r in jobs]
                index = len(calls)
                calls.append((self.provider, tuple(ids)))
                verdict = action(index, ids, self.provider) if action else None
                if verdict == "raise":
                    raise rl.TemporaryProviderError(self.provider, "503 upstream unavailable")
                if verdict == "output":
                    return rl.ProviderBatchResult(self.provider, self.model, (), frozenset(ids),
                                                  rl.empty_usage(), "invalid JSON: boom", "output")
                keep = [i for i in ids if i not in set(omit)]
                missing = frozenset(set(ids) - set(keep))
                return rl.ProviderBatchResult(
                    self.provider, self.model, tuple(test.evaluation(i, scores.get(i, 30)) for i in keep), missing,
                    rl.empty_usage(), ("missing/invalid IDs: " + ", ".join(sorted(missing))) if missing else None,
                    "completeness" if missing else None)

        original = rl.ProviderMatcher
        rl.ProviderMatcher = Stub
        try:
            yield calls
        finally:
            rl.ProviderMatcher = original

    def backfill(self, settings, scores, omit=(), action=None, **kwargs):
        out = io.StringIO()
        with self.provider(scores, omit=omit, action=action) as calls, contextlib.redirect_stdout(out):
            rl.run_backfill(settings, dry_run=False, **kwargs)
        return out.getvalue(), calls

    def report(self, settings, **kwargs):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            rl.run_backfill_report(settings, **kwargs)
        return out.getvalue()

    def store_matches(self, settings, count, score=95):
        self.seed_history(settings, [(f"m{i:02d}", f"Match {i}", "2026-09-10T23:59:59", count - i, True)
                                     for i in range(count)])
        self.backfill(settings, {f"m{i:02d}": score for i in range(count)})

    def runs_rows(self, settings):
        db = rl.Database(settings.db_path)
        try:
            return db.conn.execute("SELECT status, stats_json, error FROM runs ORDER BY id").fetchall()
        finally:
            db.close()


class HistoricalRecoveryTests(_BackfillHarness):
    def test_selection_is_open_historical_unevaluated_and_ordered_by_urgency(self):
        with tempfile.TemporaryDirectory() as tmp:
            s = self.build_home(tmp)
            self.seed_history(s, [
                ("later", "Later", "2026-09-20T23:59:59", 99, True),
                ("today-a", "Today low", "2026-09-01T23:59:59", 5, True),
                ("today-b", "Today high", "2026-09-01T23:59:59", 40, True),
                ("tomorrow", "Tomorrow", "2026-09-02T23:59:59", 99, True),
                ("undated", "No deadline", None, 99, True),
                ("gone", "Closed yesterday", "2026-08-31T23:59:59", 99, True),
                ("live", "Live", "2026-09-10T23:59:59", 99, False),
            ])
            ids, excluded = self.select(s)
            self.assertEqual(ids, ["today-b", "today-a", "tomorrow", "later", "undated"])
            self.assertEqual(excluded, {})
            self.assertEqual(len(self.select(s, limit=2)[0]), 2, "the candidate ceiling bounds the snapshot")

    def test_a_bootstrap_database_has_nothing_historical(self):
        with tempfile.TemporaryDirectory() as tmp:
            s = self.build_home(tmp)
            self.seed_history(s, [("h", "H", "2026-09-10T23:59:59", 5, True)])
            db = rl.Database(s.db_path)
            try:
                db.conn.execute("DELETE FROM meta WHERE key='live_since'")
                db.conn.commit()
                self.assertEqual(db.historical_pending(self.version_of(s), 300, today=self.TODAY), [])
            finally:
                db.close()

    def test_market_date_drives_expiry_not_the_utc_date(self):
        if rl._MARKET_TZ is None:
            self.skipTest("no tz database on this platform")
        # 22:30 UTC on 1 September is already 2 September in Stockholm.
        moment = dt.datetime(2026, 9, 1, 22, 30, tzinfo=dt.timezone.utc)
        self.assertEqual(rl.market_today(moment), dt.date(2026, 9, 2))
        self.assertTrue(rl.application_expired("2026-09-01T23:59:59", rl.market_today(moment)))

    def test_backfill_evaluates_and_stores_but_delivers_nothing(self):
        with tempfile.TemporaryDirectory() as tmp:
            s = self.build_home(tmp)
            self.seed_history(s, [(f"h{i:02d}", f"Job {i}", "2026-09-10T23:59:59", i, True) for i in range(12)])
            out, calls = self.backfill(s, {f"h{i:02d}": 95 for i in range(12)})
            self.assertEqual(self.scalar(s, "SELECT COUNT(*) FROM evaluations"), 12)
            self.assertEqual(self.scalar(s, "SELECT COUNT(*) FROM notifications"), 0)
            self.assertEqual([len(ids) for _, ids in calls], [10, 2])
            self.assertIn("nothing was delivered", out)
            [row] = self.runs_rows(s)
            stats = json.loads(row["stats_json"])
            self.assertEqual((row["status"], stats["mode"], stats["evaluated"]), ("success", "backfill", 12))

    def test_a_gap_gets_one_cleanup_pass_and_a_transport_failure_one_fallback(self):
        with tempfile.TemporaryDirectory() as tmp:
            s = self.build_home(tmp)
            self.seed_history(s, [(f"h{i:02d}", f"Job {i}", "2026-09-10T23:59:59", 50 - i, True) for i in range(20)])
            out, calls = self.backfill(s, {f"h{i:02d}": 40 for i in range(20)}, omit={"h00"})
            self.assertEqual([len(ids) for _, ids in calls], [10, 10, 1])
            self.assertIn("1 unresolved", out)
        with tempfile.TemporaryDirectory() as tmp:
            s = self.build_home(tmp)
            self.seed_history(s, [(f"h{i:02d}", f"Job {i}", "2026-09-10T23:59:59", i, True) for i in range(5)])
            _, calls = self.backfill(s, {}, action=lambda i, ids, p: "raise" if p == rl.PRIMARY_PROVIDER else None)
            self.assertEqual([p for p, _ in calls], [rl.PRIMARY_PROVIDER, rl.FALLBACK_PROVIDER])
            self.assertTrue(json.loads(self.runs_rows(s)[0]["stats_json"])["fallback_reasons"])

    def test_an_unparseable_envelope_is_recorded_as_partial_and_preserves_the_rest(self):
        with tempfile.TemporaryDirectory() as tmp:
            s = self.build_home(tmp)
            self.seed_history(s, [(f"h{i:02d}", f"Job {i}", "2026-09-10T23:59:59", 50 - i, True) for i in range(25)])
            out, _ = self.backfill(s, {}, action=lambda i, ids, p: "output" if i == 1 else None)
            self.assertEqual(self.scalar(s, "SELECT COUNT(*) FROM evaluations"), 10)
            self.assertEqual(self.counts(s)["historical_still_open_pending"], 15)
            self.assertEqual(self.runs_rows(s)[0]["status"], "partial")

    def test_a_dry_run_and_an_empty_backlog_record_nothing(self):
        with tempfile.TemporaryDirectory() as tmp:
            s = self.build_home(tmp)
            self.seed_history(s, [("h1", "One", "2026-09-02T23:59:59", 10, True),
                                  ("gone", "Closed", "2026-08-01T23:59:59", 5, True)])
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                rl.run_backfill(s, dry_run=True)
            self.assertIn("DRY RUN", out.getvalue())
            self.assertEqual((self.scalar(s, "SELECT COUNT(*) FROM evaluations"), len(self.runs_rows(s))), (0, 0))

    def test_pages_deliver_every_match_exactly_once_and_never_leak_into_the_live_alert(self):
        with tempfile.TemporaryDirectory() as tmp:
            s = self.build_home(tmp)
            self.store_matches(s, 12)
            db = rl.Database(s.db_path)
            try:
                self.assertEqual(db.unnotified(50), [], "live delivery must not see them")
            finally:
                db.close()
            seen = []
            for _ in range(6):
                text = self.report(s, limit=5)
                seen += re.findall(r"Match \d+", text)
                if "all worthwhile open matches delivered" in text:
                    break
            self.assertEqual(len(seen), len(set(seen)), "no match delivered twice")
            self.assertEqual(len(seen), 12, "every match delivered")

    def test_a_page_never_marks_more_delivered_than_it_emitted(self):
        with tempfile.TemporaryDirectory() as tmp:
            s = self.build_home(tmp)
            self.store_matches(s, 40)
            emitted = self.report(s, limit=40).count("Opportunity")
            self.assertLess(emitted, 40, "the size guard must truncate the page")
            self.assertEqual(self.scalar(s, "SELECT COUNT(*) FROM notifications"), emitted)

    def test_status_breaks_the_backlog_down_by_urgency(self):
        with tempfile.TemporaryDirectory() as tmp:
            s = self.build_home(tmp)
            self.seed_history(s, [
                ("today", "Today", "2026-09-01T23:59:59", 1, True),
                ("tomorrow", "Tomorrow", "2026-09-02T23:59:59", 1, True),
                ("in3", "Three days", "2026-09-04T23:59:59", 1, True),
                ("in7", "Seven days", "2026-09-08T23:59:59", 1, True),
                ("far", "Far", "2026-10-30T23:59:59", 1, True),
                ("expired", "Expired", "2026-08-01T23:59:59", 1, True),
                ("live", "Live", "2026-09-10T23:59:59", 1, False)])
            c = self.counts(s)
            self.assertEqual((c["historical_unevaluated"], c["historical_expired_unevaluated"],
                              c["historical_still_open_pending"]), (6, 1, 5))
            self.assertEqual((c["closing_today"], c["closing_tomorrow"], c["closing_within_3_days"],
                              c["closing_within_7_days"]), (1, 1, 3, 4))


class FallbackReasonTests(unittest.TestCase):
    """A provider failure must record which failure it was."""

    def refusal(self, code, body=b"", headers=None):
        error = urllib.error.HTTPError("http://x", code, "refused", headers or {}, io.BytesIO(body))

        def opener(request, timeout=None):
            raise error

        with unittest.mock.patch.object(rl.urllib.request, "urlopen", opener):
            with self.assertRaises(rl.RemoteAPIError) as caught:
                rl.provider_json_request(provider="vertex_gemini", url="http://x", headers={}, payload={}, timeout=5)
        return caught.exception

    def test_http_status_and_transport_failures_become_bounded_reasons(self):
        for code in (429, 503, 500, 408):
            self.assertEqual(rl.TemporaryProviderError("vertex_gemini", "boom", status=code).reason, f"http_{code}")
        self.assertEqual(rl.TemporaryProviderError("p", "p transport failure: TimeoutError").reason, "transport_TimeoutError")
        noisy = rl.TemporaryProviderError("p", "503 upstream unavailable, " + "x" * 500)
        self.assertEqual(noisy.reason, "transport_unknown")

    def test_a_rate_limit_keeps_its_status_and_says_how_long_to_wait(self):
        error = self.refusal(429, headers={"Retry-After": "30"})
        self.assertIsInstance(error, rl.TemporaryProviderError)
        self.assertEqual(error.reason, "http_429")
        self.assertIn("Retry-After: 30", str(error))

    def test_refusals_are_access_errors_and_an_ordinary_bad_request_is_not(self):
        for code in (401, 403, 404):
            self.assertEqual(self.refusal(code).reason, f"access_http_{code}")
        self.assertIsInstance(self.refusal(400, b'{"error": {"message": "API key not valid."}}'), rl.ProviderAccessError)
        ordinary = self.refusal(400, b'{"error": {"message": "Invalid JSON payload received."}}')
        self.assertNotIsInstance(ordinary, (rl.ProviderAccessError, rl.TemporaryProviderError))

    def test_reasons_are_counted_into_run_stats(self):
        class Down:
            def __init__(self, provider, status):
                self.provider, self.model, self.status = provider, "m", status

            def evaluate(self, jobs):
                raise rl.TemporaryProviderError(self.provider, "down", status=self.status)

        stats = rl.RunStats()
        result, used = rl.evaluate_with_fallback(Down(rl.PRIMARY_PROVIDER, 503), Down(rl.FALLBACK_PROVIDER, 429), [],
                                                 stats=stats)
        self.assertTrue(used)
        self.assertEqual(result.error_kind, "transport")
        self.assertEqual(stats.fallback_reasons, {"http_503": 1, "both_failed:http_503": 1})


class FirstReadTests(_PipelineHarness):
    """The cheap first read settles clear rejections and can never cost a match."""

    def build_home(self, tmp, **overrides):
        overrides.setdefault("triage_model", "gemini-2.5-flash-lite")
        return super().build_home(tmp, **overrides)

    def install_first_read(self, script):
        reads = []

        class Stub:
            def __init__(self, settings, secrets, profile):
                self.settings = settings
                self.model = settings.triage_model

            def read(self, rows):
                ids = [str(row["source_job_id"]) for row in rows]
                reads.append(tuple(ids))
                verdicts = {sid: rl.TriageVerdict(sid, passed, fit, "scripted") for sid, (passed, fit) in script(ids).items()}
                return rl.TriageResult(verdicts, {"prompt_tokens": 1000, "completion_tokens": 100,
                                                  "reasoning_tokens": 50, "total_tokens": 1150})

        self.addCleanup(setattr, rl, "TriageClient", rl.TriageClient)
        rl.TriageClient = Stub
        return reads

    def test_a_confident_rejection_is_settled_and_everything_else_is_judged(self):
        with tempfile.TemporaryDirectory() as tmp:
            settings = self.build_home(tmp)
            self.seed(settings, self.distinct(4))
            self.install_first_read(lambda ids: {
                "job0001": (False, 20), "job0002": (True, 85), "job0003": (False, rl.TRIAGE_SETTLE_BELOW_FIT)})
            _, calls = self.drive(settings)
            self.assertEqual({sid for _, ids in calls for sid in ids}, {"job0002", "job0003", "job0004"},
                             "a pass, a rejection near a match and a missing verdict all reach the judge")
            db = rl.Database(settings.db_path)
            try:
                settled = db.conn.execute(
                    "SELECT e.model, e.decision, e.career_fit FROM evaluations e JOIN jobs j ON j.id = e.job_id "
                    "WHERE j.source_job_id = 'job0001'").fetchone()
            finally:
                db.close()
            self.assertEqual(tuple(settled), ("vertex_gemini:gemini-2.5-flash-lite", "store_no_notify", 20))
            stats = self.last_run(settings)
            self.assertEqual((stats["triage_calls"], stats["triage_settled"], stats["triage_escalated"], stats["evaluated"]),
                             (1, 1, 3, 4))

    def test_a_refused_first_read_hands_the_run_to_the_judge_and_says_so(self):
        with tempfile.TemporaryDirectory() as tmp:
            settings = self.build_home(tmp, triage_batch_size=1)
            self.seed(settings, self.distinct(3))

            def refuse(ids):
                raise rl.ProviderAccessError(rl.PRIMARY_PROVIDER, "HTTP 404 model not found", status=404)

            reads = self.install_first_read(refuse)
            out, calls = self.drive(settings)
            self.assertEqual(len(reads), 1, "a refused model is not asked again in the same run")
            self.assertEqual({sid for _, ids in calls for sid in ids}, {"job0001", "job0002", "job0003"})
            self.assertIn("first-read model failed (access_http_404)", out)

    def test_a_passing_outage_sends_only_that_batch_to_the_judge(self):
        with tempfile.TemporaryDirectory() as tmp:
            settings = self.build_home(tmp, triage_batch_size=1)
            self.seed(settings, self.distinct(3))
            attempts = []

            def flaky(ids):
                attempts.append(ids)
                if len(attempts) == 1:
                    raise rl.TemporaryProviderError(rl.PRIMARY_PROVIDER, "HTTP 503", status=503)
                return {sid: (False, 10) for sid in ids}

            self.install_first_read(flaky)
            out, calls = self.drive(settings)
            self.assertEqual({sid for _, ids in calls for sid in ids}, set(attempts[0]))
            self.assertNotIn("first-read model failed", out)

    def test_switched_off_the_judge_reads_every_ad(self):
        with tempfile.TemporaryDirectory() as tmp:
            settings = self.build_home(tmp, triage_model="")
            self.seed(settings, self.distinct(2))
            reads = self.install_first_read(lambda ids: {sid: (False, 0) for sid in ids})
            _, calls = self.drive(settings)
            self.assertEqual(reads, [])
            self.assertEqual({sid for _, ids in calls for sid in ids}, {"job0001", "job0002"})

    def test_verdicts_are_parsed_strictly(self):
        rows = [{"source_job_id": sid} for sid in ("a", "b", "c", "d")]
        content = json.dumps({"ads": [
            {"id": "a", "reason": "nursing role", "fit": 5, "verdict": "reject"},
            {"id": "b", "reason": "applied AI product work", "fit": 88, "verdict": "pass"},
            {"id": "c", "reason": "first answer", "fit": 10, "verdict": "reject"},
            {"id": "c", "reason": "second answer", "fit": 12, "verdict": "reject"},
            {"id": "d", "reason": "out of range", "fit": 150, "verdict": "reject"},
            {"id": "zz", "reason": "not asked about", "fit": 1, "verdict": "reject"},
        ]})
        verdicts, error = rl.parse_triage_verdicts(content, rows)
        self.assertEqual(set(verdicts), {"a", "b"})
        self.assertIn("2 of 4", error)
        self.assertEqual(rl.parse_triage_verdicts("{", rows)[0], {})
        below = rl.TRIAGE_SETTLE_BELOW_FIT - 1
        self.assertTrue(rl.triage_settles(rl.TriageVerdict("x", False, below, "")))
        self.assertFalse(rl.triage_settles(rl.TriageVerdict("x", False, rl.TRIAGE_SETTLE_BELOW_FIT, "")))
        self.assertFalse(rl.triage_settles(rl.TriageVerdict("x", True, 0, "")), "a pass is never settled")

    def test_the_request_is_compact_and_built_from_the_profile(self):
        with tempfile.TemporaryDirectory() as tmp:
            settings = self.build_home(tmp)
            profile, _, _ = rl.load_profile_bundle(settings)
            client = rl.TriageClient(settings, {"VERTEX_GEMINI_API_KEY": "k"}, profile)
            ad = {"source_job_id": "123", "title": "Data Engineer", "company": "Acme AB", "municipality": "Stockholm",
                  "region": "", "description": "We build pipelines. " + "Lorem ipsum. " * 2000 + "Requirements: SQL."}
            payload = client.request_payload([ad])
            text = payload["contents"][0]["parts"][0]["text"]
            self.assertEqual(payload["generationConfig"]["thinkingConfig"], {"thinkingBudget": settings.triage_thinking_budget})
            self.assertIn("123 | Data Engineer | Acme AB | Stockholm", text)
            self.assertIn("Requirements: SQL.", text, "requirements close an ad, so the tail is kept")
            self.assertLess(len(text), 9_000)
            self.assertNotIn("knowledge_catalogue", text)
            for part in ("Target roles:", "Not a fit:", "No evidence of:", "Swedish:", "Professional experience:"):
                self.assertIn(part, client.card)
            self.assertEqual(rl.doctor(settings)["first_read"]["model"], "gemini-2.5-flash-lite")

    def test_the_thinking_budget_is_checked(self):
        with tempfile.TemporaryDirectory() as tmp:
            for bad in (100, -1, True, "1024", 30_000):
                with self.assertRaises(rl.ConfigurationError):
                    self.build_home(tmp, triage_thinking_budget=bad)
            self.assertEqual(self.build_home(tmp, triage_thinking_budget=0).triage_thinking_budget, 0)


class SecondJudgementTests(_PipelineHarness):
    """An ad scored near the card line is judged again and decided on the mean."""

    def stored(self, settings):
        db = rl.Database(settings.db_path)
        try:
            return db.conn.execute("SELECT opportunity_score, decision, raw_json FROM evaluations").fetchall()
        finally:
            db.close()

    def evaluation_with(self, opportunity, blockers=()):
        return rl.validate_evaluation(evaluation_item("1", opportunity=opportunity, career_fit=90, blockers=list(blockers)))

    def test_only_ads_near_the_line_are_judged_again(self):
        with tempfile.TemporaryDirectory() as tmp:
            settings = self.build_home(tmp)
            self.seed(settings, self.distinct(3))
            out, calls = self.drive(settings, scores={"job0001": 70, "job0002": 92, "job0003": 30})
            self.assertEqual(len(calls), 2)
            self.assertEqual(calls[1][1], ("job0001",))
            self.assertIn("AI Engineer 1", out)

    def test_the_decision_uses_the_mean_of_both_scores(self):
        with tempfile.TemporaryDirectory() as tmp:
            settings = self.build_home(tmp)
            self.seed(settings, self.distinct(1))
            self.drive(settings, scores=lambda index, sid: 72 if index == 0 else 50)
            [(opportunity, decision, raw)] = self.stored(settings)
            self.assertEqual((opportunity, decision), (61, "notify_stretch"))
            self.assertEqual(json.loads(raw)["second_judgement"]["second"]["opportunity_score"], 50)

    def test_a_failed_second_call_keeps_the_first_judgement(self):
        with tempfile.TemporaryDirectory() as tmp:
            settings = self.build_home(tmp)
            self.seed(settings, self.distinct(1))
            _, calls = self.drive(settings, script=lambda index, ids, provider: "raise" if index else "ok",
                                  scores={"job0001": 72})
            self.assertEqual(len(calls), 3, "the first read, then both providers failing the second")
            [(opportunity, decision, _)] = self.stored(settings)
            self.assertEqual((opportunity, decision), (72, "notify_good"))

    def test_a_hard_blocker_counts_only_when_both_judgements_found_one(self):
        blocked = [{"type": "hard", "reason": "Core job is embedded C firmware."}]
        self.assertEqual(rl.combine_judgements(self.evaluation_with(74, blocked), self.evaluation_with(72)).decision,
                         "notify_good")
        self.assertEqual(rl.combine_judgements(self.evaluation_with(74, blocked), self.evaluation_with(72, blocked)).decision,
                         "store_no_notify")


class CommandLineTests(_PipelineHarness):
    def setUp(self):
        super().setUp()
        # main() configures root logging for a real run; tests must not inherit it.
        patcher = unittest.mock.patch.object(rl.logging, "basicConfig")
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_a_bare_invocation_runs_the_pipeline(self):
        """Script-only schedulers such as Hermes Agent run the script with no arguments."""
        with tempfile.TemporaryDirectory() as tmp:
            self.build_home(tmp)
            seen = {}

            def fake(settings, *, fetch_only, evaluate_only):
                seen.update(fetch_only=fetch_only, evaluate_only=evaluate_only)
                return 0

            with unittest.mock.patch.object(rl, "run_pipeline", fake):
                self.assertEqual(rl.main(["--home", tmp]), 0)
            self.assertEqual(seen, {"fetch_only": False, "evaluate_only": False})

    def test_a_configuration_error_exits_2(self):
        with tempfile.TemporaryDirectory() as tmp, self.assertLogs(rl.LOG, level="ERROR"):
            self.assertEqual(rl.main(["--home", tmp]), 2)


class SecretsTests(unittest.TestCase):
    def write(self, tmp, text, mode=0o600):
        path = Path(tmp) / "secrets.env"
        path.write_text(text, encoding="utf-8")
        path.chmod(mode)
        return path

    def test_values_may_be_quoted_or_exported_and_the_environment_wins(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = self.write(tmp, "# comment\nexport VERTEX_GEMINI_API_KEY='from-file'\nAZURE_OPENAI_DEPLOYMENT=\"d\"\n")
            clean = {key: value for key, value in os.environ.items() if key not in rl.SECRET_KEYS}
            with unittest.mock.patch.dict(os.environ, clean, clear=True):
                self.assertEqual(rl.load_secrets(path), {"VERTEX_GEMINI_API_KEY": "from-file", "AZURE_OPENAI_DEPLOYMENT": "d"})
                os.environ["VERTEX_GEMINI_API_KEY"] = "from-env"
                self.assertEqual(rl.load_secrets(path)["VERTEX_GEMINI_API_KEY"], "from-env")

    def test_a_malformed_line_is_refused_without_echoing_it(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = self.write(tmp, "this-line-holds-a-credential-value\n")
            with self.assertRaises(rl.ConfigurationError) as caught:
                rl.load_secrets(path)
            self.assertNotIn("credential-value", str(caught.exception))

    @unittest.skipUnless(os.name == "posix", "file modes are POSIX")
    def test_a_readable_secrets_file_is_refused(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = self.write(tmp, "VERTEX_GEMINI_API_KEY=x\n", mode=0o644)
            if path.stat().st_mode & 0o077 == 0:
                self.skipTest("filesystem does not honour chmod")
            with self.assertRaises(rl.ConfigurationError):
                rl.load_secrets(path)


if __name__ == "__main__":
    unittest.main()
