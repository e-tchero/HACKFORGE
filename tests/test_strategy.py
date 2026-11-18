"""Phase 3: strategy generation, scoring, and the no-evidence refusal."""

from __future__ import annotations

from datetime import date

from hackforge.core.models import (
    Opportunity,
    ResearchFinding,
    SourceRef,
    Timeline,
    VerificationLevel,
)
from hackforge.engines.strategy import SCORING_WEIGHTS, StrategyEngine


def _verified_research() -> list[ResearchFinding]:
    return [
        ResearchFinding(
            question="problem",
            answer="Clinicians lack affordable screening at scale.",
            evidence=["https://example.org/rules"],
            confidence=VerificationLevel.VERIFIED,
        ),
        ResearchFinding(
            question="competition",
            answer="Judging criteria: impact, innovation, technical execution.",
            evidence=["https://example.org/rules"],
            confidence=VerificationLevel.VERIFIED,
        ),
        ResearchFinding(
            question="product",
            answer="The user is a community health worker without a specialist.",
            evidence=["https://example.org/rules"],
            confidence=VerificationLevel.VERIFIED,
        ),
    ]


def _rich_opportunity() -> Opportunity:
    return Opportunity(
        id="op-1",
        name="Global AI Hackathon",
        timeline=Timeline(submission_deadline=date(2026, 4, 18)),
        technologies=["Python", "React"],
        judging_criteria=["Impact", "Innovation", "Technical Execution"],
        deliverables=["Working demo", "Repository"],
        sources=[SourceRef(kind="official_website", reference="https://example.org")],
        research=_verified_research(),
    )


class TestCandidateGeneration:
    def test_generates_multiple_candidates(self, sample_opportunity):
        candidates = StrategyEngine().generate_candidates(sample_opportunity)

        assert len(candidates) >= 4
        assert len({candidate.name for candidate in candidates}) == len(candidates)

    def test_every_candidate_is_fully_populated(self, sample_opportunity):
        for candidate in StrategyEngine().generate_candidates(sample_opportunity):
            assert candidate.problem
            assert candidate.proposed_solution
            assert candidate.core_functionality
            assert candidate.demo_potential
            assert candidate.differentiation
            assert candidate.estimated_build_scope

    def test_scores_are_bounded_and_weighted(self, sample_opportunity):
        candidates = StrategyEngine().generate_candidates(sample_opportunity)

        for candidate in candidates:
            assert 0 <= candidate.total_score <= 100
            assert set(candidate.scores) == set(SCORING_WEIGHTS)

    def test_unknown_deadline_is_stated(self, sample_opportunity):
        sample_opportunity.timeline = Timeline()
        candidates = StrategyEngine().generate_candidates(sample_opportunity)

        assert all("UNKNOWN" in c.estimated_build_scope for c in candidates)


class TestRecommendation:
    def test_recommends_with_reasoning(self, sample_opportunity):
        sample_opportunity.research = _verified_research()
        recommendation = StrategyEngine().recommend(sample_opportunity)

        assert recommendation.recommended is not None
        assert recommendation.reasoning
        assert "Recommended because" in recommendation.reasoning
        # Must not be a bare assertion.
        assert len(recommendation.reasoning) > 80

    def test_reasoning_names_driving_criteria(self, sample_opportunity):
        sample_opportunity.research = _verified_research()
        recommendation = StrategyEngine().recommend(sample_opportunity)

        assert any(
            criterion in recommendation.reasoning
            for criterion in SCORING_WEIGHTS
        )

    def test_refuses_without_verified_research(self, sample_opportunity):
        sample_opportunity.research = [
            ResearchFinding(
                question="problem",
                answer="guess",
                evidence=[],
                confidence=VerificationLevel.UNVERIFIED,
            )
        ]
        recommendation = StrategyEngine().recommend(sample_opportunity)

        assert recommendation.recommended is None
        assert "fabrication" in recommendation.reasoning
        assert recommendation.caveats

    def test_flags_missing_judging_criteria(self, sample_opportunity):
        sample_opportunity.judging_criteria = []
        sample_opportunity.research = _verified_research()
        recommendation = StrategyEngine().recommend(sample_opportunity)

        assert any("Judging criteria UNKNOWN" in c for c in recommendation.caveats)

    def test_flags_missing_deadline(self, sample_opportunity):
        sample_opportunity.timeline = Timeline()
        sample_opportunity.research = _verified_research()
        recommendation = StrategyEngine().recommend(sample_opportunity)

        assert any("deadline UNKNOWN" in c for c in recommendation.caveats)

    def test_ranked_by_score(self, sample_opportunity):
        sample_opportunity.research = _verified_research()
        recommendation = StrategyEngine().recommend(sample_opportunity)

        scores = [candidate.total_score for candidate in recommendation.candidates]
        assert scores == sorted(scores, reverse=True)

    def test_markdown_output(self, sample_opportunity):
        sample_opportunity.research = _verified_research()
        recommendation = StrategyEngine().recommend(sample_opportunity)
        markdown = recommendation.to_markdown(sample_opportunity)

        assert "# Strategy Recommendation" in markdown
        assert "## Why This Direction" in markdown
        assert "## Scoring Breakdown" in markdown

    def test_markdown_states_no_recommendation_when_unverified(self, sample_opportunity):
        recommendation = StrategyEngine().recommend(sample_opportunity)
        markdown = recommendation.to_markdown(sample_opportunity)

        assert "NO RECOMMENDATION" in markdown