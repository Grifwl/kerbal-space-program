"""Rules for flying tourists on suborbital hops (pure, no I/O).

A tourism contract carries one or more tourists, each with an itinerary of
destinations ("Suborbital spaceflight at Kerbin", "Flyby of the Mun"...).
The tourism campaign only takes contracts whose every destination can be
reached by a suborbital hop from the launch pad and packs as many tourists
as fit in the craft. This module decides:

* which contracts a suborbital hop satisfies;
* which contracts to fly next (already accepted first, then offers);
* which pilot flies next (rotation);
* when to stage during the ascent and how to throttle towards the target
  apoapsis.

Everything here works on plain values so it can be tested without the game.
"""

from __future__ import annotations

import itertools
import re
from dataclasses import dataclass
from enum import Enum

# Words that identify a suborbital destination in the objective titles.
# "Vuelo espacial suborbital en Kerbin" (Spanish) and "Suborbital
# spaceflight at Kerbin" (English) both contain "suborbital".
SUBORBITAL_WORDS = ("suborbital",)

# Crew trait of the kerbals that can fly a crewed command pod.
PILOT_TRAIT = "Pilot"


class ContractStatus(str, Enum):
    """Where a contract is in its life cycle, as far as the campaign cares."""

    OFFERED = "offered"
    ACTIVE = "active"
    COMPLETED = "completed"
    FAILED = "failed"
    OTHER = "other"  # cancelled, declined, expired, withdrawn...

    @classmethod
    def from_name(cls, name: str | None) -> "ContractStatus":
        """Map a game contract state name to a status.

        Args:
            name: Name such as ``"active"`` or ``"ContractState.offered"``.

        Returns:
            The matching status, ``OTHER`` when it is none of the known ones.
        """
        normalized = (name or "").rsplit(".", 1)[-1].lower()
        for status in cls:
            if status.value == normalized:
                return status
        return cls.OTHER


@dataclass(frozen=True)
class Tourist:
    """One tourist of a contract.

    Attributes:
        itinerary: Title of the tourist's objective ("Samroda's itinerary").
        destinations: Titles of the places the tourist wants to visit.
        kerbal: Full roster name of the tourist, when it could be matched.
    """

    itinerary: str
    destinations: tuple[str, ...]
    kerbal: str | None = None


@dataclass(frozen=True)
class TourismContract:
    """A tourism contract, offered or accepted.

    Attributes:
        contract_id: Identifier valid for the adapter that read it.
        title: Contract title.
        status: Life-cycle status.
        tourists: The tourists to fly.
        reward: Funds paid in total (advance, objectives and completion).
    """

    contract_id: str
    title: str
    status: ContractStatus
    tourists: tuple[Tourist, ...]
    reward: float = 0.0

    @property
    def seats(self) -> int:
        """Seats needed to fly every tourist of the contract at once.

        Returns:
            Number of tourists.
        """
        return len(self.tourists)


@dataclass(frozen=True)
class Kerbal:
    """A member of the astronaut roster.

    Attributes:
        name: Full name ("Jebediah Kerman").
        kind: Roster type: ``crew``, ``tourist``, ``applicant``, ``unowned``.
        trait: Profession ("Pilot", "Engineer", "Scientist", "Tourist").
        available: Whether the kerbal is at the space center and can board.
    """

    name: str
    kind: str
    trait: str
    available: bool


@dataclass(frozen=True)
class PassengerPlan:
    """The contracts flown on the next flight.

    Attributes:
        carry: Contracts already accepted whose tourists board this flight.
        accept: Offered contracts to accept now and fly this flight.
    """

    carry: tuple[TourismContract, ...] = ()
    accept: tuple[TourismContract, ...] = ()

    @property
    def contracts(self) -> tuple[TourismContract, ...]:
        """Every contract of the flight.

        Returns:
            Accepted contracts first, then the offers to accept.
        """
        return self.carry + self.accept

    @property
    def seats_used(self) -> int:
        """Seats taken by the tourists of the plan.

        Returns:
            Number of tourists.
        """
        return sum(c.seats for c in self.contracts)

    @property
    def reward(self) -> float:
        """Funds the plan pays when every contract is completed.

        Returns:
            Total reward.
        """
        return sum(c.reward for c in self.contracts)

    @property
    def is_empty(self) -> bool:
        """Whether there is nobody to fly.

        Returns:
            True when the plan has no contract.
        """
        return not self.contracts


def is_suborbital_destination(title: str, body: str = "Kerbin") -> bool:
    """Tell whether a destination is a suborbital flight at a body.

    Args:
        title: Destination title, e.g. ``"Vuelo espacial suborbital en Kerbin"``.
        body: Body launched from.

    Returns:
        True when the title names a suborbital flight at that body.
    """
    text = title.casefold()
    has_word = any(word in text for word in SUBORBITAL_WORDS)
    # Whole-word match so "Kerbin" does not match inside another name.
    has_body = re.search(rf"\b{re.escape(body.casefold())}\b", text) is not None
    return has_word and has_body


