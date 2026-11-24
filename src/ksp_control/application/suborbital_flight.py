"""Use case: fly a crewed rocket on a suborbital hop and land it.

The flight goes through these phases:

1. **Launch**: full throttle, straight up, ignite the first stage.
2. **Ascent**: turn gradually from vertical to the profile's pitch
   between the first and the tenth kilometre,
   throttle down while the air pushes back too hard (dynamic pressure
   above a limit, or drag above the weight), stage when boosters burn
   out, cut
   the engines once the apoapsis is above the target (relight if drag
   pulls it down).
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

While the player switches to another vessel (an EVA, say), the director
waits without commanding anything: every control acts on the active
vessel, so it would be steering the kerbal instead of the rocket.

The director polls the game rather than streaming: a suborbital hop has
seconds of slack everywhere, and polling keeps the adapter simple.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from dataclasses import dataclass
from enum import Enum

from ksp_control.application.ports import TourismGamePort
from ksp_control.domain.tourism import (
    FlightProfile,
    FlightTelemetry,
    ascent_throttle,
    air_limited_throttle,
    ascent_pitch,
    reentry_time,
    should_stage_on_ascent,
)

logger = logging.getLogger(__name__)


class FlightError(RuntimeError):
    """The flight cannot go on (vessel lost, no control, timeout...)."""


class Phase(Enum):
    """Phases of the hop, in order."""

    ASCENT = "ascent"
    SPACE = "space"
    REENTRY = "reentry"
    DESCENT = "descent"
    LANDED = "landed"


@dataclass(frozen=True)
class FlightReport:
    """How the hop went.

    Attributes:
        vessel: Name of the vessel.
        reached_space: Whether it went above the atmosphere (the tourists'
            suborbital destination).
        max_altitude: Highest altitude reached, in metres.
        situation: Final situation (``landed`` or ``splashed``).
    """

    vessel: str
    reached_space: bool
    max_altitude: float
    situation: str


class SuborbitalFlightDirector:
    """Flies the active vessel from the launch pad back to the ground.

    Responsibilities: run the phase machine described in the module
    docstring, issue the controls through ``TourismGamePort`` and report
    the outcome. It assumes the staging of a typical tourist rocket:
    engines and boosters in the upper stages, the parachutes in the last
    stage(s), with the crew capsule and its heat shield left after every
    decoupler has fired.

    Usage notes: ``sleep`` and ``clock`` are injectable so tests run
    instantly. ``fly`` blocks for the whole flight (minutes).
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

    def fly(self) -> FlightReport:
        """Fly the hop.

        Returns:
            The report, once the vessel rests on the ground or the sea.

        Raises:
            FlightError: When the vessel is not on the pad, cannot be
                controlled, is lost or the flight takes too long.
            GameUnavailableError: When the game stops answering.
        """
        profile = self._profile
        t = self._read()
        if t.situation != "pre_launch":
            raise FlightError(f"{t.vessel} is not on the launch pad ({t.situation})")
        if t.parachute_stage is None:
            raise FlightError(f"{t.vessel} has no parachutes")
        if not self._game.has_control():
            raise FlightError(f"{t.vessel} cannot be controlled: is the pilot in the command pod?")

        logger.info("Launching %s (target apoapsis %.0f m)", t.vessel, profile.target_apoapsis)
        self._game.set_throttle(1.0)
        self._game.point(90.0, profile.heading)
        self._do_stage()

        deadline = self._clock() + self._timeout
        phase = Phase.ASCENT
        throttle = 1.0
        drag_limit = 1.0
        limiting = False
        last_ut = t.ut
        released = False
        pitch = 90.0
        reached_space = False
        warped = False
        reentry_ut: float | None = None
        max_altitude = t.altitude
        vessel = t.vessel
        away = False

        while phase is not Phase.LANDED:
            if self._clock() > deadline:
                self._game.release_controls()
                raise FlightError(f"flight not finished after {self._timeout:.0f} s")
            self._sleep(self._interval)
            t = self._read()
            dt, last_ut = t.ut - last_ut, t.ut
            if t.vessel != vessel:
                # Controls only act on the active vessel: wait until the
                # player is back on the rocket.
                if not away:
                    logger.warning("%s is active instead of %s: waiting, no commands "
                                   "until you switch back", t.vessel, vessel)
                    away = True
                continue
            if away:
                logger.info("Back on %s", vessel)
                away = False
            max_altitude = max(max_altitude, t.altitude)

            if phase is Phase.ASCENT:
                if t.altitude >= t.atmosphere_depth:
                    # Out of the atmosphere: the vessel is suborbital and the
                    # tourists' destination is reached.
                    self._game.set_throttle(0.0)
                    reached_space = True
                    reentry_ut = reentry_time(t.ut, t.time_to_apoapsis)
                    logger.info("Above %.0f m: suborbital flight reached (apoapsis %.0f m)",
                                t.atmosphere_depth, t.apoapsis)
                    phase = Phase.SPACE
                    continue
                if t.vertical_speed < 0 and t.altitude > profile.turn_altitude:
                    # Falling back without reaching space: ran out of fuel.
                    self._game.set_throttle(0.0)
                    logger.warning("Out of thrust at %.0f m without reaching space", t.altitude)
                    self._game.hold_retrograde()
                    phase = Phase.REENTRY
                    continue
                wanted_pitch = ascent_pitch(t.altitude, profile.turn_altitude,
                                            profile.turn_end_altitude, profile.pitch)
                # Re-aim only every half degree: one RPC per change.
                if abs(wanted_pitch - pitch) >= 0.5 or (wanted_pitch == profile.pitch != pitch):
                    self._game.point(wanted_pitch, profile.heading)
                    pitch = wanted_pitch
                drag_limit = air_limited_throttle(
                    drag_limit, t.dynamic_pressure, profile.max_dynamic_pressure,
                    t.drag, t.weight, profile.max_drag_ratio, dt,
                    floor=profile.min_throttle,
                )
                if (drag_limit < 0.99) != limiting:
                    limiting = not limiting
                    logger.info("%s at %.0f m, %.0f m/s (q %.1f kPa, drag %.2fx the weight)",
                                "Throttling down for the air" if limiting else "Air limit released",
                                t.altitude, t.surface_speed, t.dynamic_pressure / 1000,
                                t.drag / t.weight if t.weight else 0.0)
                cut = ascent_throttle(t.apoapsis, profile.target_apoapsis)
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

            elif phase is Phase.SPACE:
                if self._jettison(t):
                    continue
                if not warped:
                    self._game.hold_retrograde()
                    warped = True
                    # Stop early enough for the autopilot to turn the heat
                    # shield forward: orientation is frozen during warp.
                    if profile.warp and reentry_ut is not None and reentry_ut - 30.0 > t.ut:
                        logger.info("Warping to the reentry")
                        self._game.warp_to(reentry_ut - 30.0)
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
                    released = True
                    if profile.warp and profile.physics_warp > 0 and not t.on_ground:
                        self._game.set_physics_warp(profile.physics_warp)
                if t.on_ground and abs(t.vertical_speed) < 1.0:
                    self._game.set_physics_warp(0)
                    logger.info("%s: %s", t.vessel, t.situation)
                    phase = Phase.LANDED

        # Let the vessel settle before it is recovered.
        self._sleep(3.0)
        final = self._read()
        return FlightReport(final.vessel, reached_space, max_altitude, final.situation)

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
