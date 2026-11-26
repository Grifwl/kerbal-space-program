"""Rules for flying tourists to space and back (pure, no I/O).

A tourism contract carries one or more tourists, each with an itinerary of
destinations ("Suborbital spaceflight at Kerbin", "Orbit Kerbin", "Flyby
of the Mun"...). The tourism campaign takes the contracts whose every
destination is a suborbital flight or an orbit of the launch body, and
packs as many tourists as fit in the craft. An orbital flight passes
through the suborbital situation on its way up, so it also satisfies the
suborbital destinations: tourists of both kinds fly together, and the
flight is orbital as soon as one of them wants an orbit. KSP pays and
completes each tourist's itinerary on its own, when that tourist is
recovered, so a contract with more tourists than seats is flown over
several flights. This module decides:

* which mission (suborbital or orbital) a destination, a tourist and a
  contract need;
* which tourists fly next (accepted contracts first, then offers), splitting
  a contract between flights when it does not fit;
* which pilot flies next (rotation);
* when to stage during the ascent and how to throttle towards the target
  apoapsis;
* the orbital mechanics of the orbital flight: burns to circularize and to
  come back down, their duration, and when the vessel reaches the
  atmosphere again.

Everything here works on plain values so it can be tested without the game.
"""

from __future__ import annotations

import itertools
import math
import re
import unicodedata
from dataclasses import dataclass
from enum import Enum, IntEnum

# Word starts that identify a destination in the objective titles, after
# removing accents and case. "Vuelo espacial suborbital en Kerbin" and
# "Suborbital spaceflight at Kerbin" contain "suborbit"; "Orbita alrededor
# de Kerbin" (Spanish) and "Orbit Kerbin" (English) contain "orbit". The
# suborbital check goes first, as "suborbital" also contains "orbit".
SUBORBITAL_PREFIX = "suborbit"
ORBIT_PREFIX = "orbit"

# Standard gravity, used by KSP to turn specific impulse into exhaust speed.
STANDARD_GRAVITY = 9.80665

# Crew trait of the kerbals that can fly a crewed command pod.
PILOT_TRAIT = "Pilot"


