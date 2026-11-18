"""Human approval gate (specification section 11).

The agent may reach any recommendation it likes. Recording the user's decision
is the only thing that moves execution past ``AWAITING_APPROVAL``. Decisions are
persisted so project memory (section 26) survives restarts.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path

from ..core.models import ExecutionStatus, Opportunity


class Decision(str, Enum):
    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"
    MODIFIED = "modified"
    MORE_RESEARCH = "more_research"


@dataclass
class ApprovalRecord:
    opportunity_id: str
    decision: Decision = Decision.PENDING
    selected_solution: str | None = None
    notes: str = ""
    decided_at: str | None = None
    history: list[dict[str, str]] = field(default_factory=list)

    @property
    def is_approved(self) -> bool:
        return self.decision in {Decision.APPROVED, Decision.MODIFIED}

    def to_dict(self) -> dict[str, object]:
        return {
            "opportunity_id": self.opportunity_id,
            "decision": self.decision.value,
            "selected_solution": self.selected_solution,
            "notes": self.notes,
            "decided_at": self.decided_at,
            "history": list(self.history),
        }

    @classmethod
    def from_dict(cls, data: dict[str, object]) -> "ApprovalRecord":
        return cls(
            opportunity_id=str(data.get("opportunity_id", "")),
            decision=Decision(str(data.get("decision", "pending"))),
            selected_solution=data.get("selected_solution"),  # type: ignore[arg-type]
            notes=str(data.get("notes", "")),
            decided_at=data.get("decided_at"),  # type: ignore[arg-type]
            history=list(data.get("history", [])),  # type: ignore[arg-type]
        )


class ApprovalStore:
    def __init__(self, path: Path) -> None:
        self.path = Path(path)

    def load(self) -> dict[str, ApprovalRecord]:
        if not self.path.is_file():
            return {}
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8") or "{}")
        except json.JSONDecodeError:
            return {}
        return {
            key: ApprovalRecord.from_dict(value)
            for key, value in raw.items()
            if isinstance(value, dict)
        }

    def save(self, records: dict[str, ApprovalRecord]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {key: record.to_dict() for key, record in records.items()}
        self.path.write_text(json.dumps(payload, indent=2), encoding="utf-8")

    def get(self, opportunity_id: str) -> ApprovalRecord:
        return self.load().get(opportunity_id, ApprovalRecord(opportunity_id))

    def record(
        self,
        opportunity_id: str,
        decision: Decision,
        selected_solution: str | None = None,
        notes: str = "",
    ) -> ApprovalRecord:
        records = self.load()
        entry = records.get(opportunity_id, ApprovalRecord(opportunity_id))
        stamp = datetime.now(timezone.utc).isoformat(timespec="seconds")
        entry.history.append(
            {
                "decision": decision.value,
                "selected_solution": selected_solution or "",
                "notes": notes,
                "at": stamp,
            }
        )
        entry.decision = decision
        entry.decided_at = stamp
        if selected_solution:
            entry.selected_solution = selected_solution
        if notes:
            entry.notes = notes
        records[opportunity_id] = entry
        self.save(records)
        return entry


class ApprovalGate:
    """Applies an approval decision to an opportunity record."""

    def __init__(self, store: ApprovalStore) -> None:
        self._store = store

    def approve(
        self,
        opportunity: Opportunity,
        selected_solution: str | None = None,
        notes: str = "",
    ) -> ApprovalRecord:
        record = self._store.record(
            opportunity.id,
            Decision.APPROVED,
            selected_solution,
            notes,
        )
        opportunity.strategy_status = "approved"
        opportunity.selected_solution = selected_solution or opportunity.selected_solution
        opportunity.execution_status = (
            ExecutionStatus.BUILDING
            if opportunity.project_directory
            else ExecutionStatus.APPROVED
        )
        opportunity.touch()
        return record

    def reject(self, opportunity: Opportunity, notes: str = "") -> ApprovalRecord:
        record = self._store.record(opportunity.id, Decision.REJECTED, None, notes)
        opportunity.strategy_status = "rejected"
        opportunity.touch()
        return record

    def request_more_research(self, opportunity: Opportunity, notes: str = "") -> ApprovalRecord:
        record = self._store.record(opportunity.id, Decision.MORE_RESEARCH, None, notes)
        opportunity.strategy_status = "more_research_requested"
        opportunity.execution_status = ExecutionStatus.RESEARCHING
        opportunity.touch()
        return record

    def status(self, opportunity_id: str) -> ApprovalRecord:
        return self._store.get(opportunity_id)