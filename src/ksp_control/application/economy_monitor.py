"""Use cases of the career economy monitor.

Two use cases live here:

* **Record** (``poll_once``): read the game, detect ledger events since the
  previous reading and persist both the balances and the events.
* **Report** (``dashboard``): build the read model the web UI displays.

They are grouped in one service because they share the monitor status
(connected or not, last error) that the dashboard shows to the player.
"""

from __future__ import annotations

import logging
import threading
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from ksp_control.application.ports import (
    EconomyRepositoryPort,
    GameTelemetryPort,
    GameUnavailableError,
)
from ksp_control.domain.ledger import LedgerAnalyzer, LedgerSummary
from ksp_control.domain.model import GameSnapshot, LedgerEvent, ResourceSample

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class MonitorStatus:
    """Health of the link with the game, as shown in the dashboard header.

    Attributes:
        connected: Whether the last poll succeeded.
        message: Human readable status or error message.
        checked_at: When the last poll happened, or ``None`` before the first.
    """

    connected: bool = False
    message: str = "Waiting for the first reading..."
    checked_at: datetime | None = None


@dataclass(frozen=True)
class DashboardView:
    """Read model consumed by the web dashboard.

    Attributes:
        status: Link health.
        current: Latest stored sample, if any.
        baseline: First stored sample (start of tracking), if any.
        samples: Balance history, oldest first.
        summary: Aggregated ledger.
        recent_events: Latest ledger events, newest first.
    """

    status: MonitorStatus
    current: ResourceSample | None
    baseline: ResourceSample | None
    samples: list[ResourceSample]
    summary: LedgerSummary
    recent_events: list[LedgerEvent] = field(default_factory=list)


class EconomyMonitorService:
    """Records the career economy and reports it.

    Responsibilities:

    * Keep the previous complete snapshot in memory and feed consecutive
      snapshots to the ``LedgerAnalyzer``.
    * Persist samples sparingly: only when something changed or when the
      heartbeat interval elapsed, so a long session does not fill the
      database with identical rows while still drawing a continuous line.
    * Expose the status of the game link.

    Usage notes: ``poll_once`` is meant to be called periodically from a
    single thread (see ``PollingLoop``); ``dashboard`` may be called from any
    thread at the same time.
    """

    def __init__(
        self,
        telemetry: GameTelemetryPort,
        repository: EconomyRepositoryPort,
        analyzer: LedgerAnalyzer | None = None,
        heartbeat: timedelta = timedelta(seconds=30),
    ) -> None:
        """Create the service.

        Args:
            telemetry: Port used to read the game.
            repository: Port used to persist history and ledger.
            analyzer: Ledger rules; a default instance is used when omitted.
            heartbeat: Maximum time between two stored samples when nothing
                changes.
        """
        self._telemetry = telemetry
        self._repository = repository
        self._analyzer = analyzer or LedgerAnalyzer()
        self._heartbeat = heartbeat
        self._previous: GameSnapshot | None = None
        self._status = MonitorStatus()
        self._lock = threading.Lock()

    @property
    def status(self) -> MonitorStatus:
        """Current health of the game link.

        Returns:
            The latest status (immutable, safe to share).
        """
        return self._status

    def poll_once(self) -> list[LedgerEvent]:
        """Read the game once, detect events and persist everything.

        Returns:
            The events detected in this poll (empty if nothing happened or
            the game was unavailable).
        """
        with self._lock:
            now = datetime.now(timezone.utc)
            try:
                snapshot = self._telemetry.read_snapshot()
            except GameUnavailableError as error:
                self._status = MonitorStatus(False, str(error), now)
                return []

            # A snapshot without contracts (e.g. read while a scene was
            # loading) is stored for the charts but never used for the diff:
            # comparing it would push contract money into the residual, and
            # the next complete snapshot would then count it a second time.
            # The last complete snapshot stays as the reference instead.
            events: list[LedgerEvent] = []
            if snapshot.contracts is not None:
                if self._previous is not None:
                    events = self._analyzer.compare(self._previous, snapshot)
                    if events:
                        self._repository.add_events(events)
                        for event in events:
                            logger.info(
                                "%s: %s %s", event.kind.value, event.description, event.delta
                            )
                self._previous = snapshot

            sample = ResourceSample.from_snapshot(snapshot)
            if events or self._should_store(sample):
                self._repository.add_sample(sample)

            self._status = MonitorStatus(True, _describe(snapshot), now)
            return events

    def dashboard(self, sample_limit: int = 5000, event_limit: int = 100) -> DashboardView:
        """Build the dashboard read model.

        Args:
            sample_limit: Maximum number of history points returned.
            event_limit: Maximum number of recent events returned.

        Returns:
            Everything the dashboard needs in one object.
        """
        all_events = self._repository.list_events()
        return DashboardView(
            status=self._status,
            current=self._repository.last_sample(),
            baseline=self._repository.first_sample(),
            samples=self._repository.list_samples(limit=sample_limit),
            summary=LedgerSummary.from_events(all_events),
            recent_events=all_events[:event_limit],
        )

    def close(self) -> None:
        """Release the game connection."""
        self._telemetry.close()

    def _should_store(self, sample: ResourceSample) -> bool:
        """Decide whether a sample adds information to the history.

        Args:
            sample: The candidate sample.

        Returns:
            True when balances, scene or vessel changed, or the heartbeat
            interval elapsed since the last stored sample.
        """
        last = self._repository.last_sample()
        if last is None:
            return True
        changed = (
            not (sample.resources - last.resources).cleaned().is_negligible
            or sample.scene is not last.scene
            or sample.active_vessel != last.active_vessel
        )
        return changed or sample.taken_at - last.taken_at >= self._heartbeat


def _describe(snapshot: GameSnapshot) -> str:
    """Summarize where the player is, for the status line.

    Args:
        snapshot: Latest snapshot.

    Returns:
        A short text such as ``"Connected - flight (Kerbal X)"``.
    """
    where = snapshot.scene.value.replace("_", " ")
    if snapshot.active_vessel:
        where += f" ({snapshot.active_vessel})"
    return f"Connected - {where}"
