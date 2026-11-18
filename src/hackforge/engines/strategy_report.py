"""Strategy validation report (specification section 6 of Phase 4.5).

Produces the fourteen required sections in order. Two rules govern the whole
document:

* A score never hides uncertainty. Where evidence is missing the report says so
  in the same section as the score.
* A recommendation is withheld unless verified evidence exists.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from ..core.models import CandidateIdea, Opportunity, VerificationLevel
from .research import ResearchResult
from .strategy import SCORING_WEIGHTS, StrategyRecommendation
from .synthesis import Synthesis


@dataclass
class ValidationReport:
    opportunity: Opportunity
    research: ResearchResult
    synthesis: Synthesis
    recommendation: StrategyRecommendation
    unknown_items: list[str] = field(default_factory=list)
    risks: list[str] = field(default_factory=list)
    change_conditions: list[str] = field(default_factory=list)

    @property
    def evidence_sufficient(self) -> bool:
        return bool(self.synthesis.verified_count)

    def to_markdown(self) -> str:
        return "\n".join(self._sections()) + "\n"

    # -- the fourteen sections -------------------------------------------

    def _sections(self) -> list[str]:
        return [
            self._section(1, "Hackathon Identity", self._identity()),
            self._section(2, "Verified Requirements", self._requirements()),
            self._section(3, "Verified Judging Criteria", self._judging()),
            self._section(4, "Constraints", self._constraints()),
            self._section(5, "Relevant Technologies", self._technologies()),
            self._section(6, "Candidate Concepts", self._candidates()),
            self._section(7, "Evidence Supporting Each Candidate", self._candidate_evidence()),
            self._section(8, "Criteria Used", self._criteria()),
            self._section(9, "Transparent Scoring", self._scoring()),
            self._section(10, "Recommendation", self._recommendation()),
            self._section(11, "Reasons for the Recommendation", self._reasons()),
            self._section(12, "Unknowns", self._unknowns()),
            self._section(13, "Risks", self._risk_list()),
            self._section(14, "What Would Change the Recommendation", self._change()),
        ]

    @staticmethod
    def _section(number: int, title: str, body: list[str]) -> str:
        return "\n".join([f"## {number}. {title}", ""] + body)

    def _identity(self) -> list[str]:
        opportunity = self.opportunity
        website = next(
            (s.reference for s in opportunity.sources if s.kind == "official_website"),
            "UNKNOWN",
        )
        provenance = opportunity.provenance
        return [
            f"- **Name:** {opportunity.name}",
            f"- **Type:** {opportunity.type}",
            f"- **Organizer:** {opportunity.organizer or 'UNKNOWN'}",
            f"- **Official website:** {website}",
            f"- **Registration status:** {opportunity.status.value}",
            f"- **Discovered via:** {provenance.source_label or 'UNKNOWN'} "
            f"message {provenance.message_id or 'UNKNOWN'}",
            f"- **Detection confidence:** {provenance.detection_confidence}",
            f"- **Submission deadline:** "
            f"{opportunity.timeline.submission_deadline or 'UNKNOWN'}",
        ]

    def _requirements(self) -> list[str]:
        deliverables = self.opportunity.deliverables
        if not deliverables:
            return ["UNKNOWN - no submission requirements were stated in retrieved sources."]
        return [f"- {item}" for item in deliverables]

    def _judging(self) -> list[str]:
        criteria = self.opportunity.judging_criteria
        if not criteria:
            return [
                "UNKNOWN - no judging criteria were stated in retrieved sources. "
                "This materially weakens every candidate below."
            ]
        return [f"- {item}" for item in criteria]

    def _constraints(self) -> list[str]:
        constraints: list[str] = []
        deadline = self.opportunity.timeline.submission_deadline
        if deadline:
            constraints.append(f"- Submission deadline: {deadline} (build window is limited)")
        else:
            constraints.append(
                "- Submission deadline UNKNOWN - build window cannot be bounded."
            )
        event = self.opportunity.timeline.event_date
        if event:
            constraints.append(f"- Event/finals date: {event}")
        for blocker in self.opportunity.blockers:
            constraints.append(f"- BLOCKED: {blocker.description}")
        if self.research.failed:
            constraints.append(
                f"- {len(self.research.failed)} source retrieval(s) failed: "
                + "; ".join(url for url, _ in self.research.failed[:3])
            )
        if not constraints:
            constraints.append("UNKNOWN - no constraints could be established.")
        return constraints

    def _technologies(self) -> list[str]:
        technologies = self.opportunity.technologies
        if not technologies:
            return ["UNKNOWN - no technology requirement was found in retrieved sources."]
        return [f"- {item}" for item in technologies]

    def _candidates(self) -> list[str]:
        if not self.recommendation.candidates:
            return ["No candidates could be generated."]
        lines = []
        for candidate in self.recommendation.candidates:
            lines.append(f"### {candidate.name}")
            lines.append(f"- Problem: {candidate.problem}")
            lines.append(f"- Target user: {candidate.target_user}")
            lines.append(f"- Approach: {candidate.proposed_solution}")
            lines.append(f"- Complexity: {candidate.implementation_complexity}")
            lines.append(f"- Scope: {candidate.estimated_build_scope}")
            lines.append("")
        return lines

    def _candidate_evidence(self) -> list[str]:
        lines: list[str] = []
        for candidate in self.recommendation.candidates:
            lines.append(f"- **{candidate.name}**")
            if candidate.judging_alignment:
                lines.append(
                    "  - Aligns with stated criteria: "
                    + ", ".join(candidate.judging_alignment)
                )
            else:
                lines.append("  - Aligns with stated criteria: NONE STATED (UNKNOWN)")
            for risk in candidate.major_risks:
                lines.append(f"  - Risk: {risk}")
        if not lines:
            lines.append("UNKNOWN - no candidates to support.")
        lines.append("")
        lines.append(
            "Candidate concepts are generated from a fixed set of archetypes, not "
            "from a model and not from this opportunity's evidence. Treat them "
            "as a starting point for human judgement, not as researched proposals."
        )
        return lines

    def _criteria(self) -> list[str]:
        lines = ["Weights (sum to 100):", ""]
        for criterion, weight in SCORING_WEIGHTS.items():
            lines.append(f"- `{criterion}`: {weight}")
        lines.append("")
        lines.append(
            "The full scoring breakdown follows. Weights are a stated policy "
            "choice, not evidence about this opportunity."
        )
        return lines

    def _scoring(self) -> list[str]:
        if not self.recommendation.candidates:
            return ["UNKNOWN - nothing to score."]
        lines = ["| Candidate | " + " | ".join(SCORING_WEIGHTS) + " | Total |",
                 "| --- |" + " --- |" * (len(SCORING_WEIGHTS) + 1)]
        for candidate in sorted(
            self.recommendation.candidates,
            key=lambda item: item.total_score,
            reverse=True,
        ):
            scores = " | ".join(
                str(candidate.scores.get(criterion, "?")) for criterion in SCORING_WEIGHTS
            )
            lines.append(
                f"| {candidate.name} | {scores} | {candidate.total_score} |"
            )
        lines.append("")
        lines.append(
            "_Scores are rule-derived from complexity and what was extracted. "
            "They do not measure quality and must not be read as a verdict._"
        )
        return lines

    def _recommendation(self) -> list[str]:
        best = self.recommendation.recommended
        if best is None:
            return [
                "**NO RECOMMENDATION.**",
                "",
                "The rule is unchanged: no verified evidence means no recommendation. "
                "Selecting a direction now would be invention, not analysis.",
            ]
        return [
            f"**{best.name}** (score {best.total_score}/100)",
            "",
            f"- Target user: {best.target_user}",
            f"- Approach: {best.proposed_solution}",
            f"- Scope: {best.estimated_build_scope}",
        ]

    def _reasons(self) -> list[str]:
        if self.recommendation.recommended is None:
            return [self.recommendation.reasoning]
        lines = [self.recommendation.reasoning, ""]
        verified = len(self.research.verified_questions())
        lines.append(
            f"Evidence base: {verified} verified research question(s), "
            f"{len(self.research.failed)} failed retrieval(s)."
        )
        return lines

    def _unknowns(self) -> list[str]:
        unknowns = list(self.unknown_items)
        for question in self.research.unknown_questions():
            unknowns.append(f"Research question '{question}' returned no evidence.")
        for answer in self.synthesis.answers:
            if not answer.is_answered:
                unknowns.append(f"{answer.question} - {answer.answer}")
        if not unknowns:
            unknowns.append("None recorded. This is unusual; verify extraction quality.")
        return [f"- {item}" for item in unknowns]

    def _risk_list(self) -> list[str]:
        risks = list(self.risks)
        for blocker in self.opportunity.blockers:
            risks.append(f"{blocker.description}: {blocker.reason or 'no reason recorded'}")
        if not self.evidence_sufficient:
            risks.append(
                "Evidence is insufficient for any direction. Acting on this report "
                "would mean guessing."
            )
        else:
            # Low coverage is itself a risk and must not be reported as "no risks".
            if self.synthesis.coverage < 100:
                risks.append(
                    f"Research coverage is {self.synthesis.coverage}% - "
                    f"{self.synthesis.unknown_count} question(s) could not be answered "
                    "from evidence. Any recommendation rests on a partial picture."
                )
            if self.recommendation.recommended is None:
                risks.append(
                    "No recommendation could be produced despite some verified "
                    "evidence. The evidence is too thin to rank candidates."
                )

        if self.research.failed:
            risks.append(
                f"{len(self.research.failed)} source(s) could not be retrieved "
                f"({', '.join(url for url, _ in self.research.failed[:2])}). "
                "Conclusions are limited to what was successfully read."
            )

        best = self.recommendation.recommended
        if best is not None:
            risks.append(
                "Candidate concepts are generated from fixed archetypes, not from "
                "this opportunity's evidence. The ranking reflects complexity "
                "heuristics more than suitability."
            )
            if any("OAuth" in risk for risk in best.major_risks):
                risks.append(
                    "Recommended direction needs credentials only the user can "
                    "supply; it will block until then."
                )

        if not risks:
            risks.append("No risks identified from available evidence.")
        return [f"- {item}" for item in risks]

    def _change(self) -> list[str]:
        conditions = list(self.change_conditions)
        conditions.extend(self.recommendation.caveats)
        if not self.opportunity.judging_criteria:
            conditions.append(
                "Recovering the actual judging criteria would rescore "
                "'judging_alignment' for every candidate."
            )
        if not self.opportunity.deliverables:
            conditions.append(
                "Recovering required deliverables would rescore "
                "'requirement_alignment' for every candidate."
            )
        if self.opportunity.timeline.submission_deadline is None:
            conditions.append(
                "Establishing the real deadline would change build scope and "
                "'time_feasibility' scoring."
            )
        if not conditions:
            conditions.append(
                "Nothing currently identified; note that the candidate set is "
                "structural rather than researched."
            )
        return [f"- {item}" for item in conditions]


def build_validation_report(
    opportunity: Opportunity,
    research: ResearchResult,
    synthesis: Synthesis,
    recommendation: StrategyRecommendation,
) -> ValidationReport:
    return ValidationReport(
        opportunity=opportunity,
        research=research,
        synthesis=synthesis,
        recommendation=recommendation,
        unknown_items=[],
        risks=[],
        change_conditions=[],
    )