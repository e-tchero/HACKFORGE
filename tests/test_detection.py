"""Phase 1: detection and extraction."""

from __future__ import annotations

from datetime import date

import pytest

from hackforge.adapters.sources import RawMessage
from hackforge.core.models import OpportunityStatus
from hackforge.engines.detection import Detector, FieldExtractor, OpportunityDetector


class TestDetector:
    def test_detects_registered_hackathon(self, hackathon_email):
        result = Detector().classify(hackathon_email)

        assert result.is_opportunity
        assert result.opportunity_type == "hackathon"
        assert result.registered is True
        assert result.reasons

    def test_rejects_newsletter(self, newsletter_email):
        result = Detector().classify(newsletter_email)

        assert result.is_opportunity is False
        assert result.opportunity_type == "none"

    def test_rejects_transactional_noise(self):
        message = RawMessage(
            message_id="m",
            thread_id=None,
            subject="Your receipt",
            body="Thanks for your purchase. hackathon",
            sender="billing@shop.example.com",
        )
        result = Detector().classify(message)

        assert result.is_opportunity is False
        assert any("disqualifier" in reason for reason in result.reasons)

    def test_announcement_is_not_registration(self):
        message = RawMessage(
            message_id="m",
            thread_id=None,
            subject="Global AI Hackathon 2026 registration is now open",
            body=(
                "Join us for the hackathon. Registration is now open. "
                "Submit your project by 2026-04-18."
            ),
            sender="events@hackathon.example.org",
        )
        result = Detector().classify(message)

        assert result.is_opportunity is True
        assert result.registered is False

    @pytest.mark.parametrize(
        "body,expected_type",
        [
            ("The hackathon begins soon. Register now.", "hackathon"),
            ("Scholarship applications for the 2026 cycle.", "scholarship"),
            ("Our summer internship cohort applications are open.", "internship"),
            ("Ambassador program applications are now open.", "developer_program"),
            ("Call for papers for the workshop.", "research"),
        ],
    )
    def test_multiple_opportunity_types(self, body, expected_type):
        message = RawMessage(
            message_id="m",
            thread_id=None,
            subject=body[:40],
            body=body,
            sender="info@example.org",
        )
        result = Detector().classify(message)

        assert result.is_opportunity is True
        assert result.opportunity_type == expected_type


class TestFieldExtractor:
    def test_extracts_labelled_dates(self, hackathon_email):
        extractor = FieldExtractor(reference_year=date(2026, 1, 15))
        fields, reasons = extractor.extract_dates(hackathon_email)

        assert fields["submission_deadline"] == date(2026, 4, 18)
        assert fields["registration_deadline"] == date(2026, 2, 1)
        assert fields["build_start"] == date(2026, 3, 20)
        assert fields["event_date"] == date(2026, 4, 20)
        assert reasons

    def test_does_not_invent_dates(self):
        message = RawMessage(
            message_id="m",
            thread_id=None,
            subject="Hackathon",
            body="The hackathon will take place sometime soon with a deadline.",
            sender="a@example.org",
        )
        fields, reasons = FieldExtractor().extract_dates(message)

        assert all(value is None for value in fields.values())
        assert any("UNKNOWN" in reason or "leaving dates" in reason for reason in reasons)

    def test_bare_date_without_reference_stays_unknown(self):
        message = RawMessage(
            message_id="m",
            thread_id=None,
            subject="Hackathon",
            body="Submission deadline: March 15",
            sender="a@example.org",
        )
        fields, _ = FieldExtractor().extract_dates(message)

        assert fields["submission_deadline"] is None

    def test_bare_date_with_reference_resolves_year(self):
        message = RawMessage(
            message_id="m",
            thread_id=None,
            subject="Hackathon",
            body="Submission deadline: March 15",
            sender="a@example.org",
        )
        fields, _ = FieldExtractor(reference_year=date(2026, 1, 10)).extract_dates(message)

        assert fields["submission_deadline"] == date(2026, 3, 15)

    def test_rolls_past_date_into_next_year(self):
        message = RawMessage(
            message_id="m",
            thread_id=None,
            subject="Hackathon",
            body="Submission deadline: January 10",
            sender="a@example.org",
        )
        fields, _ = FieldExtractor(reference_year=date(2026, 12, 20)).extract_dates(message)

        assert fields["submission_deadline"] == date(2027, 1, 10)

    def test_skips_unsubscribe_links(self):
        message = RawMessage(
            message_id="m",
            thread_id=None,
            subject="Registration confirmed",
            body="Confirmed",
            sender="a@example.org",
            html_body='<a href="https://x.example/unsubscribe">unsub</a>'
            '<a href="https://hackathon.example.org/2026">site</a>',
        )
        assert FieldExtractor().extract_url(message) == "https://hackathon.example.org/2026"

    def test_extracts_technologies(self, hackathon_email):
        technologies = FieldExtractor().extract_technologies(hackathon_email)

        assert "python" in technologies
        assert "react" in technologies


