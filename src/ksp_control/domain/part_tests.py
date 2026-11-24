"""Part test contracts: what they ask for, in which order, and how to fly them.

KSP's "test a part" contracts (``Contracts.Templates.PartTest``) have one
objective, "Test <part>" or "Have <part>", whose sub-objectives are the
conditions: a celestial body, a situation (flying, landed, orbiting...),
and optionally an altitude range and a speed range. The game re-evaluates
those conditions every frame and only counts the test when the part is
activated while all of them hold.

This module is pure: it knows nothing about kRPC. It provides

* the requirement model (``PartTestRequirement``) and its live progress,
* ``check_conditions``: our own evaluation of the conditions, used to warn
  ahead of time (the game's verdict is still the one that counts),
* ``plan_tests``: the order in which several tests should be flown and
  which of them need a different flight,
* ``select_target``: which test the vessel is heading to right now,
* ``SpeedGovernor``: a throttle controller that keeps the speed inside a
  window.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
from enum import Enum


class Situation(Enum):
    """Vessel situations, named like kRPC's ``VesselSituation``."""

    PRE_LAUNCH = "pre_launch"
    LANDED = "landed"
    SPLASHED = "splashed"
    FLYING = "flying"
    SUB_ORBITAL = "sub_orbital"
    ORBITING = "orbiting"
    ESCAPING = "escaping"
    DOCKED = "docked"

    @classmethod
    def from_name(cls, name: str) -> "Situation | None":
        """Parse a kRPC situation name.

        Args:
            name: For example ``"flying"`` or ``"VesselSituation.flying"``.

        Returns:
            The situation, or ``None`` when unknown.
        """
        key = name.rsplit(".", 1)[-1].lower()
        return next((s for s in cls if s.value == key), None)


# Situations where KSP compares the surface speed against both ends of a
# speed range. Elsewhere it uses the orbital speed for the upper bound
# (decompiled ``ReachSpeedEnvelope.checkVesselWithinEnvelope``).
_SURFACE_SPEED_SITUATIONS = {
    Situation.PRE_LAUNCH, Situation.LANDED, Situation.SPLASHED, Situation.FLYING,
}

# Order in which situations are met during a normal flight: launch, climb,
# space, and finally touching down again.
_PHASE = {
    Situation.PRE_LAUNCH: 0,
    Situation.FLYING: 1,
    Situation.SUB_ORBITAL: 2,
    Situation.ORBITING: 3,
    Situation.ESCAPING: 4,
    Situation.DOCKED: 4,
    Situation.LANDED: 5,
    Situation.SPLASHED: 5,
}


class ObjectiveKind(Enum):
    """What the contract wants done with the part."""

    TEST = "test"  # activate it while the conditions hold
    HAVE = "have"  # just carry it while the conditions hold ("haul")


class Activation(Enum):
    """How a part test is triggered."""

    STAGING = "staging"  # the part must fire from the staging sequence
    MENU = "menu"        # "Run Test" in the part's right-click menu
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class Range:
    """A closed numeric interval.

    Attributes:
        low: Lower bound.
        high: Upper bound.
    """

    low: float
    high: float

    def contains(self, value: float) -> bool:
        """Tell whether a value is inside the interval.

        Args:
            value: The value.

        Returns:
            True when ``low <= value <= high``.
        """
        return self.low <= value <= self.high

    def overlaps(self, other: "Range") -> bool:
        """Tell whether two intervals share at least one value.

        Args:
            other: The other interval.

        Returns:
            True when they overlap.
        """
        return self.low <= other.high and other.low <= self.high

    def intersection(self, other: "Range") -> "Range | None":
        """Return the common part of two intervals.

        Args:
            other: The other interval.

        Returns:
            The intersection, or ``None`` when they do not overlap.
        """
        if not self.overlaps(other):
            return None
        return Range(max(self.low, other.low), min(self.high, other.high))

    def aim(self, fraction: float = 0.4) -> float:
        """Return a point inside the interval, a fraction of the way up.

        Args:
            fraction: 0 is the lower bound, 1 the upper bound.

        Returns:
            The point.
        """
        return self.low + (self.high - self.low) * fraction


