"""HACKFORGE command-line interface.

Commands map directly to the specification phases:

    scan        Phase 1 - fetch and register opportunities
    list        Show the opportunity registry
    research    Phase 2 - research an opportunity
    strategize  Phase 3 - generate candidates and recommend a direction
    approve     Record a human decision (never taken automatically)
    workspace   Phase 4 - create the project directory tree
    report      Daily report / completion report
    status      Show autonomy level and configuration
"""

from __future__ import annotations

import argparse
import sys
from datetime import date
from pathlib import Path

from ..adapters.gmail import GmailSource, GmailStatus
from ..config import Config, load_config, load_dotenv
from ..core.models import ExecutionStatus, Opportunity, VerificationLevel
from ..core.registry import OpportunityRegistry
from ..core.store import build_store
from ..engines.approval import ApprovalGate, ApprovalStore, Decision
from ..engines.deadlines import AccountabilityEngine, DeadlineEngine
from ..engines.detection import OpportunityDetector
from ..engines.reporting import ReportRenderer
from ..engines.research import ResearchEngine, ResearchResult
from ..engines.strategy import StrategyEngine
from ..engines.strategy_report import build_validation_report
from ..engines.synthesis import Synthesis, SynthesisEngine
from ..engines.workspace import WorkspaceError, WorkspaceManager


def _registry(config: Config) -> OpportunityRegistry:
    config.ensure_dirs()
    store = build_store(config)
    return OpportunityRegistry(store)


def _sync_state_file(config: Config, opportunity: Opportunity) -> None:
    """Keep HACKFORGE_STATE.md in sync with the registry after a change.

    The state file is the workspace's source of truth, so it must never lag
    behind the registry. Failure to write is reported, not silently swallowed.
    """

    if not opportunity.project_directory:
        return
    try:
        WorkspaceManager(config.workspace_root, config.autonomy_level).write_state(
            opportunity
        )
    except (WorkspaceError, OSError) as exc:
        print(f"Warning: state file not updated - {exc}", file=sys.stderr)


def _resolve(config: Config, registry: OpportunityRegistry, reference: str) -> Opportunity:
    opportunity = registry.get(reference) or registry.find_by_name(reference)
    if opportunity is None:
        raise SystemExit(
            f"No opportunity matched '{reference}'. Run `hackforge list` to see the registry."
        )
    return opportunity


# -- commands -----------------------------------------------------------


def cmd_scan(args: argparse.Namespace, config: Config) -> int:
    registry = _registry(config)
    detector = OpportunityDetector()
    source = GmailSource(config)

    outcome = source.fetch_detailed(limit=args.limit)

    if not outcome.scanned:
        # FAILED, not "scanned with zero results". Report the real cause.
        print(f"GMAIL READ: FAILED ({outcome.status.value})", file=sys.stderr)
        print(f"  Reason: {outcome.reason}", file=sys.stderr)
        print(
            "  No inbox data was read. No opportunity was discovered. "
            "HACKFORGE did not scan Gmail.",
            file=sys.stderr,
        )
        return 2

    if outcome.status is GmailStatus.OK_EMPTY:
        print("GMAIL READ: SUCCESS - 0 messages matched the configured query.")
        print(f"  {outcome.reason}")
        print("  No opportunity was discovered because no email was read.")
        return 0

    messages = outcome.messages
    print(f"GMAIL READ: SUCCESS - retrieved {len(messages)} message(s).")
    print(f"  Query: {config.gmail.query}")
    if outcome.message_ids_seen != len(messages):
        print(
            f"  Note: {outcome.message_ids_seen} message id(s) matched but only "
            f"{len(messages)} could be read"
            f" ({outcome.message_ids_unreadable} unreadable)."
        )

    detected = 0
    for message in messages:
        opportunity, result = detector.from_message(message)
        if opportunity is None:
            continue
        registry.register(opportunity)
        detected += 1
        print(
            f"  + {opportunity.name} [{opportunity.type}] "
            f"confidence={result.confidence} status={opportunity.status.value}"
        )
        for reason in result.reasons[:4]:
            print(f"      why: {reason}")

    print(f"\nRegistered {detected} opportunity record(s).")
    print("Dates that could not be verified remain UNKNOWN.")
    return 0


