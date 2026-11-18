"""Core model, store, registry, and deadline engine behaviour."""

from __future__ import annotations

from datetime import date

import pytest

from hackforge.core.models import (
    Blocker,
    ExecutionStatus,
    Opportunity,
    OpportunityStatus,
    Timeline,
    VerificationLevel,
)
from hackforge.core.registry import OpportunityRegistry
from hackforge.core.store import JsonStore, SqliteStore
from hackforge.engines.deadlines import AccountabilityEngine, DeadlineEngine


class TestUnknownHandling:
    def test_missing_dates_serialise_as_unknown(self):
        opportunity = Opportunity(id="o", name="Test")
        data = opportunity.to_dict()

        assert data["timeline"]["submission_deadline"] == "UNKNOWN"
        assert data["execution"]["project_directory"] == "UNKNOWN"
        assert data["registration"]["date"] == "UNKNOWN"

    def test_round_trip_preserves_known_and_unknown(self):
        opportunity = Opportunity(
            id="o",
            name="Test",
            registration_date=date(2026, 1, 15),
            timeline=Timeline(submission_deadline=date(2026, 4, 18), event_date=None),
        )
        restored = Opportunity.from_dict(opportunity.to_dict())

        assert restored.registration_date == date(2026, 1, 15)
        assert restored.timeline.submission_deadline == date(2026, 4, 18)
        assert restored.timeline.event_date is None

    def test_invalid_date_string_degrades_to_none(self):
        data = Opportunity(id="o", name="Test").to_dict()
        data["timeline"]["submission_deadline"] = "not-a-date"

        restored = Opportunity.from_dict(data)

        assert restored.timeline.submission_deadline is None

    def test_strategy_status_survives_round_trip(self):
        """Regression: approval state was written under execution but read from top level."""
        opportunity = Opportunity(id="o", name="Test", strategy_status="approved")
        opportunity.selected_solution = "Chosen idea"
        opportunity.execution_status = ExecutionStatus.BUILDING

        restored = Opportunity.from_dict(opportunity.to_dict())

        assert restored.strategy_status == "approved"
        assert restored.selected_solution == "Chosen idea"
        assert restored.execution_status is ExecutionStatus.BUILDING

    def test_strategy_status_defaults_to_pending(self):
        restored = Opportunity.from_dict(Opportunity(id="o", name="T").to_dict())

        assert restored.strategy_status == "pending"

    def test_blockers_round_trip(self):
        opportunity = Opportunity(id="o", name="Test")
        opportunity.blockers.append(
            Blocker(
                description="BLOCKED - API key required",
                reason="No key in environment",
                user_action_required="Provide the key",
            )
        )
        restored = Opportunity.from_dict(opportunity.to_dict())

        assert restored.blockers[0].description == "BLOCKED - API key required"
        assert restored.blockers[0].user_action_required == "Provide the key"


class TestSourceMerging:
    def test_add_source_dedupes_by_reference(self):
        from hackforge.core.models import SourceRef

        opportunity = Opportunity(id="o", name="T")
        opportunity.add_source(SourceRef(kind="official_website", reference="https://a.example"))
        opportunity.add_source(
            SourceRef(kind="official_website", reference="https://a.example", verified=True)
        )

        assert len(opportunity.sources) == 1
        assert opportunity.sources[0].verified is True

    def test_add_source_keeps_distinct_references(self):
        from hackforge.core.models import SourceRef

        opportunity = Opportunity(id="o", name="T")
        opportunity.add_source(SourceRef(kind="official_website", reference="https://a.example"))
        opportunity.add_source(SourceRef(kind="rules", reference="https://b.example/rules"))

        assert len(opportunity.sources) == 2

    def test_verified_flag_is_not_downgraded(self):
        from hackforge.core.models import SourceRef

        opportunity = Opportunity(id="o", name="T")
        opportunity.add_source(
            SourceRef(kind="official_website", reference="https://a.example", verified=True)
        )
        opportunity.add_source(
            SourceRef(kind="official_website", reference="https://a.example", verified=False)
        )

        assert opportunity.sources[0].verified is True