def is_suborbital_hop(contract: TourismContract, body: str = "Kerbin") -> bool:
    """Tell whether one suborbital hop completes a tourism contract.

    Every tourist must want only suborbital flights at the launch body: a
    single other destination (an orbit, a flyby of the Mun...) would leave
    the contract unfinished after the hop.

    Args:
        contract: The contract.
        body: Body launched from.

    Returns:
        True when the hop satisfies every destination of every tourist.
    """
    if not contract.tourists:
        return False
    return all(
        tourist.destinations
        and all(is_suborbital_destination(d, body) for d in tourist.destinations)
        for tourist in contract.tourists
    )


def match_kerbal(itinerary: str, roster_names: list[str]) -> str | None:
    """Find the roster member an itinerary objective is about.

    Objective titles name the tourist by first name only ("Itinerario de
    viaje de Samroda"), in a language dependent sentence. Looking for each
    roster first name as a whole word works in any language.

    Args:
        itinerary: Title of the tourist's objective.
        roster_names: Full names of every kerbal in the roster.

    Returns:
        The full name, or ``None`` when no single kerbal matches.
    """
    matches = []
    for name in roster_names:
        first = name.split()[0] if name.split() else ""
        if first and re.search(rf"(?<!\w){re.escape(first)}(?!\w)", itinerary):
            matches.append(name)
    # Two kerbals sharing a first name cannot be told apart from the title.
    return matches[0] if len(matches) == 1 else None


def _boardable(contract: TourismContract, available: set[str]) -> bool:
    """Tell whether every tourist of an accepted contract can board now.

    Args:
        contract: An active contract.
        available: Full names of the kerbals available at the space center.

    Returns:
        True when all its tourists were matched and are available.
    """
    return all(t.kerbal is not None and t.kerbal in available for t in contract.tourists)


def plan_passengers(
    contracts: list[TourismContract],
    seats: int,
    available: set[str],
    body: str = "Kerbin",
    allow_offers: bool = True,
    max_offers_considered: int = 16,
) -> PassengerPlan:
    """Choose the contracts to fly on the next suborbital hop.

    Contracts already accepted come first, in order, as long as they fit:
    their tourists are waiting and their deadlines run. The seats left are
    filled with offers, picking the combination that seats the most
    tourists and, among those, pays the most. Offers are few (a handful at
    a time), so trying every combination is cheap.

    Args:
        contracts: Tourism contracts, offered and active.
        seats: Passenger seats of the craft (pilot excluded).
        available: Full names of the kerbals available at the space center.
        body: Body launched from.
        allow_offers: Whether offers may be accepted.
        max_offers_considered: Best offers (by reward per seat) tried.

    Returns:
        The plan; empty when nothing can be flown.
    """
    hops = [c for c in contracts if is_suborbital_hop(c, body)]

    carry: list[TourismContract] = []
    free = seats
    for contract in hops:
        if contract.status is ContractStatus.ACTIVE and _boardable(contract, available):
            if 0 < contract.seats <= free:
                carry.append(contract)
                free -= contract.seats

    accept: tuple[TourismContract, ...] = ()
    offers = [c for c in hops if c.status is ContractStatus.OFFERED and 0 < c.seats <= free]
    if allow_offers and offers and free > 0:
        offers.sort(key=lambda c: c.reward / c.seats, reverse=True)
        offers = offers[:max_offers_considered]
        best: tuple[int, float] = (0, 0.0)
        # Smaller combinations are tried first and only a strictly better
        # score replaces the best one, so among equal (seats, reward) the
        # plan with fewer contracts wins (fewer acceptances to go wrong).
        for size in range(1, len(offers) + 1):
            for combo in itertools.combinations(offers, size):
                used = sum(c.seats for c in combo)
                if used > free:
                    continue
                score = (used, sum(c.reward for c in combo))
                if score > best:
                    best, accept = score, combo
    return PassengerPlan(carry=tuple(carry), accept=tuple(accept))


def choose_pilot(roster: list[Kerbal], flight_number: int, preferred: tuple[str, ...] = ()) -> str | None:
    """Pick the pilot of a flight, taking turns between the pilots.

    Args:
        roster: The astronaut roster.
        flight_number: Number of the flight in the campaign, from 0.
        preferred: Only these pilots, in this order; every hired pilot
            when empty.

    Returns:
        The pilot's name, or ``None`` when no pilot is available.
    """
    pilots = [k for k in roster if k.kind == "crew" and k.trait == PILOT_TRAIT]
    if preferred:
        by_name = {k.name.casefold(): k for k in roster}
        pilots = [by_name[name.casefold()] for name in preferred if name.casefold() in by_name]
    else:
        pilots.sort(key=lambda k: k.name)
    if not pilots:
        return None
    # Rotate over the whole list, so the turn does not shift when a pilot
    # is temporarily unavailable; skip the unavailable ones.
    for offset in range(len(pilots)):
        candidate = pilots[(flight_number + offset) % len(pilots)]
        if candidate.available:
            return candidate.name
    return None


