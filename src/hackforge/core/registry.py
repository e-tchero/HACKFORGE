"""Opportunity registry.

Sits between detection and persistence. Owns the merge policy so repeated
scans of the same inbox do not create duplicates, and keeps an activity trail so
the accountability engine can measure inactivity (specification section 20).
"""

from __future__ import annotations

from typing import Iterable

from .models import EvidenceRef, Opportunity, OpportunityStatus, Provenance, now_iso
from .store import Store


def _merge_evidence(merged: Opportunity, incoming: Opportunity) -> None:
    """Accumulate registration evidence without duplicating identical mail.

    Several distinct emails can confirm one registration (a receipt plus a
    reminder). Each is retained because they are separate pieces of evidence,
    but a message already recorded is not added twice.
    """

    if incoming.registration_evidence is None:
        return

    seen = {item.url for item in merged.registration_evidence_history if item.url}
    if incoming.registration_evidence.url in seen:
        return

    merged.registration_evidence_history.append(incoming.registration_evidence)

    # Prefer a verified piece of evidence over an unverified one.
    current = merged.registration_evidence
    if current is None or (
        not current.is_verified and incoming.registration_evidence.is_verified
    ):
        merged.registration_evidence = incoming.registration_evidence


def _merge_provenance(merged: Opportunity, incoming: Opportunity) -> None:
    """Keep the strongest detection provenance and remember every source."""

    if not merged.provenance.message_id:
        merged.provenance = incoming.provenance
        return

    if incoming.provenance.detection_confidence > merged.provenance.detection_confidence:
        keep = list(merged.provenance.reasons)
        keep_messages = [
            f"message {merged.provenance.message_id}",
            f"message {incoming.provenance.message_id}",
        ]
        merged.provenance = incoming.provenance
        merged.provenance.reasons = keep + [
            f"also detected in {', '.join(keep_messages)}"
        ]


class OpportunityRegistry:
    def __init__(self, store: Store, activity: list[tuple[str, str]] | None = None) -> None:
        self._store = store
        # Injected so the registry stays storage-agnostic.
        self._activity_log = activity if activity is not None else []

    # -- queries --------------------------------------------------------

    def all(self) -> list[Opportunity]:
        return self._store.load_all()

    def get(self, opportunity_id: str) -> Opportunity | None:
        return self._store.get(opportunity_id)

    def find_by_name(self, name: str) -> Opportunity | None:
        target = name.strip().lower()
        for opportunity in self._store.load_all():
            if opportunity.name.strip().lower() == target:
                return opportunity
        return None

    # -- mutation -------------------------------------------------------

    def register(self, opportunity: Opportunity) -> Opportunity:
        """Insert or merge. Existing work is never overwritten by a re-scan."""

        existing = self._store.get(opportunity.id)
        if existing is None:
            self._store.upsert(opportunity)
            self._log(
                opportunity.id,
                f"detected {opportunity.type} '{opportunity.name}' "
                f"(status={opportunity.status.value})",
            )
            return opportunity

        merged = self._merge(existing, opportunity)
        self._store.upsert(merged)
        return merged

    def save(self, opportunity: Opportunity) -> Opportunity:
        opportunity.touch()
        self._store.upsert(opportunity)
        return opportunity

    def mark_verified(self, opportunity: Opportunity) -> Opportunity:
        opportunity.mark_verified()
        self._store.upsert(opportunity)
        return opportunity

    def remove(self, opportunity_id: str) -> bool:
        before = self.get(opportunity_id)
        self._store.delete(opportunity_id)
        if before is not None:
            self._log(opportunity_id, f"removed '{before.name}' from registry")
            return True
        return False

    # -- merging --------------------------------------------------------

    @staticmethod
    def _merge(existing: Opportunity, incoming: Opportunity) -> Opportunity:
        """Fill gaps in the stored record; never regress verified values."""

        merged = Opportunity.from_dict(existing.to_dict())

        if incoming.organizer and not merged.organizer:
            merged.organizer = incoming.organizer
        if incoming.registration_date and not merged.registration_date:
            merged.registration_date = incoming.registration_date

        for field in ("registration_deadline", "build_start", "submission_deadline", "event_date"):
            if getattr(merged.timeline, field) is None:
                setattr(merged.timeline, field, getattr(incoming.timeline, field))

        merged.technologies = sorted(set(merged.technologies) | set(incoming.technologies))
        merged.tracks = sorted(set(merged.tracks) | set(incoming.tracks))
        merged.deliverables = sorted(set(merged.deliverables) | set(incoming.deliverables))

        known = {source.reference for source in merged.sources}
        for source in incoming.sources:
            if source.reference not in known:
                merged.sources.append(source)

        # A later confirmation email promotes the record; it never demotes it.
        if incoming.status == OpportunityStatus.REGISTERED:
            merged.status = OpportunityStatus.REGISTERED

        _merge_evidence(merged, incoming)
        _merge_provenance(merged, incoming)

        merged.touch()
        return merged

    def _log(self, opportunity_id: str, entry: str) -> None:
        stamped = f"{now_iso()} {entry}"
        self._activity_log.append((opportunity_id, stamped))

    # -- activity -------------------------------------------------------

    @property
    def activity(self) -> list[tuple[str, str]]:
        return list(self._activity_log)

    def last_activity(self, opportunity_id: str) -> str | None:
        entries = [entry for oid, entry in self._activity_log if oid == opportunity_id]
        return max(entries) if entries else None


def summarise(opportunities: Iterable[Opportunity]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for opportunity in opportunities:
        counts[opportunity.execution_status.value] = (
            counts.get(opportunity.execution_status.value, 0) + 1
        )
    return counts