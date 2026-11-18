"""HACKFORGE engines: detection, research, strategy, approval, deadlines, reporting."""

from .approval import ApprovalGate, ApprovalStore, Decision
from .deadlines import AccountabilityEngine, DeadlineEngine, RiskReport, UrgencyAssessment
from .detection import (
    DetectionResult,
    Detector,
    FieldExtractor,
    OpportunityDetector,
)
from .reporting import ReportRenderer
from .research import ResearchEngine, ResearchResult
from .strategy import CandidateIdea, StrategyEngine, StrategyRecommendation
from .workspace import WorkspaceError, WorkspaceManager

__all__ = [
    "AccountabilityEngine",
    "ApprovalGate",
    "ApprovalStore",
    "CandidateIdea",
    "Decision",
    "DeadlineEngine",
    "DetectionResult",
    "Detector",
    "FieldExtractor",
    "OpportunityDetector",
    "ReportRenderer",
    "ResearchEngine",
    "ResearchResult",
    "RiskReport",
    "StrategyEngine",
    "StrategyRecommendation",
    "UrgencyAssessment",
    "WorkspaceError",
    "WorkspaceManager",
]