class Mission(IntEnum):
    """Kind of flight a tourist needs, from the least to the most demanding.

    The order matters: a more demanding mission also satisfies the ones
    below it (an orbital flight is suborbital on its way up), so the
    mission of a group is the highest one in it.
    """

    SUBORBITAL = 1
    ORBITAL = 2

    @property
    def label(self) -> str:
        """Lower-case name for messages.

        Returns:
            ``"suborbital"`` or ``"orbital"``.
        """
        return self.name.lower()


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
        reward: Funds paid when the itinerary is completed (the objective
            and its destinations).
        completed: Whether the itinerary is already completed, i.e. the
            tourist already flew and was recovered.
        visited: Destinations the game already marks as reached (during
            the flight, before the recovery completes the itinerary).
    """

    itinerary: str
    destinations: tuple[str, ...]
    kerbal: str | None = None
    reward: float = 0.0
    completed: bool = False
    visited: tuple[str, ...] = ()


@dataclass(frozen=True)
class TourismContract:
    """A tourism contract, offered or accepted.

    Attributes:
        contract_id: Identifier valid for the adapter that read it.
        title: Contract title. KSP gives most tourism contracts the same
            title ("Take two tourists..."), so it does not identify one.
        status: Life-cycle status.
        tourists: Every tourist of the contract, flown or not.
        reward: Funds paid in total (advance, objectives and completion).
    """

    contract_id: str
    title: str
    status: ContractStatus
    tourists: tuple[Tourist, ...]
    reward: float = 0.0

    @property
    def pending(self) -> tuple[Tourist, ...]:
        """Tourists still waiting for their flight.

        Returns:
            The tourists whose itinerary is not completed, in order.
        """
        return tuple(t for t in self.tourists if not t.completed)

    @property
    def seats(self) -> int:
        """Seats needed to fly every pending tourist at once.

        It may be more than the craft has: the contract is then flown over
        several flights.

        Returns:
            Number of pending tourists.
        """
        return len(self.pending)

    @property
    def bonus(self) -> float:
        """Funds of the contract itself, beyond the tourists' itineraries.

        It is the advance (paid on acceptance) plus the completion reward
        (paid when the last tourist is delivered).

        Returns:
            The reward not tied to any single tourist; never negative.
        """
        return max(0.0, self.reward - sum(t.reward for t in self.tourists))

    def describe(self) -> str:
        """Short text naming the contract by its tourists.

        Titles are shared by many contracts, so the tourists' names are
        what tells them apart in the logs.

        Returns:
            E.g. ``"2 tourist(s): Samroda Kerman, Robala Kerman"``.
        """
        names = ", ".join(t.kerbal or t.itinerary for t in self.tourists)
        return f"{len(self.tourists)} tourist(s): {names}"


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
class Boarding:
    """The tourists of one contract that board the next flight.

    Attributes:
        contract: The contract.
        tourists: The tourists flying now; the contract's other pending
            tourists wait for a later flight.
    """

    contract: TourismContract
    tourists: tuple[Tourist, ...]

    @property
    def completes_contract(self) -> bool:
        """Whether this flight delivers every tourist the contract has left.

        Returns:
            True when nobody of the contract is left waiting.
        """
        return len(self.tourists) >= self.contract.seats

    @property
    def waiting(self) -> int:
        """Tourists of the contract left for a later flight.

        Returns:
            Number of pending tourists not boarding now.
        """
        return max(0, self.contract.seats - len(self.tourists))

    @property
    def left_behind(self) -> tuple[Tourist, ...]:
        """Pending tourists of the contract that do not board this flight.

        Returns:
            The tourists waiting for a later flight, in order.
        """
        return tuple(t for t in self.contract.pending if t not in self.tourists)

    @property
    def reward(self) -> float:
        """Funds this flight earns from the contract.

        Returns:
            The itineraries of the tourists on board, plus the contract's
            own reward when the flight completes it.
        """
        own = self.contract.bonus if self.completes_contract else 0.0
        return sum(t.reward for t in self.tourists) + own


@dataclass(frozen=True)
class PassengerPlan:
    """The tourists flown on the next flight.

    Attributes:
        carry: Tourists of contracts already accepted.
        accept: Offered contracts to accept now, with the tourists of each
            that fly this flight.
        mission: The mission the tourists on board need: orbital when at
            least one of them wants an orbit.
    """

    carry: tuple[Boarding, ...] = ()
    accept: tuple[Boarding, ...] = ()
    mission: Mission = Mission.SUBORBITAL

    @property
    def boardings(self) -> tuple[Boarding, ...]:
        """Every contract of the flight with its tourists on board.

        Returns:
            Accepted contracts first, then the offers to accept.
        """
        return self.carry + self.accept

    @property
    def contracts(self) -> tuple[TourismContract, ...]:
        """Every contract of the flight.

        Returns:
            Accepted contracts first, then the offers to accept.
        """
        return tuple(b.contract for b in self.boardings)

    @property
    def tourists(self) -> tuple[Tourist, ...]:
        """Every tourist on board, in boarding order.

        Returns:
            The tourists.
        """
        return tuple(t for b in self.boardings for t in b.tourists)

    @property
    def seats_used(self) -> int:
        """Seats taken by the tourists of the plan.

        Returns:
            Number of tourists.
        """
        return len(self.tourists)

    @property
    def reward(self) -> float:
        """Funds the flight earns when every tourist on board is delivered.

        Returns:
            Total reward of this flight.
        """
        return sum(b.reward for b in self.boardings)

    @property
    def is_empty(self) -> bool:
        """Whether there is nobody to fly.

        Returns:
            True when no tourist boards.
        """
        return not self.tourists


def _normalize(text: str) -> str:
    """Remove accents and case, so titles compare in any language.

    Args:
        text: Any text.

    Returns:
        The text without diacritics, case folded.
    """
    decomposed = unicodedata.normalize("NFKD", text)
    return "".join(c for c in decomposed if not unicodedata.combining(c)).casefold()


def destination_mission(title: str, body: str = "Kerbin") -> Mission | None:
    """Tell which mission reaches a destination.

    Args:
        title: Destination title, e.g. ``"Orbita alrededor de Kerbin"``.
        body: Body launched from.

    Returns:
        ``SUBORBITAL`` for a suborbital flight at the body, ``ORBITAL`` for
        an orbit of it, ``None`` for anything else (another body, a flyby,
        a landing...).
    """
    words = re.findall(r"\w+", _normalize(title))
    # Whole-word match so "Kerbin" does not match inside another name.
    if _normalize(body) not in words:
        return None
    if any(w.startswith(SUBORBITAL_PREFIX) for w in words):
        return Mission.SUBORBITAL
    if any(w.startswith(ORBIT_PREFIX) for w in words):
        return Mission.ORBITAL
    return None


def tourist_mission(tourist: Tourist, body: str = "Kerbin") -> Mission | None:
    """Tell which mission completes a tourist's whole itinerary.

    Args:
        tourist: The tourist.
        body: Body launched from.

    Returns:
        The most demanding mission among the destinations, or ``None`` when
        a destination cannot be reached by either mission.
    """
    missions = [destination_mission(d, body) for d in tourist.destinations]
    if not missions or None in missions:
        return None
    return max(m for m in missions if m is not None)


def contract_mission(contract: TourismContract, body: str = "Kerbin") -> Mission | None:
    """Tell which mission the pending tourists of a contract need.

    A single destination that neither mission reaches (a flyby of the
    Mun...) rules the contract out: that tourist could never be delivered.
    Each tourist can still fly on a different flight (and mission).

    Args:
        contract: The contract.
        body: Body launched from.

    Returns:
        The most demanding mission among the pending tourists, or ``None``
        when the contract cannot be flown (or nobody is left).
    """
    missions = [tourist_mission(t, body) for t in contract.pending]
    if not missions or None in missions:
        return None
    return max(m for m in missions if m is not None)


def orbit_visited(tourist: Tourist, body: str = "Kerbin") -> bool:
    """Tell whether the game marked a tourist's orbit destinations reached.

    Args:
        tourist: The tourist, as read during the flight.
        body: Body launched from.

    Returns:
        True when every orbit destination of the tourist is visited (also
        when the tourist wants no orbit at all).
    """
    return all(d in tourist.visited for d in tourist.destinations
               if destination_mission(d, body) is Mission.ORBITAL)


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


def _board_offers(combo: tuple[TourismContract, ...], free: int) -> tuple[Boarding, ...]:
    """Seat the tourists of a set of offers.

    Smaller contracts board first, so that whole contracts fly together and
    at most the last one is split, its remaining tourists waiting for a
    later flight.

    Args:
        combo: Offers to accept.
        free: Seats available for them.

    Returns:
        One boarding per offer that gets at least one seat.
    """
    boardings = []
    for contract in sorted(combo, key=lambda c: c.seats):
        take = contract.pending[:free]
        if take:
            boardings.append(Boarding(contract, take))
            free -= len(take)
    return tuple(boardings)


def plan_passengers(
    contracts: list[TourismContract],
    seats: int,
    available: set[str],
    body: str = "Kerbin",
    allow_offers: bool = True,
    max_offers_considered: int = 16,
    max_mission: Mission = Mission.ORBITAL,
) -> PassengerPlan:
    """Choose the tourists to fly on the next flight.

    Tourists of contracts already accepted come first: they are waiting and
    their deadlines run. Contracts with fewer tourists left go first, so
    contracts are finished (and stop counting against the Mission Control
    limit) as soon as possible; a contract that does not fit is split, the
    rest of its tourists flying on the next flights.

    The seats left are filled with offers. Every combination of offers is
    scored by, in order: the seats it fills on this flight, the funds this
    flight earns from them, and the fewest tourists left waiting for a
    later flight. An offer bigger than the free seats is therefore taken
    when it pays better per seat: the tourists left behind cost nothing,
    they board first on the next flight and are paid then. Between equally
    paying choices, the one that fits exactly wins. Offers are few (a
    handful at a time), so trying every combination is cheap.

    Suborbital and orbital tourists are mixed freely: the plan is never
    shaped to keep the flight suborbital. The flight is orbital as soon as
    one tourist on board wants an orbit.

    Args:
        contracts: Tourism contracts, offered and active.
        seats: Passenger seats of the craft (pilot excluded).
        available: Full names of the kerbals available at the space center.
        body: Body launched from.
        allow_offers: Whether offers may be accepted.
        max_offers_considered: Best offers (by reward per seat) tried.
        max_mission: Most demanding mission allowed; contracts needing
            more are left alone.

    Returns:
        The plan; empty when nothing can be flown.
    """
    hops = [c for c in contracts
            if c.seats > 0 and (m := contract_mission(c, body)) is not None and m <= max_mission]

    carry: list[Boarding] = []
    free = seats
    # sorted() is stable: equal sizes keep the game's order (oldest first).
    active = sorted((c for c in hops if c.status is ContractStatus.ACTIVE), key=lambda c: c.seats)
    for contract in active:
        # A tourist that was not matched to the roster, or is not at the
        # space center, cannot be boarded; the others of the contract can.
        boardable = tuple(t for t in contract.pending
                          if t.kerbal is not None and t.kerbal in available)
        take = boardable[:free]
        if take:
            carry.append(Boarding(contract, take))
            free -= len(take)

    accept: tuple[Boarding, ...] = ()
    offers = [c for c in hops if c.status is ContractStatus.OFFERED]
    if allow_offers and offers and free > 0:
        offers.sort(key=lambda c: c.reward / c.seats, reverse=True)
        offers = offers[:max_offers_considered]
        best: tuple[int, float, int] | None = None
        # Smaller combinations are tried first and only a strictly better
        # score replaces the best one, so among equal scores the plan with
        # fewer contracts wins (fewer acceptances to go wrong).
        for size in range(1, len(offers) + 1):
            for combo in itertools.combinations(offers, size):
                wanted = sum(c.seats for c in combo)
                # The biggest contract boards last: if the others already
                # take every seat it gets none, and the same combination
                # without it scores better (fewer tourists left waiting).
                if wanted - max(c.seats for c in combo) >= free:
                    continue
                boardings = _board_offers(combo, free)
                filled = sum(len(b.tourists) for b in boardings)
                # filled - wanted is minus the tourists left waiting.
                score = (filled, sum(b.reward for b in boardings), filled - wanted)
                if best is None or score > best:
                    best, accept = score, boardings
    missions = [tourist_mission(t, body) for b in (*carry, *accept) for t in b.tourists]
    mission = max((m for m in missions if m is not None), default=Mission.SUBORBITAL)
    return PassengerPlan(carry=tuple(carry), accept=accept, mission=mission)


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
        periapsis: Periapsis altitude above sea level, in metres.
        semi_major_axis: Semi-major axis of the orbit, in metres.
        eccentricity: Eccentricity of the orbit.
        radius: Distance from the centre of the body, in metres.
        time_to_periapsis: Seconds to the periapsis.
        period: Orbital period, in seconds.
        mu: Gravitational parameter of the body, in m^3/s^2.
        body_radius: Equatorial radius of the body, in metres.
        thrust: Thrust the active engines can give at full throttle, in
            newtons.
        mass: Mass of the vessel, in kilograms.
        heat_ratio: Temperature of the hottest part as a fraction of the
            temperature it withstands (inside or skin, whichever is
            closer); 0 when unknown.
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
    periapsis: float = 0.0
    semi_major_axis: float = 0.0
    eccentricity: float = 0.0
    radius: float = 0.0
    time_to_periapsis: float = 0.0
    period: float = 0.0
    mu: float = 0.0
    body_radius: float = 0.0
    thrust: float = 0.0
    mass: float = 0.0
    heat_ratio: float = 0.0

    @property
    def on_ground(self) -> bool:
        """Whether the vessel is resting on the ground or the sea.

        Returns:
            True when landed or splashed down.
        """
        return self.situation in ("landed", "splashed")

    @property
    def in_orbit(self) -> bool:
        """Whether the vessel is on a closed orbit above the atmosphere.

        Returns:
            True when the periapsis is above the atmosphere.
        """
        return self.periapsis > self.atmosphere_depth


@dataclass(frozen=True)
class Propulsion:
    """What the vessel can still do with its engines.

    Attributes:
        thrust: Thrust of the active engines at full throttle, in newtons.
        isp: Vacuum specific impulse of the active engines, in seconds.
        mass: Mass of the vessel, in kilograms.
        delta_v: Vacuum delta-v left in the whole vessel (every stage), in
            m/s, as KSP's own delta-v readout computes it.
    """

    thrust: float
    isp: float
    mass: float
    delta_v: float


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


def ascent_throttle(apoapsis: float, target_apoapsis: float, cut: bool = False,
                    relight_margin: float = 500.0) -> float:
    """Throttle while climbing through the atmosphere.

    Full throttle until the apoapsis reaches the target, then coast. Drag
    lowers the apoapsis while still in the air, so the engines are lit
    again if it drops clearly below the target: only past
    ``relight_margin``, since the air takes a few metres off it every
    second and relighting for each of them would only flicker the engines
    on and off. A few hundred metres less hardly change the orbit, which
    is circularized at whatever the apoapsis is.

    Args:
        apoapsis: Current apoapsis altitude, in metres.
        target_apoapsis: Wanted apoapsis altitude, in metres.
        cut: Whether the engines are already cut after reaching it.
        relight_margin: How far below the target the apoapsis may drop
            before relighting, in metres.

    Returns:
        1.0 or 0.0.
    """
    threshold = target_apoapsis - relight_margin if cut else target_apoapsis
    return 1.0 if apoapsis < threshold else 0.0


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
    heat_ratio: float = 0.0,
    max_heat_ratio: float = 0.0,
) -> float:
    """Throttle that eases off while the air pushes back too hard.

    Three limits, any of which lowers the throttle:

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
    * **Heat**: the hottest part's temperature over what it withstands.
      Higher up the air is too thin for the dynamic pressure limit to act
      (20 kPa at 25 km is about 1,900 m/s), but a rocket flying fast and
      flat there still heats up. Less thrust means the rocket gains speed
      later, higher up, in thinner air.

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
        heat_ratio: Hottest part's temperature as a fraction of its limit.
        max_heat_ratio: Allowed ``heat_ratio``; 0 or less disables this
            limit.

    Returns:
        The new throttle limit, between ``floor`` and 1.
    """
    loads = []
    if max_dynamic_pressure > 0:
        loads.append(dynamic_pressure / max_dynamic_pressure)
    if max_drag_ratio > 0 and weight > 0:
        loads.append(drag / weight / max_drag_ratio)
    if max_heat_ratio > 0:
        loads.append(heat_ratio / max_heat_ratio)
    if not loads:
        return 1.0
    # A load of 1.0 is exactly at its limit; above it the throttle drops.
    worst = max(loads)
    value = previous + gain * (1.0 - worst) * max(dt, 0.0)
    return min(1.0, max(floor, value))


def ascent_pitch(altitude: float, turn_start: float, turn_end: float, final_pitch: float) -> float:
    """Pitch to hold at an altitude during the climb of a suborbital hop.

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