# --------------------------------------------------------------------- ascent


@dataclass(frozen=True)
class EngineState:
    """What the ascent logic needs to know about one engine.

    Attributes:
        active: Whether the engine has been ignited.
        has_fuel: Whether it still has propellant.
        decouple_stage: Stage that detaches it from the vessel (-1: never).
    """

    active: bool
    has_fuel: bool
    decouple_stage: int


@dataclass(frozen=True)
class FlightTelemetry:
    """One reading of the active vessel during the hop.

    Attributes:
        vessel: Vessel name.
        situation: KSP situation (``pre_launch``, ``flying``,
            ``sub_orbital``, ``landed``, ``splashed``...).
        altitude: Altitude above sea level, in metres.
        vertical_speed: Surface-relative vertical speed, m/s (up > 0).
        surface_speed: Surface-relative speed, m/s.
        apoapsis: Apoapsis altitude above sea level, in metres.
        time_to_apoapsis: Seconds to the apoapsis.
        ut: Universal time, in seconds.
        current_stage: Stage of the vessel (``Control.current_stage``).
        parachute_stage: Highest activation stage of a parachute, or
            ``None`` when the vessel has none left.
        atmosphere_depth: Height of the atmosphere of the body, in metres.
        drag: Aerodynamic drag on the vessel, in newtons.
        weight: Weight of the vessel at its altitude, in newtons.
        dynamic_pressure: Dynamic pressure of the air flow, in pascals.
    """

    vessel: str
    situation: str
    altitude: float
    vertical_speed: float
    surface_speed: float
    apoapsis: float
    time_to_apoapsis: float
    ut: float
    current_stage: int
    parachute_stage: int | None
    atmosphere_depth: float
    drag: float = 0.0
    weight: float = 0.0
    dynamic_pressure: float = 0.0

    @property
    def on_ground(self) -> bool:
        """Whether the vessel is resting on the ground or the sea.

        Returns:
            True when landed or splashed down.
        """
        return self.situation in ("landed", "splashed")


def should_stage_on_ascent(current_stage: int, keep_stage: int, engines: list[EngineState]) -> bool:
    """Decide whether to activate the next stage while climbing.

    The next stage is activated when every engine it drops has burnt out
    (boosters, asparagus side engines), or when no engine is pushing at
    all. Stages from ``keep_stage`` down (the parachutes) are never
    activated on the way up. Requiring *every* dropped engine to be empty
    avoids dropping boosters that still burn because another engine ran
    dry first.

    Args:
        current_stage: Stage of the vessel (``Control.current_stage``).
        keep_stage: Highest stage kept for the descent (the parachutes).
        engines: The engines of the vessel.

    Returns:
        True when the next stage should be activated now.
    """
    next_stage = current_stage - 1
    if next_stage <= keep_stage:
        return False
    if not any(e.active and e.has_fuel for e in engines):
        return True
    dropped = [e for e in engines if e.decouple_stage == next_stage]
    return bool(dropped) and all(not e.has_fuel for e in dropped)


def ascent_throttle(apoapsis: float, target_apoapsis: float) -> float:
    """Throttle while climbing through the atmosphere.

    Full throttle until the apoapsis reaches the target, then coast. Drag
    lowers the apoapsis while still in the air, so the engines are lit
    again if it drops below the target.

    Args:
        apoapsis: Current apoapsis altitude, in metres.
        target_apoapsis: Wanted apoapsis altitude, in metres.

    Returns:
        1.0 or 0.0.
    """
    return 1.0 if apoapsis < target_apoapsis else 0.0


