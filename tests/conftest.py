"""Shared fixtures."""

from __future__ import annotations

from datetime import date

import pytest

from hackforge.adapters.sources import RawMessage
from hackforge.core.models import Opportunity, Timeline


@pytest.fixture
def hackathon_email() -> RawMessage:
    body = """
Hello ETCHE,

Thank you for registering for Global AI Hackathon 2026.

Registration confirmed. Your team ID is TEAM-88213.

Details:
Submission deadline: 2026-04-18
Registration deadline: 2026-02-01
Build period: March 20, 2026
Event date: April 20, 2026

Tracks: Innovation, Social Impact, Education
Technologies: Python, React, LLM

Judging criteria: Impact, Innovation, Technical Execution, Demo Quality

Organizer site: https://hackathon.example.org/2026
"""
    return RawMessage(
        message_id="msg-001",
        thread_id="thread-001",
        subject="Registration confirmed - Global AI Hackathon 2026",
        body=body,
        sender="events@hackathon.example.org",
        sender_name="Global AI Hackathon",
        received_at=date(2026, 1, 15),
    )


@pytest.fixture
def newsletter_email() -> RawMessage:
    return RawMessage(
        message_id="msg-002",
        thread_id="thread-002",
        subject="Weekly product newsletter",
        body="Here are this week's top stories. Unsubscribe anytime.",
        sender="news@saasnews.example.com",
        sender_name="SaaS News",
        received_at=date(2026, 1, 16),
    )


@pytest.fixture
def sample_opportunity() -> Opportunity:
    return Opportunity(
        id="test-opportunity",
        name="Test Hackathon",
        type="hackathon",
        organizer="Test Org",
        timeline=Timeline(submission_deadline=date(2026, 4, 18)),
        technologies=["Python", "React"],
        judging_criteria=["Impact", "Innovation", "Technical Execution"],
    )


@pytest.fixture
def temp_config(tmp_path):
    from hackforge.config import Config

    return Config(
        autonomy_level=0,
        data_dir=tmp_path / "data",
        workspace_root=tmp_path / "workspace",
    )