def apoapsis_pitch(apoapsis: float, start_apoapsis: float, target_apoapsis: float,
                   shape: float = 1.0) -> float:
    """Pitch of the gravity turn to orbit, driven by the apoapsis.

    The turn starts when the vertical climb ends and follows how much of
    the way the apoapsis has risen, from where it was then to the target:
    90 degrees at the start, horizontal once the apoapsis reaches the
    target. The turn thus adapts to the rocket: a powerful one raises its
    apoapsis fast and tilts fast, a weak one tilts slowly.

    ``shape`` bends the turn: 1 tilts in proportion to the apoapsis rise;
    below 1 the rocket tilts sooner (0.5: halfway over when the apoapsis
    has risen a quarter of the way). The apoapsis grows with the square of
    the vertical speed, so with a proportional turn and plenty of thrust
    the rocket reaches the target apoapsis while still climbing steeply,
    and circularizing at the top costs far more delta-v than after a
    flatter ascent.

    Args:
        apoapsis: Highest apoapsis reached so far, in metres; passing the
            highest keeps the nose from rising again when drag lowers the
            apoapsis for a moment.
        start_apoapsis: Apoapsis when the vertical climb ended, in metres.
        target_apoapsis: Apoapsis of the orbit, in metres.
        shape: Exponent of the turn progress (1: proportional).

    Returns:
        The pitch, in degrees above the horizon.
    """
    if target_apoapsis <= start_apoapsis:
        return 0.0
    progress = min(1.0, max(0.0, (apoapsis - start_apoapsis) / (target_apoapsis - start_apoapsis)))
    return 90.0 * (1.0 - progress ** shape)


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


