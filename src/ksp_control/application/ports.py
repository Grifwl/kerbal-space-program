"""Driven ports: the interfaces the use cases need from the outside world.

The use cases only know these abstract classes. Concrete adapters (kRPC,
SQLite, the simulator, in-memory fakes for tests) implement them and are
plugged in by an entrypoint, so the core never imports an infrastructure
library.
"""

from __future__ import annotations

import math
from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import datetime
from enum import Enum

from ksp_control.domain.constellation import Body, Orbit
from ksp_control.domain.model import GameScene, GameSnapshot, LedgerEvent, ResourceSample
from ksp_control.domain.part_tests import EditorState, FlightState, TestContract
from ksp_control.domain.recovery import VesselReport
from ksp_control.domain.tourism import (
    ContractStatus,
    EngineState,
    FlightTelemetry,
    Kerbal,
    OfferedContract,
    Propulsion,
    TourismContract,
)


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
    def paid_objectives(self) -> set[tuple[str, int]]:
        """Return the contract objectives known to be paid already.

        Returns:
            ``(contract key, objective index)`` pairs.
        """

    @abstractmethod
    def add_paid_objectives(self, objectives: set[tuple[str, int]]) -> None:
        """Remember contract objectives that have been paid.

        Args:
            objectives: ``(contract key, objective index)`` pairs.
        """

    @abstractmethod
    def forget_contract_objectives(self, contract_key: str) -> None:
        """Drop the paid objectives of a contract that is finished.

        Args:
            contract_key: Key of the contract.
        """

    @abstractmethod
    def list_events(self, limit: int | None = None) -> list[LedgerEvent]:
        """Return ledger events, newest first.

        Args:
            limit: Maximum number of events, or ``None`` for all of them.

        Returns:
            Events ordered from newest to oldest.
        """


class FleetPort(ABC):
    """Lists the vessels of the save and recovers them."""

    @abstractmethod
    def scene(self) -> GameScene:
        """Return the scene the game is in.

        Returns:
            The current scene.

        Raises:
            GameUnavailableError: When the game cannot be reached.
        """

    @abstractmethod
    def funds(self) -> float:
        """Return the current funds balance.

        Returns:
            Funds.

        Raises:
            GameUnavailableError: When the game cannot be reached.
        """

    @abstractmethod
    def list_vessels(self) -> list[VesselReport]:
        """Read every vessel of the save with what is needed to value it.

        Returns:
            One report per vessel.

        Raises:
            GameUnavailableError: When the game cannot be reached or the
                current scene does not give access to the vessels.
        """

    @abstractmethod
    def vessel_exists(self, vessel_id: str) -> bool:
        """Tell whether a vessel listed earlier is still in the game.

        Args:
            vessel_id: Identifier from ``list_vessels``.

        Returns:
            False once the vessel has been recovered or destroyed.
        """

    @abstractmethod
    def recover(self, vessel_id: str) -> None:
        """Ask the game to recover a vessel (returns without waiting).

        Args:
            vessel_id: Identifier from ``list_vessels``.

        Raises:
            GameUnavailableError: When the game refuses or cannot be reached.
        """

    @abstractmethod
    def close(self) -> None:
        """Release any connection held by the adapter."""


class NoticeLevel(Enum):
    """What a message means to the player; adapters pick the colour."""

    SUCCESS = "success"  # something was achieved (green)
    ACTION = "action"    # the player has to do something now (yellow)
    INFO = "info"        # for information (white)
    WARNING = "warning"  # a problem (orange)
    ERROR = "error"      # something is missing or wrong (red)


class NoticePlacement(Enum):
    """Where a message is shown."""

    BANNER = "banner"  # large, centred: needs attention now
    SIDE = "side"      # small, at the side of the screen: background info


@dataclass(frozen=True)
class Notice:
    """A message for the player.

    Attributes:
        text: The message.
        level: Its meaning.
        placement: Where to show it.
        duration: Seconds on screen.
    """

    text: str
    level: NoticeLevel = NoticeLevel.INFO
    placement: NoticePlacement = NoticePlacement.SIDE
    duration: float = 6.0


