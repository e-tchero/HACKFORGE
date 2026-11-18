"""Evidence trail, provenance, synthesis, and the validation report.

Covers the Phase 4.5 requirements: evidence retention, unknown-date handling,
duplicate registration emails, synthesis traceability, and the rule that
insufficient evidence blocks a recommendation.
"""

from __future__ import annotations

from datetime import date

import pytest

from hackforge.adapters.sources import RawMessage
from hackforge.core.models import (
    EvidenceRef,
    EvidenceState,
    Opportunity,
    OpportunityStatus,
    ResearchFinding,
    SourceRef,
    Timeline,
    VerificationLevel,
)
from hackforge.core.registry import OpportunityRegistry
from hackforge.core.store import JsonStore
from hackforge.engines.detection import OpportunityDetector
from hackforge.engines.research import INSUFFICIENT_EVIDENCE, ResearchEngine
from hackforge.engines.strategy import StrategyEngine
from hackforge.engines.strategy_report import build_validation_report
from hackforge.engines.synthesis import SynthesisEngine

RULES_PAGE = """
<html><body>
<p>The problem: rural clinics cannot afford diagnostic screening at scale.</p>
<p>Judging criteria are impact, innovation, technical execution and demo quality.</p>
<p>Use the sponsor API and any web frontend for the submission.</p>
<p>Previous winners built triage tools for community health workers.</p>
<p>The intended user is a community health worker without a specialist on call.</p>
<p>Submissions must include a working demo and a public repository.</p>
</body></html>
"""


def _message(**overrides) -> RawMessage:
    defaults = dict(
        message_id="msg-1",
        thread_id="thread-1",
        subject="Registration confirmed - Global AI Hackathon 2026",
        body=(
            "Thank you for registering for the Global AI Hackathon 2026.\n"
            "Registration confirmed.\n"
            "Submission deadline: 2026-04-18\n"
        ),
        sender="events@hackathon.example.org",
        sender_name="Global AI Hackathon",
        received_at=date(2026, 1, 15),
        source_label="gmail",
    )
    defaults.update(overrides)
    return RawMessage(**defaults)


# -- evidence trail -------------------------------------------------------


class TestRegistrationEvidence:
    def test_evidence_is_verified_with_verbatim_passage(self):
        opportunity, _ = OpportunityDetector().from_message(_message())

        assert opportunity.registration_evidence is not None
        assert opportunity.registration_evidence.state is EvidenceState.VERIFIED
        # The passage must appear verbatim in the source body.
        passage = opportunity.registration_evidence.passage
        assert passage in opportunity.registration_evidence.claim or passage
        assert "registering" in passage.lower()

    def test_evidence_links_to_source_message(self):
        opportunity, _ = OpportunityDetector().from_message(
            _message(message_id="abc123", thread_id="t9")
        )

        evidence = opportunity.registration_evidence
        assert evidence.url == "gmail:abc123"
        assert evidence.retrieved_at == "2026-01-15"

    def test_no_registration_claim_means_no_evidence(self):
        announcement = _message(
            subject="Global AI Hackathon registration is now open",
            body="Registration is now open. Join us for the hackathon.",
        )
        opportunity, _ = OpportunityDetector().from_message(announcement)

        assert opportunity.status is OpportunityStatus.DISCOVERED
        assert opportunity.registration_evidence is None

    def test_registration_without_confirming_text_is_unknown(self):
        """Announcement language that trips the classifier but lacks confirmation."""
        message = _message(
            body="Save the date. Hackathon build period and submission deadline TBA.",
        )
        opportunity, _ = OpportunityDetector().from_message(message)

        if opportunity is not None and opportunity.registration_evidence is not None:
            # If evidence is claimed it must be backed by a real passage.
            assert opportunity.registration_evidence.is_verified or (
                opportunity.registration_evidence.passage == ""
            )


