"""Opportunity source adapters.

The detector consumes ``RawMessage`` records. Any source that can produce those
records (Gmail today, Outlook / Fastmail / a CSV export tomorrow) plugs in
without touching detection logic. This is what keeps section 4 of the spec from
hard-coding HACKFORGE to hackathons or to Gmail.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from typing import Protocol, runtime_checkable


@dataclass
class RawMessage:
    """A single retrieved message, normalised across sources."""

    message_id: str
    thread_id: str | None
    subject: str
    body: str
    sender: str
    sender_name: str = ""
    received_at: date | None = None
    source_kind: str = "email"
    source_label: str = "gmail"
    raw_headers: dict[str, str] = field(default_factory=dict)
    html_body: str = ""

    @property
    def searchable_text(self) -> str:
        return f"{self.subject}\n{self.body}".lower()


@runtime_checkable
class OpportunitySource(Protocol):
    """Anything that can hand HACKFORGE messages to inspect."""

    name: str

    def fetch(self, limit: int | None = None) -> list[RawMessage]: ...

    def available(self) -> tuple[bool, str]:
        """Return ``(is_available, reason)``. Never raises for missing config."""