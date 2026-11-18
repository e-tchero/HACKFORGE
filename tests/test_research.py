"""Phase 2: research engine."""

from __future__ import annotations

from datetime import date

import pytest

from hackforge.core.models import (
    Opportunity,
    ResearchFinding,
    SourceRef,
    Timeline,
    VerificationLevel,
)
from hackforge.engines.research import ResearchEngine, html_to_text

RULES_PAGE = """
<html><body>
<h1>Global AI Hackathon 2026</h1>
<p>The problem: small clinics cannot afford diagnostic screening at scale.</p>
<p>Judging criteria are impact, innovation, technical execution and demo quality.</p>
<p>Submissions are judged on real working impact rather than slideware.</p>
<p>Use the OpenAI API and any React frontend. Previous winners built triage tools.</p>
<p>The intended user is a community health worker operating without a specialist.</p>
<script>console.log('ignore me')</script>
</body></html>
"""

RULES_PAGE_FAILING = "<html><body><p>Nothing useful here.</p></body></html>"


def _opportunity_with_website() -> Opportunity:
    return Opportunity(
        id="op-1",
        name="Global AI Hackathon",
        type="hackathon",
        timeline=Timeline(submission_deadline=date(2026, 4, 18)),
        sources=[
            SourceRef(kind="official_website", reference="https://example.org/hackathon"),
            SourceRef(kind="registration_email", reference="gmail:msg-1"),
        ],
    )


class TestHtmlToText:
    def test_strips_scripts_and_tags(self):
        text = html_to_text(RULES_PAGE)

        assert "console.log" not in text
        assert "<" not in text
        assert "Global AI Hackathon 2026" in text


class TestResearchEngine:
    def test_verifies_findings_from_retrieved_source(self):
        engine = ResearchEngine(fetcher=lambda url: RULES_PAGE)
        result = engine.research(_opportunity_with_website())

        assert result.retrieved
        assert result.failed == []
        verified = [
            finding
            for finding in result.findings
            if finding.confidence == VerificationLevel.VERIFIED
        ]
        assert verified
        assert all(finding.evidence for finding in verified)
        assert result.completeness > 0

    def test_no_documents_yields_unverified_findings(self):
        def failing_fetch(url):
            raise OSError("network unreachable")

        result = ResearchEngine(fetcher=failing_fetch).research(
            _opportunity_with_website()
        )

        assert result.retrieved == []
        assert result.failed
        assert result.completeness == 0
        assert all(
            finding.confidence == VerificationLevel.UNVERIFIED
            for finding in result.findings
        )
        assert all("UNKNOWN" in finding.answer for finding in result.findings)

    def test_empty_document_is_a_failure_not_a_finding(self):
        result = ResearchEngine(fetcher=lambda url: "").research(
            _opportunity_with_website()
        )

        assert result.retrieved == []
        assert any("empty document" in error for _, error in result.failed)
        assert result.completeness == 0

    def test_summary_reports_verified_count(self):
        result = ResearchEngine(fetcher=lambda url: RULES_PAGE).research(
            _opportunity_with_website()
        )

        summary = result.summary()

        assert "verified" in summary
        assert "sources retrieved" in summary

    def test_non_http_reference_is_never_fetched(self):
        """The gmail: registration reference must not be passed to the fetcher."""

        fetched: list[str] = []

        def recording_fetch(url: str) -> str:
            fetched.append(url)
            return RULES_PAGE

        result = ResearchEngine(fetcher=recording_fetch).research(
            _opportunity_with_website()
        )

        assert fetched == ["https://example.org/hackathon"]
        assert all(source.reference.startswith("http") for source in result.retrieved)


class TestFindingSerialisation:
    def test_round_trip(self):
        finding = ResearchFinding(
            question="problem",
            answer="answer text",
            evidence=["https://example.org"],
            confidence=VerificationLevel.VERIFIED,
        )
        restored = ResearchFinding.from_dict(finding.to_dict())

        assert restored.question == "problem"
        assert restored.confidence == VerificationLevel.VERIFIED
        assert restored.evidence == ["https://example.org"]