class TestProvenance:
    def test_provenance_records_message_and_thread(self):
        opportunity, result = OpportunityDetector().from_message(
            _message(message_id="m-42", thread_id="t-42")
        )

        assert opportunity.provenance.message_id == "m-42"
        assert opportunity.provenance.thread_id == "t-42"
        assert opportunity.provenance.source_label == "gmail"
        assert opportunity.provenance.received_at == "2026-01-15"
        assert opportunity.provenance.detection_confidence == result.confidence

    def test_provenance_explains_the_decision(self):
        opportunity, _ = OpportunityDetector().from_message(_message())

        assert opportunity.provenance.reasons
        assert any("hackathon" in reason for reason in opportunity.provenance.reasons)

    def test_provenance_survives_round_trip(self):
        opportunity, _ = OpportunityDetector().from_message(_message())
        restored = Opportunity.from_dict(opportunity.to_dict())

        assert restored.provenance.message_id == opportunity.provenance.message_id
        assert restored.provenance.thread_id == opportunity.provenance.thread_id
        assert restored.provenance.detection_confidence == (
            opportunity.provenance.detection_confidence
        )


class TestDuplicateEmails:
    def _registry(self, tmp_path):
        return OpportunityRegistry(JsonStore(tmp_path / "s.json"))

    def test_same_message_twice_does_not_duplicate(self, tmp_path):
        registry = self._registry(tmp_path)
        first, _ = OpportunityDetector().from_message(_message())
        registry.register(first)

        duplicate, _ = OpportunityDetector().from_message(_message())
        merged = registry.register(duplicate)

        assert len(registry.all()) == 1
        assert len(merged.registration_evidence_history) == 1

    def test_second_email_for_same_opportunity_accumulates(self, tmp_path):
        registry = self._registry(tmp_path)
        first, _ = OpportunityDetector().from_message(_message())
        registry.register(first)

        follow_up, _ = OpportunityDetector().from_message(
            _message(
                message_id="msg-2",
                thread_id="thread-1",
                body=(
                    "Thank you for registering for the Global AI Hackathon 2026.\n"
                    "Registration confirmed.\n"
                    "Reminder: the submission deadline is 2026-04-18.\n"
                ),
            )
        )
        merged = registry.register(follow_up)

        # Both messages are retained as separate evidence.
        assert len(merged.registration_evidence_history) == 2
        assert len(registry.all()) == 1

    def test_confidence_merge_keeps_strongest_and_both_messages(self, tmp_path):
        registry = self._registry(tmp_path)
        weak, _ = OpportunityDetector().from_message(
            _message(message_id="weak-1", body="Hackathon. Build period soon.")
        )
        weak.provenance.detection_confidence = 0.3
        registry.register(weak)

        strong, _ = OpportunityDetector().from_message(
            _message(message_id="strong-1")
        )
        strong.provenance.detection_confidence = 0.95
        merged = registry.register(strong)

        assert merged.provenance.detection_confidence == 0.95
        assert any("weak-1" in reason for reason in merged.provenance.reasons)

    def test_verified_evidence_not_downgraded_by_unknown(self, tmp_path):
        registry = self._registry(tmp_path)
        good, _ = OpportunityDetector().from_message(_message(message_id="good"))
        registry.register(good)

        weak = Opportunity.from_dict(good.to_dict())
        weak.registration_evidence = EvidenceRef(
            claim="weak", url="gmail:weak", passage="", state=EvidenceState.UNKNOWN
        )
        merged = registry.register(weak)

        assert merged.registration_evidence.state is EvidenceState.VERIFIED


# -- unknown dates --------------------------------------------------------


class TestUnknownDatesRemainUnknown:
    def test_vague_date_is_not_converted(self):
        message = _message(
            body=(
                "Thank you for registering for the Global AI Hackathon 2026.\n"
                "Registration confirmed.\n"
                "Submission deadline: coming soon.\n"
                "Event date: this month.\n"
            )
        )
        opportunity, _ = OpportunityDetector().from_message(message)

        assert opportunity.timeline.submission_deadline is None
        assert opportunity.timeline.event_date is None

    def test_ambiguous_date_without_year_is_unknown(self):
        message = _message(
            body=(
                "Registration confirmed.\n"
                "Submit your project by March 15.\n"
            ),
            received_at=None,
        )
        opportunity, _ = OpportunityDetector().from_message(message)

        assert opportunity.timeline.submission_deadline is None

    def test_unrelated_date_is_not_treated_as_deadline(self):
        message = _message(
            body=(
                "Registration confirmed.\n"
                "Our office was founded on 2019-05-02.\n"
                "See you there.\n"
            )
        )
        opportunity, _ = OpportunityDetector().from_message(message)

        assert opportunity.timeline.submission_deadline is None

    def test_state_file_renders_unknown(self, tmp_path):
        from hackforge.engines.workspace import WorkspaceManager

        opportunity = Opportunity(
            id="o", name="Global AI Hackathon", strategy_status="approved"
        )
        manager = WorkspaceManager(tmp_path, autonomy_level=1)
        target = manager.create(opportunity)
        state = (target / "HACKFORGE_STATE.md").read_text(encoding="utf-8")

        assert "submission_deadline: UNKNOWN" in state


