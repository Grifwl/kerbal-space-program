"""kRPC adapter for the tourism campaign.

Notes on kRPC 0.8.0 and KSP 1.12 that shape this adapter (checked on a
Spanish game):

* Tourism contracts are ``FinePrint.Contracts.TourismContract``. Each
  top-level objective is one tourist's itinerary ("Itinerario de viaje de
  Samroda") and its children are the destinations ("Vuelo espacial
  suborbital en Kerbin").
* The tourists of an *offered* contract already exist in the roster, as
  type ``unowned`` and status ``assigned``; accepting the contract makes
  them ``tourist`` and ``available``.
* ``launch_vessel`` takes the craft *file* name, in which KSP replaced
  characters such as ``.`` ("Passatgers v1.1" is saved as
  "Passatgers v1_1.craft"). Named crew fill the seats in part order, so
  the pilot must come first to sit in the command pod when it is the root
  part. The call blocks until the vessel is on the pad.
* The autopilot is switched with the ``AutoPilot.engaged`` property
  (0.8 has no ``engage``/``disengage`` methods any more).
* ``Parachute.safe_state`` is the game's own "safe to open" rating (the
  colour of the staging icon).
"""

from __future__ import annotations

import logging
import math
import re
from typing import Any

from ksp_control.adapters.game.krpc_connection import (
    RECOVERABLE_ERRORS,
    KrpcEndpoint,
    connect,
    is_connection_problem,
    read_scene,
)
from ksp_control.application.ports import GameUnavailableError, TourismGamePort
from ksp_control.domain.model import GameScene
from ksp_control.domain.tourism import (
    ContractStatus,
    EngineState,
    FlightTelemetry,
    Kerbal,
    Tourist,
    TourismContract,
    match_kerbal,
)

logger = logging.getLogger(__name__)

TOURISM_CONTRACT_TYPE = "FinePrint.Contracts.TourismContract"

# Above this pitch the autopilot aims at the zenith: kRPC documents the
# heading as ill-defined near the vertical.
VERTICAL_PITCH = 89.5


def craft_file_name(craft: str, available: list[str]) -> str | None:
    """Find the file name KSP gave to a craft.

    Args:
        craft: Craft name as typed by the player ("Passatgers v1.1").
        available: File names of the launchable crafts (no extension).

    Returns:
        The matching file name, or ``None``.
    """
    def normalize(name: str) -> str:
        # KSP replaces characters that are not safe in file names (".",
        # "/", ":"...) with "_" when saving; compare without case too.
        return re.sub(r"[^\w\- ]", "_", name).casefold()

    wanted = normalize(craft)
    for name in available:
        if name == craft:
            return name
    matches = [name for name in available if normalize(name) == wanted]
    return matches[0] if len(matches) == 1 else None


def _enum_name(value: Any) -> str:
    """Lower-case name of a kRPC enum value.

    Args:
        value: kRPC enum member.

    Returns:
        Its name, e.g. ``"sub_orbital"``.
    """
    return str(getattr(value, "name", value)).lower()


def _total_funds(parameters: list[Any]) -> float:
    """Add up the funds paid by a tree of objectives.

    Args:
        parameters: Remote ``ContractParameter`` objects.

    Returns:
        Funds paid on completion of all of them and their children.
    """
    total = 0.0
    for parameter in parameters:
        total += float(parameter.funds_completion)
        total += _total_funds(list(parameter.children))
    return total