class TestStores:
    @pytest.mark.parametrize("factory", ["json", "sqlite"])
    def test_upsert_and_get(self, tmp_path, factory):
        store = _make_store(tmp_path, factory)
        opportunity = Opportunity(id="o1", name="Test")

        store.upsert(opportunity)
        loaded = store.get("o1")

        assert loaded is not None
        assert loaded.name == "Test"

    @pytest.mark.parametrize("factory", ["json", "sqlite"])
    def test_upsert_replaces_without_duplicating(self, tmp_path, factory):
        store = _make_store(tmp_path, factory)
        opportunity = Opportunity(id="o1", name="Test")

        store.upsert(opportunity)
        opportunity.name = "Updated"
        store.upsert(opportunity)

        assert len(store.load_all()) == 1
        assert store.get("o1").name == "Updated"

    @pytest.mark.parametrize("factory", ["json", "sqlite"])
    def test_missing_returns_none(self, tmp_path, factory):
        assert _make_store(tmp_path, factory).get("nope") is None

    @pytest.mark.parametrize("factory", ["json", "sqlite"])
    def test_delete(self, tmp_path, factory):
        store = _make_store(tmp_path, factory)
        store.upsert(Opportunity(id="o1", name="Test"))
        store.delete("o1")

        assert store.get("o1") is None
        assert store.load_all() == []

    def test_sqlite_indexes_deadline(self, tmp_path):
        store = _make_store(tmp_path, "sqlite")
        store.upsert(
            Opportunity(
                id="o1", name="A", timeline=Timeline(submission_deadline=date(2026, 4, 18))
            )
        )
        rows = store._connection.execute(
            "SELECT id FROM opportunities WHERE submission_deadline = ?",
            ("2026-04-18",),
        ).fetchall()

        assert [row["id"] for row in rows] == ["o1"]

    def test_sqlite_activity_log(self, tmp_path):
        store = _make_store(tmp_path, "sqlite")
        store.log_activity("o1", "did a thing")

        entries = store.recent_activity("o1")

        assert len(entries) == 1
        assert "did a thing" in entries[0][1]


def _make_store(tmp_path, factory: str):
    if factory == "sqlite":
        return SqliteStore(tmp_path / "state.db")
    return JsonStore(tmp_path / "state.json")


class TestRegistry:
    def test_registers_new_opportunity(self, tmp_path):
        registry = OpportunityRegistry(JsonStore(tmp_path / "s.json"))

        registry.register(Opportunity(id="o1", name="Test"))

        assert registry.get("o1") is not None
        assert registry.activity

    def test_rescan_does_not_overwrite_progress(self, tmp_path):
        store = JsonStore(tmp_path / "s.json")
        registry = OpportunityRegistry(store)
        existing = Opportunity(
            id="o1",
            name="Test",
            build_completion=42,
            selected_solution="Chosen approach",
            execution_status=ExecutionStatus.BUILDING,
        )
        store.upsert(existing)

        incoming = Opportunity(id="o1", name="Test", organizer="New Org")
        merged = registry.register(incoming)

        assert merged.build_completion == 42
        assert merged.selected_solution == "Chosen approach"
        assert merged.organizer == "New Org"

    def test_registration_email_promotes_status(self, tmp_path):
        store = JsonStore(tmp_path / "s.json")
        store.upsert(Opportunity(id="o1", name="Test", status=OpportunityStatus.DISCOVERED))

        incoming = Opportunity(id="o1", name="Test", status=OpportunityStatus.REGISTERED)
        merged = OpportunityRegistry(store).register(incoming)

        assert merged.status is OpportunityStatus.REGISTERED

    def test_rescan_never_demotes_registered(self, tmp_path):
        store = JsonStore(tmp_path / "s.json")
        store.upsert(Opportunity(id="o1", name="Test", status=OpportunityStatus.REGISTERED))

        incoming = Opportunity(id="o1", name="Test", status=OpportunityStatus.DISCOVERED)
        merged = OpportunityRegistry(store).register(incoming)

        assert merged.status is OpportunityStatus.REGISTERED

    def test_find_by_name(self, tmp_path):
        registry = OpportunityRegistry(JsonStore(tmp_path / "s.json"))
        registry.register(Opportunity(id="o1", name="Global AI Hackathon"))

        assert registry.find_by_name("global ai hackathon").id == "o1"
        assert registry.find_by_name("Nope") is None