@dataclass(frozen=True)
class PartTestRequirement:
    """What one part test contract asks for.

    Attributes:
        contract_id: Stable identifier of the contract while it is active.
        contract_title: Contract title as shown in game.
        part_title: Title of the part, as KSP shows it (localized).
        kind: Test the part or just have it on board.
        activation: How the test is triggered (only meaningful for TEST).
        body: Name of the celestial body, or ``None`` when not understood.
        situation: Required situation, or ``None`` when not understood.
        altitude: Altitude above sea level range, in metres, if any.
        speed: Speed range, in m/s, if any.
    """

    contract_id: str
    contract_title: str
    part_title: str
    kind: ObjectiveKind
    activation: Activation = Activation.UNKNOWN
    body: str | None = None
    situation: Situation | None = None
    altitude: Range | None = None
    speed: Range | None = None

    def describe(self) -> str:
        """Summarize the conditions in one line.

        Returns:
            Human readable conditions, for messages and logs.
        """
        parts = []
        if self.situation is not None:
            parts.append(self.situation.value.replace("_", " "))
        if self.body:
            parts.append(f"at {self.body}")
        if self.altitude is not None:
            parts.append(f"{_km(self.altitude.low)}-{_km(self.altitude.high)}")
        if self.speed is not None:
            parts.append(f"{self.speed.low:.0f}-{self.speed.high:.0f} m/s")
        return ", ".join(parts) if parts else "no conditions"


@dataclass(frozen=True)
class RequirementProgress:
    """The game's own verdict on a requirement, as last read.

    Attributes:
        done: The objective ("Test X" / "Have X") is complete.
        conditions_met: Every condition holds right now, according to the
            game, or ``None`` when the game's flags could not be read.
    """

    done: bool = False
    conditions_met: bool | None = None


@dataclass(frozen=True)
class TestContract:
    """A part test contract together with its live progress.

    Attributes:
        requirement: What it asks for.
        progress: Where it stands.
    """

    __test__ = False  # not a pytest test class, despite the name

    requirement: PartTestRequirement
    progress: RequirementProgress


@dataclass(frozen=True)
class TestPart:
    """A part on the vessel that can be the subject of a test.

    Attributes:
        title: Part title (localized, as in the contracts).
        stage: Stage in which it activates, or -1 when not staged.
        can_run_test: True when "Run Test" is available in its menu.
    """

    __test__ = False  # not a pytest test class, despite the name

    title: str
    stage: int = -1
    can_run_test: bool = False


@dataclass(frozen=True)
class FlightState:
    """What the active vessel is doing.

    Attributes:
        vessel: Vessel name.
        body: Name of the body it is around.
        situation: Its situation.
        altitude: Altitude above sea level, in metres.
        vertical_speed: Positive when climbing, in m/s.
        surface_speed: Speed relative to the surface, in m/s.
        orbital_speed: Speed relative to the body's centre, in m/s.
        throttle: Current main throttle, 0 to 1.
        thrust: Current thrust, in newtons.
        available_thrust: Thrust at full throttle, in newtons.
        mass: Vessel mass, in kilograms.
        parts: Test subject parts on board, or ``None`` when unknown.
    """

    vessel: str
    body: str
    situation: Situation | None
    altitude: float
    vertical_speed: float
    surface_speed: float
    orbital_speed: float
    throttle: float = 0.0
    thrust: float = 0.0
    available_thrust: float = 0.0
    mass: float = 1.0
    parts: tuple[TestPart, ...] | None = None

    def part_count(self, title: str) -> int | None:
        """Count the parts with a given title.

        Args:
            title: Part title.

        Returns:
            How many are on board, or ``None`` when parts are unknown.
        """
        if self.parts is None:
            return None
        wanted = normalize_title(title)
        return sum(1 for part in self.parts if normalize_title(part.title) == wanted)


@dataclass(frozen=True)
class EditorState:
    """The craft being designed in the VAB or the SPH.

    Attributes:
        craft: Craft name.
        parts: Test subject parts on it, or ``None`` when unreadable.
    """

    craft: str
    parts: tuple[TestPart, ...] | None


def missing_parts(contracts: list[TestContract], parts: tuple[TestPart, ...]) -> list[TestContract]:
    """List the pending contracts whose part is not on a craft.

    Args:
        contracts: Part test contracts.
        parts: Test subject parts on the craft.

    Returns:
        The pending contracts lacking their part, in input order.
    """
    on_board = {normalize_title(part.title) for part in parts}
    return [
        c for c in contracts
        if not c.progress.done and normalize_title(c.requirement.part_title) not in on_board
    ]


def describe_place(requirement: PartTestRequirement) -> str:
    """Say where a test happens, for "conditions met" messages.

    Args:
        requirement: The requirement.

    Returns:
        For example ``"on the launch pad"`` or ``"altitude and speed OK"``.
    """
    if requirement.altitude is not None or requirement.speed is not None:
        return "altitude and speed OK"
    return {
        Situation.PRE_LAUNCH: "on the launch pad",
        Situation.LANDED: "landed",
        Situation.SPLASHED: "splashed down",
        Situation.FLYING: "flying",
        Situation.SUB_ORBITAL: "sub-orbital",
        Situation.ORBITING: "in orbit",
        Situation.ESCAPING: "escaping",
    }.get(requirement.situation, "conditions met")


