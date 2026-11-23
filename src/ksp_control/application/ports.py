"""Driven ports: the interfaces the use cases need from the outside world.

The use cases only know these abstract classes. Concrete adapters (kRPC,
SQLite, the simulator, in-memory fakes for tests) implement them and are
plugged in by an entrypoint, so the core never imports an infrastructure
library.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from datetime import datetime

from ksp_control.domain.model import GameSnapshot, LedgerEvent, ResourceSample


class GameUnavailableError(RuntimeError):
    """The game cannot be read right now.

    Raised by telemetry adapters when KSP is not running, the kRPC server is
    stopped, a scene is loading, or the save is not a career game. It is an
    expected, recoverable situation: callers should wait and retry.
    """


class GameTelemetryPort(ABC):
    """Reads the current economic state of the game."""

    @abstractmethod
    def read_snapshot(self) -> GameSnapshot:
        """Take a consistent reading of balances, scene and contracts.

        Returns:
            The current game snapshot.

        Raises:
            GameUnavailableError: When the game cannot be read at the moment.
        """

    @abstractmethod
    def close(self) -> None:
        """Release any connection held by the adapter."""


class EconomyRepositoryPort(ABC):
    """Persists the balance history and the ledger events.

    Implementations must be safe to call from several threads, because the
    polling loop writes while the web server reads.
    """

    @abstractmethod
    def add_sample(self, sample: ResourceSample) -> None:
        """Store one point of the balance history.

        Args:
            sample: The point to store.
        """

    @abstractmethod
    def last_sample(self) -> ResourceSample | None:
        """Return the most recent stored point.

        Returns:
            The newest sample, or ``None`` when the history is empty.
        """

    @abstractmethod
    def first_sample(self) -> ResourceSample | None:
        """Return the oldest stored point (when tracking started).

        Returns:
            The oldest sample, or ``None`` when the history is empty.
        """

    @abstractmethod
    def list_samples(self, since: datetime | None = None, limit: int = 5000) -> list[ResourceSample]:
        """Return the balance history in chronological order.

        Args:
            since: Only return samples taken at or after this time.
            limit: Maximum number of samples; the most recent ones are kept.

        Returns:
            Samples ordered from oldest to newest.
        """

    @abstractmethod
    def add_events(self, events: list[LedgerEvent]) -> None:
        """Store ledger events.

        Args:
            events: Events to append to the ledger.
        """

    @abstractmethod
    def list_events(self, limit: int | None = None) -> list[LedgerEvent]:
        """Return ledger events, newest first.

        Args:
            limit: Maximum number of events, or ``None`` for all of them.

        Returns:
            Events ordered from newest to oldest.
        """