class TestDeadlineEngine:
    # Reference date is 2026-04-01 for every case.
    @pytest.mark.parametrize(
        "deadline,expected",
        [
            (date(2026, 12, 1), "LONG_RANGE"),      # 244 days out
            (date(2026, 4, 20), "PLANNING"),        # 19 days
            (date(2026, 4, 10), "ACTIVE"),           # 9 days
            (date(2026, 4, 4), "URGENT"),            # 3 days
            (date(2026, 4, 2), "CRITICAL"),          # 24h..72h
            (date(2026, 4, 1), "FINAL_WINDOW"),      # deadline day
            (date(2026, 3, 25), "EXPIRED"),          # past
        ],
    )
    def test_urgency_bands(self, deadline, expected):
        opportunity = Opportunity(
            id="o", name="T", timeline=Timeline(submission_deadline=deadline)
        )
        result = DeadlineEngine().assess(opportunity, today=date(2026, 4, 1))

        assert result.urgency.value == expected

    def test_unknown_deadline_is_not_guessed(self):
        opportunity = Opportunity(id="o", name="T")
        result = DeadlineEngine().assess(opportunity)

        assert result.urgency.value == "UNKNOWN"
        assert result.days_remaining is None
        assert "UNKNOWN" in result.reason

    def test_expired_is_detected(self):
        opportunity = Opportunity(
            id="o", name="T", timeline=Timeline(submission_deadline=date(2026, 1, 1))
        )
        result = DeadlineEngine().assess(opportunity, today=date(2026, 4, 18))

        assert result.urgency.value == "EXPIRED"
        assert result.days_remaining == -107


class TestAccountabilityEngine:
    def test_no_activity_is_flagged(self):
        opportunity = Opportunity(
            id="o", name="T", timeline=Timeline(submission_deadline=date(2026, 4, 20))
        )
        report = AccountabilityEngine().assess(opportunity, None, today=date(2026, 4, 1))

        assert report.inactive_days is None
        assert any("No recorded activity" in reason for reason in report.reasons)

    def test_inactivity_is_flagged(self):
        from datetime import datetime, timezone

        opportunity = Opportunity(
            id="o", name="T", timeline=Timeline(submission_deadline=date(2026, 4, 20))
        )
        last = datetime(2026, 3, 25, tzinfo=timezone.utc)
        report = AccountabilityEngine().assess(opportunity, last, today=date(2026, 4, 1))

        assert report.inactive_days == 7
        assert any("No recorded activity" in reason for reason in report.reasons)

    def test_blockers_are_surfaced(self):
        opportunity = Opportunity(id="o", name="T")
        opportunity.blockers.append(Blocker(description="BLOCKED - API key required"))
        report = AccountabilityEngine().assess(opportunity)

        assert report.risk_level in {"HIGH", "MEDIUM"}
        assert report.next_action.startswith("Resolve blocker")

    def test_research_pending_next_action(self):
        opportunity = Opportunity(id="o", name="T")
        report = AccountabilityEngine().assess(opportunity)

        assert report.next_action == "Begin research."

    def test_testing_status_is_not_faked(self):
        opportunity = Opportunity(id="o", name="T")
        assert opportunity.testing_status is VerificationLevel.PLANNED