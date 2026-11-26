"""Use case: fly a crewed rocket to space (suborbital or orbital) and land it.

A suborbital flight goes through these phases:

1. **Launch**: full throttle, straight up, ignite the first stage.
2. **Ascent**: turn gradually from vertical to the profile's pitch
   between the first and the tenth kilometre, throttle down while the air
   pushes back too hard (dynamic pressure above a limit, or drag above the
   weight), stage when boosters burn out, cut the engines once the
   apoapsis is above the target (relight if drag pulls it clearly below).
3. **Space**: above the atmosphere the vessel is suborbital, which
   completes the tourists' destination. Every stage above the parachutes
   is jettisoned, the capsule turns its heat shield forward and time warp
   skips to the reentry.
4. **Reentry**: hold the heat shield forward and stage the parachutes as
   soon as the game rates one safe to open (or below a minimum altitude).
   KSP keeps them armed until it is safe, which may be much lower.
5. **Descent**: the heat shield is held forward until a parachute really
   opens; then the autopilot lets go and physics warp runs until the
   vessel rests on the ground or the sea.

An orbital flight is only attempted when KSP's delta-v on the launch pad
reaches the profile's orbit budget. Otherwise the vessel flies a suborbital
hop when that reaches a destination of someone on board, or does not take
off at all (``NotEnoughDeltaVError``). It changes the ascent and adds
three steps between the ascent and the space phase:

* **Ascent**: vertical for the first kilometre, then a gravity turn to
  the east driven by the apoapsis: the pitch drops from 90 degrees as the
  apoapsis rises from where it was at the end of the vertical climb, and
  is horizontal when it reaches the orbit's altitude (see
  ``apoapsis_pitch``). The engines are cut then, and relit if the air
  pulls the apoapsis clearly below before leaving the atmosphere. Leaving
  the atmosphere on the way to the apoapsis completes the suborbital
  destinations.
* **Circularization**: a maneuver node at the apoapsis raises the
  periapsis to the same altitude. Before it, KSP's own delta-v figure must
  cover the circularization, the deorbit burn and a reserve; otherwise the
  vessel is not put into orbit at all (it could be stranded there) and the
  flight goes on as a suborbital one. A long burn loses some of its push
  past the apoapsis; if the periapsis is still in the atmosphere after it,
  a prograde top-up with the delta-v beyond the deorbit burn and the
  reserve lifts it out. When no orbit is reached and the periapsis only
  grazes the atmosphere, it is lowered to the deorbit periapsis before the
  engines are jettisoned, so the capsule does come down.
* **Orbit**: the director waits until the game marks the tourists' orbit
  destinations as reached.
* **Deorbit**: a retrograde node at the apoapsis lowers the periapsis
  into the atmosphere. The core stage is only jettisoned afterwards, in
  the space phase, so it can still burn until then.

While the player switches to another vessel (an EVA, say), the director
waits without commanding anything: every control acts on the active
vessel, so it would be steering the kerbal instead of the rocket.

The director polls the game rather than streaming: these flights have
seconds of slack everywhere, and polling keeps the adapter simple.
"""

from __future__ import annotations

import logging
import math
import time
from collections.abc import Callable
from dataclasses import dataclass
from enum import Enum

from ksp_control.application.ports import TourismGamePort
from ksp_control.domain.tourism import (
    FlightProfile,
    FlightTelemetry,
    Mission,
    Propulsion,
    air_limited_throttle,
    apsis_burn_dv,
    apoapsis_pitch,
    ascent_pitch,
    ascent_throttle,
    burn_throttle,
    burn_time,
    reentry_time,
    should_stage_on_ascent,
    time_to_radius,
)

logger = logging.getLogger(__name__)

# Seconds the autopilot is given to turn the vessel before a burn starts;
# time warp stops this long before the burn.
TURN_MARGIN = 30.0
# The burn is aimed well enough below this pointing error, in degrees.
ALIGNED_DEGREES = 5.0
# A node burn ends when this little delta-v is left, in m/s.
NODE_TOLERANCE = 0.5
# How long to wait in orbit for the game to count the tourists' orbit.
ORBIT_OBJECTIVES_TIMEOUT = 120.0
# A periapsis top-up stops this far above the atmosphere, in metres.
PERIAPSIS_MARGIN = 1_000.0
# Without an orbit, a periapsis this far above the deorbit periapsis only
# grazes the atmosphere: the capsule could skim it for many orbits.
GRAZING_MARGIN = 10_000.0
# Landing gear goes down under the canopy once slower than this, in m/s:
# out of the reentry heat and the fast air, long before the ground.
GEAR_SPEED = 100.0