def vis_viva_speed(mu: float, radius: float, semi_major_axis: float) -> float:
    """Orbital speed at a distance from the centre of the body.

    Args:
        mu: Gravitational parameter of the body, in m^3/s^2.
        radius: Distance from the centre, in metres.
        semi_major_axis: Semi-major axis of the orbit, in metres.

    Returns:
        The speed, in m/s (vis-viva equation).
    """
    return math.sqrt(max(0.0, mu * (2.0 / radius - 1.0 / semi_major_axis)))


def apsis_burn_dv(mu: float, radius: float, semi_major_axis: float, new_opposite_radius: float) -> float:
    """Prograde delta-v that moves the opposite apsis, burning at an apsis.

    A burn along the velocity at an apsis keeps that apsis and moves the
    one on the other side of the orbit. Burning at the apoapsis so that
    the periapsis becomes as high as the apoapsis circularizes the orbit;
    burning backwards to bring the periapsis into the atmosphere is the
    deorbit burn.

    Args:
        mu: Gravitational parameter of the body, in m^3/s^2.
        radius: Distance of the apsis where the burn happens, in metres.
        semi_major_axis: Semi-major axis of the orbit now, in metres.
        new_opposite_radius: Wanted distance of the other apsis, in metres.

    Returns:
        The delta-v, in m/s: positive forwards (prograde), negative
        backwards (retrograde).
    """
    new_semi_major_axis = (radius + new_opposite_radius) / 2.0
    return (vis_viva_speed(mu, radius, new_semi_major_axis)
            - vis_viva_speed(mu, radius, semi_major_axis))