class TestOpportunityDetector:
    def test_builds_full_record(self, hackathon_email):
        opportunity, result = OpportunityDetector().from_message(hackathon_email)

        assert opportunity is not None
        assert result.is_opportunity
        assert opportunity.name == "Global AI Hackathon 2026"
        assert opportunity.status is OpportunityStatus.REGISTERED
        assert opportunity.registration_date == date(2026, 1, 15)
        assert opportunity.timeline.submission_deadline == date(2026, 4, 18)
        assert any(source.kind == "registration_email" for source in opportunity.sources)
        assert any(source.kind == "official_website" for source in opportunity.sources)
        assert "Impact" in opportunity.judging_criteria

    def test_returns_none_for_noise(self, newsletter_email):
        opportunity, result = OpportunityDetector().from_message(newsletter_email)

        assert opportunity is None
        assert result.is_opportunity is False

    def test_id_is_stable_for_same_source(self, hackathon_email):
        first, _ = OpportunityDetector().from_message(hackathon_email)
        second, _ = OpportunityDetector().from_message(hackathon_email)

        assert first.id == second.id

    def test_id_differs_for_different_organiser(self, hackathon_email):
        other = RawMessage(
            message_id="msg-999",
            thread_id="t",
            subject="Registration confirmed - Global AI Hackathon 2026",
            body=hackathon_email.body,
            sender="events@other-hackathon.example.net",
            received_at=date(2026, 1, 15),
        )
        first, _ = OpportunityDetector().from_message(hackathon_email)
        second, _ = OpportunityDetector().from_message(other)

        assert first.id != second.id

class TestWebsiteExtraction:
    """A markup/tracking link must never be recorded as the official website."""

    def _message(self, html: str, body: str = "Register now") -> RawMessage:
        return RawMessage(
            message_id="m-url",
            thread_id="t-url",
            subject="Hackathon registration",
            body=body,
            sender="events@hackathon.example.org",
            received_at=date(2026, 1, 15),
            html_body=html,
        )

    def _extractor(self):
        from hackforge.engines.detection import FieldExtractor

        return FieldExtractor(reference_year=date(2026, 1, 15))

    def test_skips_xml_dtd_declaration(self):
        html = (
            '<!DOCTYPE html PUBLIC "-//W3C//DTD XHTML 1.0 Strict//EN" '
            '"http://www.w3.org/TR/xhtml1/DTD/xhtml1-strict.dtd">'
            "<p>Register at https://hackathon.example.org/2026</p>"
        )

        assert self._extractor().extract_url(self._message(html)) == (
            "https://hackathon.example.org/2026"
        )

    def test_skips_tracking_pixel_and_unsubscribe(self):
        html = (
            '<img src="https://track.example.net/open.aspx?id=1">'
            '<a href="https://lists.example.com/unsubscribe">Unsubscribe</a>'
            "<a href='https://devpost.com/hackathon'>Visit</a>"
        )

        assert self._extractor().extract_url(self._message(html)) == (
            "https://devpost.com/hackathon"
        )

    def test_returns_none_when_only_boilerplate_links_exist(self):
        html = '<img src="https://www.google-analytics.com/collect">'

        assert self._extractor().extract_url(self._message(html)) is None

    def test_accepts_real_organiser_site(self):
        html = "<p><a href='https://hackthebox.com/events'>Details</a></p>"

        assert self._extractor().extract_url(self._message(html)) == (
            "https://hackthebox.com/events"
        )

    def test_skips_marketing_automation_host(self):
        html = (
            "<a href='https://gcgb7.r.a.d.sendibm1.com/mk/op/sh/abc'>Register</a>"
            "<p><a href='https://devpost.com/hack'>Site</a></p>"
        )

        assert self._extractor().extract_url(self._message(html)) == (
            "https://devpost.com/hack"
        )

    def test_skips_font_asset_url(self):
        html = "<p style=\"font-family:x\">Register</p><p>https://images.nvidia.com/etc/designs/f/GLOBAL/NVIDIASans.woff2</p>"

        assert self._extractor().extract_url(self._message(html)) is None

    def test_skips_slack_deep_link(self):
        html = "<p>Unread <a href='https://datahubspace.slack.com/x-p123/team?t=view_all_unread'>messages</a></p>"

        assert self._extractor().extract_url(self._message(html)) is None
