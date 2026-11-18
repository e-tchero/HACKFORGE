"""Source adapters."""

from .gmail import GmailError, GmailResult, GmailSource, GmailStatus, RawMessage

__all__ = [
    "GmailError",
    "GmailResult",
    "GmailSource",
    "GmailStatus",
    "OpportunitySource",
    "RawMessage",
]