def normalize_title(title: str) -> str:
    """Normalize a part title for comparisons.

    Args:
        title: Part title.

    Returns:
        Lower case title with collapsed white space.
    """
    return " ".join(title.split()).casefold()


@dataclass(frozen=True)
class ConditionCheck:
    """Our own evaluation of a requirement against the flight state.

    Each flag is True when the condition holds or the contract does not
    have it.

    Attributes:
        body: Around the right body (True when the body is not understood).
        situation: In the right situation.
        altitude: Inside the altitude range.
        speed: Inside the speed range.
    """

    body: bool
    situation: bool
    altitude: bool
    speed: bool

    @property
    def all(self) -> bool:
        """Every condition holds."""
        return self.body and self.situation and self.altitude and self.speed


def check_conditions(requirement: PartTestRequirement, state: FlightState) -> ConditionCheck:
    """Evaluate a requirement the way KSP does.

    Args:
        requirement: The requirement.
        state: The vessel's state.

    Returns:
        Which conditions hold.
    """
    body_ok = requirement.body is None or _same_body(requirement.body, state.body)
    situation_ok = requirement.situation is None or requirement.situation == state.situation
    altitude_ok = requirement.altitude is None or requirement.altitude.contains(state.altitude)
    speed_ok = requirement.speed is None or speed_in_range(requirement.speed, state)
    return ConditionCheck(body_ok, situation_ok, altitude_ok, speed_ok)


def speed_in_range(speed: Range, state: FlightState) -> bool:
    """Check a speed range with the same speeds KSP uses.

    Args:
        speed: Required range.
        state: The vessel's state.

    Returns:
        True when the vessel is inside the range.
    """
    if state.situation in _SURFACE_SPEED_SITUATIONS or state.situation is None:
        return speed.contains(state.surface_speed)
    return state.orbital_speed <= speed.high and state.surface_speed >= speed.low


def _same_body(wanted: str, actual: str) -> bool:
    """Compare a body name from a contract with kRPC's internal name.

    Args:
        wanted: Name from the contract (localized display name).
        actual: Internal body name from kRPC.

    Returns:
        True when they name the same body.
    """
    return _BODY_ALIASES.get(wanted.casefold(), wanted.casefold()) == actual.casefold()


# Localized display names that differ from the internal body names.
_BODY_ALIASES = {"muna": "mun"}


class Verdict(Enum):
    """Where a test fits in the current flight."""

    DONE = "done"                   # already completed
    THIS_FLIGHT = "this_flight"     # do it in this flight, in plan order
    TOGETHER = "together"           # same moment as an earlier test
    ANOTHER_FLIGHT = "another"      # cannot be done in this flight
    MISSING_PART = "missing_part"   # the vessel does not carry the part
    NOT_ASSISTED = "not_assisted"   # at a body the assistant leaves alone


@dataclass(frozen=True)
class PlanEntry:
    """One test in the flight plan.

    Attributes:
        contract: The contract and its progress.
        verdict: Where it fits.
        order: Position in the flight (1-based), or 0 when not this flight.
        reason: Explanation for the player.
    """

    contract: TestContract
    verdict: Verdict
    order: int = 0
    reason: str = ""

    @property
    def requirement(self) -> PartTestRequirement:
        """Shortcut to the requirement."""
        return self.contract.requirement

    @property
    def actionable(self) -> bool:
        """True when the test is to be done in this flight."""
        return self.verdict in (Verdict.THIS_FLIGHT, Verdict.TOGETHER)


# Bodies where the assistant acts. Tests elsewhere usually need orbital
# preparation (transfers, specific orbits) that is better left to the player.
ASSISTED_BODIES = ("Kerbin",)