def burn_time(delta_v: float, thrust: float, isp: float, mass: float) -> float:
    """Seconds the engines need at full throttle to give a delta-v.

    The vessel gets lighter as it burns, so this comes from the rocket
    equation rather than from ``delta_v / acceleration``.

    Args:
        delta_v: Delta-v, in m/s (the sign is ignored).
        thrust: Thrust at full throttle, in newtons.
        isp: Specific impulse, in seconds.
        mass: Mass when the burn starts, in kilograms.

    Returns:
        The burn time, in seconds; infinite without thrust.
    """
    if thrust <= 0 or isp <= 0 or mass <= 0:
        return math.inf
    exhaust_speed = isp * STANDARD_GRAVITY
    final_mass = mass * math.exp(-abs(delta_v) / exhaust_speed)
    return (mass - final_mass) * exhaust_speed / thrust


def burn_throttle(remaining_dv: float, thrust: float, mass: float,
                  settle_time: float = 2.0, floor: float = 0.05) -> float:
    """Throttle for the end of a burn, easing off as the target nears.

    Full throttle until the remaining delta-v would take less than
    ``settle_time`` seconds at full power, then proportionally less, so the
    burn ends close to the wanted delta-v instead of overshooting it.

    Args:
        remaining_dv: Delta-v still to give, in m/s.
        thrust: Thrust at full throttle, in newtons.
        mass: Mass of the vessel, in kilograms.
        settle_time: Seconds of full power below which the throttle eases.
        floor: Lowest throttle.

    Returns:
        The throttle, between ``floor`` and 1.
    """
    if thrust <= 0 or mass <= 0:
        return 1.0
    acceleration = thrust / mass
    return min(1.0, max(floor, remaining_dv / (acceleration * settle_time)))


