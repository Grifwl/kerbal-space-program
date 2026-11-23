"""Use case: recover every vessel that is worth recovering.

The service plans with the pure rules of ``domain.recovery`` and then
recovers the chosen vessels one at a time, checking after each one that the
game actually removed it and measuring the real payout.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from dataclasses import dataclass

from ksp_control.application.ports import FleetPort, GameUnavailableError
from ksp_control.domain.model import GameScene
from ksp_control.domain.recovery import RecoveryDecision, RecoveryPolicy, plan_recovery

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class RecoveryOutcome:
    """What happened when recovering one vessel.

    Attributes:
        decision: The decision that was executed.
        recovered: Whether the vessel disappeared from the game.
        payout: Funds actually received, when it could be measured.
        message: Human readable result.
    """

    decision: RecoveryDecision
    recovered: bool
    payout: float | None
    message: str


class VesselRecoveryService:
    """Plans and performs vessel recoveries.

    Responsibilities:

    * Recover vessels sequentially and stop as soon as the game changes
      scene, because the remaining vessels may no longer be reachable.
      Recovering the active vessel always ends the flight (that is when KSP
      shows the mission summary), so it is planned last. With kRPC >= 0.7
      other vessels are recovered without leaving the current scene; with
      0.5.x any recovery ends the flight.

    Usage notes: ``sleep`` and ``clock`` are injectable so tests do not wait.
    """

    def __init__(
        self,
        fleet: FleetPort,
        timeout: float = 15.0,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        """Create the service.

        Args:
            fleet: Port giving access to the vessels.
            timeout: Seconds to wait for the game to remove a vessel.
            sleep: Function used to wait between checks.
            clock: Monotonic clock, in seconds.
        """
        self._fleet = fleet
        self._timeout = timeout
        self._sleep = sleep
        self._clock = clock

    def plan(self, policy: RecoveryPolicy) -> list[RecoveryDecision]:
        """Decide what to recover.

        Args:
            policy: The player's preferences.

        Returns:
            One decision per vessel, the ones to recover first.

        Raises:
            GameUnavailableError: When the game cannot be reached or the
                current scene does not give access to the vessels.
        """
        return plan_recovery(self._fleet.list_vessels(), policy)

    def execute(
        self,
        decisions: list[RecoveryDecision],
        on_outcome: Callable[[RecoveryOutcome], None] | None = None,
    ) -> list[RecoveryOutcome]:
        """Recover the vessels marked for recovery, in order.

        Args:
            decisions: Output of ``plan``.
            on_outcome: Called after each vessel, for live progress output.

        Returns:
            The outcome of every attempted or skipped recovery.
        """
        outcomes: list[RecoveryOutcome] = []
        pending = [d for d in decisions if d.recover]
        start_scene = self._safe_scene()
        for index, decision in enumerate(pending):
            outcome = self._recover_one(decision, start_scene)
            outcomes.append(outcome)
            if on_outcome:
                on_outcome(outcome)

            if self._safe_scene() is not start_scene:
                # A scene change may make the other vessels unreachable;
                # report them instead of failing on each one.
                for skipped in pending[index + 1:]:
                    late = RecoveryOutcome(
                        skipped, False, None,
                        "skipped: the game changed scene; run the script again",
                    )
                    outcomes.append(late)
                    if on_outcome:
                        on_outcome(late)
                break
        return outcomes

    def _recover_one(self, decision: RecoveryDecision, start_scene: GameScene) -> RecoveryOutcome:
        """Recover one vessel and wait until the game confirms it.

        Args:
            decision: Decision for the vessel.
            start_scene: Scene the game was in when the recoveries started.

        Returns:
            The outcome, with the measured payout when possible.
        """
        vessel = decision.vessel
        try:
            funds_before = self._fleet.funds()
            self._fleet.recover(vessel.vessel_id)
        except GameUnavailableError as error:
            return RecoveryOutcome(decision, False, None, f"failed: {error}")

        deadline = self._clock() + self._timeout
        gone = False
        while self._clock() < deadline:
            self._sleep(0.5)
            try:
                if self._fleet.scene() is not start_scene:
                    # The recovery moved the game to the space center; the
                    # vessel can no longer be checked, but it is recovered.
                    gone = True
                    break
                if not self._fleet.vessel_exists(vessel.vessel_id):
                    gone = True
                    break
            except GameUnavailableError:
                continue  # scene loading: try again until the deadline

        payout = self._measure_payout(funds_before) if gone else None
        if not gone:
            return RecoveryOutcome(decision, False, None, "failed: the vessel is still there")
        return RecoveryOutcome(decision, True, payout, "recovered")

    def _measure_payout(self, funds_before: float) -> float | None:
        """Compute the funds received since a reference balance.

        KSP credits the funds a moment after removing the vessel, so the
        balance is sampled until it stops changing.

        Args:
            funds_before: Balance before the recovery.

        Returns:
            Funds received, or ``None`` when the balance cannot be read.
        """
        last = None
        for _ in range(6):
            try:
                current = self._fleet.funds()
            except GameUnavailableError:
                self._sleep(0.5)
                continue
            if last is not None and abs(current - last) < 0.5 and current != funds_before:
                break
            last = current
            self._sleep(0.5)
        return None if last is None else last - funds_before

    def _safe_scene(self) -> GameScene:
        """Read the scene, mapping errors to ``UNKNOWN``.

        Returns:
            The scene, or ``GameScene.UNKNOWN`` while it cannot be read.
        """
        try:
            return self._fleet.scene()
        except GameUnavailableError:
            return GameScene.UNKNOWN

    def close(self) -> None:
        """Release the game connection."""
        self._fleet.close()