class FlightError(RuntimeError):
    """The flight cannot go on (vessel lost, no control, timeout...)."""


class NotEnoughDeltaVError(FlightError):
    """The vessel cannot reach orbit and a hop would help nobody.

    Raised on the launch pad, before anything is ignited: the vessel is
    still there, whole, and can be recovered for its full cost.
    """


class Phase(Enum):
    """Phases of the main loop, in order.

    The orbital steps (circularization, orbit, deorbit) run as one block
    between the ascent and the space phase.
    """

    ASCENT = "ascent"
    SPACE = "space"
    REENTRY = "reentry"
    DESCENT = "descent"
    LANDED = "landed"


@dataclass(frozen=True)
class FlightReport:
    """How the flight went.

    Attributes:
        vessel: Name of the vessel.
        reached_space: Whether it went above the atmosphere (the tourists'
            suborbital destination).
        max_altitude: Highest altitude reached, in metres.
        situation: Final situation (``landed`` or ``splashed``).
        reached_orbit: Whether it went into orbit (periapsis above the
            atmosphere) and came back down from it.
        delta_v_launch: Vacuum delta-v on the launch pad, in m/s.
        delta_v_orbit: Vacuum delta-v left once in orbit, or when the
            orbit was given up for lack of it, in m/s.
        orbit_delta_v_needed: Set when an orbital flight was flown as a
            suborbital hop because the vessel had less delta-v on the pad
            than this, in m/s.
        ascent_heat: Highest temperature of any part during the ascent, as
            a fraction of what it withstands.
        reentry_heat: The same during the reentry and the descent.
    """

    vessel: str
    reached_space: bool
    max_altitude: float
    situation: str
    reached_orbit: bool = False
    delta_v_launch: float | None = None
    delta_v_orbit: float | None = None
    orbit_delta_v_needed: float | None = None
    ascent_heat: float = 0.0
    reentry_heat: float = 0.0


