"""Domain models for HACKFORGE.

Design rule enforced throughout this module: a value that could not be
verified is ``None``, never a guess. Serialisation renders unverified values as
``UNKNOWN`` so downstream reports cannot accidentally present a guess as fact.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from datetime import date, datetime, timezone
from enum import Enum
from typing import Any


class OpportunityStatus(str, Enum):
    """Lifecycle of the opportunity itself (specification section 5)."""

    DISCOVERED = "discovered"
    REGISTERED = "registered"
    ACTIVE = "active"
    COMPLETED = "completed"
    EXPIRED = "expired"
    CANCELLED = "cancelled"


class ExecutionStatus(str, Enum):
    """How far execution has progressed. Ordered by the verification law."""

    DISCOVERED = "discovered"
    RESEARCHING = "researching"
    STRATEGY_PENDING = "strategy_pending"
    AWAITING_APPROVAL = "awaiting_approval"
    APPROVED = "approved"
    BUILDING = "building"
    TESTING = "testing"
    VERIFIED = "verified"
    SUBMISSION_READY = "submission_ready"
    SUBMITTED = "submitted"
    BLOCKED = "blocked"


class Urgency(str, Enum):
    LONG_RANGE = "LONG_RANGE"
    PLANNING = "PLANNING"
    ACTIVE = "ACTIVE"
    URGENT = "URGENT"
    CRITICAL = "CRITICAL"
    FINAL_WINDOW = "FINAL_WINDOW"
    EXPIRED = "EXPIRED"
    UNKNOWN = "UNKNOWN"


class VerificationLevel(str, Enum):
    """Distinguishes intent from evidence (specification section 15)."""

    PLANNED = "planned"
    ATTEMPTED = "attempted"
    IMPLEMENTED = "implemented"
    TESTED = "tested"
    VERIFIED = "verified"
    COMPLETED = "completed"
    FAILED = "failed"
    UNVERIFIED = "unverified"


class EvidenceState(str, Enum):
    """Retrieval outcome for a single claim.

    ``UNKNOWN`` means the question was asked but no source answered it.
    ``FAILED`` means retrieval was attempted and did not succeed. The two are
    deliberately distinct: a failed fetch is not the same as a silent gap.
    """

    VERIFIED = "verified"
    UNKNOWN = "unknown"
    FAILED = "failed"


class EvidenceRef:
    """A single retrievable source with its retrieval outcome.

    ``passage`` is the exact supporting text. It is never paraphrased, because
    a paraphrase cannot be checked against the source.
    """

    __slots__ = ("claim", "url", "passage", "state", "retrieval_status", "retrieved_at")

    def __init__(
        self,
        claim: str,
        url: str = "",
        passage: str = "",
        state: EvidenceState = EvidenceState.UNKNOWN,
        retrieval_status: str = "not_attempted",
        retrieved_at: str | None = None,
    ) -> None:
        self.claim = claim
        self.url = url
        self.passage = passage
        self.state = EvidenceState(state)
        self.retrieval_status = retrieval_status
        self.retrieved_at = retrieved_at

    @property
    def is_verified(self) -> bool:
        return self.state is EvidenceState.VERIFIED and bool(self.passage)

    def to_dict(self) -> dict[str, Any]:
        return {
            "claim": self.claim,
            "url": self.url,
            "passage": self.passage,
            "state": self.state.value,
            "retrieval_status": self.retrieval_status,
            "retrieved_at": self.retrieved_at,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "EvidenceRef":
        return cls(
            claim=data.get("claim", ""),
            url=data.get("url", ""),
            passage=data.get("passage", ""),
            state=EvidenceState(data.get("state", "unknown")),
            retrieval_status=data.get("retrieval_status", "not_attempted"),
            retrieved_at=data.get("retrieved_at"),
        )

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, EvidenceRef):
            return NotImplemented
        return self.to_dict() == other.to_dict()

    def __repr__(self) -> str:
        return f"EvidenceRef(state={self.state.value!r}, claim={self.claim!r})"


class Provenance:
    """Where a detected opportunity came from, and how confident detection was."""

    __slots__ = (
        "source_label",
        "message_id",
        "thread_id",
        "received_at",
        "detection_confidence",
        "reasons",
        "scored_types",
    )

    def __init__(
        self,
        source_label: str = "",
        message_id: str = "",
        thread_id: str | None = None,
        received_at: str | None = None,
        detection_confidence: float = 0.0,
        reasons: list[str] | None = None,
        scored_types: dict[str, float] | None = None,
    ) -> None:
        self.source_label = source_label
        self.message_id = message_id
        self.thread_id = thread_id
        self.received_at = received_at
        self.detection_confidence = detection_confidence
        self.reasons = list(reasons or [])
        self.scored_types = dict(scored_types or {})

    def to_dict(self) -> dict[str, Any]:
        return {
            "source_label": self.source_label,
            "message_id": self.message_id,
            "thread_id": self.thread_id,
            "received_at": self.received_at,
            "detection_confidence": self.detection_confidence,
            "reasons": list(self.reasons),
            "scored_types": dict(self.scored_types),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Provenance":
        return cls(
            source_label=data.get("source_label", ""),
            message_id=data.get("message_id", ""),
            thread_id=data.get("thread_id"),
            received_at=data.get("received_at"),
            detection_confidence=float(data.get("detection_confidence", 0.0)),
            reasons=list(data.get("reasons", [])),
            scored_types=dict(data.get("scored_types", {})),
        )


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _serialise(value: Any) -> Any:
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, datetime):
        return value.isoformat(timespec="seconds")
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, (list, tuple)):
        return [_serialise(item) for item in value]
    if isinstance(value, dict):
        return {key: _serialise(item) for key, item in value.items()}
    return value


@dataclass
class Timeline:
    """All dates optional. ``None`` means UNKNOWN, never "today" or a guess."""

    registration_deadline: date | None = None
    build_start: date | None = None
    submission_deadline: date | None = None
    event_date: date | None = None

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        return {key: (value.isoformat() if value else "UNKNOWN") for key, value in data.items()}

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None) -> "Timeline":
        data = data or {}

        def parse(key: str) -> date | None:
            raw = data.get(key)
            if not raw or raw == "UNKNOWN":
                return None
            try:
                return date.fromisoformat(str(raw))
            except ValueError:
                return None

        return cls(
            registration_deadline=parse("registration_deadline"),
            build_start=parse("build_start"),
            submission_deadline=parse("submission_deadline"),
            event_date=parse("event_date"),
        )


@dataclass
class SourceRef:
    """A traceable pointer to where a fact came from."""

    kind: str
    reference: str
    retrieved_at: str | None = None
    verified: bool = False

    def to_dict(self) -> dict[str, Any]:
        return _serialise(asdict(self))

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "SourceRef":
        return cls(
            kind=data.get("kind", "unknown"),
            reference=data.get("reference", ""),
            retrieved_at=data.get("retrieved_at"),
            verified=bool(data.get("verified", False)),
        )


@dataclass
class ResearchFinding:
    question: str
    answer: str
    evidence: list[str] = field(default_factory=list)
    confidence: VerificationLevel = VerificationLevel.UNVERIFIED

    def to_dict(self) -> dict[str, Any]:
        return _serialise(asdict(self))

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "ResearchFinding":
        return cls(
            question=data.get("question", ""),
            answer=data.get("answer", ""),
            evidence=list(data.get("evidence", [])),
            confidence=VerificationLevel(data.get("confidence", "unverified")),
        )


@dataclass
class CandidateIdea:
    name: str
    problem: str = ""
    target_user: str = ""
    proposed_solution: str = ""
    core_functionality: list[str] = field(default_factory=list)
    required_technology: list[str] = field(default_factory=list)
    implementation_complexity: str = "unassessed"
    differentiation: str = ""
    demo_potential: str = ""
    judging_alignment: list[str] = field(default_factory=list)
    major_risks: list[str] = field(default_factory=list)
    estimated_build_scope: str = ""
    scores: dict[str, int] = field(default_factory=dict)
    total_score: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return _serialise(asdict(self))

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "CandidateIdea":
        return cls(**{k: v for k, v in data.items() if k in cls.__dataclass_fields__})


@dataclass
class Blocker:
    description: str
    reason: str = ""
    attempted: str = ""
    remaining: str = ""
    user_action_required: str = ""
    raised_at: str = field(default_factory=now_iso)

    def to_dict(self) -> dict[str, Any]:
        return _serialise(asdict(self))

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Blocker":
        return cls(
            description=data.get("description", ""),
            reason=data.get("reason", ""),
            attempted=data.get("attempted", ""),
            remaining=data.get("remaining", ""),
            user_action_required=data.get("user_action_required", ""),
            raised_at=data.get("raised_at", now_iso()),
        )


@dataclass
class Opportunity:
    """The unit of work. Everything downstream keys off this record."""

    id: str
    name: str
    type: str = "hackathon"
    organizer: str | None = None
    status: OpportunityStatus = OpportunityStatus.DISCOVERED
    registration_date: date | None = None
    timeline: Timeline = field(default_factory=Timeline)
    tracks: list[str] = field(default_factory=list)
    technologies: list[str] = field(default_factory=list)
    deliverables: list[str] = field(default_factory=list)
    judging_criteria: list[str] = field(default_factory=list)
    sources: list[SourceRef] = field(default_factory=list)
    execution_status: ExecutionStatus = ExecutionStatus.DISCOVERED
    research: list[ResearchFinding] = field(default_factory=list)
    candidates: list[CandidateIdea] = field(default_factory=list)
    selected_solution: str | None = None
    strategy_status: str = "pending"
    build_completion: int = 0
    testing_status: VerificationLevel = VerificationLevel.PLANNED
    submission_status: str = "not_started"
    project_directory: str | None = None
    blockers: list[Blocker] = field(default_factory=list)
    provenance: Provenance = field(default_factory=Provenance)
    registration_evidence: EvidenceRef | None = None
    registration_evidence_history: list[EvidenceRef] = field(default_factory=list)
    created_at: str = field(default_factory=now_iso)
    updated_at: str = field(default_factory=now_iso)
    last_verified: str | None = None

    def __post_init__(self) -> None:
        # Records arrive from email extraction, JSON state files, and CLI flags.
        # Coerce once here so every consumer can rely on the enum type.
        self.status = OpportunityStatus(self.status)
        self.execution_status = ExecutionStatus(self.execution_status)
        self.testing_status = VerificationLevel(self.testing_status)

    @property
    def deadline(self) -> date | None:
        return self.timeline.submission_deadline

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "type": self.type,
            "organizer": self.organizer,
            "status": self.status.value,
            "registration": {
                "status": self.status.value,
                "date": self.registration_date.isoformat()
                if self.registration_date
                else "UNKNOWN",
            },
            "timeline": self.timeline.to_dict(),
            "requirements": {
                "tracks": list(self.tracks),
                "technologies": list(self.technologies),
                "deliverables": list(self.deliverables),
                "judging_criteria": list(self.judging_criteria),
            },
            "sources": [source.to_dict() for source in self.sources],
            "execution": {
                "status": self.execution_status.value,
                "project_directory": self.project_directory or "UNKNOWN",
                "strategy_status": self.strategy_status,
                "build_status": "in_progress"
                if self.build_completion
                else "pending",
                "build_completion": self.build_completion,
                "testing_status": self.testing_status.value,
                "submission_status": self.submission_status,
            },
            "research": [finding.to_dict() for finding in self.research],
            "candidates": [candidate.to_dict() for candidate in self.candidates],
            "selected_solution": self.selected_solution,
            "blockers": [blocker.to_dict() for blocker in self.blockers],
            "provenance": self.provenance.to_dict(),
            "registration_evidence": (
                self.registration_evidence.to_dict()
                if self.registration_evidence
                else None
            ),
            "registration_evidence_history": [
                item.to_dict() for item in self.registration_evidence_history
            ],
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "last_verified": self.last_verified,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Opportunity":
        registration = data.get("registration", {})
        requirements = data.get("requirements", {})
        execution = data.get("execution", {})

        registration_date = None
        raw_registration_date = registration.get("date")
        if raw_registration_date and raw_registration_date != "UNKNOWN":
            try:
                registration_date = date.fromisoformat(str(raw_registration_date))
            except ValueError:
                registration_date = None

        raw_project_dir = execution.get("project_directory")
        if raw_project_dir == "UNKNOWN":
            raw_project_dir = None

        return cls(
            id=data["id"],
            name=data["name"],
            type=data.get("type", "hackathon"),
            organizer=data.get("organizer"),
            status=OpportunityStatus(data.get("status", "discovered")),
            registration_date=registration_date,
            timeline=Timeline.from_dict(data.get("timeline")),
            tracks=list(requirements.get("tracks", [])),
            technologies=list(requirements.get("technologies", [])),
            deliverables=list(requirements.get("deliverables", [])),
            judging_criteria=list(requirements.get("judging_criteria", [])),
            sources=[SourceRef.from_dict(item) for item in data.get("sources", [])],
            execution_status=ExecutionStatus(execution.get("status", "discovered")),
            research=[ResearchFinding.from_dict(item) for item in data.get("research", [])],
            candidates=[
                CandidateIdea.from_dict(item) for item in data.get("candidates", [])
            ],
            selected_solution=data.get("selected_solution"),
            # Written at the top level, but older records nested it under execution.
            strategy_status=data.get("strategy_status")
            or execution.get("strategy_status")
            or "pending",
            build_completion=int(execution.get("build_completion", 0)),
            testing_status=VerificationLevel(execution.get("testing_status", "planned")),
            submission_status=execution.get("submission_status", "not_started"),
            project_directory=raw_project_dir,
            blockers=[Blocker.from_dict(item) for item in data.get("blockers", [])],
            provenance=Provenance.from_dict(data.get("provenance", {}) or {}),
            registration_evidence=(
                EvidenceRef.from_dict(data["registration_evidence"])
                if data.get("registration_evidence")
                else None
            ),
            registration_evidence_history=[
                EvidenceRef.from_dict(item)
                for item in data.get("registration_evidence_history", [])
            ],
            created_at=data.get("created_at", now_iso()),
            updated_at=data.get("updated_at", now_iso()),
            last_verified=data.get("last_verified"),
        )

    def add_source(self, source: SourceRef) -> None:
        """Merge a source by reference, preferring the verified copy.

        Research re-reads pages the detector already recorded; without this the
        state file accumulates duplicate rows for the same URL.
        """

        for index, existing in enumerate(self.sources):
            if existing.reference != source.reference:
                continue
            merged = SourceRef(
                kind=source.kind or existing.kind,
                reference=source.reference,
                retrieved_at=source.retrieved_at or existing.retrieved_at,
                verified=existing.verified or source.verified,
            )
            self.sources[index] = merged
            return
        self.sources.append(source)

    def touch(self) -> None:
        self.updated_at = now_iso()

    def mark_verified(self) -> None:
        self.last_verified = now_iso()
        self.touch()


def dumps(obj: Any) -> str:
    return json.dumps(_serialise(obj), indent=2, ensure_ascii=False)


def loads(raw: str) -> Any:
    return json.loads(raw)