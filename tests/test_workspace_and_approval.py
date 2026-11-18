"""Workspace scaffolding, autonomy enforcement, and the approval gate."""

from __future__ import annotations

from datetime import date
from pathlib import Path

import pytest

from hackforge.core.models import (
    ExecutionStatus,
    Opportunity,
    SourceRef,
    Timeline,
)
from hackforge.engines.approval import ApprovalGate, ApprovalStore, Decision
from hackforge.engines.workspace import (
    STATE_FILE,
    WorkspaceError,
    WorkspaceManager,
)


def _approved_opportunity() -> Opportunity:
    return Opportunity(
        id="op-1",
        name="Global AI Hackathon",
        organizer="Global Org",
        status="registered",
        timeline=Timeline(submission_deadline=date(2026, 4, 18)),
        sources=[SourceRef(kind="official_website", reference="https://example.org")],
    )


class TestWorkspaceManager:
    def test_refuses_at_autonomy_zero(self, tmp_path):
        opportunity = _approved_opportunity()
        manager = WorkspaceManager(tmp_path, autonomy_level=0)

        with pytest.raises(WorkspaceError, match="autonomy level 1"):
            manager.create(opportunity)

        assert opportunity.project_directory is None

    def test_creates_full_structure(self, tmp_path):
        opportunity = _approved_opportunity()
        manager = WorkspaceManager(tmp_path, autonomy_level=1)

        target = manager.create(opportunity)

        for name in ["00_SOURCE", "01_RESEARCH", "02_STRATEGY", "03_BUILD", "04_TESTING", "05_SUBMISSION"]:
            assert (target / name).is_dir()
        assert (target / STATE_FILE).is_file()
        assert (target / "05_SUBMISSION" / "README.md").is_file()
        assert (target / "00_SOURCE" / "source-links.md").is_file()

    def test_links_project_directory(self, tmp_path):
        opportunity = _approved_opportunity()

        target = WorkspaceManager(tmp_path, autonomy_level=1).create(opportunity)

        assert opportunity.project_directory == str(target)

    def test_state_file_records_its_own_directory(self, tmp_path):
        """Regression: the state file used to record project_directory: UNKNOWN."""
        opportunity = _approved_opportunity()

        target = WorkspaceManager(tmp_path, autonomy_level=1).create(opportunity)
        state = (target / STATE_FILE).read_text(encoding="utf-8")

        assert f"project_directory: {target}" in state
        assert "project_directory: UNKNOWN" not in state

    def test_state_file_contains_unknown_for_missing_dates(self, tmp_path):
        opportunity = _approved_opportunity()

        target = WorkspaceManager(tmp_path, autonomy_level=1).create(opportunity)
        state = (target / STATE_FILE).read_text(encoding="utf-8")

        assert "submission_deadline: 2026-04-18" in state
        assert "registration_deadline: UNKNOWN" in state
        assert "event_date: UNKNOWN" in state
        # project_directory is known at creation time now.
        assert "project_directory: UNKNOWN" not in state
        assert "```yaml" in state

    def test_refuses_to_overwrite_existing_work(self, tmp_path):
        opportunity = _approved_opportunity()
        manager = WorkspaceManager(tmp_path, autonomy_level=1)
        manager.create(opportunity)

        with pytest.raises(WorkspaceError, match="already exists"):
            manager.create(_approved_opportunity())

    def test_write_document_confined_to_workspace(self, tmp_path):
        opportunity = _approved_opportunity()
        manager = WorkspaceManager(tmp_path, autonomy_level=1)
        manager.create(opportunity)

        with pytest.raises(WorkspaceError, match="outside the workspace"):
            manager.write_document(opportunity, "../../escaped.md", "nope")

    def test_write_document_creates_parents(self, tmp_path):
        opportunity = _approved_opportunity()
        manager = WorkspaceManager(tmp_path, autonomy_level=1)
        manager.create(opportunity)

        path = manager.write_document(
            opportunity, "02_STRATEGY/build-plan.md", "# Plan\n"
        )

        assert path.is_file()
        assert "Plan" in path.read_text(encoding="utf-8")

    def test_write_state_requires_workspace(self, tmp_path):
        with pytest.raises(WorkspaceError, match="No workspace exists"):
            WorkspaceManager(tmp_path, autonomy_level=1).write_state(_approved_opportunity())

    def test_write_state_updates_existing_file(self, tmp_path):
        opportunity = _approved_opportunity()
        manager = WorkspaceManager(tmp_path, autonomy_level=1)
        manager.create(opportunity)

        opportunity.build_completion = 55
        manager.write_state(opportunity)
        state = (Path(tmp_path) / "Global-AI-Hackathon" / STATE_FILE).read_text(
            encoding="utf-8"
        )

        assert "build_completion: 55" in state

    def test_state_file_starts_with_newline_after_fence(self, tmp_path):
        opportunity = _approved_opportunity()
        target = WorkspaceManager(tmp_path, autonomy_level=1).create(opportunity)
        state = (target / STATE_FILE).read_text(encoding="utf-8")

        assert "```yaml\nid:" in state


class TestApprovalGate:
    def _gate(self, tmp_path) -> ApprovalGate:
        return ApprovalGate(ApprovalStore(tmp_path / "approvals.json"))

    def test_approve_sets_strategy_status(self, tmp_path):
        opportunity = _approved_opportunity()

        record = self._gate(tmp_path).approve(opportunity, "On-device triage tool")

        assert record.is_approved
        assert opportunity.strategy_status == "approved"
        assert opportunity.selected_solution == "On-device triage tool"
        assert opportunity.execution_status is ExecutionStatus.APPROVED

    def test_approve_with_workspace_moves_to_building(self, tmp_path):
        opportunity = _approved_opportunity()
        opportunity.project_directory = str(tmp_path / "ws")

        self._gate(tmp_path).approve(opportunity, "Idea")

        assert opportunity.execution_status is ExecutionStatus.BUILDING

    def test_reject_does_not_start_build(self, tmp_path):
        opportunity = _approved_opportunity()

        self._gate(tmp_path).reject(opportunity, "Not feasible")

        assert opportunity.strategy_status == "rejected"
        assert opportunity.execution_status is not ExecutionStatus.BUILDING

    def test_more_research_returns_to_researching(self, tmp_path):
        opportunity = _approved_opportunity()

        self._gate(tmp_path).request_more_research(opportunity, "Need judging criteria")

        assert opportunity.execution_status is ExecutionStatus.RESEARCHING

    def test_decision_history_persists(self, tmp_path):
        gate = self._gate(tmp_path)
        opportunity = _approved_opportunity()

        gate.reject(opportunity, "first")
        record = gate.approve(opportunity, "Second idea")

        assert len(record.history) == 2
        assert record.history[0]["decision"] == Decision.REJECTED.value
        assert record.history[1]["decision"] == Decision.APPROVED.value

    def test_history_survives_new_store_instance(self, tmp_path):
        self._gate(tmp_path).approve(_approved_opportunity(), "Idea")
        reopened = self._gate(tmp_path).status("op-1")

        assert reopened.is_approved
        assert reopened.selected_solution == "Idea"