def cmd_list(args: argparse.Namespace, config: Config) -> int:
    registry = _registry(config)
    opportunities = registry.all()
    if not opportunities:
        print("Registry is empty. Run `hackforge scan` to detect opportunities.")
        return 0

    for opportunity in opportunities:
        deadline = opportunity.timeline.submission_deadline
        print(
            f"{opportunity.id}\n"
            f"    name      {opportunity.name}\n"
            f"    type      {opportunity.type}\n"
            f"    status    {opportunity.status.value} / {opportunity.execution_status.value}\n"
            f"    deadline  {deadline.isoformat() if deadline else 'UNKNOWN'}\n"
        )
    return 0


def cmd_research(args: argparse.Namespace, config: Config) -> int:
    registry = _registry(config)
    opportunity = _resolve(config, registry, args.opportunity)

    engine = ResearchEngine()
    result = engine.research(opportunity)

    opportunity.research = result.findings
    for source in result.retrieved:
        opportunity.add_source(source)
    if result.findings and opportunity.execution_status == ExecutionStatus.DISCOVERED:
        opportunity.execution_status = ExecutionStatus.RESEARCHING
    registry.save(opportunity)
    _sync_state_file(config, opportunity)

    print(f"Research: {result.summary()}")
    print(f"Completeness: {result.completeness}%")
    for reference, error in result.failed:
        print(f"  FAILED {reference}: {error}")
    for finding in result.findings:
        print(f"\n[{finding.confidence.value.upper()}] {finding.question}")
        for line in finding.answer.splitlines()[:6]:
            print(f"  {line}")
        if finding.evidence:
            print(f"  evidence: {', '.join(finding.evidence)}")
    return 0


def cmd_strategize(args: argparse.Namespace, config: Config) -> int:
    registry = _registry(config)
    opportunity = _resolve(config, registry, args.opportunity)

    engine = StrategyEngine()
    recommendation = engine.recommend(opportunity)

    opportunity.candidates = recommendation.candidates
    if recommendation.recommended is not None:
        opportunity.execution_status = ExecutionStatus.AWAITING_APPROVAL
        opportunity.strategy_status = "pending_approval"
    registry.save(opportunity)
    _sync_state_file(config, opportunity)

    if opportunity.project_directory:
        manager = WorkspaceManager(config.workspace_root, config.autonomy_level)
        try:
            manager.write_document(
                opportunity,
                "02_STRATEGY/idea-comparison.md",
                recommendation.to_markdown(opportunity),
            )
        except WorkspaceError as exc:
            print(f"Note: strategy document not written - {exc}")

    print(recommendation.to_markdown(opportunity))
    print("\nNo build work will start until you approve a direction.")
    print(
        f"Approve with: hackforge approve {opportunity.id} "
        f"--select \"<candidate name>\""
    )
    return 0


def cmd_approve(args: argparse.Namespace, config: Config) -> int:
    registry = _registry(config)
    opportunity = _resolve(config, registry, args.opportunity)
    gate = ApprovalGate(ApprovalStore(config.data_dir / "approvals.json"))

    if args.decision == Decision.REJECTED:
        gate.reject(opportunity, args.notes or "")
        opportunity.execution_status = ExecutionStatus.STRATEGY_PENDING
        print(f"Rejected '{opportunity.name}'.")
    elif args.decision == Decision.MORE_RESEARCH:
        gate.request_more_research(opportunity, args.notes or "")
        print(f"Marked '{opportunity.name}' for more research.")
    else:
        if not args.select and not opportunity.selected_solution:
            if not opportunity.candidates:
                print(
                    "No candidates exist yet. Run `hackforge strategize` first.",
                    file=sys.stderr,
                )
                return 2
            print("No --select given. Available candidates:", file=sys.stderr)
            for candidate in opportunity.candidates:
                print(f"  - {candidate.name} ({candidate.total_score}/100)", file=sys.stderr)
            print(
                "Re-run with --select \"<candidate name>\".", file=sys.stderr
            )
            return 2
        gate.approve(opportunity, args.select, args.notes or "")
        print(f"Approved '{opportunity.name}' -> {opportunity.execution_status.value}.")

    registry.save(opportunity)
    _sync_state_file(config, opportunity)
    return 0