def _mean_anomaly(true_anomaly: float, eccentricity: float) -> float:
    """Mean anomaly of a point of an elliptical orbit.

    Args:
        true_anomaly: True anomaly, in radians.
        eccentricity: Eccentricity, below 1.

    Returns:
        The mean anomaly, in radians, between 0 and 2 pi.
    """
    eccentric = 2.0 * math.atan2(math.sqrt(1.0 - eccentricity) * math.sin(true_anomaly / 2.0),
                                 math.sqrt(1.0 + eccentricity) * math.cos(true_anomaly / 2.0))
    return (eccentric - eccentricity * math.sin(eccentric)) % (2.0 * math.pi)


def time_to_radius(mu: float, semi_major_axis: float, eccentricity: float, radius: float,
                   rising: bool, target_radius: float) -> float | None:
    """Seconds until the vessel comes down to a distance from the body.

    Used to time warp until just before the vessel falls back into the
    atmosphere: the point of the orbit at ``target_radius`` on its way
    down (between the apoapsis and the periapsis).

    Args:
        mu: Gravitational parameter of the body, in m^3/s^2.
        semi_major_axis: Semi-major axis of the orbit, in metres.
        eccentricity: Eccentricity of the orbit.
        radius: Distance from the centre now, in metres.
        rising: Whether the vessel is climbing now (between the periapsis
            and the apoapsis).
        target_radius: Distance to reach on the way down, in metres.

    Returns:
        The seconds, or ``None`` when the orbit never gets that low or
        high, or is too round or open for the question to make sense.
    """
    if semi_major_axis <= 0 or not 1e-6 < eccentricity < 1.0:
        return None
    semi_latus = semi_major_axis * (1.0 - eccentricity ** 2)

    def true_anomaly(r: float) -> float | None:
        """True anomaly (0 to pi) where the orbit is at distance ``r``."""
        cosine = (semi_latus / r - 1.0) / eccentricity
        if cosine > 1.0 + 1e-9 or cosine < -1.0 - 1e-9:
            return None
        return math.acos(min(1.0, max(-1.0, cosine)))

    now = true_anomaly(radius)
    target = true_anomaly(target_radius)
    if now is None or target is None:
        return None
    # Between the apoapsis (pi) and the periapsis (2 pi) the vessel falls.
    if not rising:
        now = 2.0 * math.pi - now
    target = 2.0 * math.pi - target
    mean_motion = math.sqrt(mu / semi_major_axis ** 3)
    delta = (_mean_anomaly(target, eccentricity) - _mean_anomaly(now, eccentricity)) % (2.0 * math.pi)
    return delta / mean_motion


