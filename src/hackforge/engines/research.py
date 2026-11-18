"""Research engine (Phase 2, hardened for Phase 4.5).

Fetches primary sources, records what was actually retrieved, and answers the
question set from specification section 8 with evidence attached.

Invariants:

1. A finding is ``VERIFIED`` only if a real document backs it, and the exact
   supporting passage is retained.
2. A failed retrieval is recorded as ``FAILED`` and is never rendered as text.
3. An unanswered question is ``UNKNOWN`` - distinct from a failed attempt.
"""

from __future__ import annotations

import html
import re
from dataclasses import dataclass, field
from typing import Any, Callable, Protocol

from ..core.models import (
    EvidenceRef,
    EvidenceState,
    Opportunity,
    ResearchFinding,
    SourceRef,
    VerificationLevel,
    now_iso,
)

Fetcher = Callable[[str], str]

_SCRIPT_RE = re.compile(r"<(script|style|noscript)[^>]*>.*?</\1>", re.IGNORECASE | re.DOTALL)
_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"[ \t]{2,}")

# Source priority from specification section 7, highest first.
SOURCE_TIERS: list[tuple[int, str, str]] = [
    (1, "official_website", "Official opportunity website"),
    (2, "rules", "Official rules / terms"),
    (3, "documentation", "Official documentation"),
    (4, "sponsor_docs", "Sponsor documentation"),
    (5, "api_sdk_docs", "API / SDK documentation"),
    (6, "announcements", "Organizer announcements"),
    (7, "past_winners", "Previous winners"),
    (8, "secondary", "Reliable secondary source"),
    (9, "community", "Community discussion"),
]

# Questions the synthesis layer must answer, with the claim each answers.
RESEARCH_QUESTIONS: list[tuple[str, list[str]]] = [
    ("problem", ["problem", "challenge", "task", "prompt", "objective"]),
    ("competition", ["judging", "criteria", "scoring", "evaluation", "prize", "track"]),
    ("historical", ["winner", "previous", "past project", "last year"]),
    ("technical", ["api", "sdk", "technology", "stack", "integration", "tool"]),
    ("product", ["user", "audience", "demo", "product", "use case"]),
    ("submission", ["submit", "submission", "deliverable", "deadline", "requirement"]),
]

# How much of a matching section to surface. Capped so one long page cannot
# dominate the report.
MAX_EXCERPTS_PER_SOURCE = 4
MAX_ANSWER_EXCERPTS = 8

INSUFFICIENT_EVIDENCE = "INSUFFICIENT EVIDENCE"


@dataclass
class RetrievalAttempt:
    """One URL fetch, recorded whether it succeeded or not."""

    url: str
    kind: str
    tier: int
    ok: bool
    detail: str = ""
    retrieved_at: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "url": self.url,
            "kind": self.kind,
            "tier": self.tier,
            "ok": self.ok,
            "detail": self.detail,
            "retrieved_at": self.retrieved_at,
        }


@dataclass
class ResearchResult:
    findings: list[ResearchFinding] = field(default_factory=list)
    retrieved: list[SourceRef] = field(default_factory=list)
    failed: list[tuple[str, str]] = field(default_factory=list)
    documents: dict[str, str] = field(default_factory=dict)
    evidence: list[EvidenceRef] = field(default_factory=list)
    attempts: list[RetrievalAttempt] = field(default_factory=list)

    @property
    def completeness(self) -> int:
        """Percentage of question groups answered with verified evidence."""

        answered = {
            finding.question
            for finding in self.findings
            if finding.confidence == VerificationLevel.VERIFIED
        }
        return round(100 * len(answered) / max(len(RESEARCH_QUESTIONS), 1))

    def state_for(self, question: str) -> EvidenceState:
        finding = next(
            (item for item in self.findings if item.question == question), None
        )
        if finding is None:
            return EvidenceState.UNKNOWN
        if finding.confidence == VerificationLevel.VERIFIED:
            return EvidenceState.VERIFIED
        # If every retrieval failed, the question is FAILED rather than UNKNOWN:
        # an attempted-but-broken fetch is not the same as a genuine gap.
        if self.failed and not self.documents:
            return EvidenceState.FAILED
        if any(url in dict(self.failed) for url in finding.evidence):
            return EvidenceState.FAILED
        return EvidenceState.UNKNOWN

    def verified_questions(self) -> list[str]:
        return [
            item.question
            for item in self.findings
            if item.confidence == VerificationLevel.VERIFIED
        ]

    def unknown_questions(self) -> list[str]:
        return [item.question for item in self.findings if self.state_for(item.question) is EvidenceState.UNKNOWN]

    def summary(self) -> str:
        verified = len(self.verified_questions())
        return (
            f"{verified}/{len(self.findings)} findings verified, "
            f"{len(self.retrieved)} sources retrieved, "
            f"{len(self.failed)} failed"
        )


def html_to_text(raw: str) -> str:
    stripped = _SCRIPT_RE.sub(" ", raw)
    stripped = re.sub(r"<br\s*/?>", "\n", stripped, flags=re.IGNORECASE)
    stripped = re.sub(r"</(p|div|li|h[1-6])>", "\n", stripped, flags=re.IGNORECASE)
    stripped = _TAG_RE.sub(" ", stripped)
    return _WS_RE.sub(" ", html.unescape(stripped)).strip()


class Fetcher(Protocol):
    def __call__(self, url: str) -> str: ...


def default_fetcher(url: str) -> str:
    """Stdlib fetch so the core has no runtime dependency.

    Network errors propagate; the caller records them as failures rather than
    silently producing an empty document.
    """

    from urllib.request import Request, urlopen

    request = Request(url, headers={"User-Agent": "hackforge-research/0.1"})
    with urlopen(request, timeout=20) as response:  # noqa: S310
        charset = response.headers.get_content_charset() or "utf-8"
        return response.read().decode(charset, "replace")


