"""CLI wiring: parser construction and end-to-end command behaviour."""

from __future__ import annotations

from datetime import date

import pytest

from hackforge.cli.main import build_parser, main
from hackforge.config import Config
from hackforge.core.models import (
    ExecutionStatus,
    Opportunity,
    OpportunityStatus,
    SourceRef,
    Timeline,
)


@pytest.fixture
def env(tmp_path, monkeypatch):
    # Run outside the repo so main()'s load_dotenv() cannot re-inject the developer's
    # real .env (which points GMAIL_CREDENTIALS_PATH at a live credentials file).
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("HACKFORGE_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("HACKFORGE_WORKSPACE_ROOT", str(tmp_path / "ws"))
    monkeypatch.setenv("HACKFORGE_AUTONOMY_LEVEL", "0")
    monkeypatch.delenv("HACKFORGE_DATABASE_URL", raising=False)
    monkeypatch.delenv("GMAIL_CREDENTIALS_PATH", raising=False)
    monkeypatch.delenv("GMAIL_TOKEN_PATH", raising=False)
    monkeypatch.delenv("HACKFORGE_CONFIG_FILE", raising=False)
    return tmp_path


class TestParser:
    def test_builds(self):
        parser = build_parser()
        assert parser.prog == "hackforge"

    @pytest.mark.parametrize(
        "command",
        ["scan", "list", "research", "strategize", "approve", "workspace", "report", "status"],
    )
    def test_registers_command(self, command):
        args = build_parser().parse_args([command, "op-1"] if command not in {"scan", "list", "status"} else [command])
        assert args.command == command

    def test_research_requires_target(self):
        with pytest.raises(SystemExit):
            build_parser().parse_args(["research"])


class TestCommands:
    def test_status_runs(self, env, capsys):
        assert main(["status"]) == 0
        assert "autonomy level" in capsys.readouterr().out

    def test_list_on_empty_registry(self, env, capsys):
        assert main(["list"]) == 0
        assert "empty" in capsys.readouterr().out.lower()

    def test_scan_reports_blocked_without_credentials(self, env, capsys):
        exit_code = main(["scan"])
        captured = capsys.readouterr()

        assert exit_code == 2
        assert "GMAIL READ: FAILED" in captured.err
        assert "No inbox data was read" in captured.err
        assert "did not scan Gmail" in captured.err

    def test_scan_never_claims_success_when_unconfigured(self, env, capsys):
        main(["scan"])
        captured = capsys.readouterr()

        assert "SUCCESS" not in captured.err
        assert "did not scan Gmail" in captured.err
        assert "No opportunity was discovered" in captured.err

    def test_status_reports_gmail_not_attempted(self, env, capsys):
        assert main(["status"]) == 0

    def _seed(self, **overrides):
        from hackforge.config import load_config
        from hackforge.core.registry import OpportunityRegistry
        from hackforge.core.store import build_store

        config = load_config()
        config.ensure_dirs()
        registry = OpportunityRegistry(build_store(config))

        defaults = dict(
            id="op-1",
            name="Global AI Hackathon",
            organizer="Global Org",
            status=OpportunityStatus.REGISTERED,
            timeline=Timeline(submission_deadline=date(2026, 4, 18)),
            sources=[
                SourceRef(kind="official_website", reference="https://example.org")
            ],
        )
        defaults.update(overrides)

        opportunity = registry.register(Opportunity(**defaults))
        return opportunity

    def test_report_renders_daily(self, env, capsys):
        self._seed()
        assert main(["report"]) == 0
        out = capsys.readouterr().out

        assert "DAILY REPORT" in out
        assert "Global AI Hackathon" in out

    def test_report_flags_unknown_deadline(self, env, capsys):
        self._seed(timeline=Timeline())
        assert main(["report"]) == 0

        assert "deadline UNKNOWN" in capsys.readouterr().out

    def test_completion_report_says_not_submitted(self, env, capsys):
        self._seed()
        assert main(["report", "op-1"]) == 0
        out = capsys.readouterr().out

        assert "COMPLETION REPORT" in out
        assert "NOT STARTED" in out.upper()
        assert "does not submit autonomously" in out

    def test_strategize_refuses_without_research(self, env, capsys):
        self._seed()
        assert main(["strategize", "op-1"]) == 0

        out = capsys.readouterr().out
        assert "NO RECOMMENDATION" in out
        assert "fabrication" in out
        assert "Run research before" in out

    def test_workspace_refused_at_autonomy_zero(self, env, capsys):
        self._seed(strategy_status="approved")
        exit_code = main(["workspace", "op-1"])

        assert exit_code == 2
        assert "BLOCKED" in capsys.readouterr().err

    def test_workspace_refused_without_approval(self, env, capsys, monkeypatch):
        monkeypatch.setenv("HACKFORGE_AUTONOMY_LEVEL", "1")
        self._seed()
        exit_code = main(["workspace", "op-1"])

        assert exit_code == 2
        assert "not approved" in capsys.readouterr().err

    def test_workspace_created_after_approval(self, env, capsys, monkeypatch):
        monkeypatch.setenv("HACKFORGE_AUTONOMY_LEVEL", "1")
        self._seed(strategy_status="approved")
        assert main(["workspace", "op-1"]) == 0

        out = capsys.readouterr().out
        assert "Workspace created" in out
        workspace = env / "ws" / "Global-AI-Hackathon"
        assert (workspace / "HACKFORGE_STATE.md").is_file()

    def test_approve_sets_approved_status(self, env, capsys):
        self._seed()
        assert main(["approve", "op-1", "--select", "On-device tool"]) == 0

        assert "Approved" in capsys.readouterr().out

        from hackforge.config import load_config
        from hackforge.core.registry import OpportunityRegistry
        from hackforge.core.store import build_store

        registry = OpportunityRegistry(build_store(load_config()))
        assert registry.get("op-1").strategy_status == "approved"
        assert registry.get("op-1").execution_status is ExecutionStatus.APPROVED

    def test_approve_without_selection_lists_candidates(self, env, capsys):
        self._seed()
        exit_code = main(["approve", "op-1"])

        assert exit_code == 2
        assert "No candidates exist yet" in capsys.readouterr().err

    def test_unknown_opportunity_exits_cleanly(self, env, capsys):
        with pytest.raises(SystemExit, match="No opportunity matched"):
            main(["research", "does-not-exist"])