def cmd_workspace(args: argparse.Namespace, config: Config) -> int:
    registry = _registry(config)
    opportunity = _resolve(config, registry, args.opportunity)

    if opportunity.strategy_status != "approved":
        print(
            "Refusing to create a workspace: strategy is not approved. "
            f"Current strategy status: {opportunity.strategy_status}",
            file=sys.stderr,
        )
        return 2

    manager = WorkspaceManager(config.workspace_root, config.autonomy_level)
    try:
        target = manager.create(opportunity)
    except WorkspaceError as exc:
        print(f"BLOCKED - {exc}", file=sys.stderr)
        return 2

    registry.save(opportunity)
    print(f"Workspace created: {target}")
    print("Structure per specification section 12.")
    print(f"State file: {target / 'HACKFORGE_STATE.md'}")
    return 0


def cmd_validate(args: argparse.Namespace, config: Config) -> int:
    """Research + synthesis + 14-section strategy report, then STOP for approval.

    This command never creates a workspace and never approves anything. It
    ends by asking for an explicit human decision.
    """

    registry = _registry(config)
    opportunity = _resolve(config, registry, args.opportunity)

    research = ResearchEngine().research(opportunity)
    opportunity.research = research.findings
    for source in research.retrieved:
        opportunity.add_source(source)

    synthesis = SynthesisEngine().synthesise(opportunity, research)
    recommendation = StrategyEngine().recommend(opportunity, research=research.findings)
    opportunity.candidates = recommendation.candidates
    if recommendation.recommended is not None:
        opportunity.execution_status = ExecutionStatus.AWAITING_APPROVAL
        opportunity.strategy_status = "pending_approval"

    report = build_validation_report(opportunity, research, synthesis, recommendation)
    markdown = report.to_markdown()

    if args.output:
        destination = Path(args.output).expanduser()
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(markdown, encoding="utf-8")
        print(f"Validation report written to {destination}")
    else:
        print(markdown)

    if opportunity.project_directory:
        try:
            manager = WorkspaceManager(config.workspace_root, config.autonomy_level)
            manager.write_document(opportunity, "01_RESEARCH/synthesis.md", synthesis.to_markdown(opportunity))
            manager.write_document(opportunity, "02_STRATEGY/validation-report.md", markdown)
            print("Research and strategy documents written into the existing workspace.")
        except WorkspaceError as exc:
            print(f"Note: documents not written - {exc}")

    registry.save(opportunity)
    _sync_state_file(config, opportunity)

    _print_approval_request(opportunity, recommendation, synthesis, research)
    return 0


def _print_approval_request(
    opportunity: Opportunity,
    recommendation: object,
    synthesis: Synthesis,
    research: ResearchResult,
) -> None:
    """Present the decision and stop. Nothing proceeds without a reply."""

    print("")
    print("=" * 60)
    print("APPROVAL REQUIRED - HACKFORGE WILL STOP HERE")
    print("=" * 60)
    print(f"Opportunity:      {opportunity.name}")
    print(f"Research:         {len(research.verified_questions())} verified, "
          f"{len(research.failed)} failed retrieval(s)")
    print(f"Synthesis:        {synthesis.verified_count} answered, "
          f"{synthesis.unknown_count} insufficient evidence")
    best = getattr(recommendation, "recommended", None)
    if best is None:
        print("Recommendation:   NONE - evidence is insufficient.")
        print("")
        print("Required next step: gather evidence, then re-run.")
        print("Approval is NOT required and cannot be given, because there is")
        print("no recommendation to approve.")
    else:
        print(f"Recommendation:   {best.name} ({best.total_score}/100)")
        print("")
        print("To accept this direction, run explicitly:")
        print(f"  hackforge approve {opportunity.id} \\")
        print(f'    --select "{best.name}"')
        print("")
        print("To reject it:")
        print(f"  hackforge approve {opportunity.id} --decision rejected --notes \"<reason>\"")
        print("")
        print("Until one of those commands is run, no build work will start.")
        print("Opening this report, or running another command, does NOT count")
        print("as approval.")
    print("=" * 60)