class ResearchEngine:
    def __init__(self, fetcher: Fetcher | None = None, max_pages: int = 25) -> None:
        self._fetch = fetcher or default_fetcher
        self._page_budget = max_pages

    def research(self, opportunity: Opportunity) -> ResearchResult:
        result = ResearchResult()
        targets = self._targets(opportunity)

        for kind, reference, tier, _label in targets:
            if self._page_budget <= 0:
                result.failed.append((reference, "page budget exhausted"))
                result.attempts.append(
                    RetrievalAttempt(reference, kind, tier, False, "page budget exhausted")
                )
                continue
            self._page_budget -= 1

            try:
                raw = self._fetch(reference)
            except Exception as exc:  # noqa: BLE001 - failures are data, not crashes
                detail = f"{type(exc).__name__}: {exc}"
                result.failed.append((reference, detail))
                result.attempts.append(
                    RetrievalAttempt(reference, kind, tier, False, detail)
                )
                continue

            text = html_to_text(raw)
            if not text.strip():
                result.failed.append((reference, "empty document"))
                result.attempts.append(
                    RetrievalAttempt(reference, kind, tier, False, "empty document")
                )
                continue

            stamp = now_iso()
            result.documents[reference] = text
            result.attempts.append(
                RetrievalAttempt(reference, kind, tier, True, "retrieved", stamp)
            )
            result.retrieved.append(
                SourceRef(
                    kind=kind,
                    reference=reference,
                    retrieved_at=stamp,
                    verified=True,
                )
            )

        result.findings = self._answer(opportunity, result)
        result.evidence = self._build_evidence(result)
        return result

    def _targets(self, opportunity: Opportunity) -> list[tuple[str, str, int, str]]:
        targets: list[tuple[str, str, int, str]] = []
        seen: set[str] = set()

        ordered = sorted(
            opportunity.sources,
            key=lambda source: next(
                (tier for tier, kind, _ in SOURCE_TIERS if kind == source.kind), 99
            ),
        )
        for source in ordered:
            if not source.reference.startswith("http"):
                continue
            if source.reference in seen:
                continue
            seen.add(source.reference)
            tier, kind, label = next(
                ((t, k, lb) for t, k, lb in SOURCE_TIERS if k == source.kind),
                (99, source.kind, source.kind),
            )
            targets.append((source.kind, source.reference, tier, label))

        # Recorded but untiered links are still worth reading, at lowest priority.
        for source in opportunity.sources:
            if source.reference.startswith("http") and source.reference not in seen:
                seen.add(source.reference)
                targets.append((source.kind, source.reference, 99, source.kind))

        targets.sort(key=lambda item: item[2])
        return targets

    def _answer(
        self, opportunity: Opportunity, result: ResearchResult
    ) -> list[ResearchFinding]:
        findings: list[ResearchFinding] = []

        for group, keywords in RESEARCH_QUESTIONS:
            evidence: list[str] = []
            excerpts: list[str] = []

            for url, text in result.documents.items():
                matches = [
                    sentence
                    for sentence in self._sentences(text)
                    if any(keyword in sentence.lower() for keyword in keywords)
                ]
                if not matches:
                    continue
                evidence.append(url)
                excerpts.extend(match[:280] for match in matches[:MAX_EXCERPTS_PER_SOURCE])

            if not result.documents:
                if result.failed:
                    answer = (
                        f"UNKNOWN - all {len(result.failed)} retrieval attempt(s) failed. "
                        "No evidence was gathered."
                    )
                else:
                    answer = (
                        "UNKNOWN - no retrievable source is recorded for this opportunity."
                    )
                findings.append(
                    ResearchFinding(
                        question=group,
                        answer=answer,
                        evidence=[],
                        confidence=VerificationLevel.UNVERIFIED,
                    )
                )
                continue

            if not evidence:
                findings.append(
                    ResearchFinding(
                        question=group,
                        answer=(
                            "UNKNOWN - retrieved sources did not contain information "
                            "addressing this question."
                        ),
                        evidence=[],
                        confidence=VerificationLevel.UNVERIFIED,
                    )
                )
                continue

            unique_evidence = list(dict.fromkeys(evidence))
            findings.append(
                ResearchFinding(
                    question=group,
                    answer="\n".join(
                        f"- {excerpt}"
                        for excerpt in excerpts[:MAX_ANSWER_EXCERPTS]
                    ),
                    evidence=unique_evidence[:5],
                    confidence=VerificationLevel.VERIFIED,
                )
            )

        return findings

    def _build_evidence(self, result: ResearchResult) -> list[EvidenceRef]:
        """One evidence record per verified claim, with the exact passage."""

        evidence: list[EvidenceRef] = []
        for finding in result.findings:
            if finding.confidence != VerificationLevel.VERIFIED:
                continue
            for line in finding.answer.splitlines():
                passage = line.lstrip("- ").strip()
                if not passage:
                    continue
                evidence.append(
                    EvidenceRef(
                        claim=finding.question,
                        url=finding.evidence[0] if finding.evidence else "",
                        passage=passage,
                        state=EvidenceState.VERIFIED,
                        retrieval_status="retrieved",
                    )
                )
        return evidence

    @staticmethod
    def _sentences(text: str, max_sentences: int = 400) -> list[str]:
        parts = re.split(r"(?<=[.!?])\s+|\n{2,}", text)
        cleaned = [part.strip() for part in parts if len(part.strip()) > 40]
        return cleaned[:max_sentences]