class PartTestGamePort(ABC):
    """What the part test assistant needs from the game."""

    @abstractmethod
    def read_test_contracts(self) -> list[TestContract]:
        """Read the active part test contracts and their live progress.

        Returns:
            The contracts; empty outside a career game.

        Raises:
            GameUnavailableError: When the game cannot be read.
        """

    @abstractmethod
    def contract_state(self, contract_id: str) -> str | None:
        """Tell what happened to a contract that is no longer active.

        Args:
            contract_id: Identifier from ``read_test_contracts``.

        Returns:
            ``"completed"``, ``"failed"``, ``"cancelled"``... or ``None``
            when unknown.
        """

    @abstractmethod
    def read_flight(self) -> FlightState | None:
        """Read the active vessel.

        Returns:
            Its state, or ``None`` outside the flight scene.

        Raises:
            GameUnavailableError: When the game cannot be read.
        """

    @abstractmethod
    def read_editor(self) -> EditorState | None:
        """Read the craft being designed.

        Returns:
            The craft, or ``None`` outside the VAB and the SPH.

        Raises:
            GameUnavailableError: When the game cannot be read.
        """

    @abstractmethod
    def set_throttle(self, value: float) -> None:
        """Set the active vessel's main throttle.

        Args:
            value: Between 0 and 1.
        """

    @abstractmethod
    def run_test(self, part_title: str) -> bool:
        """Press "Run Test" on a part of the active vessel.

        Args:
            part_title: Title of the part.

        Returns:
            True when a part offered the action and it was triggered.
        """

    @abstractmethod
    def show(self, notice: Notice) -> None:
        """Show a message on the game screen.

        Args:
            notice: The message.
        """

    @abstractmethod
    def close(self) -> None:
        """Release any connection held by the adapter."""


