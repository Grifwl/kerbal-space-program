"""SQLite storage for the balance history and the ledger.

SQLite was chosen because it ships with Python, needs no server and keeps
one career in one file: using a different database file per save game is
the simplest way to keep careers apart.
"""

from __future__ import annotations

import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path

from ksp_control.application.ports import EconomyRepositoryPort
from ksp_control.domain.model import (
    EventKind,
    GameScene,
    LedgerEvent,
    ResourceSample,
    Resources,
)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS samples (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    taken_at    TEXT    NOT NULL,
    ut          REAL    NOT NULL,
    scene       TEXT    NOT NULL,
    funds       REAL    NOT NULL,
    science     REAL    NOT NULL,
    reputation  REAL    NOT NULL,
    vessel      TEXT
);
CREATE TABLE IF NOT EXISTS events (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    occurred_at TEXT    NOT NULL,
    ut          REAL    NOT NULL,
    kind        TEXT    NOT NULL,
    description TEXT    NOT NULL,
    funds       REAL    NOT NULL,
    science     REAL    NOT NULL,
    reputation  REAL    NOT NULL,
    vessel      TEXT
);
CREATE INDEX IF NOT EXISTS samples_taken_at ON samples (taken_at);
CREATE TABLE IF NOT EXISTS paid_objectives (
    contract    TEXT    NOT NULL,
    objective   INTEGER NOT NULL,
    PRIMARY KEY (contract, objective)
);
"""


class SqliteEconomyRepository(EconomyRepositoryPort):
    """Stores samples and ledger events in a SQLite database file.

    Responsibilities: create the schema, map domain objects to rows and back.

    Usage notes: a single connection is shared by all threads and guarded by
    a lock. That is plenty for one writer polling every few seconds and a
    dashboard reading every few seconds, and it also lets ``":memory:"``
    databases work in tests (each connection would otherwise get its own
    empty in-memory database).
    """

    def __init__(self, path: str | Path) -> None:
        """Open (and create if needed) the database.

        Args:
            path: Database file path, or ``":memory:"``.
        """
        if str(path) != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(path), check_same_thread=False)
        self._lock = threading.Lock()
        with self._lock, self._conn:
            self._conn.executescript(_SCHEMA)

    def add_sample(self, sample: ResourceSample) -> None:
        """Store one point of the balance history.

        Args:
            sample: The point to store.
        """
        r = sample.resources
        with self._lock, self._conn:
            self._conn.execute(
                "INSERT INTO samples (taken_at, ut, scene, funds, science, reputation, vessel)"
                " VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    sample.taken_at.isoformat(), sample.universal_time, sample.scene.value,
                    r.funds, r.science, r.reputation, sample.active_vessel,
                ),
            )

    def last_sample(self) -> ResourceSample | None:
        """Return the most recent stored point.

        Returns:
            The newest sample, or ``None`` when the history is empty.
        """
        rows = self._query("SELECT * FROM samples ORDER BY id DESC LIMIT 1")
        return _to_sample(rows[0]) if rows else None

    def first_sample(self) -> ResourceSample | None:
        """Return the oldest stored point.

        Returns:
            The oldest sample, or ``None`` when the history is empty.
        """
        rows = self._query("SELECT * FROM samples ORDER BY id ASC LIMIT 1")
        return _to_sample(rows[0]) if rows else None

    def list_samples(self, since: datetime | None = None, limit: int = 5000) -> list[ResourceSample]:
        """Return the balance history in chronological order.

        Args:
            since: Only return samples taken at or after this time.
            limit: Maximum number of samples; the most recent ones are kept.

        Returns:
            Samples ordered from oldest to newest.
        """
        # Select the newest ``limit`` rows, then flip them back to oldest-first.
        if since is None:
            rows = self._query("SELECT * FROM samples ORDER BY id DESC LIMIT ?", (limit,))
        else:
            rows = self._query(
                "SELECT * FROM samples WHERE taken_at >= ? ORDER BY id DESC LIMIT ?",
                (since.isoformat(), limit),
            )
        return [_to_sample(row) for row in reversed(rows)]

    def add_events(self, events: list[LedgerEvent]) -> None:
        """Store ledger events.

        Args:
            events: Events to append to the ledger.
        """
        with self._lock, self._conn:
            self._conn.executemany(
                "INSERT INTO events (occurred_at, ut, kind, description, funds, science,"
                " reputation, vessel) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                [
                    (
                        e.occurred_at.isoformat(), e.universal_time, e.kind.value, e.description,
                        e.delta.funds, e.delta.science, e.delta.reputation, e.vessel,
                    )
                    for e in events
                ],
            )

    def paid_objectives(self) -> set[tuple[str, int]]:
        """Return the contract objectives known to be paid already.

        Returns:
            ``(contract key, objective index)`` pairs.
        """
        return {(row["contract"], row["objective"]) for row in self._query(
            "SELECT contract, objective FROM paid_objectives"
        )}

    def add_paid_objectives(self, objectives: set[tuple[str, int]]) -> None:
        """Remember contract objectives that have been paid.

        Args:
            objectives: ``(contract key, objective index)`` pairs.
        """
        with self._lock, self._conn:
            self._conn.executemany(
                "INSERT OR IGNORE INTO paid_objectives (contract, objective) VALUES (?, ?)",
                sorted(objectives),
            )

    def forget_contract_objectives(self, contract_key: str) -> None:
        """Drop the paid objectives of a contract that is finished.

        Args:
            contract_key: Key of the contract.
        """
        with self._lock, self._conn:
            self._conn.execute("DELETE FROM paid_objectives WHERE contract = ?", (contract_key,))

    def list_events(self, limit: int | None = None) -> list[LedgerEvent]:
        """Return ledger events, newest first.

        Args:
            limit: Maximum number of events, or ``None`` for all of them.

        Returns:
            Events ordered from newest to oldest.
        """
        sql = "SELECT * FROM events ORDER BY id DESC"
        rows = self._query(sql + " LIMIT ?", (limit,)) if limit is not None else self._query(sql)
        return [_to_event(row) for row in rows]

    def close(self) -> None:
        """Close the database connection."""
        with self._lock:
            self._conn.close()

    def _query(self, sql: str, params: tuple = ()) -> list[sqlite3.Row]:
        """Run a read query under the lock.

        Args:
            sql: SQL statement.
            params: Bound parameters.

        Returns:
            All result rows, addressable by column name.
        """
        with self._lock:
            cursor = self._conn.execute(sql, params)
            cursor.row_factory = sqlite3.Row
            return cursor.fetchall()


def _parse_time(value: str) -> datetime:
    """Parse a stored ISO timestamp, assuming UTC when no zone is present.

    Args:
        value: ISO 8601 text.

    Returns:
        A timezone aware datetime.
    """
    parsed = datetime.fromisoformat(value)
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _to_sample(row: sqlite3.Row) -> ResourceSample:
    """Map a ``samples`` row to the domain object.

    Args:
        row: Database row.

    Returns:
        The sample.
    """
    return ResourceSample(
        taken_at=_parse_time(row["taken_at"]),
        universal_time=row["ut"],
        scene=GameScene.from_name(row["scene"]),
        resources=Resources(row["funds"], row["science"], row["reputation"]),
        active_vessel=row["vessel"],
    )


def _to_event(row: sqlite3.Row) -> LedgerEvent:
    """Map an ``events`` row to the domain object.

    Args:
        row: Database row.

    Returns:
        The ledger event.
    """
    return LedgerEvent(
        occurred_at=_parse_time(row["occurred_at"]),
        universal_time=row["ut"],
        kind=EventKind(row["kind"]),
        description=row["description"],
        delta=Resources(row["funds"], row["science"], row["reputation"]),
        vessel=row["vessel"],
    )
