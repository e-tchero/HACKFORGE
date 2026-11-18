"""Persistence boundary.

Engines depend only on the ``Store`` protocol, never on a specific database.
``JsonStore`` is the zero-dependency default; ``SqliteStore`` is the production
choice for now. A ``PostgresStore`` can be added later by implementing the same
four methods without touching any engine or CLI code.
"""

from __future__ import annotations

import json
import sqlite3
import tempfile
from pathlib import Path
from typing import Any, Iterable, Protocol

from .models import Opportunity, dumps, loads, now_iso


class Store(Protocol):
    """Everything HACKFORGE needs from a persistence layer."""

    def load_all(self) -> list[Opportunity]: ...

    def get(self, opportunity_id: str) -> Opportunity | None: ...

    def upsert(self, opportunity: Opportunity) -> None: ...

    def delete(self, opportunity_id: str) -> None: ...


class JsonStore:
    """Single-file store. Simple, inspectable, and safe for concurrent readers."""

    def __init__(self, path: Path) -> None:
        self.path = Path(path)

    def _read(self) -> list[dict[str, Any]]:
        if not self.path.is_file():
            return []
        raw = self.path.read_text(encoding="utf-8").strip()
        if not raw:
            return []
        data = json.loads(raw)
        return data if isinstance(data, list) else [data]

    def load_all(self) -> list[Opportunity]:
        return [Opportunity.from_dict(item) for item in self._read()]

    def get(self, opportunity_id: str) -> Opportunity | None:
        for item in self._read():
            if item.get("id") == opportunity_id:
                return Opportunity.from_dict(item)
        return None

    def upsert(self, opportunity: Opportunity) -> None:
        records = [item for item in self._read() if item.get("id") != opportunity.id]
        records.append(opportunity.to_dict())
        self.path.parent.mkdir(parents=True, exist_ok=True)
        # Atomic replace so an interrupted write cannot truncate the state file.
        with tempfile.NamedTemporaryFile(
            "w", delete=False, dir=str(self.path.parent), suffix=".tmp"
        ) as handle:
            handle.write(dumps(records))
            temp_path = Path(handle.name)
        temp_path.replace(self.path)

    def delete(self, opportunity_id: str) -> None:
        records = [item for item in self._read() if item.get("id") != opportunity_id]
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(dumps(records), encoding="utf-8")


_SCHEMA = """
CREATE TABLE IF NOT EXISTS opportunities (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    type TEXT NOT NULL DEFAULT 'hackathon',
    status TEXT NOT NULL DEFAULT 'discovered',
    execution_status TEXT NOT NULL DEFAULT 'discovered',
    submission_deadline TEXT,
    document TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_opportunities_deadline
    ON opportunities (submission_deadline);
CREATE INDEX IF NOT EXISTS idx_opportunities_status
    ON opportunities (status, execution_status);

CREATE TABLE IF NOT EXISTS activity_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    opportunity_id TEXT,
    entry TEXT NOT NULL,
    logged_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_activity_opportunity
    ON activity_log (opportunity_id, logged_at);
"""


class SqliteStore:
    """SQLite-backed store.

    The full opportunity document is kept as JSON in a single column so schema
    evolution does not require migrations every time the record shape changes.
    Indexed columns are the ones we filter and sort on in reporting.
    """

    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._connection = sqlite3.connect(str(self.path))
        self._connection.row_factory = sqlite3.Row
        self._connection.executescript(_SCHEMA)
        self._connection.commit()

    def load_all(self) -> list[Opportunity]:
        rows = self._connection.execute(
            "SELECT document FROM opportunities ORDER BY updated_at DESC"
        ).fetchall()
        return [Opportunity.from_dict(json.loads(row["document"])) for row in rows]

    def get(self, opportunity_id: str) -> Opportunity | None:
        row = self._connection.execute(
            "SELECT document FROM opportunities WHERE id = ?", (opportunity_id,)
        ).fetchone()
        if row is None:
            return None
        return Opportunity.from_dict(json.loads(row["document"]))

    def upsert(self, opportunity: Opportunity) -> None:
        self._connection.execute(
            """
            INSERT INTO opportunities
                (id, name, type, status, execution_status, submission_deadline,
                 document, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
                name = excluded.name,
                type = excluded.type,
                status = excluded.status,
                execution_status = excluded.execution_status,
                submission_deadline = excluded.submission_deadline,
                document = excluded.document,
                updated_at = excluded.updated_at
            """,
            (
                opportunity.id,
                opportunity.name,
                opportunity.type,
                opportunity.status.value,
                opportunity.execution_status.value,
                opportunity.timeline.submission_deadline.isoformat()
                if opportunity.timeline.submission_deadline
                else None,
                dumps(opportunity.to_dict()),
                opportunity.updated_at or now_iso(),
            ),
        )
        self._connection.commit()

    def delete(self, opportunity_id: str) -> None:
        self._connection.execute(
            "DELETE FROM opportunities WHERE id = ?", (opportunity_id,)
        )
        self._connection.commit()

    def log_activity(self, opportunity_id: str | None, entry: str) -> None:
        self._connection.execute(
            "INSERT INTO activity_log (opportunity_id, entry, logged_at) VALUES (?, ?, ?)",
            (opportunity_id, entry, now_iso()),
        )
        self._connection.commit()

    def recent_activity(
        self, opportunity_id: str, limit: int = 20
    ) -> list[tuple[str, str]]:
        rows = self._connection.execute(
            "SELECT logged_at, entry FROM activity_log WHERE opportunity_id = ? "
            "ORDER BY logged_at DESC LIMIT ?",
            (opportunity_id, limit),
        ).fetchall()
        return [(row["logged_at"], row["entry"]) for row in rows]

    def close(self) -> None:
        self._connection.close()


def build_store(config: Any) -> Store:
    """Choose a store from configuration.

    ``HACKFORGE_DATABASE_URL`` is honoured today for ``sqlite:`` URLs and is
    reserved so a ``postgresql://`` URL can be routed to a future store without
    any caller changing.
    """

    url = getattr(config, "database_url", "") or ""
    state_path = config.state_path

    if url.startswith("sqlite:"):
        path = Path(url.split("sqlite:", 1)[1].lstrip("/") or str(state_path))
        return SqliteStore(path)
    if url.startswith("postgresql:") or url.startswith("postgres:"):
        raise NotImplementedError(
            "PostgreSQL store is not implemented yet. Point HACKFORGE_DATABASE_URL "
            "at sqlite: or implement PostgresStore against the Store protocol."
        )
    if state_path.suffix == ".db" or state_path.suffix == ".sqlite":
        return SqliteStore(state_path)
    return JsonStore(state_path)