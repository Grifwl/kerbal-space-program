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
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta, timezone

from ksp_control.application.ports import (
    EconomyRepositoryPort,
    GameTelemetryPort,
    GameUnavailableError,
    VesselDesignsPort,
)
from ksp_control.domain.ledger import LedgerAnalyzer, LedgerSummary
from ksp_control.domain.model import (
    ContractBook,
    EventKind,
    GameSnapshot,
    LedgerEvent,
    ResourceSample,
)

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
        designs: VesselDesignsPort | None = None,
    ) -> None:
        """Create the service.

        Args:
            telemetry: Port used to read the game.
            repository: Port used to persist history and ledger.
            analyzer: Ledger rules; a default instance is used when omitted.
            heartbeat: Maximum time between two stored samples when nothing
                changes.
            designs: Craft designs of vessels renamed by other scripts, to
                group the per-vessel table by design; ``None`` groups by
                vessel name.
        """
        self._designs = designs
        self._telemetry = telemetry
        self._repository = repository
        self._analyzer = analyzer or LedgerAnalyzer()
        self._heartbeat = heartbeat
        self._previous: GameSnapshot | None = None
        self._last_read_contracts: ContractBook | None = None
        # Objectives already paid, kept across restarts: kRPC does not always
        # flag paid objectives as completed, and a completion must not pay
        # them a second time.
        self._paid_objectives = repository.paid_objectives()
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

            # A snapshot whose contracts are not trustworthy (read while a
            # scene was loading) is stored for the charts but never used for
            # the diff: comparing it would invent contract events, and the
            # next good snapshot would then invent the opposite ones. The
            # last trusted snapshot stays as the reference instead, so the
            # money is attributed once, when the contracts settle.
            events: list[LedgerEvent] = []
            if self._contracts_trusted(snapshot):
                snapshot = self._with_paid_objectives(snapshot)
                if self._previous is None:
                    events = self._offline_changes(snapshot)
                    if events:
                        self._repository.add_events(events)
                else:
                    events = self._analyzer.compare(self._previous, snapshot)
                    if events:
                        self._repository.add_events(events)
                        self._remember_objectives(events)
                        snapshot = self._with_paid_objectives(snapshot)
                        for event in events:
                            logger.info(
                                "%s: %s %s", event.kind.value, event.description, event.delta
                            )
                self._previous = snapshot

            # History starts with the first trusted reference, so the first
            # stored sample (the "since tracking began" baseline) and the
            # ledger cover exactly the same period and always agree.
            sample = ResourceSample.from_snapshot(snapshot)
            if self._previous is not None and (events or self._should_store(sample)):
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
            summary=LedgerSummary.from_events(
                all_events, self._designs.designs() if self._designs else None),
            recent_events=all_events[:event_limit],
        )

    def close(self) -> None:
        """Release the game connection."""
        self._telemetry.close()

    def _with_paid_objectives(self, snapshot: GameSnapshot) -> GameSnapshot:
        """Mark the objectives known to be paid as completed in a snapshot.

        Args:
            snapshot: Snapshot with the flags read from the game.

        Returns:
            A copy whose active contracts also flag the remembered objectives.
        """
        book = snapshot.contracts
        if book is None or not self._paid_objectives:
            return snapshot
        progress = []
        for index, terms in enumerate(book.active):
            read = book.active_progress[index] if index < len(book.active_progress) else ()
            read = tuple(read) + (False,) * (len(terms.parameters) - len(read))
            key = terms.key
            progress.append(tuple(
                flag or (key, i) in self._paid_objectives for i, flag in enumerate(read)
            ))
        return replace(snapshot, contracts=replace(book, active_progress=tuple(progress)))

    def _remember_objectives(self, events: list[LedgerEvent]) -> None:
        """Update the paid objectives after a batch of events.

        Args:
            events: Events just detected.
        """
        new: set[tuple[str, int]] = set()
        for event in events:
            if event.contract is None:
                continue
            if event.kind is EventKind.CONTRACT_PROGRESS:
                new |= {(event.contract, i) for i in event.objectives}
            elif event.kind in (
                EventKind.CONTRACT_COMPLETED,
                EventKind.CONTRACT_FAILED,
                EventKind.CONTRACT_CANCELLED,
            ):
                # Finished: a future contract with identical terms starts clean.
                self._repository.forget_contract_objectives(event.contract)
                self._paid_objectives = {
                    item for item in self._paid_objectives if item[0] != event.contract
                }
        if new:
            self._repository.add_paid_objectives(new)
            self._paid_objectives |= new

    def _offline_changes(self, snapshot: GameSnapshot) -> list[LedgerEvent]:
        """Account for what changed while the monitor was not running.

        When the monitor restarts on an existing history, the balances may
        have moved in the meantime. Recording the difference keeps the
        invariant "the ledger adds up to the change in balances", and makes
        the gap visible instead of silently losing it.

        Args:
            snapshot: First trusted snapshot after (re)starting.

        Returns:
            One OTHER event, or nothing when the balances did not change.
        """
        last = self._repository.last_sample()
        if last is None:
            return []
        delta = (snapshot.resources - last.resources).cleaned()
        if delta.is_negligible:
            return []
        return [
            LedgerEvent(
                occurred_at=snapshot.taken_at,
                universal_time=snapshot.universal_time,
                kind=EventKind.OTHER,
                description="Changes while the monitor was not running",
                delta=delta,
            )
        ]

    def _contracts_trusted(self, snapshot: GameSnapshot) -> bool:
        """Decide whether the contract lists of a snapshot can be diffed.

        While KSP loads a scene (e.g. right after pressing Launch) kRPC may
        briefly return empty or partial contract lists. Two guards filter
        those transient readings out:

        * the lists must be identical in two consecutive polls (they settle
          within a poll or two, while real changes then stay put);
        * a completely empty book is rejected when the reference had
          contracts (KSP never forgets finished contracts by itself).

        Args:
            snapshot: The new snapshot.

        Returns:
            True when the snapshot may be compared with the reference.
        """
        book = snapshot.contracts
        last_read, self._last_read_contracts = self._last_read_contracts, book
        if book is None or last_read is None or not book.same_as(last_read):
            return False
        reference = self._previous.contracts if self._previous else None
        if book.is_empty and reference is not None and not reference.is_empty:
            return False
        return True

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