def plan_tests(
    contracts: list[TestContract],
    state: FlightState | None,
    assisted_bodies: tuple[str, ...] = ASSISTED_BODIES,
) -> list[PlanEntry]:
    """Decide the order of the pending tests and what fits in one flight.

    The plan assumes a normal flight profile: launch, climb through the
    atmosphere, maybe reach space, come back down and land. Tests are
    ordered by that profile and, within the atmosphere, by altitude.

    Args:
        contracts: Active part test contracts.
        state: The vessel's state, or ``None`` outside the flight scene
            (parts are then unknown and everything is planned for Kerbin).
        assisted_bodies: Bodies where tests are assisted; tests at any
            other body are marked ``NOT_ASSISTED`` and never acted upon,
            even when the vessel is there.

    Returns:
        One entry per contract: first the ones of this flight, in order,
        then the rest.
    """
    body = state.body if state is not None else "Kerbin"
    done = [PlanEntry(c, Verdict.DONE, reason="completed") for c in contracts if c.progress.done]
    pending = [c for c in contracts if not c.progress.done]

    planned: list[PlanEntry] = []
    later: list[PlanEntry] = []
    candidates: list[TestContract] = []
    for contract in pending:
        requirement = contract.requirement
        count = state.part_count(requirement.part_title) if state is not None else None
        if requirement.body is not None and not any(
            _same_body(requirement.body, assisted) for assisted in assisted_bodies
        ):
            later.append(PlanEntry(contract, Verdict.NOT_ASSISTED,
                                   reason=f"at {requirement.body}: not assisted"))
        elif count == 0:
            later.append(PlanEntry(contract, Verdict.MISSING_PART,
                                   reason=f"no {requirement.part_title} on board"))
        elif requirement.body is not None and not _same_body(requirement.body, body):
            later.append(PlanEntry(contract, Verdict.ANOTHER_FLIGHT,
                                   reason=f"it has to be done at {requirement.body}"))
        else:
            candidates.append(contract)

    candidates.sort(key=_flight_order)
    # Parts used up by each staging test: a staged part (a parachute, a
    # decoupler, an engine) fires once, so two staging tests of the same
    # part need two copies, unless one activation satisfies both.
    used: Counter[str] = Counter()
    landing: Situation | None = None
    order = 0
    for contract in candidates:
        requirement = contract.requirement
        partner = _shared_moment(requirement, planned)
        if partner is not None:
            planned.append(PlanEntry(contract, Verdict.TOGETHER, partner.order,
                                     f"same moment as test {partner.order}"))
            continue
        # Only one touchdown per flight: landed and splashed tests exclude
        # each other.
        if requirement.situation in (Situation.LANDED, Situation.SPLASHED):
            if landing is not None and landing != requirement.situation:
                later.append(PlanEntry(contract, Verdict.ANOTHER_FLIGHT,
                                       reason=f"the flight already ends {landing.value}"))
                continue
            landing = requirement.situation
        title = normalize_title(requirement.part_title)
        consumes = (requirement.kind is ObjectiveKind.TEST
                    and requirement.activation is not Activation.MENU)
        if consumes:
            count = state.part_count(requirement.part_title) if state is not None else None
            if count is not None and used[title] >= count:
                later.append(PlanEntry(contract, Verdict.ANOTHER_FLIGHT,
                                       reason=f"only {count} {requirement.part_title} on board "
                                              "and staging uses it up"))
                continue
            used[title] += 1
        order += 1
        planned.append(PlanEntry(contract, Verdict.THIS_FLIGHT, order,
                                 _transition_note(requirement, planned)))
    return planned + later + done


def _flight_order(contract: TestContract) -> tuple[int, float]:
    """Sort key following the flight profile.

    Args:
        contract: A pending contract.

    Returns:
        ``(phase, altitude)``: earlier phases and lower windows first.
    """
    requirement = contract.requirement
    phase = _PHASE.get(requirement.situation, 1) if requirement.situation else 1
    altitude = requirement.altitude.low if requirement.altitude else 0.0
    return phase, altitude


def _shared_moment(requirement: PartTestRequirement, planned: list[PlanEntry]) -> PlanEntry | None:
    """Find an earlier test whose conditions can hold at the same time.

    Args:
        requirement: The test being planned.
        planned: Tests already placed in this flight.

    Returns:
        The earlier test, or ``None`` when no window overlaps.
    """
    for entry in planned:
        if entry.verdict is not Verdict.THIS_FLIGHT:
            continue
        other = entry.requirement
        if other.situation != requirement.situation:
            continue
        if not _ranges_compatible(other.altitude, requirement.altitude):
            continue
        if not _ranges_compatible(other.speed, requirement.speed):
            continue
        return entry
    return None


def _ranges_compatible(first: Range | None, second: Range | None) -> bool:
    """Tell whether two optional ranges can be satisfied at once.

    Args:
        first: A range, or ``None`` for "any value".
        second: A range, or ``None`` for "any value".

    Returns:
        True when some value satisfies both.
    """
    return first is None or second is None or first.overlaps(second)


