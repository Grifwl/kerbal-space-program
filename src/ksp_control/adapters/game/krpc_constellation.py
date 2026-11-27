"""kRPC adapter for the relay constellation deployment.

Notes on kRPC 0.8.0 and KSP 1.12 that shape this adapter:

* Orbits are read as Keplerian elements (``Orbit.inclination``,
  ``longitude_of_ascending_node``, ``argument_of_periapsis``,
  ``mean_anomaly_at_epoch``, ``epoch``, all in radians and seconds). They
  are plain data, available for vessels far away and outside the flight
  scene, where positions and reference frames are not.
* ``Decoupler.decouple`` and ``DockingPort.undock`` return the new vessel,
  but may make it the active one; the carrier is made active again.
* A vessel created by a decoupler has no control point, even with a probe
  core (``Parts.controlling`` is null); it would be steered from its root
  part, which may be the decoupler. The probe core is set as the control
  point.
* ``Vessel.orbital_reference_frame``: x anti-radial, y prograde, z normal.
* Setting ``SpaceCenter.active_vessel`` works from any scene (from the
  space center or the tracking station it loads the flight scene).
* ``SpaceCenter.AlarmManager.add_vessel_alarm`` takes the seconds from now,
  not a universal time.
"""

from __future__ import annotations

import logging
import math
import time
from typing import Any

from ksp_control.adapters.game.krpc_connection import (
    RECOVERABLE_ERRORS,
    KrpcEndpoint,
    connect,
    is_connection_problem,
    read_scene,
    show_notice,
)
from ksp_control.application.ports import (
    ConstellationGamePort,
    Direction,
    GameUnavailableError,
    Notice,
    VesselOrbit,
)
from ksp_control.domain.constellation import Body, Orbit, orbital_speed
from ksp_control.domain.model import GameScene
from ksp_control.domain.tourism import Propulsion

logger = logging.getLogger(__name__)

# Seconds a vessel switch may take (a scene load from the space center).
SWITCH_TIMEOUT = 180.0
# Seconds given to the game after a switch, for the vessels around to unpack.
SETTLE_AFTER_SWITCH = 3.0


def _enum_name(value: Any) -> str:
    """Turn a kRPC enumeration value into a lowercase name.

    Args:
        value: Enumeration value (or anything printable).

    Returns:
        The name, e.g. ``"orbiting"``.
    """
    return str(getattr(value, "name", value)).lower()