class TourismGamePort(ABC):
    """What the tourism campaign needs from the game.

    Covers the space center side (funds, contracts, roster, launching) and
    the flight side (telemetry and the controls a suborbital hop or a
    flight to orbit and back needs, maneuver nodes included). Recovering
    vessels goes through ``FleetPort``.
    """

    # ---------------------------------------------------------- space center

    @abstractmethod
    def funds(self) -> float:
        """Return the current funds balance.

        Returns:
            Funds.

        Raises:
            GameUnavailableError: When the game cannot be read.
        """

    @abstractmethod
    def read_tourism_contracts(self) -> list[TourismContract]:
        """Read the tourism contracts on offer and accepted.

        Returns:
            The contracts, with their tourists matched to the roster.

        Raises:
            GameUnavailableError: When the game cannot be read.
        """

    @abstractmethod
    def accept_contract(self, contract_id: str) -> None:
        """Accept an offered contract (returns without checking).

        Args:
            contract_id: Identifier from ``read_tourism_contracts``.

        Raises:
            GameUnavailableError: When the game refuses or cannot be reached.
        """

    @abstractmethod
    def read_offers(self) -> list[OfferedContract]:
        """Read every contract on offer, of any type.

        Returns:
            The offers.

        Raises:
            GameUnavailableError: When the game cannot be read.
        """

    @abstractmethod
    def decline_contract(self, contract_id: str) -> None:
        """Decline an offered contract (it costs reputation).

        Args:
            contract_id: Identifier from ``read_offers``.

        Raises:
            GameUnavailableError: When the game refuses or cannot be reached.
        """

    @abstractmethod
    def contract_status(self, contract_id: str) -> ContractStatus:
        """Read the status of a contract seen earlier.

        Args:
            contract_id: Identifier from ``read_tourism_contracts``.

        Returns:
            Its current status.

        Raises:
            GameUnavailableError: When the game cannot be read.
        """

    @abstractmethod
    def roster(self) -> list[Kerbal]:
        """Read the astronaut roster.

        Returns:
            Every kerbal, hired or not.

        Raises:
            GameUnavailableError: When the game cannot be read.
        """

    @abstractmethod
    def launch(self, craft: str, crew: list[str]) -> str:
        """Launch a VAB craft from the launch pad with the given crew.

        Blocks until the flight scene is loaded and the vessel is on the pad.

        Args:
            craft: Craft name as shown in the VAB.
            crew: Kerbals to board, in seat order (the pilot first).

        Returns:
            The name of the launched vessel.

        Raises:
            GameUnavailableError: When the game refuses the launch.
        """

    @abstractmethod
    def read_crew(self) -> tuple[int, list[str]]:
        """Read who is on board the active vessel and how many fit.

        Returns:
            The crew capacity (pilot seat included) and the names of the
            kerbals on board.

        Raises:
            GameUnavailableError: When the game cannot be read.
        """

    # ---------------------------------------------------------------- flight

    @abstractmethod
    def read_flight(self) -> FlightTelemetry | None:
        """Read the active vessel.

        Returns:
            Its telemetry, or ``None`` when there is no active vessel (the
            game left the flight scene or the vessel was destroyed).

        Raises:
            GameUnavailableError: When the game cannot be read.
        """

    @abstractmethod
    def read_engines(self) -> list[EngineState]:
        """Read the engines of the active vessel.

        Returns:
            One entry per engine.
        """

    @abstractmethod
    def parachutes_safe(self) -> bool:
        """Tell whether the game rates a parachute safe to open now.

        Returns:
            True when at least one parachute of the vessel is safe.
        """

    @abstractmethod
    def parachutes_open(self) -> bool:
        """Tell whether a parachute of the vessel has opened.

        Returns:
            True when at least one is semi-deployed or deployed.
        """

    @abstractmethod
    def has_control(self) -> bool:
        """Tell whether the active vessel can be controlled.

        Returns:
            False when no pilot or probe core is in command.
        """

    @abstractmethod
    def stage(self) -> None:
        """Activate the next stage (like pressing space)."""

    @abstractmethod
    def set_throttle(self, value: float) -> None:
        """Set the main throttle.

        Args:
            value: Between 0 and 1.
        """

    @abstractmethod
    def point(self, pitch: float, heading: float) -> None:
        """Hold a pitch and heading with the autopilot.

        Args:
            pitch: Degrees above the horizon.
            heading: Compass degrees.
        """

    @abstractmethod
    def hold_retrograde(self) -> None:
        """Point the nose against the surface velocity (heat shield first)."""

    @abstractmethod
    def set_rcs(self, on: bool) -> None:
        """Switch the reaction control system (RCS) on or off.

        Args:
            on: True to let the autopilot use the RCS thrusters.
        """

    @abstractmethod
    def release_controls(self) -> None:
        """Disengage the autopilot and leave the throttle at zero."""

    @abstractmethod
    def deploy_landing_gear(self) -> int:
        """Lower the landing legs and retractable wheels of the vessel.

        Returns:
            How many parts were lowered; 0 when the vessel has none (or
            only fixed ones, which need nothing).
        """

    @abstractmethod
    def warp_to(self, ut: float) -> None:
        """Time warp until a universal time (blocks).

        Args:
            ut: Universal time to reach.
        """

    @abstractmethod
    def set_physics_warp(self, factor: int) -> None:
        """Set the physics warp factor.

        Args:
            factor: 0 (normal speed) to 3 (4x).
        """

    # ------------------------------------------------------------- orbital

    @abstractmethod
    def read_propulsion(self) -> Propulsion:
        """Read what the active vessel can still do with its engines.

        Blocks while the game brings its delta-v figures up to date.

        Returns:
            Thrust, specific impulse, mass and delta-v left.
        """

    @abstractmethod
    def add_node(self, ut: float, prograde: float) -> None:
        """Plan a maneuver node, replacing any other one.

        Args:
            ut: Universal time of the burn.
            prograde: Delta-v along the velocity, in m/s (negative:
                retrograde).
        """

    @abstractmethod
    def node_remaining_dv(self) -> float | None:
        """Read the delta-v still to burn for the maneuver node.

        Returns:
            The remaining delta-v, in m/s, or ``None`` without a node. It
            grows again if the burn goes past the node.
        """

    @abstractmethod
    def aim_at_node(self) -> None:
        """Point the vessel along the burn of the maneuver node."""

    @abstractmethod
    def remove_nodes(self) -> None:
        """Delete every maneuver node of the active vessel."""

    @abstractmethod
    def pointing_error(self) -> float:
        """Angle between where the autopilot aims and where the nose points.

        Returns:
            Degrees.
        """

    @abstractmethod
    def close(self) -> None:
        """Release any connection held by the adapter."""


