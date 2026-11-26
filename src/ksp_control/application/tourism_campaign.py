"""Use case: earn funds flying tourists to space and back, over and over.

Each round of the campaign:

1. stops when the funds reach the target;
2. picks the tourists of the next flight (tourists of accepted contracts
   first, then offers to fill the seats) and accepts the offers; a
   contract with more tourists than seats is split over several flights;
   suborbital and orbital tourists fly together, and the flight is orbital
   as soon as one of them wants an orbit;
3. launches the craft with a pilot (taking turns) and the tourists;
4. flies the mission (``TourismFlightDirector``), staying in orbit until
   the game counts it for the tourists;
5. recovers the vessel and whatever pieces of it landed, which brings the
   tourists home, completes their itineraries and, once a contract has no
   tourist left waiting, the contract;

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
from ksp_control.application.tourism_flight import FlightReport
from ksp_control.application.vessel_recovery import VesselRecoveryService
from ksp_control.domain.ledger import owner_vessel
from ksp_control.domain.recovery import RecoveryPolicy
from ksp_control.domain.tourism import (
    Boarding,
    ContractStatus,
    Mission,
    PassengerPlan,
    choose_pilot,
    orbit_visited,
    plan_passengers,
    tourist_mission,
)

logger = logging.getLogger(__name__)

T = TypeVar("T")

# Passenger seats assumed until the craft is seen on the launch pad.
DEFAULT_SEATS = 4


class CampaignError(RuntimeError):
    """The campaign cannot go on without the player (see the message)."""


class StopReason(Enum):
    """Why the campaign ended normally."""

    TARGET_REACHED = "target reached"
    NO_CONTRACTS = "no tourism contracts left that the campaign can fly"
    FLIGHT_LIMIT = "flight limit reached"


@dataclass(frozen=True)
class CampaignOptions:
    """What the player wants from the campaign.

    Attributes:
        craft: Name of the VAB craft to launch.
        target_funds: Stop once the funds reach this amount.
        seats: Passenger seats of the craft (the pilot's seat excluded)
            for the first flight; ``None`` assumes ``DEFAULT_SEATS``. Each
            launch reads the real number from the craft, which the next
            flights use.
        pilots: Pilots to take turns, in order; every hired pilot if empty.
        max_flights: Stop after this many flights (``None``: no limit).
        body: Body launched from.
        max_mission: Most demanding mission the craft may fly; tourists
            needing more are left alone.
        orbit_test: Fly every flight to orbit, but board only tourists
            that want a suborbital flight: a way to check that the craft
            reaches orbit and comes back without risking any contract
            (falling short still completes their suborbital flight).
    """

    craft: str
    target_funds: float
    seats: int | None = None
    pilots: tuple[str, ...] = ()
    max_flights: int | None = None
    body: str = "Kerbin"
    max_mission: Mission = Mission.ORBITAL
    orbit_test: bool = False

    @property
    def boarding_mission(self) -> Mission:
        """Most demanding mission a tourist may need to board.

        Returns:
            Suborbital in an orbit test, ``max_mission`` otherwise.
        """
        return Mission.SUBORBITAL if self.orbit_test else self.max_mission


@dataclass(frozen=True)
class FlightSummary:
    """The outcome of one round.

    Attributes:
        number: Flight number, from 1.
        pilot: Who flew.
        contracts: The contracts flown, described by their tourists.
        report: How the flight went.
        launch_cost: Funds charged for the vessel at launch.
        contract_rewards: Rewards earned by the tourists delivered (their
            itineraries) and by the contracts this flight completed.
        completed: Contracts completed after the recovery.
        funds_before: Balance just before the launch.
        funds: Balance at the end of the round.
        tourists: Tourists on board.
        delivered: Tourists whose itinerary was completed after the
            recovery.
        waiting: Tourists of the flown contracts left for later flights.
        mission: Suborbital hop or orbital flight.
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
    tourists: int = 0
    delivered: int = 0
    waiting: int = 0
    mission: Mission = Mission.SUBORBITAL

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
    """Runs tourist flights to space until a funds target is reached.

    Responsibilities: choose and accept contracts, crew and launch the
    craft, delegate the flight, recover the vessel and its debris, and
    check that every tourist on board had their itinerary completed.
    Anything unexpected (a tourist not delivered, a flight that did not
    reach space, or orbit when it had to) stops the campaign with
    ``CampaignError`` instead of repeating the mistake.

    Contracts with more tourists than seats are accepted and flown over
    several flights: the tourists left behind board first on the next
    ones, since they belong to an accepted contract.

    The passenger seats come from the options for the first flight; every
    launch then reads the crew capacity of the craft on the pad and the
    next flights plan with it. Tourists that did not fit stay at the space
    center and fly next.

    Usage notes: the flight is injected as a callable taking the mission
    and the orbit check (normally ``TourismFlightDirector.fly``) and the
    recovery as a
    ``VesselRecoveryService``, so tests can replace both.
    """

    def __init__(
        self,
        game: TourismGamePort,
        fly: Callable[[Mission, Callable[[], bool] | None], FlightReport],
        recovery: VesselRecoveryService,
        options: CampaignOptions,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        """Create the campaign.

        Args:
            game: Port to the game.
            fly: Flies the launched vessel on a mission and back to the
                ground; the second argument tells whether the game counts
                the orbit for every tourist on board.
            recovery: Recovers the vessel and its debris.
            options: What the player wants.
            sleep: Function used to wait for the game to settle.
        """
        self._game = game
        self._fly = fly
        self._recovery = recovery
        self._options = options
        self._sleep = sleep
        self._seats = options.seats if options.seats is not None else DEFAULT_SEATS
        self._on_flight: Callable[[FlightSummary], None] | None = None

    @property
    def seats(self) -> int:
        """Passenger seats the next flight is planned with.

        Returns:
            The seats given in the options until a launch shows the craft's
            real number.
        """
        return self._seats

    def next_plan(self, allow_offers: bool = True) -> PassengerPlan:
        """Choose the contracts of the next flight, without acting.

        Args:
            allow_offers: Whether offers may be added.

        Returns:
            The plan (empty when nothing can be flown).
        """
        contracts = self._game.read_tourism_contracts()
        available = {k.name for k in self._game.roster() if k.available}
        return plan_passengers(contracts, self._seats, available,
                               self._options.body, allow_offers=allow_offers,
                               max_mission=self._options.boarding_mission)

    def run(self, on_flight: Callable[[FlightSummary], None] | None = None) -> CampaignResult:
        """Fly until the target is reached or no contract is left.

        Args:
            on_flight: Called after each flight, for progress output, also
                for a flight that stops the campaign.

        Returns:
            Why the campaign stopped and what it did.

        Raises:
            CampaignError: When something needs the player's attention.
            FlightError: When a flight goes wrong.
            GameUnavailableError: When the game stops answering.
        """
        options = self._options
        self._on_flight = on_flight
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

            flights.append(self._flight(len(flights) + 1, plan))

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
        for boarding in plan.accept:
            contract = boarding.contract
            logger.info("Accepting: %s (%s; %s funds)", contract.title, contract.describe(),
                        f"{contract.reward:,.0f}")
            if boarding.waiting:
                logger.info("  %d of them fly now, %d on later flight(s)",
                            len(boarding.tourists), boarding.waiting)
            self._game.accept_contract(contract.contract_id)
        self._sleep(1.0)  # the roster is updated on the next frames
        for boarding in plan.accept:
            contract = boarding.contract
            status = self._game.contract_status(contract.contract_id)
            if status is not ContractStatus.ACTIVE:
                logger.warning("The game did not accept '%s' (%s; %s); is the active contract "
                               "limit of Mission Control reached?", contract.title,
                               contract.describe(), status.value)
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
                reach space or a tourist's itinerary was not completed.
        """
        options = self._options
        pilot = choose_pilot(self._game.roster(), number - 1, options.pilots)
        if pilot is None:
            raise CampaignError("no pilot is available at the space center")
        # plan_passengers only boards tourists matched to the roster.
        tourists = [t.kerbal for t in plan.tourists if t.kerbal]
        mission = Mission.ORBITAL if options.orbit_test else plan.mission
        logger.info("Flight %d (%s%s): pilot %s, tourists %s", number, mission.label,
                    ", orbit test" if options.orbit_test else "", pilot, ", ".join(tourists))
        for boarding in plan.boardings:
            if boarding.left_behind:
                logger.info("  Waiting for a later flight: %s",
                            ", ".join(t.kerbal or t.itinerary for t in boarding.left_behind))

        funds_before = self._retry(self._game.funds)
        vessel = self._game.launch(options.craft, [pilot, *tourists])
        launch_cost = funds_before - self._retry(self._game.funds)
        plan = self._check_crew(plan)
        if not options.orbit_test:
            mission = plan.mission
        report = self._fly(mission, lambda: self._orbit_counted(plan))
        self._recover(vessel)

        statuses, delivered = self._wait_for_delivery(plan)
        # Rewards paid between the launch and now: the itineraries of the
        # tourists delivered, and the contract's own completion reward
        # (``bonus``) when the contract was completed. The advance was paid
        # on acceptance, before the launch, but KSP tourism contracts have
        # none, so ``bonus`` is only the completion reward in practice.
        rewards = 0.0
        completed = 0
        for boarding in plan.boardings:
            contract = boarding.contract
            rewards += sum(t.reward for t in boarding.tourists
                           if (contract.contract_id, t.itinerary) in delivered)
            if statuses[contract.contract_id] is ContractStatus.COMPLETED:
                rewards += contract.bonus
                completed += 1
        funds = self._retry(self._game.funds)
        summary = FlightSummary(
            number, pilot, tuple(c.describe() for c in plan.contracts), report,
            launch_cost=launch_cost,
            contract_rewards=rewards,
            completed=completed,
            funds_before=funds_before,
            funds=funds,
            tourists=plan.seats_used,
            delivered=len(delivered),
            waiting=sum(b.waiting for b in plan.boardings),
            mission=mission,
        )
        # Reported before any check below stops the campaign, so the
        # flight's outcome is always shown.
        if self._on_flight:
            self._on_flight(summary)
        if not report.reached_space:
            raise CampaignError(
                f"flight {number} only reached {report.max_altitude:,.0f} m: the craft "
                "cannot get the tourists to space as it is"
            )
        if mission is Mission.ORBITAL and not report.reached_orbit:
            if report.orbit_delta_v_needed is not None and report.delta_v_launch is not None:
                left = (f" (it had {report.delta_v_launch:,.0f} m/s of delta-v on the pad; an "
                        f"orbit needs about {report.orbit_delta_v_needed:,.0f}), so it flew a "
                        "suborbital hop")
            elif report.delta_v_orbit is not None:
                left = f" ({report.delta_v_orbit:,.0f} m/s of delta-v left above the atmosphere)"
            else:
                left = ""
            raise CampaignError(
                f"flight {number} did not reach orbit{left}: the craft needs more fuel or "
                "thrust; until then run the campaign with --suborbital-only"
            )
        if len(delivered) < plan.seats_used:
            raise CampaignError(
                f"only {len(delivered)} of {plan.seats_used} tourist(s) completed their "
                f"itinerary after flight {number}; check the contracts in Mission Control"
            )
        return summary

    def _check_crew(self, plan: PassengerPlan) -> PassengerPlan:
        """Learn the craft's seats on the pad and see who really boarded.

        The craft is only known once it is on the pad: its crew capacity,
        minus the pilot's seat, is what the next flights plan with. If it
        holds fewer than were boarded, the tourists left out simply stay
        at the space center: the flight goes on with those on board, and
        the others fly next (their contract is already accepted).

        Args:
            plan: The tourists meant to fly.

        Returns:
            The plan reduced to the tourists actually on board.
        """
        capacity, aboard = self._retry(self._game.read_crew)
        seats = max(0, capacity - 1)
        if seats != self._seats:
            logger.info("%s has %d passenger seat(s) (%d in all, pilot included): the next "
                        "flights plan with %d", self._options.craft, seats, capacity, seats)
            self._seats = seats
        on_board = set(aboard)
        missing = [t for t in plan.tourists if t.kerbal not in on_board]
        if not missing:
            return plan
        logger.warning("No seat for %s: they wait for the next flight",
                       ", ".join(t.kerbal or t.itinerary for t in missing))

        def keep(boardings: tuple[Boarding, ...]) -> tuple[Boarding, ...]:
            """Boardings reduced to the tourists on board (empty ones dropped)."""
            kept = (Boarding(b.contract, tuple(t for t in b.tourists if t.kerbal in on_board))
                    for b in boardings)
            return tuple(b for b in kept if b.tourists)

        carry, accept = keep(plan.carry), keep(plan.accept)
        missions = [tourist_mission(t, self._options.body) for b in (*carry, *accept)
                    for t in b.tourists]
        mission = max((m for m in missions if m is not None), default=Mission.SUBORBITAL)
        return PassengerPlan(carry=carry, accept=accept, mission=mission)

    def _orbit_counted(self, plan: PassengerPlan) -> bool:
        """Tell whether the game counts the orbit for every tourist on board.

        Read during the flight, while the vessel is in orbit: the game
        marks each destination of an itinerary as soon as it is reached.

        Args:
            plan: The flight's tourists.

        Returns:
            True when every orbit destination of every tourist on board is
            marked as reached (also when nobody wants an orbit).
        """
        current = {c.contract_id: c for c in self._retry(self._game.read_tourism_contracts)}
        for boarding in plan.boardings:
            contract = current.get(boarding.contract.contract_id)
            if contract is None:
                continue  # no longer active: nothing left to wait for
            by_itinerary = {t.itinerary: t for t in contract.tourists}
            for tourist in boarding.tourists:
                now = by_itinerary.get(tourist.itinerary, tourist)
                if not orbit_visited(now, self._options.body):
                    return False
        return True

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

    def _wait_for_delivery(
        self, plan: PassengerPlan, timeout: float = 15.0
    ) -> tuple[dict[str, ContractStatus], set[tuple[str, str]]]:
        """Wait until the game completes the itineraries of the flight.

        KSP completes a tourist's itinerary when the tourist is recovered,
        a moment after the recovery, and the contract once every tourist of
        it is delivered. A contract split over several flights stays active
        after this one, so the tourists are checked one by one: a
        completed contract delivered all of them; for one still active, its
        itinerary objectives are read again.

        Args:
            plan: The flight's tourists.
            timeout: Seconds to wait.

        Returns:
            The status of each contract flown, by id, and the tourists
            delivered, as ``(contract id, itinerary title)`` pairs.
        """
        on_board = {(b.contract.contract_id, t.itinerary) for b in plan.boardings
                    for t in b.tourists}
        finishing = [b.contract for b in plan.boardings if b.completes_contract]
        statuses: dict[str, ContractStatus] = {}
        delivered: set[tuple[str, str]] = set()
        waited = 0.0
        while True:
            statuses = {c.contract_id: self._retry(
                lambda c=c: self._game.contract_status(c.contract_id)) for c in plan.contracts}
            delivered = {(cid, itinerary) for cid, itinerary in on_board
                         if statuses[cid] is ContractStatus.COMPLETED}
            if any(s is ContractStatus.ACTIVE for s in statuses.values()):
                current = {c.contract_id: c for c in self._retry(self._game.read_tourism_contracts)}
                for cid, status in statuses.items():
                    if status is ContractStatus.ACTIVE and cid in current:
                        done = {t.itinerary for t in current[cid].tourists if t.completed}
                        delivered |= {(c, i) for c, i in on_board if c == cid and i in done}
            # Done when everyone is delivered and the contracts this flight
            # finishes are closed (KSP completes the contract a moment after
            # the last itinerary), or when nothing can change any more
            # (every contract completed, failed or cancelled).
            closing = all(statuses[c.contract_id] is not ContractStatus.ACTIVE
                          for c in finishing)
            settled = all(s is not ContractStatus.ACTIVE for s in statuses.values())
            if (delivered >= on_board and closing) or settled or waited >= timeout:
                return statuses, delivered
            self._sleep(1.0)
            waited += 1.0

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