class KrpcConstellationGame(ConstellationGamePort):
    """Reads orbits and flies the carrier and the probes of a constellation.

    Responsibilities: find vessels by name, translate their orbits into
    domain values, find the probes attached to the carrier and set them
    free, switch vessels, steer and throttle the active one, manage game
    alarms and screen messages.

    Usage notes: not thread safe; one instance per run. Every kRPC failure
    is turned into ``GameUnavailableError``; the connection is reopened on
    the next call when the socket broke.
    """

    def __init__(self, endpoint: KrpcEndpoint, client_name: str = "KSP constellation") -> None:
        """Configure the adapter; the connection is opened on first use.

        Args:
            endpoint: kRPC server location.
            client_name: Name shown in the kRPC server window in KSP.
        """
        self._endpoint = endpoint
        self._client_name = client_name
        self._conn: Any = None
        # Vessel name -> remote vessel; checked on every use.
        self._vessels: dict[str, Any] = {}
        # (remote vessel, stream of its orbital period).
        self._period: tuple[Any, Any] | None = None

    # ------------------------------------------------------------ reading

    def scene(self) -> GameScene:
        """Read the current game scene.

        Returns:
            The scene (``UNKNOWN`` for a building open in the space center).
        """
        return self._call(read_scene)

    def ut(self) -> float:
        """Read the universal time.

        Returns:
            Seconds since the start of the game.
        """
        return self._call(lambda conn: float(conn.space_center.ut))

    def body(self, name: str) -> Body:
        """Read a celestial body.

        Args:
            name: Internal name (``Mun``).

        Returns:
            Its figures.

        Raises:
            KeyError: When there is no such body.
        """
        def read(conn: Any) -> Body:
            bodies = conn.space_center.bodies
            if name not in bodies:
                raise KeyError(f"no body named {name!r}; known: {', '.join(sorted(bodies))}")
            body = bodies[name]
            return Body(
                name=name,
                mu=float(body.gravitational_parameter),
                radius=float(body.equatorial_radius),
                sphere_of_influence=float(body.sphere_of_influence),
                atmosphere_depth=float(body.atmosphere_depth) if body.has_atmosphere else 0.0,
            )

        return self._call(read)

    def active_vessel(self) -> str | None:
        """Name the vessel the player flies.

        Returns:
            Its name, or ``None`` outside the flight scene.
        """
        def read(conn: Any) -> str | None:
            if read_scene(conn) is not GameScene.FLIGHT:
                return None
            vessel = conn.space_center.active_vessel
            return None if vessel is None else str(vessel.name)

        return self._call(read)

    def vessel_names(self) -> list[str]:
        """List every vessel of the game, refreshing the name cache.

        Returns:
            Their names.
        """
        def read(conn: Any) -> list[str]:
            self._vessels = {str(v.name): v for v in conn.space_center.vessels}
            return list(self._vessels)

        return self._call(read)

    def orbit(self, vessel: str) -> VesselOrbit | None:
        """Read the orbit of a vessel.

        Args:
            vessel: Its name.

        Returns:
            The orbit, or ``None`` when no vessel has that name.
        """
        def read(conn: Any) -> VesselOrbit | None:
            remote = self._find(conn, vessel)
            if remote is None:
                return None
            orbit = remote.orbit
            body = orbit.body
            mu = float(body.gravitational_parameter)
            radius_body = float(body.equatorial_radius)
            ut = float(conn.space_center.ut)
            eccentricity = float(orbit.eccentricity)
            semi_major_axis = float(orbit.semi_major_axis)
            situation = _enum_name(remote.situation)
            periapsis = float(orbit.periapsis_altitude)
            if eccentricity < 1.0 and semi_major_axis > 0:
                elements = Orbit(
                    mu=mu,
                    semi_major_axis=semi_major_axis,
                    eccentricity=eccentricity,
                    inclination=float(orbit.inclination),
                    longitude_of_ascending_node=float(orbit.longitude_of_ascending_node),
                    argument_of_periapsis=float(orbit.argument_of_periapsis),
                    mean_anomaly_at_epoch=float(orbit.mean_anomaly_at_epoch),
                    epoch=float(orbit.epoch),
                )
                # Derived from the elements: works in every scene.
                radius = elements.radius_at(ut)
                return VesselOrbit(
                    name=vessel, body=str(body.name), situation=situation, ut=ut,
                    periapsis=periapsis, apoapsis=elements.apoapsis_radius - radius_body,
                    semi_major_axis=semi_major_axis, period=elements.period, radius=radius,
                    speed=orbital_speed(mu, radius, semi_major_axis),
                    time_to_periapsis=elements.next_periapsis(ut) - ut,
                    time_to_apoapsis=elements.next_apoapsis(ut) - ut, elements=elements,
                )
            # An escape trajectory: only in flight, where these work.
            return VesselOrbit(
                name=vessel, body=str(body.name), situation=situation, ut=ut,
                periapsis=periapsis, apoapsis=math.inf, semi_major_axis=semi_major_axis,
                period=math.inf, radius=float(orbit.radius), speed=float(orbit.speed),
                time_to_periapsis=float(orbit.time_to_periapsis), time_to_apoapsis=math.inf,
                elements=None,
            )

        return self._call(read)

    def period(self) -> float:
        """Read the period of the active vessel from a stream.

        Returns:
            Seconds per lap (``math.inf`` on an escape trajectory).
        """
        def read(conn: Any) -> float:
            vessel = conn.space_center.active_vessel
            if self._period is None or self._period[0] != vessel:
                self._drop_period_stream()
                self._period = (vessel, conn.add_stream(getattr, vessel.orbit, "period"))
            value = float(self._period[1]())
            return value if value > 0 and math.isfinite(value) else math.inf

        return self._call(read)

    def comms_power(self, vessel: str) -> float:
        """Read the combined antenna power of a vessel.

        Args:
            vessel: Its name.

        Returns:
            The power in metres, or 0 when it cannot be read.
        """
        def read(conn: Any) -> float:
            remote = self._find(conn, vessel)
            return 0.0 if remote is None else float(remote.comms.power)

        try:
            return self._call(read)
        except GameUnavailableError as error:
            logger.debug("Antenna power of %s not read: %s", vessel, error)
            return 0.0

    def relative_positions(self, origin: str, others: list[str]) -> dict[str, tuple[float, float]]:
        """Offsets of other vessels along the velocity and normal of one.

        Args:
            origin: The reference vessel.
            others: The vessels to locate.

        Returns:
            ``(along the velocity, along the normal)`` in metres, for each
            vessel found.
        """
        def read(conn: Any) -> dict[str, tuple[float, float]]:
            reference = self._find(conn, origin)
            if reference is None:
                return {}
            frame = reference.orbital_reference_frame
            result = {}
            for name in others:
                remote = self._find(conn, name)
                if remote is None:
                    continue
                # x anti-radial, y prograde, z normal.
                _, prograde, normal = remote.position(frame)
                result[name] = (float(prograde), float(normal))
            return result

        return self._call(read)

    # ------------------------------------------------------------ probes

    def mounted_probes(self) -> int:
        """Count the probes still attached to the active vessel.

        Returns:
            How many distinct probe cores some decoupler or docking port
            would set free.
        """
        def read(conn: Any) -> int:
            mounts = self._mounts(conn.space_center.active_vessel)
            return len({core for cores, _ in mounts for core in cores})

        return self._call(read)

    def release_probe(self, name: str) -> bool:
        """Set one probe free from the active vessel and get it ready.

        Args:
            name: Name to give it.

        Returns:
            False when no probe is left.
        """
        def act(conn: Any) -> bool:
            space_center = conn.space_center
            carrier = space_center.active_vessel
            mounts = [m for m in self._mounts(carrier) if len(m[0]) == 1]
            if not mounts:
                return False
            # The smallest piece first: the outer probe of a stack.
            (core,), release = min(mounts, key=lambda m: m[1][2])
            kind, remote, _ = release
            if kind == "decoupler":
                remote.decouple()
            else:
                remote.undock()
            probe = core.vessel
            probe.name = name
            probe.type = space_center.VesselType.relay
            try:
                probe.parts.controlling = core
            except RECOVERABLE_ERRORS as error:
                if is_connection_problem(error):
                    raise
                logger.warning("%s: could not control it from its probe core: %s", name, error)
            self._deploy_antennas(probe)
            if space_center.active_vessel != carrier:
                space_center.active_vessel = carrier
            self._vessels[name] = probe
            return True

        return self._call(act)

    def rename(self, vessel: str, new_name: str) -> None:
        """Rename a vessel.

        Args:
            vessel: Its current name.
            new_name: Its new name.
        """
        def act(conn: Any) -> None:
            remote = self._find(conn, vessel)
            if remote is None:
                raise GameUnavailableError(f"no vessel named {vessel}")
            remote.name = new_name
            self._vessels.pop(vessel, None)
            self._vessels[new_name] = remote

        self._call(act)

    def switch_to(self, vessel: str) -> None:
        """Make a vessel the active one, from any scene, and wait for it.

        Args:
            vessel: Its name.

        Raises:
            GameUnavailableError: When the switch does not happen in time.
        """
        def start(conn: Any) -> None:
            remote = self._find(conn, vessel)
            if remote is None:
                raise GameUnavailableError(f"no vessel named {vessel}")
            if read_scene(conn) is GameScene.FLIGHT and conn.space_center.active_vessel == remote:
                return
            self._drop_period_stream()
            conn.space_center.active_vessel = remote

        self._call(start)
        end = time.monotonic() + SWITCH_TIMEOUT
        while time.monotonic() < end:
            try:
                ready = self._call(lambda conn: read_scene(conn) is GameScene.FLIGHT
                                   and conn.space_center.active_vessel is not None
                                   and str(conn.space_center.active_vessel.name) == vessel
                                   and not conn.space_center.active_vessel.packed)
            except GameUnavailableError:
                ready = False  # the scene is loading
            if ready:
                time.sleep(SETTLE_AFTER_SWITCH)
                return
            time.sleep(1.0)
        raise GameUnavailableError(f"the game did not switch to {vessel}")

    # ----------------------------------------------------------- control

    def has_control(self) -> bool:
        """Tell whether the active vessel can be commanded.

        Returns:
            False without a pilot or a powered probe core.
        """
        return self._call(lambda conn: _enum_name(
            conn.space_center.active_vessel.control.source) != "none")

    def prepare_engines(self) -> int:
        """Ignite every engine of the active vessel that is still off.

        Returns:
            How many engines are on and have fuel.
        """
        def act(conn: Any) -> int:
            ready = 0
            for engine in conn.space_center.active_vessel.parts.engines:
                if not engine.active:
                    engine.active = True
                if engine.has_fuel:
                    ready += 1
            return ready

        return int(self._call(act))

    def deploy_solar_panels(self) -> int:
        """Deploy the solar panels of the active vessel that are retracted.

        Returns:
            How many panels this call deployed (0 when all were out).
        """
        return int(self._call(
            lambda conn: self._deploy_parts(conn.space_center.active_vessel.parts.solar_panels)))

    def propulsion(self) -> Propulsion:
        """Read what the active vessel can do with its engines.

        Returns:
            Thrust, vacuum specific impulse, mass and vacuum delta-v.
        """
        def read(conn: Any) -> Propulsion:
            vessel = conn.space_center.active_vessel
            vessel.recalculate_delta_v()
            return Propulsion(
                thrust=float(vessel.available_thrust),
                isp=float(vessel.vacuum_specific_impulse),
                mass=float(vessel.mass),
                delta_v=float(vessel.vacuum_delta_v),
            )

        return self._call(read)

    def point(self, direction: Direction) -> None:
        """Hold prograde, retrograde or normal with the autopilot.

        Args:
            direction: Where to point.
        """
        vector = {
            Direction.PROGRADE: (0.0, 1.0, 0.0),
            Direction.RETROGRADE: (0.0, -1.0, 0.0),
            Direction.NORMAL: (0.0, 0.0, 1.0),
        }[direction]
        self._aim(vector)

    def point_between(self, prograde: float, normal: float) -> None:
        """Hold a direction mixing prograde and normal with the autopilot.

        Args:
            prograde: Component along the velocity.
            normal: Component along the orbit normal.
        """
        length = math.hypot(prograde, normal) or 1.0
        self._aim((0.0, prograde / length, normal / length))

    def pointing_error(self) -> float:
        """Angle between the autopilot's target and the nose.

        Returns:
            Degrees.
        """
        return self._call(lambda conn: float(conn.space_center.active_vessel.auto_pilot.error))

    def set_throttle(self, value: float) -> None:
        """Set the main throttle.

        Args:
            value: Between 0 and 1.
        """
        def write(conn: Any) -> None:
            conn.space_center.active_vessel.control.throttle = float(value)

        self._call(write)

    def release_controls(self) -> None:
        """Disengage the autopilot and leave the throttle at zero."""
        def write(conn: Any) -> None:
            vessel = conn.space_center.active_vessel
            vessel.control.throttle = 0.0
            vessel.auto_pilot.engaged = False

        self._call(write)

    def warp_to(self, ut: float) -> None:
        """Time warp until a universal time (blocks).

        Args:
            ut: Universal time to reach.
        """
        self._call(lambda conn: conn.space_center.warp_to(float(ut)))

    # ----------------------------------------------------- alarms, notices

    def set_alarm(self, ut: float, vessel: str, title: str, description: str) -> None:
        """Create a stock alarm linked to a vessel.

        Args:
            ut: Universal time when it goes off.
            vessel: Its vessel.
            title: Its title.
            description: Its text.
        """
        def act(conn: Any) -> None:
            space_center = conn.space_center
            remote = self._find(conn, vessel)
            seconds = max(1.0, float(ut) - float(space_center.ut))
            if remote is None:
                alarm = space_center.AlarmManager.add_alarm(seconds, title, description)
            else:
                alarm = space_center.AlarmManager.add_vessel_alarm(seconds, remote, title,
                                                                   description)
            try:
                alarm.warp_action = space_center.AlarmWarpAction.stop_warp
            except RECOVERABLE_ERRORS as error:
                if is_connection_problem(error):
                    raise

        self._call(act)

    def remove_alarms(self, title_prefix: str) -> int:
        """Delete the stock alarms whose titles start with a text.

        Args:
            title_prefix: Start of the titles.

        Returns:
            How many were deleted.
        """
        def act(conn: Any) -> int:
            removed = 0
            for alarm in conn.space_center.alarm_manager.alarms:
                if str(alarm.title).startswith(title_prefix):
                    alarm.remove()
                    removed += 1
            return removed

        try:
            return int(self._call(act))
        except GameUnavailableError as error:
            logger.debug("Alarms not removed: %s", error)
            return 0

    def show(self, notice: Notice) -> None:
        """Show a message on the game screen; never fails.

        Args:
            notice: The message.
        """
        try:
            self._call(lambda conn: show_notice(conn, notice))
        except GameUnavailableError as error:
            logger.debug("Message not shown: %s", error)

    # ----------------------------------------------------------- plumbing

    def close(self) -> None:
        """Close the kRPC connection, if open."""
        self._drop_period_stream()
        if self._conn is not None:
            try:
                self._conn.close()
            except Exception:  # noqa: BLE001 - closing must never fail
                pass
        self._conn = None
        self._vessels.clear()

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
            message = str(error).splitlines()[0] if str(error) else type(error).__name__
            raise GameUnavailableError(message) from error

    def _find(self, conn: Any, name: str) -> Any:
        """Find a vessel by name, from the cache or by listing them.

        Args:
            conn: Open kRPC client.
            name: Vessel name.

        Returns:
            The remote vessel, or ``None``.
        """
        remote = self._vessels.get(name)
        if remote is not None:
            try:
                if str(remote.name) == name:
                    return remote
            except RECOVERABLE_ERRORS as error:
                if is_connection_problem(error):
                    raise
        self._vessels = {str(v.name): v for v in conn.space_center.vessels}
        return self._vessels.get(name)

    def _aim(self, vector: tuple[float, float, float]) -> None:
        """Point the active vessel along a vector of its orbital frame.

        Args:
            vector: Direction (x anti-radial, y prograde, z normal).
        """
        def write(conn: Any) -> None:
            vessel = conn.space_center.active_vessel
            autopilot = vessel.auto_pilot
            # Frame first, then target: kRPC 0.8 re-interprets the target
            # when the frame changes.
            autopilot.reference_frame = vessel.orbital_reference_frame
            autopilot.target_direction = vector
            autopilot.engaged = True

        self._call(write)

    def _drop_period_stream(self) -> None:
        """Remove the period stream, if any (the active vessel changed)."""
        if self._period is not None:
            try:
                self._period[1].remove()
            except Exception:  # noqa: BLE001 - the stream may be gone already
                pass
        self._period = None

    @staticmethod
    def _deploy_antennas(vessel: Any) -> None:
        """Deploy the antennas of a released probe.

        Only the antennas: the probe needs its link to be commanded. The
        solar panels wait until it has lined up (see
        ``deploy_solar_panels``).

        Args:
            vessel: The remote probe.
        """
        KrpcConstellationGame._deploy_parts(vessel.parts.antennas)

    @staticmethod
    def _deploy_parts(parts: Any) -> int:
        """Deploy every deployable part of a list that is still retracted.

        A part that refuses (broken, blocked by a fairing...) is skipped:
        the probe can still do its job with the others.

        Args:
            parts: kRPC antennas or solar panels.

        Returns:
            How many parts were deployed by this call.
        """
        deployed = 0
        for part in parts:
            try:
                if part.deployable and not part.deployed:
                    part.deployed = True
                    deployed += 1
            except RECOVERABLE_ERRORS as error:
                if is_connection_problem(error):
                    raise
        return deployed

    @staticmethod
    def _mounts(vessel: Any) -> list[tuple[list[Any], tuple[str, Any, int]]]:
        """Find the probes a decoupler or a docking port would set free.

        For every decoupler that has not fired and every docked docking
        port, the piece that would come off is the subtree on the far side
        of the joint from the root part. A piece with probe cores (command
        parts without crew room) is a mount.

        Args:
            vessel: The remote carrier.

        Returns:
            ``(probe cores on the piece, (kind, remote decoupler or port,
            parts on the piece))`` for every mount.
        """
        parts = vessel.parts
        cores = {p for p in parts.with_module("ModuleCommand") if int(p.crew_capacity) == 0}
        if not cores:
            return []
        children: dict[Any, list[Any]] = {p: list(p.children) for p in parts.all}

        def subtree(top: Any) -> set[Any]:
            found, stack = set(), [top]
            while stack:
                part = stack.pop()
                found.add(part)
                stack.extend(children.get(part, []))
            return found

        def far_side(part: Any, other: Any) -> set[Any] | None:
            # The joint between two parts: the child's subtree comes off.
            if other in children.get(part, []):
                return subtree(other)
            if part in children.get(other, []):
                return subtree(part)
            return None

        mounts = []
        seen: set[frozenset[Any]] = set()
        for decoupler in parts.decouplers:
            if decoupler.decoupled:
                continue
            attached = decoupler.attached_part
            piece = far_side(decoupler.part, attached) if attached is not None else None
            if piece:
                mounts.append((piece, ("decoupler", decoupler)))
        for port in parts.docking_ports:
            if _enum_name(port.state) != "docked" or port.docked_part is None:
                continue
            piece = far_side(port.part, port.docked_part)
            if piece:
                mounts.append((piece, ("port", port)))
        result = []
        for piece, (kind, remote) in mounts:
            key = frozenset(piece)
            if key in seen:
                continue  # both ports of a docked pair
            seen.add(key)
            on_piece = [c for c in cores if c in piece]
            if on_piece:
                result.append((on_piece, (kind, remote, len(piece))))
        return result