class KrpcTourismGame(TourismGamePort):
    """Reads tourism contracts and the roster, launches and flies the craft.

    Responsibilities: translate remote contracts and kerbals into domain
    values, keep the remote contract objects so that later calls act on
    the same contracts, and drive the active vessel (throttle, staging,
    autopilot, time warp).

    Usage notes: not thread safe; one instance per campaign. Every kRPC
    failure is turned into ``GameUnavailableError``; the connection is
    reopened on the next call when the socket broke.
    """

    def __init__(self, endpoint: KrpcEndpoint, client_name: str = "KSP tourism") -> None:
        """Configure the adapter; the connection is opened on first use.

        Args:
            endpoint: kRPC server location.
            client_name: Name shown in the kRPC server window in KSP.
        """
        self._endpoint = endpoint
        self._client_name = client_name
        self._conn: Any = None
        self._contracts: dict[str, Any] = {}
        # (vessel, stage) -> highest parachute stage; refreshed on staging.
        self._chutes: tuple[Any, int, int | None] | None = None
        # Engine -> decouple stage of its part; it never changes.
        self._decouple_stage: dict[Any, int] = {}
        # Body name -> (atmosphere depth, gravitational parameter).
        self._bodies: dict[str, tuple[float, float]] = {}

    # ---------------------------------------------------------- space center

    def funds(self) -> float:
        """Return the current funds balance.

        Returns:
            Funds.
        """
        return self._call(lambda conn: float(conn.space_center.funds))

    def read_tourism_contracts(self) -> list[TourismContract]:
        """Read the tourism contracts on offer and accepted.

        Returns:
            The contracts, with their tourists matched to the roster.
        """
        def read(conn: Any) -> list[TourismContract]:
            space_center = conn.space_center
            kerbals = list(space_center.kerbals)
            # Tourists are "unowned" before the contract is accepted and
            # "tourist" after; matching only among them avoids confusing a
            # tourist with a crew member who has the same first name.
            tourist_names = [str(k.name) for k in kerbals
                             if _enum_name(k.type) in ("tourist", "unowned")]
            manager = space_center.contract_manager
            result = []
            for remote in [*manager.offered_contracts, *manager.active_contracts]:
                if str(remote.type) != TOURISM_CONTRACT_TYPE:
                    continue
                contract_id = str(getattr(remote, "_object_id", id(remote)))
                self._contracts[contract_id] = remote
                itineraries = list(remote.parameters)
                tourists = tuple(
                    Tourist(
                        itinerary=str(itinerary.title),
                        destinations=tuple(str(d.title) for d in itinerary.children),
                        kerbal=match_kerbal(str(itinerary.title), tourist_names),
                    )
                    for itinerary in itineraries
                )
                reward = (float(remote.funds_advance) + float(remote.funds_completion)
                          + _total_funds(itineraries))
                result.append(TourismContract(
                    contract_id=contract_id,
                    title=str(remote.title),
                    status=ContractStatus.from_name(_enum_name(remote.state)),
                    tourists=tourists,
                    reward=reward,
                ))
            return result

        return self._call(read)

    def accept_contract(self, contract_id: str) -> None:
        """Accept an offered contract.

        Args:
            contract_id: Identifier from ``read_tourism_contracts``.
        """
        remote = self._contracts[contract_id]
        self._call(lambda conn: remote.accept())

    def contract_status(self, contract_id: str) -> ContractStatus:
        """Read the status of a contract seen earlier.

        Args:
            contract_id: Identifier from ``read_tourism_contracts``.

        Returns:
            Its current status.
        """
        remote = self._contracts[contract_id]
        return self._call(lambda conn: ContractStatus.from_name(_enum_name(remote.state)))

    def roster(self) -> list[Kerbal]:
        """Read the astronaut roster.

        Returns:
            Every kerbal, hired or not.
        """
        def read(conn: Any) -> list[Kerbal]:
            return [
                Kerbal(
                    name=str(k.name),
                    kind=_enum_name(k.type),
                    trait=str(k.trait),
                    available=_enum_name(k.roster_status) == "available" and not bool(k.inactive),
                )
                for k in conn.space_center.kerbals
            ]

        return self._call(read)

    def launch(self, craft: str, crew: list[str]) -> str:
        """Launch a VAB craft from the launch pad with the given crew.

        Args:
            craft: Craft name as shown in the VAB.
            crew: Kerbals to board, the pilot first.

        Returns:
            The name of the launched vessel.

        Raises:
            GameUnavailableError: When the craft does not exist or the
                game refuses the launch.
        """
        def launch(conn: Any) -> str:
            space_center = conn.space_center
            available = [str(name) for name in space_center.launchable_vessels("VAB")]
            file_name = craft_file_name(craft, available)
            if file_name is None:
                raise GameUnavailableError(
                    f"no VAB craft named '{craft}' (available: {', '.join(available)})"
                )
            space_center.launch_vessel(
                craft_directory="VAB", name=file_name, launch_site="LaunchPad",
                crew=list(crew), recover=True,
            )
            self._chutes = None
            return str(space_center.active_vessel.name)

        return self._call(launch)

    # ---------------------------------------------------------------- flight

    def read_flight(self) -> FlightTelemetry | None:
        """Read the active vessel.

        Returns:
            Its telemetry, or ``None`` outside the flight scene.
        """
        def read(conn: Any) -> FlightTelemetry | None:
            if read_scene(conn) is not GameScene.FLIGHT:
                return None
            space_center = conn.space_center
            vessel = space_center.active_vessel
            if vessel is None:
                return None
            orbit = vessel.orbit
            body = orbit.body
            body_name = str(body.name)
            if body_name not in self._bodies:
                self._bodies[body_name] = (float(body.atmosphere_depth),
                                           float(body.gravitational_parameter))
            atmosphere, mu = self._bodies[body_name]
            # The body's rotating frame gives surface-relative speeds.
            flight = vessel.flight(body.reference_frame)
            stage = int(vessel.control.current_stage)
            # Drag is a vector; its length does not depend on the frame.
            drag = math.sqrt(sum(c * c for c in flight.drag))
            radius = float(orbit.radius)
            return FlightTelemetry(
                vessel=str(vessel.name),
                situation=_enum_name(vessel.situation),
                altitude=float(flight.mean_altitude),
                vertical_speed=float(flight.vertical_speed),
                surface_speed=float(flight.speed),
                apoapsis=float(orbit.apoapsis_altitude),
                time_to_apoapsis=float(orbit.time_to_apoapsis),
                ut=float(space_center.ut),
                current_stage=stage,
                parachute_stage=self._parachute_stage(vessel, stage),
                atmosphere_depth=atmosphere,
                drag=drag,
                # Local weight: m * mu / r^2 (gravity weakens with height).
                weight=float(vessel.mass) * mu / (radius * radius),
                dynamic_pressure=float(flight.dynamic_pressure),
            )

        return self._call(read)

    def _parachute_stage(self, vessel: Any, stage: int) -> int | None:
        """Highest activation stage of the vessel's parachutes (cached).

        Args:
            vessel: Remote active vessel.
            stage: Its current stage; the cache is refreshed when it changes.

        Returns:
            The stage, or ``None`` without parachutes.
        """
        if self._chutes is not None and self._chutes[0] == vessel and self._chutes[1] == stage:
            return self._chutes[2]
        stages = [int(chute.part.stage) for chute in vessel.parts.parachutes]
        value = max(stages) if stages else None
        self._chutes = (vessel, stage, value)
        return value

    def read_engines(self) -> list[EngineState]:
        """Read the engines of the active vessel.

        Returns:
            One entry per engine.
        """
        def read(conn: Any) -> list[EngineState]:
            engines = []
            for engine in conn.space_center.active_vessel.parts.engines:
                if engine not in self._decouple_stage:
                    self._decouple_stage[engine] = int(engine.part.decouple_stage)
                engines.append(EngineState(
                    active=bool(engine.active),
                    has_fuel=bool(engine.has_fuel),
                    decouple_stage=self._decouple_stage[engine],
                ))
            return engines

        return self._call(read)

    def parachutes_safe(self) -> bool:
        """Tell whether the game rates a parachute safe to open now.

        Returns:
            True when at least one parachute is rated safe.
        """
        return self._call(lambda conn: any(
            _enum_name(chute.safe_state) == "safe"
            for chute in conn.space_center.active_vessel.parts.parachutes
        ))

    def parachutes_open(self) -> bool:
        """Tell whether a parachute of the vessel has opened.

        Returns:
            True when at least one is semi-deployed or deployed.
        """
        return self._call(lambda conn: any(
            _enum_name(chute.state) in ("semi_deployed", "deployed")
            for chute in conn.space_center.active_vessel.parts.parachutes
        ))

    def has_control(self) -> bool:
        """Tell whether the active vessel can be controlled.

        Returns:
            False when neither a pilot nor a probe core is in command.
        """
        return self._call(
            lambda conn: _enum_name(conn.space_center.active_vessel.control.source) != "none"
        )

    def stage(self) -> None:
        """Activate the next stage."""
        self._call(lambda conn: conn.space_center.active_vessel.control.activate_next_stage())

    def set_throttle(self, value: float) -> None:
        """Set the main throttle.

        Args:
            value: Between 0 and 1.
        """
        def write(conn: Any) -> None:
            conn.space_center.active_vessel.control.throttle = float(value)

        self._call(write)

    def point(self, pitch: float, heading: float) -> None:
        """Hold a pitch and heading with the autopilot.

        Args:
            pitch: Degrees above the horizon.
            heading: Compass degrees.
        """
        def write(conn: Any) -> None:
            vessel = conn.space_center.active_vessel
            autopilot = vessel.auto_pilot
            # Frame first, then target: kRPC 0.8 re-interprets the target
            # when the frame changes.
            autopilot.reference_frame = vessel.surface_reference_frame
            if pitch >= VERTICAL_PITCH:
                # The heading is ill-defined straight up; aim at the zenith
                # (+x in the surface frame) instead.
                autopilot.target_direction = (1.0, 0.0, 0.0)
            else:
                autopilot.target_pitch_and_heading(float(pitch), float(heading))
            autopilot.engaged = True

        self._call(write)

    def hold_retrograde(self) -> None:
        """Point the nose against the surface velocity (heat shield first)."""
        def write(conn: Any) -> None:
            vessel = conn.space_center.active_vessel
            autopilot = vessel.auto_pilot
            # In this frame +y is the direction of travel over the surface,
            # so -y is surface retrograde.
            autopilot.reference_frame = vessel.surface_velocity_reference_frame
            autopilot.target_direction = (0.0, -1.0, 0.0)
            autopilot.engaged = True

        self._call(write)

    def release_controls(self) -> None:
        """Disengage the autopilot and leave the throttle at zero."""
        def write(conn: Any) -> None:
            vessel = conn.space_center.active_vessel
            vessel.auto_pilot.engaged = False
            vessel.control.throttle = 0.0

        self._call(write)

    def warp_to(self, ut: float) -> None:
        """Time warp until a universal time (blocks).

        Args:
            ut: Universal time to reach.
        """
        self._call(lambda conn: conn.space_center.warp_to(float(ut)))

    def set_physics_warp(self, factor: int) -> None:
        """Set the physics warp factor.

        Args:
            factor: 0 (normal speed) to 3 (4x).
        """
        def write(conn: Any) -> None:
            conn.space_center.physics_warp_factor = int(factor)

        self._call(write)

    # ------------------------------------------------------------- plumbing

    def close(self) -> None:
        """Close the kRPC connection, if open."""
        if self._conn is not None:
            try:
                self._conn.close()
            except Exception:  # noqa: BLE001 - closing must never fail
                pass
        self._conn = None
        self._contracts.clear()
        self._decouple_stage.clear()
        self._chutes = None

    def _call(self, action: Any) -> Any:
        """Run an action against the game, translating kRPC errors.

        Args:
            action: Callable receiving the kRPC client.

        Returns:
            Whatever the action returns.

        Raises:
            GameUnavailableError: On any kRPC failure.
        """
        if self._conn is None:
            self._conn = connect(self._endpoint, self._client_name)
        try:
            return action(self._conn)
        except RECOVERABLE_ERRORS as error:
            if is_connection_problem(error):
                self.close()
            # kRPC appends the C# stack trace; the first line is enough.
            message = str(error).splitlines()[0] if str(error) else type(error).__name__
            raise GameUnavailableError(message) from error