@dataclass(frozen=True)
class FlightProfile:
    """How to fly the suborbital hop and the orbital flight.

    Attributes:
        target_apoapsis: Apoapsis of the suborbital hop, in metres. It must
            be above the atmosphere (70 km at Kerbin) for the vessel to
            become suborbital, which is what the tourists pay for.
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
        use_rcs: Switch the RCS on from the launch until the parachutes
            open, so the autopilot can steer with the RCS thrusters too
            (a vessel launched by kRPC starts with the RCS off).
        max_heat_ratio: Throttle down while the hottest part is above this
            fraction of the temperature it withstands; 0 disables.
        orbit_apoapsis: Apoapsis of the orbital ascent, which then becomes
            the altitude of the circular orbit, in metres.
        orbit_turn_shape: Shape of the orbital gravity turn, which starts
            at ``turn_altitude`` and is horizontal once the apoapsis
            reaches ``orbit_apoapsis`` (see ``apoapsis_pitch``); lower
            tilts sooner.
        turn_after_first_stage: Start the orbital gravity turn only once
            the first stage has been dropped (and above
            ``turn_altitude``): a long rocket may lose control if its
            boosters separate while it is tilted. Climbing straight up for
            longer costs delta-v.
        deorbit_periapsis: Periapsis the deorbit burn aims for, in metres:
            deep enough in the atmosphere to come down in one pass.
        delta_v_reserve: Delta-v that must be left after circularizing
            and deorbiting, in m/s; with less, the vessel does not go into
            orbit (it would risk being stranded there).
        orbit_delta_v: Vacuum delta-v the vessel needs on the launch pad
            to attempt an orbit, in m/s. With less, an orbital ascent would
            burn its fuel sideways and might not even reach space, so the
            vessel flies a suborbital hop instead.
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
    use_rcs: bool = True
    max_heat_ratio: float = 0.8
    orbit_apoapsis: float = 80_000.0
    orbit_turn_shape: float = 1.0
    turn_after_first_stage: bool = False
    deorbit_periapsis: float = 35_000.0
    delta_v_reserve: float = 100.0
    orbit_delta_v: float = 3_600.0
