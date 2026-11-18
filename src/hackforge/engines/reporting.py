"""Reporting engine (specification sections 21 and 22).

Renders the daily opportunity report and the completion report. Reports state
what is verified and label the rest UNVERIFIED - they never smooth over gaps.
"""

from __future__ import annotations

from datetime import date, datetime

from ..core.models import Opportunity, Urgency, VerificationLevel
from .deadlines import AccountabilityEngine, DeadlineEngine

RULE = "-" * 60


class ReportRenderer:
    def __init__(
        self,
        deadline_engine: DeadlineEngine | None = None,
        accountability: AccountabilityEngine | None = None,
    ) -> None:
        self._deadlines = deadline_engine or DeadlineEngine()
        self._accountability = accountability or AccountabilityEngine(self._deadlines)

    # -- daily report ---------------------------------------------------

    def daily_report(
        self,
        opportunities: list[Opportunity],
        today: date | None = None,
        last_activity: dict[str, datetime] | None = None,
    ) -> str:
        today = today or date.today()
        last_activity = last_activity or {}

        active = [
            opportunity
            for opportunity in opportunities
            if opportunity.status.value not in {"expired", "cancelled", "completed"}
        ]
        ordered = sorted(
            active,
            key=lambda item: (
                item.timeline.submission_deadline
                or date.max.replace(year=9999, month=12, day=31)
            ),
        )

        lines = [
            "HACKFORGE - DAILY REPORT",
            f"Generated: {today.isoformat()}",
            f"ACTIVE OPPORTUNITIES: {len(active)}",
            RULE,
        ]

        if not ordered:
            lines.append("No active opportunities detected.")
            return "\n".join(lines) + "\n"

        for index, opportunity in enumerate(ordered, start=1):
            lines.extend(self._opportunity_block(index, opportunity, last_activity, today))

        return "\n".join(lines) + "\n"

    def _opportunity_block(
        self,
        index: int,
        opportunity: Opportunity,
        last_activity: dict[str, datetime],
        today: date,
    ) -> list[str]:
        urgency = self._deadlines.assess(opportunity, today)
        risk = self._accountability.assess(
            opportunity, last_activity.get(opportunity.id), today
        )
        verified_research = sum(
            1
            for finding in opportunity.research
            if finding.confidence == VerificationLevel.VERIFIED
        )

        lines = [
            f"{index}. {opportunity.name}",
            f"   Type:      {opportunity.type}",
            f"   Status:    {opportunity.execution_status.value.upper()}",
            f"   Urgency:   {urgency.urgency.value}"
            + (
                f" ({urgency.days_remaining} days remaining)"
                if urgency.days_remaining is not None
                else ""
            ),
            f"   Research:  {verified_research} verified finding(s)"
            + (
                f" / {len(opportunity.research)} total"
                if opportunity.research
                else " - not started"
            ),
            f"   Strategy:  {opportunity.strategy_status.upper()}",
            f"   Build:     {opportunity.build_completion}%",
            f"   Tests:     {opportunity.testing_status.value.upper()}",
            f"   Submission: {opportunity.submission_status.replace('_', ' ').upper()}",
        ]

        if urgency.urgency == Urgency.UNKNOWN:
            lines.append("   WARNING:    Submission deadline UNKNOWN - verify it.")

        if opportunity.blockers:
            lines.append("   BLOCKER:")
            for blocker in opportunity.blockers:
                lines.append(f"     - {blocker.description}")
                if blocker.user_action_required:
                    lines.append(f"       User action required: {blocker.user_action_required}")

        if risk.reasons:
            lines.append(f"   Risk:       {risk.risk_level}")
            for reason in risk.reasons:
                lines.append(f"     - {reason}")

        lines.append(f"   NEXT ACTION: {risk.next_action}")
        lines.append(RULE)
        return lines

    # -- completion report ----------------------------------------------

    def completion_report(self, opportunity: Opportunity) -> str:
        verified = sum(
            1
            for finding in opportunity.research
            if finding.confidence == VerificationLevel.VERIFIED
        )
        rows = [
            ("Opportunity", opportunity.name),
            ("Organizer", opportunity.organizer or "UNKNOWN"),
            ("Project", opportunity.selected_solution or "NOT SELECTED"),
            (
                "Problem",
                "see 02_STRATEGY/problem.md"
                if opportunity.project_directory
                else "NOT DOCUMENTED",
            ),
            (
                "Solution",
                "see 02_STRATEGY/solution.md"
                if opportunity.project_directory
                else "NOT DOCUMENTED",
            ),
        ]
        status_rows = [
            (
                "Research",
                f"{'COMPLETE' if verified else 'INCOMPLETE'} "
                f"({verified} verified finding(s))",
            ),
            ("Strategy", opportunity.strategy_status.replace("_", " ").upper()),
            ("Implementation", f"{opportunity.build_completion}%"),
            ("Tests", opportunity.testing_status.value.upper()),
            (
                "Demo",
                "VERIFIED"
                if opportunity.testing_status == VerificationLevel.VERIFIED
                else "UNVERIFIED",
            ),
            (
                "Submission materials",
                "READY" if opportunity.submission_status == "ready" else "NOT READY",
            ),
            (
                "Submission",
                opportunity.submission_status.replace("_", " ").upper(),
            ),
        ]

        width = max(len(label) for label, _ in rows + status_rows) + 1
        lines = ["HACKFORGE COMPLETION REPORT", RULE]
        lines.extend(f"{label + ':':<{width}}{value}" for label, value in rows)
        lines.append(RULE)
        lines.extend(f"{label + ':':<{width}}{value}" for label, value in status_rows)
        lines.append(RULE)

        if opportunity.submission_status != "submitted":
            lines.append(
                "Remaining action: submission is performed by the user through the "
                "official portal. HACKFORGE does not submit autonomously."
            )
        for blocker in opportunity.blockers:
            lines.append(f"Open blocker: {blocker.description}")
        return "\n".join(lines) + "\n"