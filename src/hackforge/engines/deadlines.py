"""Deadline and accountability engines (specification sections 19 and 20).

Urgency is derived from the *verified* submission deadline. When no deadline
was confirmed the result is UNKNOWN, never a default band - a false "CRITICAL"
would be worse than an honest blank.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone

from ..config import DeadlineThresholds
from ..core.models import Opportunity, Urgency


@dataclass
class UrgencyAssessment:
    urgency: Urgency
    days_remaining: int | None
    hours_remaining: int | None
    deadline: date | None
    reason: str

    @property
    def has_verified_deadline(self) -> bool:
        return self.deadline is not None


class DeadlineEngine:
    def __init__(self, thresholds: DeadlineThresholds | None = None) -> None:
        self._t = thresholds or DeadlineThresholds()

    def assess(
        self, opportunity: Opportunity, today: date | None = None
    ) -> UrgencyAssessment:
        today = today or date.today()
        deadline = opportunity.timeline.submission_deadline

        if deadline is None:
            return UrgencyAssessment(
                urgency=Urgency.UNKNOWN,
                days_remaining=None,
                hours_remaining=None,
                deadline=None,
                reason=(
                    "Submission deadline is UNKNOWN - not extracted from any verified "
                    "source. Urgency cannot be calculated."
                ),
            )

        remaining = deadline - today
        days = remaining.days
        hours = int(remaining.total_seconds() // 3600)

        if remaining.total_seconds() < 0:
            return UrgencyAssessment(
                Urgency.EXPIRED,
                days,
                hours,
                deadline,
                f"Submission deadline passed {abs(days)} day(s) ago.",
            )

        reason = f"{days} day(s) remain."
        # Bands from specification section 19, checked most-urgent first.
        # Hour bands use strict '<': exactly 72h remaining is still URGENT.
        if hours < self._t.final_window_hours:
            return UrgencyAssessment(
                Urgency.FINAL_WINDOW, days, hours, deadline, f"{hours} hour(s) remain."
            )
        if hours < self._t.critical_hours:
            return UrgencyAssessment(
                Urgency.CRITICAL, days, hours, deadline, f"{hours} hour(s) remain."
            )
        if days <= self._t.active_days:          # 3-7 days
            return UrgencyAssessment(Urgency.URGENT, days, hours, deadline, reason)
        if days <= self._t.planning_days:        # 7-14 days
            return UrgencyAssessment(Urgency.ACTIVE, days, hours, deadline, reason)
        if days <= self._t.long_range_days:      # 14-30 days
            return UrgencyAssessment(Urgency.PLANNING, days, hours, deadline, reason)
        return UrgencyAssessment(                 # > 30 days
            Urgency.LONG_RANGE, days, hours, deadline, reason
        )


@dataclass
class RiskReport:
    risk_level: str
    reasons: list[str]
    assessment: UrgencyAssessment
    inactive_days: int | None
    next_action: str


class AccountabilityEngine:
    """Detects inactivity and reports risk rather than sending reminders."""

    def __init__(
        self,
        deadline_engine: DeadlineEngine | None = None,
        inactivity_threshold_days: int = 2,
    ) -> None:
        self._deadlines = deadline_engine or DeadlineEngine()
        self._threshold = inactivity_threshold_days

    def assess(
        self,
        opportunity: Opportunity,
        last_activity: datetime | None = None,
        today: date | None = None,
    ) -> RiskReport:
        today = today or date.today()
        urgency = self._deadlines.assess(opportunity, today)

        inactive_days: int | None = None
        if last_activity is not None:
            if last_activity.tzinfo is None:
                last_activity = last_activity.replace(tzinfo=timezone.utc)
            inactive_days = (today - last_activity.astimezone(timezone.utc).date()).days

        reasons: list[str] = []
        if urgency.urgency == Urgency.UNKNOWN:
            reasons.append("Submission deadline UNKNOWN - urgency cannot be assessed.")

        if inactive_days is None:
            reasons.append(
                "No recorded activity in the activity log. This is an absence of "
                "evidence, not evidence of no work - confirm manually."
            )
        elif inactive_days >= self._threshold:
            reasons.append(
                f"No recorded activity for {inactive_days} day(s) "
                f"(threshold {self._threshold})."
            )

        if opportunity.strategy_status == "pending" and opportunity.research:
            reasons.append("Research exists but no strategy decision has been made.")
        if opportunity.strategy_status == "approved" and not opportunity.project_directory:
            reasons.append(
                "Strategy approved but no project workspace exists - build has not started."
            )
        if opportunity.blockers:
            reasons.append(
                f"{len(opportunity.blockers)} open blocker(s): "
                + "; ".join(blocker.description for blocker in opportunity.blockers)
            )

        risk = self._risk_level(urgency, inactive_days, opportunity, reasons)
        return RiskReport(
            risk_level=risk,
            reasons=reasons,
            assessment=urgency,
            inactive_days=inactive_days,
            next_action=self._next_action(opportunity, urgency, inactive_days),
        )

    def _risk_level(
        self,
        urgency: UrgencyAssessment,
        inactive_days: int | None,
        opportunity: Opportunity,
        reasons: list[str],
    ) -> str:
        if urgency.urgency in {Urgency.CRITICAL, Urgency.FINAL_WINDOW, Urgency.EXPIRED}:
            return "HIGH"
        if urgency.urgency == Urgency.UNKNOWN and reasons:
            return "MEDIUM"
        if inactive_days is not None and inactive_days >= self._threshold:
            return "MEDIUM" if urgency.urgency == Urgency.ACTIVE else "HIGH"
        if opportunity.strategy_status == "approved" and not opportunity.project_directory:
            return "MEDIUM"
        return "LOW"

    @staticmethod
    def _next_action(
        opportunity: Opportunity,
        urgency: UrgencyAssessment,
        inactive_days: int | None,
    ) -> str:
        if opportunity.blockers:
            return f"Resolve blocker: {opportunity.blockers[0].description}"
        if not opportunity.research:
            return "Begin research."
        if opportunity.strategy_status == "pending":
            return "Review the strategy recommendation and approve a direction."
        if opportunity.strategy_status == "more_research_requested":
            return "Perform the additional research that was requested."
        if opportunity.strategy_status == "approved" and not opportunity.project_directory:
            return "Create the project workspace and begin the MVP."
        if urgency.urgency in {Urgency.CRITICAL, Urgency.FINAL_WINDOW}:
            return "Prepare submission materials immediately."
        if inactive_days is not None and inactive_days >= 2:
            return "Resume work - the project has been idle."
        return "Continue implementation toward the verified deadline."