class Direction(Enum):
    """Where the autopilot points, relative to the orbit of the vessel."""

    PROGRADE = "prograde"
    RETROGRADE = "retrograde"
    NORMAL = "normal"


@dataclass(frozen=True)
class VesselOrbit:
    """Where a vessel is and how it orbits.

    Attributes:
        name: Vessel name.
        body: Name of the body it orbits.
        situation: KSP situation (``orbiting``, ``escaping``,
            ``sub_orbital``, ``landed``...).
        ut: Universal time of the reading, in seconds.
        periapsis: Periapsis altitude, in metres.
        apoapsis: Apoapsis altitude, in metres; ``math.inf`` on an escape
            trajectory.
        semi_major_axis: In metres (negative on an escape trajectory).
        period: Seconds per lap; ``math.inf`` on an escape trajectory.
        radius: Distance from the centre of the body now, in metres.
        speed: Orbital speed now, in m/s.
        time_to_periapsis: Seconds to the next periapsis.
        time_to_apoapsis: Seconds to the next apoapsis (``math.inf`` on an
            escape trajectory).
        elements: The Keplerian elements of a closed orbit, to predict
            positions; ``None`` on an escape trajectory.
    """

    name: str
    body: str
    situation: str
    ut: float
    periapsis: float
    apoapsis: float
    semi_major_axis: float
    period: float
    radius: float
    speed: float
    time_to_periapsis: float
    time_to_apoapsis: float
    elements: Orbit | None

    @property
    def closed(self) -> bool:
        """Whether the vessel is on an elliptical orbit (not escaping).

        Returns:
            True when it has elements and a finite apoapsis.
        """
        return self.elements is not None and math.isfinite(self.apoapsis)


