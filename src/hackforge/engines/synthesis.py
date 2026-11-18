"""Traceable synthesis layer (Phase 4.5).

This is deliberately *not* an autonomous reasoning system. It reorganises
already-retrieved evidence into the specific questions specification section 5
asks, and answers each one with either:

* the supporting passage(s) and their source URLs, or
* ``UNKNOWN`` / ``INSUFFICIENT EVIDENCE``.

It never infers, extrapolates, or fills a gap. Every answer carries the exact
evidence it rests on, so a human can check it against the source.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from ..core.models import EvidenceState, Opportunity, VerificationLevel
from .research import INSUFFICIENT_EVIDENCE, RESEARCH_QUESTIONS, ResearchResult

# The questions specification section 5 requires, mapped to research groups.
QUESTIONS: list[tuple[str, list[str]]] = [
    (
        "What does this opportunity require?",
        ["problem", "submission"],
    ),
    (
        "What does the judging process reward?",
        ["competition"],
    ),
    ("What constraints exist?", ["problem", "technical", "competition"]),
    ("What technologies/APIs are relevant?", ["technical"]),
    ("What submission requirements matter?", ["submission"]),
    ("What is supported by explicit evidence?", list({g for g, _ in RESEARCH_QUESTIONS})),
]


@dataclass
class SynthesisedAnswer:
    question: str
    state: EvidenceState
    answer: str
    evidence: list[tuple[str, str]] = field(default_factory=list)  # (url, passage)
    supports: list[str] = field(default_factory=list)

    @property
    def is_answered(self) -> bool:
        return self.state is EvidenceState.VERIFIED

    def to_markdown(self) -> str:
        lines = [f"### {self.question}", "", f"**State:** {self.state.value.upper()}", ""]
        if not self.is_answered:
            lines.append(f"{self.answer}")
            lines.append("")
            lines.append("_No supporting evidence was found; this is not an answer._")
            return "\n".join(lines)

        lines.append(self.answer)
        lines.append("")
        if self.evidence:
            lines.append("**Supporting evidence:**")
            lines.append("")
            for url, passage in self.evidence:
                lines.append(f"- `{url}`: \"{passage}\"")
        return "\n".join(lines) + "\n"


@dataclass
class Synthesis:
    answers: list[SynthesisedAnswer] = field(default_factory=list)
    verified_count: int = 0
    unknown_count: int = 0

    @property
    def coverage(self) -> int:
        total = self.verified_count + self.unknown_count
        return round(100 * self.verified_count / total) if total else 0

    def to_markdown(self, opportunity: Opportunity) -> str:
        lines = [
            f"# Research Synthesis - {opportunity.name}",
            "",
            "Every statement below is either backed by a quoted passage with its "
            "source URL, or explicitly marked as unknown. Nothing here is inferred.",
            "",
            f"- Verified answers: {self.verified_count}",
            f"- Unknown answers: {self.unknown_count}",
            f"- Coverage: {self.coverage}%",
            "",
        ]
        for answer in self.answers:
            lines.append(answer.to_markdown())
        return "\n".join(lines) + "\n"


class SynthesisEngine:
    """Assembles answers from research findings without adding new claims."""

    def synthesise(
        self, opportunity: Opportunity, result: ResearchResult
    ) -> Synthesis:
        synthesis = Synthesis()
        findings = {finding.question: finding for finding in result.findings}

        for question, groups in QUESTIONS:
            support: list[tuple[str, str]] = []
            unresolved: list[str] = []

            for group in groups:
                finding = findings.get(group)
                if finding is None:
                    unresolved.append(group)
                    continue
                if finding.confidence != VerificationLevel.VERIFIED:
                    unresolved.append(group)
                    continue
                for line in finding.answer.splitlines():
                    passage = line.lstrip("- ").strip()
                    if not passage:
                        continue
                    url = finding.evidence[0] if finding.evidence else ""
                    support.append((url, passage))

            # Deduplicate while preserving order.
            seen: set[tuple[str, str]] = set()
            unique_support = []
            for url, passage in support:
                if (url, passage) in seen:
                    continue
                seen.add((url, passage))
                unique_support.append((url, passage))

            if unique_support:
                synthesis.answers.append(
                    SynthesisedAnswer(
                        question=question,
                        state=EvidenceState.VERIFIED,
                        answer="\n".join(
                            f"- {passage}" for _, passage in unique_support[:MAX_QUOTES]
                        ),
                        evidence=unique_support[:MAX_QUOTES],
                        supports=sorted({group for group in groups if group in findings}),
                    )
                )
                synthesis.verified_count += 1
            else:
                reason = self._why_unknown(unresolved, result)
                synthesis.answers.append(
                    SynthesisedAnswer(
                        question=question,
                        state=EvidenceState.UNKNOWN,
                        answer=f"{INSUFFICIENT_EVIDENCE} - {reason}",
                        supports=[],
                    )
                )
                synthesis.unknown_count += 1

        return synthesis

    @staticmethod
    def _why_unknown(unresolved: list[str], result: ResearchResult) -> str:
        if result.failed:
            failed_urls = ", ".join(url for url, _ in result.failed[:3])
            return (
                f"retrieval failed for {failed_urls}, and no other retrieved source "
                f"addresses {', '.join(sorted(set(unresolved))) or 'this question'}."
            )
        if not result.documents:
            return "no source was retrieved for this opportunity."
        return (
            f"the retrieved sources did not state this for "
            f"{', '.join(sorted(set(unresolved))) or 'this question'}."
        )


MAX_QUOTES = 6