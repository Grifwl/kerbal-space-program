"""Driven ports: the interfaces the use cases need from the outside world.

The use cases only know these abstract classes. Concrete adapters (kRPC,
SQLite, the simulator, in-memory fakes for tests) implement them and are
plugged in by an entrypoint, so the core never imports an infrastructure
library.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import datetime
from enum import Enum

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