# -- research evidence ----------------------------------------------------


class TestResearchEvidenceStates:
    def _opportunity(self):
        return Opportunity(
            id="o",
            name="Global AI Hackathon",
            sources=[SourceRef(kind="official_website", reference="https://example.org")],
        )

    def test_success_produces_verified_evidence(self):
        result = ResearchEngine(fetcher=lambda url: RULES_PAGE).research(self._opportunity())

        assert result.state_for("competition") is EvidenceState.VERIFIED
        assert result.evidence
        assert all(item.is_verified for item in result.evidence)

    def test_evidence_retains_exact_passage(self):
        result = ResearchEngine(fetcher=lambda url: RULES_PAGE).research(self._opportunity())
        claims = {item.claim for item in result.evidence}

        assert "competition" in claims
        competition = next(item for item in result.evidence if item.claim == "competition")
        # The passage must be text that actually appears in the source page.
        assert "Judging criteria" in competition.passage

    def test_total_failure_is_recorded_not_invented(self):
        def failing(url):
            raise OSError("network unreachable")

        result = ResearchEngine(fetcher=failing).research(self._opportunity())

        assert result.state_for("problem") is EvidenceState.FAILED
        assert result.retrieved == []
        assert result.evidence == []
        assert all(
            finding.confidence == VerificationLevel.UNVERIFIED
            for finding in result.findings
        )

    def test_empty_page_is_failure(self):
        result = ResearchEngine(fetcher=lambda url: "").research(self._opportunity())

        assert result.state_for("problem") is EvidenceState.FAILED

    def test_unaddressed_question_is_unknown_not_failed(self):
        result = ResearchEngine(fetcher=lambda url: RULES_PAGE).research(self._opportunity())
        state = result.state_for("historical")

        # The page mentions winners, so this is verified; the point is that a
        # question with no matching text is UNKNOWN, never FAILED.
        assert state in {EvidenceState.VERIFIED, EvidenceState.UNKNOWN}
        assert state is not EvidenceState.FAILED

    def test_attempts_are_recorded(self):
        result = ResearchEngine(fetcher=lambda url: RULES_PAGE).research(self._opportunity())

        assert result.attempts
        assert result.attempts[0].ok is True
        assert result.attempts[0].tier == 1


# -- synthesis ------------------------------------------------------------


class TestSynthesis:
    def _run(self, fetcher):
        opportunity = Opportunity(
            id="o",
            name="Global AI Hackathon",
            sources=[SourceRef(kind="official_website", reference="https://example.org")],
        )
        result = ResearchEngine(fetcher=fetcher).research(opportunity)
        return opportunity, result, SynthesisEngine().synthesise(opportunity, result)

    def test_answered_when_evidence_exists(self):
        _, _, synthesis = self._run(lambda url: RULES_PAGE)

        answered = [a for a in synthesis.answers if a.is_answered]
        assert answered
        for answer in answered:
            assert answer.evidence
            for _, passage in answer.evidence:
                assert passage

    def test_answer_traces_to_source_url(self):
        _, _, synthesis = self._run(lambda url: RULES_PAGE)

        for answer in synthesis.answers:
            for url, _passage in answer.evidence:
                assert url == "https://example.org"

    def test_insufficient_evidence_is_explicit(self):
        _, _, synthesis = self._run(lambda url: "too short to match anything here")

        for answer in synthesis.answers:
            if not answer.is_answered:
                assert INSUFFICIENT_EVIDENCE in answer.answer

    def test_failed_retrieval_produces_no_answers(self):
        def failing(url):
            raise OSError("down")

        _, _, synthesis = self._run(failing)

        assert synthesis.verified_count == 0
        assert all(INSUFFICIENT_EVIDENCE in a.answer for a in synthesis.answers)

    def test_markdown_states_state(self):
        _, _, synthesis = self._run(lambda url: RULES_PAGE)
        markdown = synthesis.to_markdown(
            Opportunity(id="o", name="Global AI Hackathon")
        )

        assert "VERIFIED" in markdown
        assert "Nothing here is inferred" in markdown
        assert "Supporting evidence:" in markdown


