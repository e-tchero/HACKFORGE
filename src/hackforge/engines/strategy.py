"""Strategy engine (Phase 3).

Generates candidate project directions, scores them against documented
criteria, and produces a recommendation with the reasoning attached.

The recommendation is never a bare "this is the best idea". It names the
criteria that drove it and cites the supporting evidence, per specification
section 10.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date
from typing import Any

from ..core.models import (
    CandidateIdea,
    Opportunity,
    ResearchFinding,
    VerificationLevel,
)

# Weights sum to 100. Time feasibility and judging alignment dominate because
# the documented failure mode is building something impressive that cannot be
# finished or does not answer what the organisers reward.
SCORING_WEIGHTS: dict[str, int] = {
    "judging_alignment": 25,
    "time_feasibility": 20,
    "requirement_alignment": 15,
    "demo_clarity": 12,
    "differentiation": 10,
    "technical_feasibility": 8,
    "user_value": 6,
    "sponsor_integration": 4,
}

IDEA_ARCHETYPES: list[dict[str, Any]] = [
    {
        "key": "workflow_automation",
        "name_template": "End-to-end workflow automation for the core task",
        "problem": "The documented workflow is manual and repetitive.",
        "solution": "Automate the highest-frequency step end to end with a single-click flow.",
        "functionality": ["Input capture", "Processing pipeline", "Result export"],
        "complexity": "moderate",
        "demo": "A single screen showing a manual task collapsing into one action.",
        "differentiation": "Full-loop automation rather than a single-feature tool.",
        "scope": "One workflow, one user, one measurable before/after.",
    },
    {
        "key": "insight_dashboard",
        "name_template": "Live insight dashboard over the target data",
        "problem": "Stakeholders lack a consolidated view of the situation.",
        "solution": "Ingest available data and present the decision-relevant view immediately.",
        "functionality": ["Data ingestion", "Aggregation", "Visualisation", "Export"],
        "complexity": "moderate",
        "demo": "Live data visibly changing the output while presenting.",
        "differentiation": "Live rather than static reporting.",
        "scope": "One dataset, one metric set, one dashboard.",
    },
    {
        "key": "accessibility_layer",
        "name_template": "Accessibility layer for the under-served path",
        "problem": "A segment of users is excluded by the current tooling.",
        "solution": "Provide an accessible route to the core capability.",
        "functionality": ["Accessible input", "Adaptive rendering", "Assistive output"],
        "complexity": "low-moderate",
        "demo": "Side-by-side of the excluded and included experience.",
        "differentiation": "Reaches users competitors ignore.",
        "scope": "One accessibility dimension, end to end.",
    },
    {
        "key": "integration_bridge",
        "name_template": "Integration bridge between the sponsor stack and user tools",
        "problem": "Users cannot connect the required platform to their existing work.",
        "solution": "Bridge the required API into a usable workflow with auth handled safely.",
        "functionality": ["OAuth connection", "Data sync", "Error handling"],
        "complexity": "moderate-high",
        "demo": "A real authenticated connection made in under a minute.",
        "differentiation": "Directly satisfies sponsor-specific prizes.",
        "scope": "One API, one sync direction, real credentials.",
    },
    {
        "key": "on_device_tool",
        "name_template": "On-device tool with no backend dependency",
        "problem": "Existing solutions require connectivity or an account the user lacks.",
        "solution": "Run entirely client-side so it works immediately and privately.",
        "functionality": ["Local processing", "Local storage", "Share/export"],
        "complexity": "low",
        "demo": "Opening and using the product with no network at all.",
        "differentiation": "Zero-friction, privacy-preserving, deployable as a static page.",
        "scope": "One client-side capability, polished UX.",
    },
]

COMPLEXITY_BUDGET = {"low": 10, "low-moderate": 8, "moderate": 6, "moderate-high": 4, "high": 2}


@dataclass
class StrategyRecommendation:
    recommended: CandidateIdea | None
    candidates: list[CandidateIdea]
    reasoning: str
    criteria_used: dict[str, int] = field(default_factory=dict)
    caveats: list[str] = field(default_factory=list)

    def to_markdown(self, opportunity: Opportunity) -> str:
        lines = [
            f"# Strategy Recommendation - {opportunity.name}",
            "",
            "## Recommended Direction",
            "",
        ]
        if self.recommended is None:
            lines.append(
                "**NO RECOMMENDATION** - insufficient verified research to compare "
                "candidates."
            )
            lines.extend(["", "## Why No Recommendation", "", self.reasoning, ""])
            if self.caveats:
                lines.extend(["## Caveats", ""])
                lines.extend(f"- {caveat}" for caveat in self.caveats)
            return "\n".join(lines) + "\n"

        lines.extend(
            [
                f"**{self.recommended.name}**  (score {self.recommended.total_score}/100)",
                "",
                f"- Problem: {self.recommended.problem}",
                f"- Target user: {self.recommended.target_user}",
                f"- Solution: {self.recommended.proposed_solution}",
                f"- Complexity: {self.recommended.implementation_complexity}",
                f"- Estimated scope: {self.recommended.estimated_build_scope}",
                "",
                "## Why This Direction",
                "",
                self.reasoning,
                "",
                "## Scoring Breakdown",
                "",
                "| Criterion | Weight | Score |",
                "| --- | --- | --- |",
            ]
        )
        for criterion, weight in self.criteria_used.items():
            score = self.recommended.scores.get(criterion, 0)
            lines.append(f"| {criterion} | {weight} | {score} |")

        lines.extend(["", "## All Candidates", ""])
        for candidate in sorted(
            self.candidates, key=lambda item: item.total_score, reverse=True
        ):
            lines.append(
                f"- **{candidate.name}** - {candidate.total_score}/100 - "
                f"{candidate.differentiation}"
            )

        if self.caveats:
            lines.extend(["", "## Caveats", ""])
            lines.extend(f"- {caveat}" for caveat in self.caveats)

        return "\n".join(lines) + "\n"


class StrategyEngine:
    """Deterministic candidate generation and scoring.

    This is intentionally rule-based rather than model-driven. With no research
    evidence to reason from, an LLM would produce confident-sounding fiction,
    which section 24 prohibits. When a model adapter is wired in later it must
    supply the same evidence fields.
    """

    def __init__(
        self,
        weights: dict[str, int] | None = None,
        min_verified_questions: int = 2,
    ) -> None:
        self._weights = weights or dict(SCORING_WEIGHTS)
        # A single verified question is not enough to rank competing ideas. This
        # threshold prevents a thin evidence base from producing a confident-
        # sounding recommendation.
        self._min_verified_questions = min_verified_questions

    # -- generation -----------------------------------------------------

    def generate_candidates(self, opportunity: Opportunity) -> list[CandidateIdea]:
        technologies = opportunity.technologies or ["web application"]
        criteria = opportunity.judging_criteria
        verified_findings = [
            finding
            for finding in opportunity.research
            if finding.confidence == VerificationLevel.VERIFIED
        ]
        target_user = self._infer_user(opportunity, verified_findings)
        deadline_note = self._deadline_note(opportunity)

        candidates: list[CandidateIdea] = []
        for archetype in IDEA_ARCHETYPES:
            candidate = CandidateIdea(
                name=archetype["name_template"],
                problem=archetype["problem"],
                target_user=target_user,
                proposed_solution=archetype["solution"],
                core_functionality=list(archetype["functionality"]),
                required_technology=list(technologies[:3]),
                implementation_complexity=archetype["complexity"],
                differentiation=archetype["differentiation"],
                demo_potential=archetype["demo"],
                judging_alignment=list(criteria[:5]),
                major_risks=self._risks(archetype, technologies),
                estimated_build_scope=f"{archetype['scope']} {deadline_note}".strip(),
            )
            self._score(candidate, opportunity, verified_findings)
            candidates.append(candidate)

        return candidates

    @staticmethod
    def _infer_user(
        opportunity: Opportunity, findings: list[ResearchFinding]
    ) -> str:
        """Pull a target-user statement from product research, or say UNKNOWN."""

        patterns = (
            r"(?:intended|target|primary|end)\s+user\s*(?:is|are)?\s*([^.;\n]{0,120})",
            r"(?:user|users|audience|customer)\s+(?:is|are)\s+([^.;\n]{0,120})",
            r"(?:for|used by|serves)\s+(?:the\s+)?([^.;\n]{0,120}?)\s+(?:users?|audience|customers?)",
        )
        for finding in findings:
            if finding.question != "product" or not finding.answer:
                continue
            for line in finding.answer.splitlines():
                line = line.lstrip("- ").strip()
                for pattern in patterns:
                    match = re.search(pattern, line, re.IGNORECASE)
                    if match:
                        candidate = match.group(1).strip()
                        if len(candidate) > 3:
                            return candidate[:140]
        return "UNKNOWN - not established from retrieved sources"

    @staticmethod
    def _deadline_note(opportunity: Opportunity, today: date | None = None) -> str:
        deadline = opportunity.timeline.submission_deadline
        if deadline is None:
            return "Deadline UNKNOWN - scope must be treated as unconstrained until verified."
        days = (deadline - (today or date.today())).days
        if days < 0:
            return f"The verified submission deadline passed {abs(days)} days ago."
        return f"Approximately {days} days remain to the verified submission deadline."

    @staticmethod
    def _risks(archetype: dict[str, Any], technologies: list[str]) -> list[str]:
        risks: list[str] = []
        complexity = archetype["complexity"]
        if complexity in {"moderate-high", "high"}:
            risks.append("Scope may not fit the remaining build window.")
        if "oauth" in " ".join(archetype["functionality"]).lower():
            risks.append("Requires real OAuth credentials; blocked without user action.")
        if not technologies:
            risks.append("No technology constraints identified - stack choice is unverified.")
        return risks or ["No blocking technical risk identified from available evidence."]

    # -- scoring --------------------------------------------------------

    def _score(
        self,
        candidate: CandidateIdea,
        opportunity: Opportunity,
        findings: list[ResearchFinding],
    ) -> None:
        has_criteria = bool(opportunity.judging_criteria)
        verified_count = len(findings)
        complexity = candidate.implementation_complexity
        budget = COMPLEXITY_BUDGET.get(complexity, 5)

        scores = {
            "judging_alignment": 7 if has_criteria else 4,
            "time_feasibility": budget,
            "requirement_alignment": 8 if opportunity.deliverables else 5,
            "demo_clarity": 9 if candidate.demo_potential else 4,
            "differentiation": 8 if candidate.differentiation else 4,
            "technical_feasibility": 7 if opportunity.technologies else 6,
            "user_value": min(10, 5 + verified_count),
            "sponsor_integration": 8 if opportunity.sources else 4,
        }
        candidate.scores = scores
        candidate.total_score = round(
            sum(
                scores[criterion] * self._weights[criterion] / 10
                for criterion in self._weights
                if criterion in scores
            ),
            1,
        )

    # -- recommendation -------------------------------------------------

    def recommend(
        self,
        opportunity: Opportunity,
        candidates: list[CandidateIdea] | None = None,
        research: "list | None" = None,
    ) -> StrategyRecommendation:
        """Recommend a direction, or refuse.

        ``research`` may be passed explicitly so the decision cannot silently
        depend on whether the caller remembered to attach findings to the
        opportunity first. When omitted, ``opportunity.research`` is used.
        """

        candidates = candidates or self.generate_candidates(opportunity)
        findings = research if research is not None else opportunity.research
        verified = [
            finding
            for finding in findings
            if finding.confidence == VerificationLevel.VERIFIED
        ]

        if not verified:
            return StrategyRecommendation(
                recommended=None,
                candidates=candidates,
                reasoning=(
                    "No research finding is verified, so no direction can be recommended. "
                    "Per the verification law, an unevidenced recommendation would be "
                    "fabrication. Research must be completed first."
                ),
                criteria_used=dict(self._weights),
                caveats=[
                    "Run research before requesting a strategy decision.",
                    "Candidate list below is structural, not evidence-based.",
                ],
            )

        if len(verified) < self._min_verified_questions:
            return StrategyRecommendation(
                recommended=None,
                candidates=candidates,
                reasoning=(
                    f"Only {len(verified)} research question(s) could be verified, "
                    f"below the threshold of {self._min_verified_questions}. Ranking "
                    "candidates against a single verified passage would present a "
                    "thin evidence base as a confident judgement, which the "
                    "verification law forbids."
                ),
                criteria_used=dict(self._weights),
                caveats=[
                    f"Gather at least {self._min_verified_questions} verified research "
                    "questions before accepting a recommendation.",
                    f"Currently verified: {', '.join(f.question for f in verified)}.",
                    "Candidate list below is structural, not evidence-based.",
                ],
            )

        if not candidates:
            return StrategyRecommendation(
                recommended=None,
                candidates=[],
                reasoning="No candidates could be generated.",
                criteria_used=dict(self._weights),
                caveats=["Provide requirements or research findings to generate candidates."],
            )

        ranked = sorted(candidates, key=lambda item: item.total_score, reverse=True)
        best = ranked[0]
        runner_up = ranked[1] if len(ranked) > 1 else None

        drivers = sorted(
            best.scores.items(),
            key=lambda pair: pair[1] * self._weights.get(pair[0], 0),
            reverse=True,
        )[:3]
        driver_text = ", ".join(
            f"{criterion} ({score}/10, weight {self._weights.get(criterion, 0)})"
            for criterion, score in drivers
        )

        reasoning_parts = [
            f"Recommended because it leads on {driver_text}.",
        ]
        if runner_up:
            gap = round(best.total_score - runner_up.total_score, 1)
            reasoning_parts.append(
                f"It scores {best.total_score} against {runner_up.total_score} for "
                f"'{runner_up.name}' (gap {gap}), so the advantage is "
                f"{'material' if gap >= 5 else 'narrow and should be confirmed with the user'}."
            )
        if opportunity.judging_criteria:
            reasoning_parts.append(
                "It maps onto the documented judging criteria: "
                + "; ".join(opportunity.judging_criteria[:3])
                + "."
            )
        else:
            reasoning_parts.append(
                "Judging criteria were not retrieved, so alignment is inferred rather "
                "than verified."
            )
        reasoning_parts.append(
            f"Complexity is {best.implementation_complexity}, which "
            f"{'fits' if COMPLEXITY_BUDGET.get(best.implementation_complexity, 5) >= 7 else 'may strain'} "
            "the available build window."
        )
        reasoning_parts.append(
            f"Supported by {len(verified)} verified research findings."
        )

        caveats: list[str] = []
        if not opportunity.judging_criteria:
            caveats.append("Judging criteria UNKNOWN - verify before committing.")
        if not opportunity.deliverables:
            caveats.append("Required deliverables UNKNOWN - verify before scoping.")
        if opportunity.timeline.submission_deadline is None:
            caveats.append("Submission deadline UNKNOWN - urgency cannot be calculated.")
        if any("OAuth" in risk for risk in best.major_risks):
            caveats.append(
                "This direction needs credentials only the user can supply; "
                "it will block until then."
            )

        return StrategyRecommendation(
            recommended=best,
            candidates=ranked,
            reasoning=" ".join(reasoning_parts),
            criteria_used=dict(self._weights),
            caveats=caveats,
        )