def _transition_note(requirement: PartTestRequirement, planned: list[PlanEntry]) -> str:
    """Explain what changes between the previous test and this one.

    Args:
        requirement: The test being placed.
        planned: Tests already placed (the last one is the previous test).

    Returns:
        Advice for the player, or an empty string.
    """
    previous = next((e.requirement for e in reversed(planned)
                     if e.verdict is Verdict.THIS_FLIGHT), None)
    if previous is None or previous.speed is None or requirement.speed is None:
        return ""
    if requirement.speed.high < previous.speed.low:
        return (f"slow down from {previous.speed.low:.0f}+ to under "
                f"{requirement.speed.high:.0f} m/s after the previous test")
    if requirement.speed.low > previous.speed.high:
        return f"speed up to {requirement.speed.low:.0f}+ m/s after the previous test"
    return ""


def select_target(plan: list[PlanEntry], state: FlightState) -> PlanEntry | None:
    """Pick the test the vessel is in, or heading to.

    Args:
        plan: The flight plan.
        state: The vessel's state.

    Returns:
        The test whose altitude window contains the vessel; otherwise the
        next window in the direction of travel; ``None`` when there is none.
    """
    candidates = [
        entry for entry in plan
        if entry.actionable and check_conditions(entry.requirement, state).body
        and entry.requirement.situation in (state.situation, None)
    ]
    inside = [e for e in candidates
              if e.requirement.altitude is None or e.requirement.altitude.contains(state.altitude)]
    if inside:
        return inside[0]
    windows = [e for e in candidates if e.requirement.altitude is not None]
    if state.vertical_speed >= 0:
        ahead = [e for e in windows if e.requirement.altitude.low > state.altitude]
        return min(ahead, key=lambda e: e.requirement.altitude.low, default=None)
    ahead = [e for e in windows if e.requirement.altitude.high < state.altitude]
    return max(ahead, key=lambda e: e.requirement.altitude.high, default=None)


def target_speed_range(target: PlanEntry, plan: list[PlanEntry]) -> Range | None:
    """Speed range to hold for a target, merged with tests done alongside.

    Args:
        target: The current target.
        plan: The flight plan.

    Returns:
        The intersection of the speed ranges of the target and the tests
        planned together with it, or ``None`` when there is no speed limit.
    """
    speed = target.requirement.speed
    for entry in plan:
        if entry.verdict is Verdict.TOGETHER and entry.order == target.order and speed is not None:
            other = entry.requirement.speed
            if other is not None:
                speed = speed.intersection(other) or speed
    return speed


@dataclass
class SpeedGovernor:
    """Throttle controller that holds the speed inside a window.

    The controller works on acceleration, which makes it independent of
    the rocket: from the current thrust-to-mass ratio it knows how much
    acceleration the full throttle gives, from two speed readings it knows
    the actual acceleration, so the difference is the drag and gravity
    loss. It then picks the throttle that gives the acceleration needed to
    reach the target speed in ``response_time`` seconds.

    Attributes:
        response_time: Seconds to close a speed error; larger is gentler.
        smoothing: Fraction of the throttle change applied per step.
    """

    response_time: float = 4.0
    smoothing: float = 0.6
    _last_speed: float | None = field(default=None, init=False, repr=False)
    _last_time: float | None = field(default=None, init=False, repr=False)

    def reset(self) -> None:
        """Forget previous readings (after a pause or a new target)."""
        self._last_speed = None
        self._last_time = None

    def command(self, target_speed: float, state: FlightState, now: float) -> float:
        """Compute the throttle for this step.

        Args:
            target_speed: Speed to hold, in m/s.
            state: The vessel's state.
            now: Monotonic time, in seconds.

        Returns:
            Throttle between 0 and 1.
        """
        speed = state.surface_speed
        measured = None
        if self._last_speed is not None and self._last_time is not None and now > self._last_time:
            measured = (speed - self._last_speed) / (now - self._last_time)
        self._last_speed, self._last_time = speed, now

        max_accel = state.available_thrust / state.mass if state.mass > 0 else 0.0
        if max_accel <= 0:
            return state.throttle
        wanted = (target_speed - speed) / self.response_time
        if measured is None:
            # No acceleration estimate yet: proportional guess around the
            # current throttle.
            proposal = state.throttle + wanted / max_accel
        else:
            current_accel = state.thrust / state.mass
            loss = current_accel - measured  # gravity and drag
            proposal = (wanted + loss) / max_accel
        throttle = state.throttle + (proposal - state.throttle) * self.smoothing
        return min(1.0, max(0.0, throttle))


def _km(metres: float) -> str:
    """Format an altitude compactly.

    Args:
        metres: Altitude in metres.

    Returns:
        ``"8 km"`` style text for round thousands, metres otherwise.
    """
    if metres >= 1000 and metres % 100 == 0:
        return f"{metres / 1000:g} km"
    return f"{metres:,.0f} m"