def air_limited_throttle(
    previous: float,
    dynamic_pressure: float,
    max_dynamic_pressure: float,
    drag: float,
    weight: float,
    max_drag_ratio: float,
    dt: float,
    gain: float = 1.0,
    floor: float = 0.1,
) -> float:
    """Throttle that eases off while the air pushes back too hard.

    Two limits, either of which lowers the throttle:

    * **Dynamic pressure** (q = rho * v^2 / 2: thick air and high speed
      together). It is what makes the air "boil" around the rocket, heats
      it and makes the drag climb steeply near the speed of sound.
      Autopilots such as MechJeb limit it the same way ("Limit Q"). The
      drag of a rocket is roughly proportional to it, so it is the main
      limit.
    * **Drag over weight**: above 1 the rocket climbs faster than its
      terminal velocity, which only burns fuel pushing air. Heavy rockets
      with a small frontal area have terminal velocities of hundreds of
      m/s in KSP (about 500 m/s at sea level for a 14 t tourist rocket),
      so this one rarely acts; it is a guard.

    It is an integral controller: the throttle moves at a rate
    proportional to how far the worst of the two is above its limit (or
    below it, raising the throttle again as the air thins). That gives
    smooth changes instead of the on/off chatter of a threshold.

    Args:
        previous: Throttle limit of the previous reading (0-1).
        dynamic_pressure: Dynamic pressure, in pascals.
        max_dynamic_pressure: Allowed dynamic pressure, in pascals; 0 or
            less disables this limit.
        drag: Drag force, in newtons.
        weight: Weight, in newtons.
        max_drag_ratio: Allowed drag as a multiple of the weight; 0 or less
            disables this limit.
        dt: Seconds since the previous reading.
        gain: Throttle change per second per unit of relative excess.
        floor: Lowest throttle: the engines are never shut down for air.

    Returns:
        The new throttle limit, between ``floor`` and 1.
    """
    loads = []
    if max_dynamic_pressure > 0:
        loads.append(dynamic_pressure / max_dynamic_pressure)
    if max_drag_ratio > 0 and weight > 0:
        loads.append(drag / weight / max_drag_ratio)
    if not loads:
        return 1.0
    # A load of 1.0 is exactly at its limit; above it the throttle drops.
    worst = max(loads)
    value = previous + gain * (1.0 - worst) * max(dt, 0.0)
    return min(1.0, max(floor, value))


def ascent_pitch(altitude: float, turn_start: float, turn_end: float, final_pitch: float) -> float:
    """Pitch to hold at an altitude during the climb.

    Straight up until ``turn_start``, then tilting linearly to
    ``final_pitch`` at ``turn_end``, and held there. A gradual turn keeps
    the nose close to the air flow while the air is thick; an abrupt tilt
    at speed would push the rocket sideways into the wind.

    Args:
        altitude: Current altitude, in metres.
        turn_start: Altitude where the turn starts.
        turn_end: Altitude where the turn ends.
        final_pitch: Pitch at the end of the turn, in degrees.

    Returns:
        The pitch, in degrees above the horizon.
    """
    if altitude <= turn_start:
        return 90.0
    if altitude >= turn_end or turn_end <= turn_start:
        return final_pitch
    progress = (altitude - turn_start) / (turn_end - turn_start)
    return 90.0 + (final_pitch - 90.0) * progress


def reentry_time(ut: float, time_to_apoapsis: float) -> float:
    """Estimate when a vertical hop falls back into the atmosphere.

    Outside the atmosphere the trajectory is a Kepler arc, symmetric about
    the apoapsis: it takes as long to come back down to the edge of the
    atmosphere as it took to climb from it to the apoapsis.

    Args:
        ut: Universal time when the vessel leaves the atmosphere.
        time_to_apoapsis: Seconds from then to the apoapsis.

    Returns:
        Universal time of the reentry.
    """
    return ut + 2.0 * max(time_to_apoapsis, 0.0)


@dataclass(frozen=True)
class FlightProfile:
    """How to fly the suborbital hop.

    Attributes:
        target_apoapsis: Apoapsis to aim for, in metres. It must be above
            the atmosphere (70 km at Kerbin) for the vessel to become
            suborbital, which is what the tourists pay for.
        pitch: Pitch reached at the end of the turn, in degrees (90 =
            straight up). The tilt to the east brings the vessel down in
            the sea east of the space center instead of on top of it: a
            near-vertical hop falls back almost where it started.
        heading: Compass heading of the tilt, in degrees.
        turn_altitude: Altitude where the turn starts, in metres.
        turn_end_altitude: Altitude where the turn reaches ``pitch``.
        chute_fallback_altitude: Parachutes are staged when the game says
            they are safe, or below this altitude anyway.
        warp: Use time warp outside the atmosphere.
        physics_warp: Physics warp factor (0-3) under the parachutes.
        max_dynamic_pressure: Throttle down while the dynamic pressure
            exceeds this, in pascals; 0 disables.
        max_drag_ratio: Throttle down while the drag exceeds this multiple
            of the weight (1.0 = climb at terminal velocity); 0 disables.
        min_throttle: Lowest throttle the air limits may set.
    """

    target_apoapsis: float = 75_000.0
    pitch: float = 80.0
    heading: float = 90.0
    turn_altitude: float = 1_000.0
    turn_end_altitude: float = 10_000.0
    chute_fallback_altitude: float = 2_000.0
    warp: bool = True
    physics_warp: int = 2
    max_dynamic_pressure: float = 20_000.0
    max_drag_ratio: float = 1.0
    min_throttle: float = 0.1