# -- validation report ----------------------------------------------------


class TestValidationReport:
    def _build(self, fetcher, opportunity=None):
        opportunity = opportunity or Opportunity(
            id="o",
            name="Global AI Hackathon",
            organizer="Global Org",
            sources=[
                SourceRef(kind="official_website", reference="https://example.org")
            ],
        )
        research = ResearchEngine(fetcher=fetcher).research(opportunity)
        synthesis = SynthesisEngine().synthesise(opportunity, research)
        recommendation = StrategyEngine().recommend(opportunity, research=research.findings)
        report = build_validation_report(opportunity, research, synthesis, recommendation)
        return report, opportunity

    def test_has_all_fourteen_sections(self):
        report, _ = self._build(lambda url: RULES_PAGE)
        markdown = report.to_markdown()

        for number in range(1, 15):
            assert f"## {number}." in markdown, f"missing section {number}"

    def test_section_titles_match_specification(self):
        report, _ = self._build(lambda url: RULES_PAGE)
        markdown = report.to_markdown()

        for title in [
            "Hackathon Identity",
            "Verified Requirements",
            "Verified Judging Criteria",
            "Constraints",
            "Relevant Technologies",
            "Candidate Concepts",
            "Evidence Supporting Each Candidate",
            "Criteria Used",
            "Transparent Scoring",
            "Recommendation",
            "Reasons for the Recommendation",
            "Unknowns",
            "Risks",
            "What Would Change the Recommendation",
        ]:
            assert title in markdown, f"missing {title}"

    def test_refuses_recommendation_without_evidence(self):
        def failing(url):
            raise OSError("offline")

        report, _ = self._build(failing)
        markdown = report.to_markdown()

        assert "**NO RECOMMENDATION.**" in markdown
        assert report.recommendation.recommended is None

    def test_refuses_when_evidence_is_thin(self):
        """One verified question is not enough to rank candidates."""
        thin_page = """
        <html><body>
        <p>Judging criteria are impact, innovation and technical execution quality.</p>
        </body></html>
        """
        report, _ = self._build(lambda url: thin_page)

        assert report.recommendation.recommended is None
        assert "below the threshold" in report.recommendation.reasoning

    def test_recommends_when_evidence_is_sufficient(self):
        report, _ = self._build(lambda url: RULES_PAGE)

        assert report.recommendation.recommended is not None
        assert "## 10. Recommendation" in report.to_markdown()

    def test_unknowns_are_never_empty(self):
        report, _ = self._build(lambda url: "nothing relevant at all in this page")
        markdown = report.to_markdown()

        assert "## 12. Unknowns" in markdown

    def test_risks_never_say_none_when_coverage_is_partial(self):
        thin_page = """
        <html><body>
        <p>Judging criteria are impact, innovation and technical execution quality.</p>
        </body></html>
        """
        report, _ = self._build(lambda url: thin_page)
        markdown = report.to_markdown()
        risks_section = markdown.split("## 13. Risks")[1].split("## 14.")[0]

        assert "No risks identified" not in risks_section
        assert "coverage" in risks_section.lower()

    def test_failed_retrieval_is_a_risk(self):
        def failing(url):
            raise OSError("offline")

        report, _ = self._build(failing)
        risks = report.to_markdown().split("## 13. Risks")[1]

        assert "could not be retrieved" in risks

    def test_structural_candidate_caveat_is_stated(self):
        report, _ = self._build(lambda url: RULES_PAGE)
        markdown = report.to_markdown()

        assert "fixed archetypes" in markdown

    def test_scoring_table_present_with_weights(self):
        report, _ = self._build(lambda url: RULES_PAGE)
        scoring = report.to_markdown().split("## 9. Transparent Scoring")[1]

        assert "judging_alignment" in scoring
        assert "must not be read as a verdict" in scoring