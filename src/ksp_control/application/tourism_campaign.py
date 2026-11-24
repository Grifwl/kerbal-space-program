"""Use case: earn funds flying tourists on suborbital hops, over and over.

Each round of the campaign:

1. stops when the funds reach the target;
2. picks the tourism contracts the next hop can complete (accepted ones
   first, then offers to fill the seats) and accepts the offers;
3. launches the craft with a pilot (taking turns) and the tourists;
4. flies the hop (``SuborbitalFlightDirector``);
5. recovers the vessel and whatever pieces of it landed, which brings the
   tourists home and completes their contracts;

and starts again, until the target is reached or no suitable contract is
left.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from dataclasses import dataclass, replace
from enum import Enum
from typing import TypeVar

from ksp_control.application.ports import GameUnavailableError, TourismGamePort
from ksp_control.application.suborbital_flight import FlightReport
from ksp_control.application.vessel_recovery import VesselRecoveryService
from ksp_control.domain.ledger import owner_vessel
from ksp_control.domain.recovery import RecoveryPolicy
from ksp_control.domain.tourism import (
    ContractStatus,
    PassengerPlan,
    TourismContract,
    choose_pilot,
    plan_passengers,
)

logger = logging.getLogger(__name__)

T = TypeVar("T")


class CampaignError(RuntimeError):
    """The campaign cannot go on without the player (see the message)."""


class StopReason(Enum):
    """Why the campaign ended normally."""

    TARGET_REACHED = "target reached"
    NO_CONTRACTS = "no suborbital tourism contracts left"
    FLIGHT_LIMIT = "flight limit reached"


@dataclass(frozen=True)
class CampaignOptions:
    """What the player wants from the campaign.

    Attributes:
        craft: Name of the VAB craft to launch.
        target_funds: Stop once the funds reach this amount.
        seats: Passenger seats of the craft (the pilot's seat excluded).
        pilots: Pilots to take turns, in order; every hired pilot if empty.
        max_flights: Stop after this many flights (``None``: no limit).
        body: Body launched from.
    """

    craft: str
    target_funds: float
    seats: int = 4
    pilots: tuple[str, ...] = ()
    max_flights: int | None = None
    body: str = "Kerbin"


@dataclass(frozen=True)
class FlightSummary:
    """The outcome of one round.

    Attributes:
        number: Flight number, from 1.
        pilot: Who flew.
        contracts: Titles of the contracts flown.
        report: How the flight went.
        launch_cost: Funds charged for the vessel at launch.
        contract_rewards: Rewards of the contracts completed (paid partly
            during the flight, partly when the tourists are recovered).
        completed: Contracts completed after the recovery.
        funds_before: Balance just before the launch.
        funds: Balance at the end of the round.
    """

    number: int
    pilot: str
    contracts: tuple[str, ...]
    report: FlightReport
    launch_cost: float
    contract_rewards: float
    completed: int
    funds_before: float
    funds: float

    @property
    def net(self) -> float:
        """Funds earned by the round, everything included.

        Returns:
            Balance change from just before the launch to the end.
        """
        return self.funds - self.funds_before

    @property
    def vessel_recovery(self) -> float:
        """Funds paid for the recovered vessel and debris.

        Measuring the recovery payout directly is not possible: KSP pays
        the tourists' itineraries in the same instant. It is what remains
        of the net once the launch and the contracts are accounted for, so
        it also holds any other income of the round (milestones, other
        contracts completed meanwhile).

        Returns:
            Estimated recovery payout, plus other income.
        """
        return self.net + self.launch_cost - self.contract_rewards


@dataclass(frozen=True)
class CampaignResult:
    """The outcome of the whole campaign.

    Attributes:
        reason: Why it stopped.
        flights: One summary per flight.
        start_funds: Balance when it started.
        end_funds: Balance when it stopped.
    """

    reason: StopReason
    flights: tuple[FlightSummary, ...]
    start_funds: float
    end_funds: float


class TourismCampaign:
    """Runs suborbital tourist flights until a funds target is reached.

    Responsibilities: choose and accept contracts, crew and launch the
    craft, delegate the flight, recover the vessel and its debris, and
    check that the contracts were completed. Anything unexpected (a
    contract not completed, a flight that did not reach space) stops the
    campaign with ``CampaignError`` instead of repeating the mistake.

    Usage notes: the flight is injected as a callable (normally
    ``SuborbitalFlightDirector.fly``) and the recovery as a
    ``VesselRecoveryService``, so tests can replace both.
    """

    def __init__(
        self,
        game: TourismGamePort,
        fly: Callable[[], FlightReport],
        recovery: VesselRecoveryService,
        options: CampaignOptions,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        """Create the campaign.

        Args:
            game: Port to the game.
            fly: Flies the launched vessel back to the ground.
            recovery: Recovers the vessel and its debris.
            options: What the player wants.
            sleep: Function used to wait for the game to settle.
        """
        self._game = game
        self._fly = fly
        self._recovery = recovery
        self._options = options
        self._sleep = sleep

    def next_plan(self, allow_offers: bool = True) -> PassengerPlan:
        """Choose the contracts of the next flight, without acting.

        Args:
            allow_offers: Whether offers may be added.

        Returns:
            The plan (empty when nothing can be flown).
        """
        contracts = self._game.read_tourism_contracts()
        available = {k.name for k in self._game.roster() if k.available}
        return plan_passengers(contracts, self._options.seats, available,
                               self._options.body, allow_offers=allow_offers)

    def run(self, on_flight: Callable[[FlightSummary], None] | None = None) -> CampaignResult:
        """Fly until the target is reached or no contract is left.

        Args:
            on_flight: Called after each flight, for progress output.

        Returns:
            Why the campaign stopped and what it did.

        Raises:
            CampaignError: When something needs the player's attention.
            FlightError: When a flight goes wrong.
            GameUnavailableError: When the game stops answering.
        """
        options = self._options
        start_funds = self._retry(self._game.funds)
        flights: list[FlightSummary] = []
        while True:
            funds = self._retry(self._game.funds)
            if funds >= options.target_funds:
                return CampaignResult(StopReason.TARGET_REACHED, tuple(flights), start_funds, funds)
            if options.max_flights is not None and len(flights) >= options.max_flights:
                return CampaignResult(StopReason.FLIGHT_LIMIT, tuple(flights), start_funds, funds)

            self._check_no_vessel_in_flight()
            plan = self._accept(self._retry(self.next_plan))
            if plan.is_empty:
                return CampaignResult(StopReason.NO_CONTRACTS, tuple(flights), start_funds, funds)

            summary = self._flight(len(flights) + 1, plan)
            flights.append(summary)
            if on_flight:
                on_flight(summary)

    # ------------------------------------------------------------- one round

    def _check_no_vessel_in_flight(self) -> None:
        """Refuse to go on while a vessel is flying.

        Launching switches to a new vessel, and KSP deletes a vessel left
        behind in the atmosphere, with whoever is on board. A vessel on
        the launch pad is fine: the launch recovers it first.

        Raises:
            CampaignError: When the active vessel is in the air or in space.
        """
        flight = self._retry(self._game.read_flight)
        if flight is not None and flight.situation not in ("pre_launch", "landed", "splashed"):
            raise CampaignError(
                f"{flight.vessel} is in flight ({flight.situation}); land and recover it "
                "first, then run the campaign again"
            )

    def _accept(self, plan: PassengerPlan) -> PassengerPlan:
        """Accept the offers of a plan and return what can really fly.

        KSP may refuse an acceptance (Mission Control limits how many
        contracts can be active), so the plan is rebuilt from the contracts
        that are actually active afterwards.

        Args:
            plan: The chosen contracts.

        Returns:
            The plan made only of accepted contracts.
        """
        if not plan.accept:
            return plan
        for contract in plan.accept:
            logger.info("Accepting: %s (%d tourist(s), %.0f funds)",
                        contract.title, contract.seats, contract.reward)
            self._game.accept_contract(contract.contract_id)
        self._sleep(1.0)  # the roster is updated on the next frames
        for contract in plan.accept:
            status = self._game.contract_status(contract.contract_id)
            if status is not ContractStatus.ACTIVE:
                logger.warning("The game did not accept '%s' (%s); is the active contract "
                               "limit of Mission Control reached?", contract.title, status.value)
        accepted = self._retry(lambda: self.next_plan(allow_offers=False))
        if accepted.is_empty:
            # Something accepted but nobody can board: flying empty would
            # only burn funds, and stopping as "no contracts" would hide it.
            raise CampaignError(
                "no accepted contract can fly: the game refused the offers or their "
                "tourists are not available at the space center"
            )
        return accepted

    def _flight(self, number: int, plan: PassengerPlan) -> FlightSummary:
        """Crew, launch, fly and recover one flight.

        Args:
            number: Flight number, from 1.
            plan: Contracts to fly, all already accepted.

        Returns:
            What happened.

        Raises:
            CampaignError: When no pilot is available, the flight did not
                reach space or the contracts were not completed.
        """
        options = self._options
        pilot = choose_pilot(self._game.roster(), number - 1, options.pilots)
        if pilot is None:
            raise CampaignError("no pilot is available at the space center")
        tourists = [t.kerbal for c in plan.contracts for t in c.tourists if t.kerbal]
        logger.info("Flight %d: pilot %s, tourists %s", number, pilot, ", ".join(tourists))

        funds_before = self._retry(self._game.funds)
        vessel = self._game.launch(options.craft, [pilot, *tourists])
        launch_cost = funds_before - self._retry(self._game.funds)
        report = self._fly()
        self._recover(vessel)

        statuses = self._wait_for_completion(plan.contracts)
        done = [c for c, s in zip(plan.contracts, statuses) if s is ContractStatus.COMPLETED]
        funds = self._retry(self._game.funds)
        summary = FlightSummary(
            number, pilot, tuple(c.title for c in plan.contracts), report,
            launch_cost=launch_cost,
            contract_rewards=sum(c.reward for c in done),
            completed=len(done),
            funds_before=funds_before,
            funds=funds,
        )
        completed = len(done)
        if not report.reached_space:
            raise CampaignError(
                f"flight {number} only reached {report.max_altitude:,.0f} m: the craft "
                "cannot get the tourists to space as it is"
            )
        if completed < len(plan.contracts):
            raise CampaignError(
                f"only {completed} of {len(plan.contracts)} contract(s) were completed "
                f"after flight {number}; check them in Mission Control"
            )
        return summary

    def _recover(self, vessel: str) -> float:
        """Recover the landed vessel and the pieces that came off it.

        Only vessels of this flight are recovered: the vessel itself and
        its debris ("X Debris", "Escombros de X"). Other landed vessels of
        the save are left alone.

        Args:
            vessel: Name of the flown vessel.

        Returns:
            Funds received.
        """
        decisions = [
            d if owner_vessel(d.vessel.name) == vessel
            else replace(d, recover=False, reason="not part of this flight")
            for d in self._recovery.plan(RecoveryPolicy(include_active=True))
        ]
        outcomes = self._recovery.execute(decisions)
        for outcome in outcomes:
            logger.info("Recovery of %s: %s%s", outcome.decision.vessel.name, outcome.message,
                        "" if outcome.payout is None else f" (+{outcome.payout:,.0f})")
        if not any(o.recovered and o.decision.vessel.is_active for o in outcomes):
            raise CampaignError(f"{vessel} could not be recovered; recover it by hand")
        return sum(o.payout or 0.0 for o in outcomes if o.recovered)

    def _wait_for_completion(
        self, contracts: tuple[TourismContract, ...], timeout: float = 15.0
    ) -> list[ContractStatus]:
        """Wait until the game completes the contracts of the flight.

        KSP completes a tourism contract when its tourists are recovered,
        a moment after the recovery.

        Args:
            contracts: The contracts flown.
            timeout: Seconds to wait.

        Returns:
            The status of each contract, in order.
        """
        statuses: list[ContractStatus] = []
        waited = 0.0
        while waited <= timeout:
            statuses = [self._retry(lambda c=c: self._game.contract_status(c.contract_id))
                        for c in contracts]
            if all(s is not ContractStatus.ACTIVE for s in statuses):
                break
            self._sleep(1.0)
            waited += 1.0
        return statuses

    def _retry(self, read: Callable[[], T], attempts: int = 30) -> T:
        """Run a read that may fail while the game loads a scene.

        After a recovery the game moves to the space center, and for a few
        seconds kRPC refuses most calls.

        Args:
            read: The read to run.
            attempts: Tries, one second apart.

        Returns:
            What the read returns.

        Raises:
            GameUnavailableError: When it still fails after every attempt.
        """
        for attempt in range(attempts):
            try:
                return read()
            except GameUnavailableError:
                if attempt == attempts - 1:
                    raise
                self._sleep(1.0)
        raise AssertionError("unreachable")