def cmd_report(args: argparse.Namespace, config: Config) -> int:
    registry = _registry(config)
    renderer = ReportRenderer(DeadlineEngine(config.deadlines))

    if args.opportunity:
        opportunity = _resolve(config, registry, args.opportunity)
        print(renderer.completion_report(opportunity))
    else:
        print(renderer.daily_report(registry.all(), today=date.today()))
    return 0


def cmd_status(args: argparse.Namespace, config: Config) -> int:
    print("HACKFORGE status")
    print(f"  autonomy level : {config.autonomy_level} (max {config.max_autonomy_level})")
    print(f"  data dir       : {config.data_dir}")
    print(f"  workspace root : {config.workspace_root}")
    print(f"  database       : {config.database_url or 'json store'}")

    source = GmailSource(config)
    diagnosis = source.diagnose()
    if diagnosis.status is GmailStatus.CONFIGURED:
        print("  gmail source   : configured (authentication not yet attempted)")
    else:
        print(f"  gmail source   : {diagnosis.status.value} - {diagnosis.reason}")
    return 0


# -- parser -------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="hackforge",
        description="Autonomous opportunity-to-execution agent.",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    scan = subparsers.add_parser("scan", help="Phase 1: detect opportunities from email")
    scan.add_argument("--limit", type=int, default=None)
    scan.set_defaults(func=cmd_scan)

    listing = subparsers.add_parser("list", help="List the opportunity registry")
    listing.set_defaults(func=cmd_list)

    research = subparsers.add_parser("research", help="Phase 2: research an opportunity")
    research.add_argument("opportunity")
    research.set_defaults(func=cmd_research)

    strategize = subparsers.add_parser("strategize", help="Phase 3: recommend a direction")
    strategize.add_argument("opportunity")
    strategize.set_defaults(func=cmd_strategize)

    validate = subparsers.add_parser(
        "validate",
        help="Phase 4.5: research + synthesis + 14-section report, then stop",
    )
    validate.add_argument("opportunity")
    validate.add_argument(
        "--output", default=None, help="Write the report to this file instead of stdout"
    )
    validate.set_defaults(func=cmd_validate)

    approve = subparsers.add_parser("approve", help="Record a human decision")
    approve.add_argument("opportunity")
    approve.add_argument(
        "--decision",
        type=Decision,
        choices=list(Decision),
        default=Decision.APPROVED,
    )
    approve.add_argument("--select", default=None, help="Candidate name to approve")
    approve.add_argument("--notes", default="")
    approve.set_defaults(func=cmd_approve)

    workspace = subparsers.add_parser("workspace", help="Phase 4: create project directory")
    workspace.add_argument("opportunity")
    workspace.set_defaults(func=cmd_workspace)

    report = subparsers.add_parser("report", help="Daily or completion report")
    report.add_argument("opportunity", nargs="?", default=None)
    report.set_defaults(func=cmd_report)

    status = subparsers.add_parser("status", help="Show configuration and readiness")
    status.set_defaults(func=cmd_status)

    return parser


def main(argv: list[str] | None = None) -> int:
    load_dotenv()
    config = load_config()
    parser = build_parser()
    args = parser.parse_args(argv)
    return args.func(args, config)


if __name__ == "__main__":
    raise SystemExit(main())