class TourismFlightDirector:
    """Flies the active vessel from the launch pad back to the ground.

    Responsibilities: run the phase machine described in the module
    docstring, for a suborbital or an orbital mission, issue the controls
    through ``TourismGamePort`` and report the outcome. It assumes the
    staging of a typical tourist rocket: engines and boosters in the upper
    stages, the parachutes in the last stage(s), with the crew capsule and
    its heat shield left after every decoupler has fired.

    Usage notes: ``sleep`` and ``clock`` are injectable so tests run
    instantly. ``fly`` blocks for the whole flight (minutes; an orbital
    flight may take half an hour of real time).
    """

    def __init__(
        self,
        game: TourismGamePort,
        profile: FlightProfile,
        interval: float = 0.2,
        stage_delay: float = 1.0,
        timeout: float = 3600.0,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        """Create the director.

        Args:
            game: Port to the game.
            profile: How to fly.
            interval: Seconds between readings.
            stage_delay: Minimum seconds between two stagings, so the game
                registers the separation before the next decision.
            timeout: Real seconds after which the flight is abandoned.
            sleep: Function used to wait.
            clock: Monotonic clock, in seconds.
        """
        self._game = game
        self._profile = profile
        self._interval = interval
        self._stage_delay = stage_delay
        self._timeout = timeout
        self._sleep = sleep
        self._clock = clock
        self._last_stage_at = float("-inf")
        # Set at the start of each flight.
        self._vessel = ""
        self._deadline = math.inf
        self._max_altitude = 0.0

    def fly(self, mission: Mission = Mission.SUBORBITAL,
            orbit_visited: Callable[[], bool] | None = None,
            hop_helps: bool = True) -> FlightReport:
        """Fly the mission.

        Args:
            mission: Suborbital hop or flight to orbit and back.
            orbit_visited: For an orbital flight, tells whether the game
                already counts the orbit for every tourist on board; the
                vessel stays in orbit until it does. ``None``: no wait.
            hop_helps: For an orbital flight, whether a suborbital hop
                would still reach a destination of someone on board; used
                when the vessel has too little delta-v for an orbit.

        Returns:
            The report, once the vessel rests on the ground or the sea.

        Raises:
            NotEnoughDeltaVError: When an orbit is out of reach and a hop
                would help nobody; the vessel is still on the pad.
            FlightError: When the vessel is not on the pad, cannot be
                controlled, is lost, is stranded in orbit or the flight
                takes too long.
            GameUnavailableError: When the game stops answering.
        """
        profile = self._profile
        orbital = mission is Mission.ORBITAL
        t = self._read()
        if t.situation != "pre_launch":
            raise FlightError(f"{t.vessel} is not on the launch pad ({t.situation})")
        if t.parachute_stage is None:
            raise FlightError(f"{t.vessel} has no parachutes")
        if not self._game.has_control():
            raise FlightError(f"{t.vessel} cannot be controlled: is the pilot in the command pod?")

        self._vessel = t.vessel
        self._deadline = self._clock() + self._timeout
        self._max_altitude = t.altitude
        delta_v_launch = self._game.read_propulsion().delta_v
        orbit_delta_v_needed: float | None = None
        if orbital and delta_v_launch < profile.orbit_delta_v and not hop_helps:
            # Flying would only burn the launch cost: nobody on board has a
            # destination a hop reaches.
            raise NotEnoughDeltaVError(
                f"{t.vessel} has {delta_v_launch:,.0f} m/s of delta-v, not enough for an orbit "
                f"(about {profile.orbit_delta_v:,.0f} m/s needed), and a suborbital hop would "
                "reach no destination of the tourists on board"
            )
        if orbital and delta_v_launch < profile.orbit_delta_v:
            # An orbital ascent spends its fuel sideways: short of an orbit
            # it may not even reach space, and then nobody's destination is
            # reached. Straight up, the same fuel goes much higher.
            logger.warning("%.0f m/s of delta-v is not enough for an orbit (about %.0f m/s "
                           "needed): flying a suborbital hop instead", delta_v_launch,
                           profile.orbit_delta_v)
            orbital = False
            orbit_delta_v_needed = profile.orbit_delta_v
        # The orbital ascent turns all the way to horizontal.
        if orbital:
            target_apoapsis, final_pitch = profile.orbit_apoapsis, 0.0
        else:
            target_apoapsis, final_pitch = profile.target_apoapsis, profile.pitch

        logger.info("Launching %s on %s flight (target apoapsis %.0f m, %.0f m/s of "
                    "delta-v in vacuum)", t.vessel, "an orbital" if orbital else "a suborbital",
                    target_apoapsis, delta_v_launch)
        self._game.set_throttle(1.0)
        self._game.point(90.0, profile.heading)
        if profile.use_rcs:
            # The autopilot only fires RCS thrusters while the RCS is on,
            # and a vessel launched by kRPC starts with it off.
            self._game.set_rcs(True)
        self._do_stage()

        phase = Phase.ASCENT
        throttle = 1.0
        drag_limit = 1.0
        limiting = False
        last_ut = t.ut
        released = False
        gear_down = False
        pitch = 90.0
        reached_space = False
        reached_orbit = False
        delta_v_orbit: float | None = None
        warped = False
        reentry_ut: float | None = None
        # Apoapsis when the vertical climb ended, and the highest since:
        # they drive the orbital gravity turn.
        turn_start_apoapsis: float | None = None
        peak_apoapsis = 0.0
        # Stages dropped during the ascent (the launch itself not counted).
        ascent_stagings = 0
        ascent_heat = 0.0
        reentry_heat = 0.0

        while phase is not Phase.LANDED:
            self._sleep(self._interval)
            t = self._read_own()
            dt, last_ut = t.ut - last_ut, t.ut
            # Peak heat of the two hot moments: climbing fast through the
            # air, and coming back down into it.
            if phase is Phase.ASCENT:
                ascent_heat = max(ascent_heat, t.heat_ratio)
            elif phase in (Phase.REENTRY, Phase.DESCENT):
                reentry_heat = max(reentry_heat, t.heat_ratio)

            if phase is Phase.ASCENT:
                if t.altitude >= t.atmosphere_depth and not reached_space:
                    # Out of the atmosphere: the vessel is suborbital and the
                    # tourists' suborbital destination is reached.
                    reached_space = True
                    logger.info("Above %.0f m: suborbital flight reached (apoapsis %.0f m)",
                                t.atmosphere_depth, t.apoapsis)
                # A suborbital hop is done climbing as soon as it is in
                # space; an orbital ascent burns on until the apoapsis is
                # high enough, unless it is already falling back.
                if reached_space and (not orbital or throttle == 0.0 or t.vertical_speed < 0):
                    self._game.set_throttle(0.0)
                    throttle = 0.0
                    if orbital:
                        reached_orbit, delta_v_orbit = self._orbit(t, orbit_visited)
                        t = self._read_own()
                    reentry_ut = self._reentry_ut(t)
                    phase = Phase.SPACE
                    continue
                if t.vertical_speed < 0 and t.altitude > profile.turn_altitude:
                    # Falling back without reaching space: ran out of fuel.
                    self._game.set_throttle(0.0)
                    logger.warning("Out of thrust at %.0f m without reaching space", t.altitude)
                    self._game.hold_retrograde()
                    phase = Phase.REENTRY
                    continue
                if not orbital:
                    wanted_pitch = ascent_pitch(t.altitude, profile.turn_altitude,
                                                profile.turn_end_altitude, final_pitch)
                elif t.altitude <= profile.turn_altitude or (
                        profile.turn_after_first_stage and ascent_stagings == 0):
                    wanted_pitch = 90.0
                else:
                    if turn_start_apoapsis is None:
                        turn_start_apoapsis = t.apoapsis
                        logger.info("Gravity turn from %.0f m (apoapsis %.0f m)",
                                    t.altitude, t.apoapsis)
                    peak_apoapsis = max(peak_apoapsis, t.apoapsis)
                    wanted_pitch = apoapsis_pitch(peak_apoapsis, turn_start_apoapsis,
                                                  target_apoapsis, profile.orbit_turn_shape)
                # Re-aim only every half degree: one RPC per change.
                if abs(wanted_pitch - pitch) >= 0.5 or (wanted_pitch == final_pitch != pitch):
                    self._game.point(wanted_pitch, profile.heading)
                    pitch = wanted_pitch
                drag_limit = air_limited_throttle(
                    drag_limit, t.dynamic_pressure, profile.max_dynamic_pressure,
                    t.drag, t.weight, profile.max_drag_ratio, dt,
                    floor=profile.min_throttle,
                    heat_ratio=t.heat_ratio, max_heat_ratio=profile.max_heat_ratio,
                )
                if (drag_limit < 0.99) != limiting:
                    limiting = not limiting
                    logger.info("%s at %.0f m, %.0f m/s (q %.1f kPa, drag %.2fx the weight, "
                                "hottest part at %.0f%%)",
                                "Throttling down for the air" if limiting else "Air limit released",
                                t.altitude, t.surface_speed, t.dynamic_pressure / 1000,
                                t.drag / t.weight if t.weight else 0.0, t.heat_ratio * 100)
                cut = ascent_throttle(t.apoapsis, target_apoapsis, cut=throttle == 0.0)
                wanted = min(cut, drag_limit)
                # Small changes are not worth an RPC each reading.
                if abs(wanted - throttle) >= 0.02 or (wanted in (0.0, 1.0) and wanted != throttle):
                    if cut == 0.0 and throttle > 0.0:
                        logger.info("Engines cut at %.0f m: apoapsis %.0f m", t.altitude, t.apoapsis)
                    elif throttle == 0.0 and wanted > 0.0:
                        logger.info("Relighting: drag lowered the apoapsis to %.0f m", t.apoapsis)
                    self._game.set_throttle(wanted)
                    throttle = wanted
                if self._can_stage() and should_stage_on_ascent(
                    t.current_stage, t.parachute_stage, self._game.read_engines()
                ):
                    logger.info("Staging: activating stage %d", t.current_stage - 1)
                    self._do_stage()
                    ascent_stagings += 1

            elif phase is Phase.SPACE:
                if self._jettison(t):
                    continue
                if not warped:
                    self._game.hold_retrograde()
                    warped = True
                    # Stop early enough for the autopilot to turn the heat
                    # shield forward: orientation is frozen during warp.
                    if profile.warp and reentry_ut is not None and reentry_ut - TURN_MARGIN > t.ut:
                        logger.info("Warping to the reentry")
                        self._game.warp_to(reentry_ut - TURN_MARGIN)
                    continue
                if t.altitude < t.atmosphere_depth and t.vertical_speed < 0:
                    logger.info("Reentry: heat shield forward")
                    phase = Phase.REENTRY

            elif phase is Phase.REENTRY:
                if self._jettison(t):
                    continue
                if t.vertical_speed < 0 and (
                    t.altitude < profile.chute_fallback_altitude or self._game.parachutes_safe()
                ):
                    logger.info("Arming parachutes at %.0f m, %.0f m/s", t.altitude, t.surface_speed)
                    while self._current_stage() > (t.parachute_stage or 0):
                        self._do_stage()
                    # The autopilot keeps the heat shield forward until a
                    # canopy is really open (see the descent phase).
                    phase = Phase.DESCENT
                elif t.on_ground:
                    phase = Phase.DESCENT  # came down without needing the chutes

            elif phase is Phase.DESCENT:
                if not released and (t.on_ground or self._game.parachutes_open()):
                    # The canopy now holds the capsule upright; fighting it
                    # with the autopilot would only spin the vessel.
                    logger.info("Parachutes open at %.0f m, %.0f m/s", t.altitude, t.surface_speed)
                    self._game.release_controls()
                    if profile.use_rcs:
                        self._game.set_rcs(False)
                    released = True
                    if profile.warp and profile.physics_warp > 0 and not t.on_ground:
                        self._game.set_physics_warp(profile.physics_warp)
                if released and not gear_down and not t.on_ground and t.surface_speed < GEAR_SPEED:
                    # Legs soften the touchdown on land; lowered earlier
                    # they would face the reentry heat and the fast air.
                    gear_down = True
                    lowered = self._game.deploy_landing_gear()
                    if lowered:
                        logger.info("Landing gear down at %.0f m, %.0f m/s (%d part(s))",
                                    t.altitude, t.surface_speed, lowered)
                if t.on_ground and abs(t.vertical_speed) < 1.0:
                    self._game.set_physics_warp(0)
                    logger.info("%s: %s", t.vessel, t.situation)
                    phase = Phase.LANDED

        # Let the vessel settle before it is recovered.
        self._sleep(3.0)
        final = self._read()
        return FlightReport(final.vessel, reached_space, self._max_altitude, final.situation,
                            reached_orbit=reached_orbit, delta_v_launch=delta_v_launch,
                            delta_v_orbit=delta_v_orbit, orbit_delta_v_needed=orbit_delta_v_needed,
                            ascent_heat=ascent_heat, reentry_heat=reentry_heat)

    # ---------------------------------------------------------------- orbit

    def _orbit(self, t: FlightTelemetry,
               orbit_visited: Callable[[], bool] | None) -> tuple[bool, float | None]:
        """Circularize, wait for the tourists' orbit and deorbit.

        Called above the atmosphere with the engines cut, on the way to
        the apoapsis. When the orbit cannot be reached safely the vessel is
        left on its suborbital path, which brings it down by itself.

        Args:
            t: Telemetry when the ascent ended.
            orbit_visited: Tells whether the game counts the orbit for
                every tourist; ``None`` to skip the wait.

        Returns:
            Whether the vessel went into orbit and back on a reentry path,
            and the delta-v left once in orbit (or found lacking).

        Raises:
            FlightError: When the vessel is stranded in orbit, or the game
                never counts the orbit for the tourists.
        """
        profile = self._profile
        if t.vertical_speed <= 0:
            logger.warning("Past the apoapsis (%.0f m) before the engines were cut: no "
                           "orbit, flying suborbital", t.apoapsis)
            return False, None

        # What the whole orbit will cost, from KSP's delta-v figure, before
        # committing to it: a vessel that circularizes and cannot deorbit is
        # stranded with its tourists.
        apoapsis_radius = t.body_radius + t.apoapsis
        circularize = apsis_burn_dv(t.mu, apoapsis_radius, t.semi_major_axis, apoapsis_radius)
        deorbit = abs(apsis_burn_dv(t.mu, apoapsis_radius, apoapsis_radius,
                                    t.body_radius + profile.deorbit_periapsis))
        propulsion = self._game.read_propulsion()
        needed = circularize + deorbit + profile.delta_v_reserve
        logger.info("Orbit: circularizing takes %.0f m/s, coming back %.0f m/s, reserve %.0f "
                    "m/s; %.0f m/s left", circularize, deorbit, profile.delta_v_reserve,
                    propulsion.delta_v)
        if propulsion.delta_v < needed:
            logger.warning("Not enough delta-v for the orbit (%.0f of %.0f m/s): flying "
                           "suborbital; the craft needs more fuel", propulsion.delta_v, needed)
            return False, propulsion.delta_v

        self._burn("Circularization", t.ut + t.time_to_apoapsis, circularize, propulsion)
        t = self._read_own()
        if not t.in_orbit:
            # A long burn centred on the apoapsis loses some of its push
            # past it. Top it up with what is left beyond the deorbit burn
            # and the reserve, stopping as soon as the periapsis is out.
            left = self._game.read_propulsion()
            budget = left.delta_v - deorbit - profile.delta_v_reserve
            if budget > 0:
                logger.info("Periapsis still at %.0f m: burning prograde to lift it above the "
                            "atmosphere (up to %.0f m/s)", t.periapsis, budget)
                self._burn("Periapsis top-up", t.ut + TURN_MARGIN, budget, left,
                           stop=lambda r: r.periapsis > r.atmosphere_depth + PERIAPSIS_MARGIN)
                t = self._read_own()
        if not t.in_orbit:
            logger.warning("Circularization fell short (periapsis %.0f m): no orbit",
                           t.periapsis)
            self._lower_grazing_periapsis(t)
            return False, self._game.read_propulsion().delta_v
        left = self._game.read_propulsion()
        logger.info("In orbit: periapsis %.0f m, apoapsis %.0f m; %.0f m/s left",
                    t.periapsis, t.apoapsis, left.delta_v)
        self._wait_for_orbit_objectives(orbit_visited)

        # Deorbit at the apoapsis: lowering the periapsis from there is the
        # cheapest, and leaves time to turn the vessel around.
        t = self._read_own()
        radius = t.body_radius + t.apoapsis
        deorbit_dv = apsis_burn_dv(t.mu, radius, t.semi_major_axis,
                                   t.body_radius + profile.deorbit_periapsis)
        burn_at = t.ut + t.time_to_apoapsis
        if t.time_to_apoapsis < 2 * TURN_MARGIN:
            burn_at += t.period
        self._burn("Deorbit", burn_at, deorbit_dv, left)
        t = self._read_own()
        if t.in_orbit:
            self._game.release_controls()
            raise FlightError(
                f"{t.vessel} is stranded in orbit (periapsis {t.periapsis:,.0f} m): the deorbit "
                "burn ran out of fuel; bring it down by hand"
            )
        logger.info("Deorbited: periapsis %.0f m", t.periapsis)
        return True, left.delta_v

    def _lower_grazing_periapsis(self, t: FlightTelemetry) -> None:
        """Bring a periapsis that only grazes the atmosphere down to deorbit.

        Without an orbit the stages above the parachutes are jettisoned
        next. With a periapsis just under the top of the atmosphere the
        capsule would skim it and climb out again, orbit after orbit,
        without an engine. While the engines are still there, a retrograde
        burn lowers the periapsis to the deorbit periapsis.

        Args:
            t: Telemetry after the failed circularization.
        """
        profile = self._profile
        if t.periapsis <= profile.deorbit_periapsis + GRAZING_MARGIN:
            return
        if not any(e.has_fuel for e in self._game.read_engines()):
            logger.warning("Periapsis at %.0f m only grazes the atmosphere and no engine has "
                           "fuel left: the capsule may take several passes to come down",
                           t.periapsis)
            return
        logger.warning("Periapsis at %.0f m only grazes the atmosphere: lowering it to %.0f m "
                       "before jettisoning the engines", t.periapsis, profile.deorbit_periapsis)
        # At the apoapsis when it comes soon, the cheapest point; otherwise
        # now. The burn stops on the periapsis anyway, so the delta-v given
        # to the node is only an upper bound.
        if 2 * TURN_MARGIN < t.time_to_apoapsis < 600.0:
            burn_at = t.ut + t.time_to_apoapsis
        else:
            burn_at = t.ut + TURN_MARGIN
        radius = t.body_radius + t.apoapsis
        estimate = apsis_burn_dv(t.mu, radius, t.semi_major_axis,
                                 t.body_radius + profile.deorbit_periapsis)
        propulsion = self._game.read_propulsion()
        delta_v = -min(propulsion.delta_v, max(2 * abs(estimate), 50.0))
        self._burn("Deorbit", burn_at, delta_v, propulsion,
                   stop=lambda r: r.periapsis <= profile.deorbit_periapsis)

    def _burn(self, label: str, ut: float, delta_v: float, propulsion: Propulsion,
              stop: Callable[[FlightTelemetry], bool] | None = None) -> None:
        """Execute a maneuver node: aim, wait (warping), burn, clean up.

        The burn is centred on the node, so it starts half its duration
        before. It ends when the node's remaining delta-v is (nearly) zero,
        when it starts growing again (the burn went past the node), or when
        the engines run dry. Stages whose engines burn out are dropped as
        during the ascent, but never the parachute stage.

        Args:
            label: Name of the burn, for the log.
            ut: Universal time of the node.
            delta_v: Prograde delta-v of the node, in m/s (negative:
                retrograde).
            propulsion: Engines at hand, to estimate the duration.
            stop: Ends the burn early when it returns True for the latest
                telemetry (the node's delta-v is then only an upper bound).
        """
        duration = burn_time(delta_v, propulsion.thrust, propulsion.isp, propulsion.mass)
        if not math.isfinite(duration):
            duration = 0.0  # no thrust now; staging may bring some
        start = ut - duration / 2.0
        self._game.add_node(ut, delta_v)
        self._game.aim_at_node()
        t = self._read_own()
        logger.info("%s: %.0f m/s %s, about %.0f s of burn, starting in %.0f s", label,
                    abs(delta_v), "prograde" if delta_v >= 0 else "retrograde", duration,
                    start - t.ut)
        self._wait_aligned()

        t = self._read_own()
        if self._profile.warp and start - TURN_MARGIN > t.ut:
            # Orientation is frozen during warp: aim first (done above),
            # and again afterwards in case the vessel drifted.
            self._game.warp_to(start - TURN_MARGIN)
            self._game.aim_at_node()
        while t.ut < start:
            self._sleep(self._interval)
            t = self._read_own()

        throttle = 0.0
        best = math.inf
        while True:
            t = self._read_own()
            if stop is not None and stop(t):
                break
            remaining = self._game.node_remaining_dv()
            if remaining is None or remaining < NODE_TOLERANCE or remaining > best + 1.0:
                break
            best = min(best, remaining)
            wanted = burn_throttle(remaining, t.thrust, t.mass)
            if abs(wanted - throttle) >= 0.02:
                self._game.set_throttle(wanted)
                throttle = wanted
            engines = self._game.read_engines()
            if self._can_stage() and should_stage_on_ascent(
                t.current_stage, t.parachute_stage, engines
            ):
                logger.info("Staging: activating stage %d", t.current_stage - 1)
                self._do_stage()
                continue
            if not any(e.active and e.has_fuel for e in engines):
                logger.warning("%s: out of fuel with %.0f m/s still to burn", label, remaining)
                break
            self._sleep(self._interval)
        self._game.set_throttle(0.0)
        self._game.remove_nodes()

    def _wait_aligned(self, timeout: float = 60.0) -> None:
        """Wait until the autopilot points where it was asked.

        Args:
            timeout: Seconds after which the burn goes ahead anyway.
        """
        end = self._clock() + timeout
        error = math.inf
        while self._clock() < end:
            self._sleep(self._interval)
            self._read_own()
            error = self._game.pointing_error()
            if error < ALIGNED_DEGREES:
                return
        logger.warning("Still %.0f degrees off target after %.0f s: burning anyway", error, timeout)

    def _wait_for_orbit_objectives(self, orbit_visited: Callable[[], bool] | None) -> None:
        """Stay in orbit until the game counts it for every tourist.

        Deorbiting before would leave the tourists' orbit destination
        unfinished: the contract would fail once they are recovered.

        Args:
            orbit_visited: The check; ``None`` to skip the wait.

        Raises:
            FlightError: When the game does not count the orbit in time;
                the vessel is then left in orbit, which is safe.
        """
        if orbit_visited is None:
            return
        end = self._clock() + ORBIT_OBJECTIVES_TIMEOUT
        while not orbit_visited():
            if self._clock() > end:
                self._game.release_controls()
                raise FlightError(
                    f"in orbit for {ORBIT_OBJECTIVES_TIMEOUT:.0f} s, but the game does not count "
                    "the orbit for every tourist; the vessel is left in orbit for you"
                )
            self._sleep(2.0)
            self._read_own()
        logger.info("The game counts the orbit for every tourist")

    def _reentry_ut(self, t: FlightTelemetry) -> float | None:
        """Estimate when the vessel falls back into the atmosphere.

        Args:
            t: Telemetry above the atmosphere.

        Returns:
            Universal time of the reentry, or ``None`` when unknown.
        """
        seconds = time_to_radius(t.mu, t.semi_major_axis, t.eccentricity, t.radius,
                                 t.vertical_speed > 0, t.body_radius + t.atmosphere_depth)
        if seconds is not None:
            return t.ut + seconds
        if t.vertical_speed > 0:
            # Without orbital elements: a Kepler arc is symmetric about the
            # apoapsis (see ``reentry_time``).
            return reentry_time(t.ut, t.time_to_apoapsis)
        return None

    # ------------------------------------------------------------- helpers

    def _read(self) -> FlightTelemetry:
        """Read the active vessel, failing when it is gone.

        Returns:
            The telemetry.

        Raises:
            FlightError: When there is no active vessel any more.
        """
        telemetry = self._game.read_flight()
        if telemetry is None:
            raise FlightError("the vessel is gone (destroyed, or the game left the flight)")
        return telemetry

    def _read_own(self) -> FlightTelemetry:
        """Read the flown vessel, waiting while another one is active.

        Controls only act on the active vessel: while the player is on
        another one (an EVA...) nothing may be commanded, so this waits
        until the player switches back. It also enforces the flight
        timeout and tracks the highest altitude.

        Returns:
            The telemetry of the flown vessel.

        Raises:
            FlightError: When the flight takes too long or the vessel is
                gone.
        """
        away = False
        while True:
            if self._clock() > self._deadline:
                self._game.release_controls()
                raise FlightError(f"flight not finished after {self._timeout:.0f} s")
            t = self._read()
            if t.vessel == self._vessel:
                if away:
                    logger.info("Back on %s", self._vessel)
                self._max_altitude = max(self._max_altitude, t.altitude)
                return t
            if not away:
                logger.warning("%s is active instead of %s: waiting, no commands until you "
                               "switch back", t.vessel, self._vessel)
                away = True
            self._sleep(self._interval)

    def _current_stage(self) -> int:
        """Read the current stage number.

        Returns:
            ``Control.current_stage`` of the active vessel.
        """
        return self._read().current_stage

    def _can_stage(self) -> bool:
        """Tell whether enough time passed since the last staging.

        Returns:
            True when staging again is allowed.
        """
        return self._clock() - self._last_stage_at >= self._stage_delay

    def _do_stage(self) -> None:
        """Activate the next stage and remember when."""
        self._game.stage()
        self._last_stage_at = self._clock()
        # Give the game time to separate the parts before reading again.
        self._sleep(self._stage_delay)

    def _jettison(self, t: FlightTelemetry) -> bool:
        """Drop one stage above the parachutes, if any is left.

        Done in space, where the pieces separate cleanly, so only the
        capsule, its heat shield and the parachutes reenter together.

        Args:
            t: Latest telemetry.

        Returns:
            True when a stage was activated (read again before going on).
        """
        if t.parachute_stage is not None and t.current_stage - 1 > t.parachute_stage:
            logger.info("Jettisoning stage %d", t.current_stage - 1)
            self._do_stage()
            return True
        return False
