"""Approval gate behaviour (specification section 7 of Phase 4.5).

The human is the decision-maker. These tests assert that nothing proceeds
without an explicit decision, and that approval survives a process restart.
"""

from __future__ import annotations

import json
from datetime import date

import pytest

from hackforge.cli.main import main
from hackforge.core.models import (
    ExecutionStatus,
    Opportunity,
    OpportunityStatus,
    SourceRef,
    Timeline,
)
from hackforge.core.registry import OpportunityRegistry
from hackforge.core.store import build_store
from hackforge.engines.approval import ApprovalStore, Decision


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("HACKFORGE_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("HACKFORGE_WORKSPACE_ROOT", str(tmp_path / "ws"))
    monkeypatch.setenv("HACKFORGE_AUTONOMY_LEVEL", "0")
    monkeypatch.delenv("HACKFORGE_DATABASE_URL", raising=False)
    monkeypatch.delenv("GMAIL_CREDENTIALS_PATH", raising=False)
    monkeypatch.delenv("HACKFORGE_CONFIG_FILE", raising=False)
    return tmp_path


def _seed(**overrides):
    from hackforge.config import load_config

    config = load_config()
    config.ensure_dirs()
    defaults = dict(
        id="op-1",
        name="Global AI Hackathon 2026",
        organizer="Global Org",
        status=OpportunityStatus.REGISTERED,
        timeline=Timeline(submission_deadline=date(2026, 12, 1)),
        sources=[SourceRef(kind="official_website", reference="https://example.org")],
    )
    defaults.update(overrides)
    return OpportunityRegistry(build_store(config)).register(Opportunity(**defaults))


class TestValidateDoesNotApprove:
    def test_validate_never_creates_a_workspace(self, env, capsys):
        """Running validation must not have any side effect on approval state."""
        _seed()
        main(["validate", "op-1"])
        capsys.readouterr()

        from hackforge.config import load_config
        from hackforge.core.registry import OpportunityRegistry

        config = load_config()
        opportunity = OpportunityRegistry(build_store(config)).get("op-1")

        assert opportunity.strategy_status != "approved"
        assert opportunity.project_directory is None

    def test_validate_prints_approval_required(self, env, capsys):
        _seed()
        main(["validate", "op-1"])
        out = capsys.readouterr().out

        assert "APPROVAL REQUIRED" in out
        assert "WILL STOP HERE" in out

    def test_no_recommendation_branch_states_approval_cannot_be_given(self, env, capsys):
        _seed()
        main(["validate", "op-1"])
        out = capsys.readouterr().out

        assert "Recommendation:   NONE" in out
        assert "Approval is NOT required" in out
        # No approve command is offered because there is nothing to approve.
        assert "hackforge approve" not in out

    def test_recommendation_branch_warns_silence_is_not_approval(self, env, capsys):
        """With a recommendation present, the gate must state silence != approval."""
        from unittest.mock import patch

        import importlib

        from hackforge.config import load_config
        from hackforge.engines.research import ResearchEngine

        # `hackforge.cli.__init__` re-exports the `main` function, which shadows
        # the submodule name. Load the module explicitly.
        cli_main = importlib.import_module("hackforge.cli.main")

        # Seed once, then attach verified research to that same record.
        opportunity = _seed()
        result = ResearchEngine(
            fetcher=lambda url: (
                "<html><body>"
                "<p>The problem: clinics cannot afford diagnostic screening at scale.</p>"
                "<p>Judging criteria are impact, innovation and technical execution.</p>"
                "<p>Use the sponsor API and a web frontend for the submission.</p>"
                "<p>The intended user is a community health worker without a specialist.</p>"
                "<p>Previous winners built triage tools for health workers.</p>"
                "</body></html>"
            )
        ).research(opportunity)
        assert len(result.verified_questions()) >= 2, "fixture must verify enough questions"

        opportunity.research = result.findings
        OpportunityRegistry(build_store(load_config())).save(opportunity)

        # `validate` re-runs research itself, so the fetch must be stubbed or the
        # live request would fail and correctly suppress the recommendation.
        page = (
            "<html><body>"
            "<p>The problem: clinics cannot afford diagnostic screening at scale.</p>"
            "<p>Judging criteria are impact, innovation and technical execution.</p>"
            "<p>Use the sponsor API and a web frontend for the submission.</p>"
            "<p>The intended user is a community health worker without a specialist.</p>"
            "<p>Previous winners built triage tools for health workers.</p>"
            "</body></html>"
        )
        with patch.object(cli_main, "ResearchEngine") as engine_cls:
            engine_cls.return_value.research.return_value = result
            main(["validate", "op-1"])

        out = capsys.readouterr().out

        assert "APPROVAL REQUIRED" in out
        assert "does NOT count" in out
        assert "hackforge approve" in out

    def test_validate_does_not_approve_without_evidence(self, env, capsys):
        """No recommendation means approval cannot even be requested."""
        _seed()
        main(["validate", "op-1"])
        out = capsys.readouterr().out

        assert "Recommendation:   NONE" in out
        assert "cannot be given" in out

    def test_validate_outputs_all_fourteen_sections(self, env, capsys):
        _seed()
        main(["validate", "op-1"])
        out = capsys.readouterr().out

        for number in range(1, 15):
            assert f"## {number}." in out

    def test_validate_can_write_report_to_file(self, env, tmp_path, capsys):
        _seed()
        destination = tmp_path / "report.md"
        main(["validate", "op-1", "--output", str(destination)])

        assert destination.is_file()
        assert "## 14." in destination.read_text(encoding="utf-8")


class TestExplicitApprovalRequired:
    def test_workspace_refused_without_approval(self, env, capsys, monkeypatch):
        monkeypatch.setenv("HACKFORGE_AUTONOMY_LEVEL", "1")
        _seed()

        exit_code = main(["workspace", "op-1"])

        assert exit_code == 2
        assert "not approved" in capsys.readouterr().err

    def test_workspace_refused_at_autonomy_zero(self, env, capsys):
        _seed(strategy_status="approved")

        exit_code = main(["workspace", "op-1"])

        assert exit_code == 2
        assert "autonomy level 1" in capsys.readouterr().err

    def test_reject_prevents_workspace(self, env, capsys, monkeypatch):
        monkeypatch.setenv("HACKFORGE_AUTONOMY_LEVEL", "1")
        _seed()
        main(["approve", "op-1", "--decision", "rejected", "--notes", "not viable"])
        capsys.readouterr()

        exit_code = main(["workspace", "op-1"])

        assert exit_code == 2

    def test_more_research_prevents_workspace(self, env, capsys, monkeypatch):
        monkeypatch.setenv("HACKFORGE_AUTONOMY_LEVEL", "1")
        _seed()
        main(["approve", "op-1", "--decision", "more_research", "--notes", "need rules"])
        capsys.readouterr()

        assert main(["workspace", "op-1"]) == 2


class TestApprovalPersistence:
    """Specifically guards the strategy_status regression."""

    def test_approval_survives_reload(self, env):
        from hackforge.config import load_config

        _seed()
        main(["approve", "op-1", "--select", "On-device tool"])

        config = load_config()
        reloaded = OpportunityRegistry(build_store(config)).get("op-1")

        assert reloaded.strategy_status == "approved"
        assert reloaded.selected_solution == "On-device tool"
        assert reloaded.execution_status is ExecutionStatus.APPROVED

    def test_approval_survives_multiple_reloads(self, env):
        from hackforge.config import load_config

        _seed()
        main(["approve", "op-1", "--select", "On-device tool"])

        for _ in range(3):
            config = load_config()
            assert (
                OpportunityRegistry(build_store(config)).get("op-1").strategy_status
                == "approved"
            )

    def test_rejected_survives_reload(self, env):
        from hackforge.config import load_config

        _seed()
        main(["approve", "op-1", "--decision", "rejected", "--notes", "no"])

        config = load_config()
        assert (
            OpportunityRegistry(build_store(config)).get("op-1").strategy_status
            == "rejected"
        )

    def test_decision_history_persists_across_processes(self, env):
        from hackforge.config import load_config

        _seed()
        main(["approve", "op-1", "--decision", "rejected", "--notes", "first"])
        main(["approve", "op-1", "--select", "Second idea", "--notes", "second"])

        store = ApprovalStore(load_config().data_dir / "approvals.json")
        record = store.get("op-1")

        assert len(record.history) == 2
        assert record.history[0]["decision"] == Decision.REJECTED.value
        assert record.history[1]["decision"] == Decision.APPROVED.value

    def test_workspace_created_after_approval(self, env, monkeypatch):
        monkeypatch.setenv("HACKFORGE_AUTONOMY_LEVEL", "1")
        _seed()
        main(["approve", "op-1", "--select", "On-device tool"])

        assert main(["workspace", "op-1"]) == 0

        state = (
            env / "ws" / "Global-AI-Hackathon-2026" / "HACKFORGE_STATE.md"
        ).read_text(encoding="utf-8")

        assert "strategy_status: approved" in state
        assert "selected_solution: On-device tool" in state

    def test_workspace_command_is_not_idempotent_by_accident(self, env, monkeypatch):
        """Second creation must refuse rather than silently succeed."""
        monkeypatch.setenv("HACKFORGE_AUTONOMY_LEVEL", "1")
        _seed()
        main(["approve", "op-1", "--select", "On-device tool"])

        assert main(["workspace", "op-1"]) == 0
        assert main(["workspace", "op-1"]) == 2


class TestApprovalRequiresSelection:
    def test_approve_without_candidates_is_refused(self, env, capsys):
        _seed()

        exit_code = main(["approve", "op-1"])

        assert exit_code == 2
        assert "No candidates exist yet" in capsys.readouterr().err

    def test_approval_records_no_silent_default(self, env):
        """An approval without an explicit selection must not invent one."""
        from hackforge.config import load_config
        from hackforge.core.models import CandidateIdea

        config = load_config()
        config.ensure_dirs()
        registry = OpportunityRegistry(build_store(config))
        registry.register(
            Opportunity(
                id="op-1",
                name="Global AI Hackathon 2026",
                candidates=[
                    CandidateIdea(name="Idea A", total_score=70.0),
                    CandidateIdea(name="Idea B", total_score=60.0),
                ],
            )
        )

        exit_code = main(["approve", "op-1"])

        assert exit_code == 2
        assert registry.get("op-1").strategy_status != "approved"