class ConstellationGamePort(ABC):
    """What the relay constellation deployment needs from the game.

    Vessels are named by their (unique) names, since the deployment names
    every probe and the carrier itself. Commands (throttle, autopilot,
    engines) act on the active vessel; readings of orbits work for any
    vessel, loaded or not, and in any scene.
    """

    @abstractmethod
    def scene(self) -> GameScene:
        """Read the current game scene.

        Returns:
            The scene; ``UNKNOWN`` while a building is open in the space
            center or the game is loading.
        """

    @abstractmethod
    def ut(self) -> float:
        """Read the universal time.

        Returns:
            Seconds since the start of the game.
        """

    @abstractmethod
    def body(self, name: str) -> Body:
        """Read a celestial body.

        Args:
            name: Internal name of the body (``Mun``).

        Returns:
            Its figures.

        Raises:
            KeyError: When there is no such body.
        """

    @abstractmethod
    def active_vessel(self) -> str | None:
        """Name the vessel the player flies.

        Returns:
            Its name, or ``None`` outside the flight scene.
        """

    @abstractmethod
    def vessel_names(self) -> list[str]:
        """List every vessel of the game.

        Returns:
            Their names.
        """

    @abstractmethod
    def orbit(self, vessel: str) -> VesselOrbit | None:
        """Read the orbit of a vessel.

        Args:
            vessel: Its name.

        Returns:
            The orbit, or ``None`` when the vessel does not exist (any
            more).
        """

    @abstractmethod
    def period(self) -> float:
        """Read the orbital period of the active vessel, quickly.

        Called many times per second while a period is trimmed, so the
        adapter should make it cheap (a stream).

        Returns:
            Seconds per lap (``math.inf`` on an escape trajectory).
        """

    @abstractmethod
    def mounted_probes(self) -> int:
        """Count the relay probes still attached to the active vessel.

        A probe is a piece that a decoupler or a docking port would set
        free, with exactly one probe core on it.

        Returns:
            How many there are.
        """

    @abstractmethod
    def release_probe(self, name: str) -> bool:
        """Set one probe free from the active vessel and get it ready.

        The new vessel gets the name, the relay type, its probe core as the
        control point, and its antennas and solar panels deployed. The
        active vessel stays the carrier.

        Args:
            name: Name to give it.

        Returns:
            False when no probe is left to release.
        """

    @abstractmethod
    def rename(self, vessel: str, new_name: str) -> None:
        """Rename a vessel.

        Args:
            vessel: Its current name.
            new_name: Its new name.
        """

    @abstractmethod
    def switch_to(self, vessel: str) -> None:
        """Make a vessel the active one, from any scene.

        Blocks until the flight scene shows it, loaded and ready.

        Args:
            vessel: Its name.

        Raises:
            GameUnavailableError: When the game does not switch.
        """

    @abstractmethod
    def has_control(self) -> bool:
        """Tell whether the active vessel can be commanded.

        Returns:
            False without a pilot or a working (powered) probe core.
        """

    @abstractmethod
    def prepare_engines(self) -> int:
        """Ignite every engine of the active vessel that is still off.

        A probe set free by a decoupler has never been staged, so its
        engines would not respond to the throttle.

        Returns:
            How many engines are on and have fuel.
        """

    @abstractmethod
    def propulsion(self) -> Propulsion:
        """Read what the active vessel can still do with its engines.

        Returns:
            Thrust, vacuum specific impulse, mass and vacuum delta-v.
        """

    @abstractmethod
    def point(self, direction: Direction) -> None:
        """Hold a direction of the orbit with the autopilot.

        Args:
            direction: Where to point.
        """

    @abstractmethod
    def relative_positions(self, origin: str, others: list[str]) -> dict[str, tuple[float, float]]:
        """Where other vessels are, seen along the orbit of one vessel.

        Measured in the same frame ``point_between`` aims in, so a side
        found here is the side the autopilot turns to.

        Args:
            origin: The vessel the positions are measured from.
            others: The vessels to locate.

        Returns:
            For each one found: its offset along the velocity of
            ``origin`` and along its orbit normal, in metres.
        """

    @abstractmethod
    def point_between(self, prograde: float, normal: float) -> None:
        """Hold a direction mixing prograde and normal, with the autopilot.

        Args:
            prograde: Component along the velocity (negative: retrograde).
            normal: Component along the orbit normal (negative: anti-normal).
        """

    @abstractmethod
    def pointing_error(self) -> float:
        """Angle between where the autopilot aims and where the nose points.

        Returns:
            Degrees.
        """

    @abstractmethod
    def set_throttle(self, value: float) -> None:
        """Set the main throttle of the active vessel.

        Args:
            value: Between 0 and 1.
        """

    @abstractmethod
    def release_controls(self) -> None:
        """Disengage the autopilot and leave the throttle at zero."""

    @abstractmethod
    def warp_to(self, ut: float) -> None:
        """Time warp until a universal time (blocks).

        Args:
            ut: Universal time to reach.
        """

    @abstractmethod
    def set_alarm(self, ut: float, vessel: str, title: str, description: str) -> None:
        """Create a game alarm (stock alarm clock) linked to a vessel.

        Args:
            ut: Universal time when it goes off.
            vessel: Vessel it belongs to (the alarm offers to jump to it).
            title: Its title.
            description: What it is about.
        """

    @abstractmethod
    def remove_alarms(self, title_prefix: str) -> int:
        """Delete the game alarms whose titles start with a text.

        Args:
            title_prefix: Start of the titles.

        Returns:
            How many were deleted.
        """

    @abstractmethod
    def show(self, notice: Notice) -> None:
        """Show a message on the game screen (never fails).

        Args:
            notice: The message.
        """

    @abstractmethod
    def comms_power(self, vessel: str) -> float:
        """Read the combined antenna power of a vessel.

        Two vessels reach each other up to ``sqrt(P1 * P2)`` metres.

        Args:
            vessel: Its name.

        Returns:
            The power, in metres; 0 when unknown.
        """

    @abstractmethod
    def close(self) -> None:
        """Release any connection held by the adapter."""
