"""Use case: spread relay probes evenly around a body from one carrier.

The player designs the carrier, loads it with the probes and flies it into
orbit around the body. From there this use case:

1. **Insertion**: puts the carrier on the resonant orbit of the plan
   (periapsis at the constellation altitude, period ``(N + 1) / N`` times
   the final one) with at most two burns at the apsides, then trims its
   period to a fraction of a second.
2. **Release**: near the apoapsis (the furthest point from the first
   braking), pointing along the orbit normal, it sets every probe free and
   names it ``CA-<km>-<body>-<nn> <person>``, with its antennas deployed.
   The carrier is renamed so a later run can find it.
3. **Drift**: each probe trims its period to a little *less* than the
   resonant one, more for the higher numbers: the group stretches forward
   into a line with the carrier at the back. This also corrects whatever
   push the decouplers gave them. Once lined up, each probe deploys its
   solar panels (the player's rule: not while it is still next to the
   carrier and the other probes).
4. **Carrier disposal**: the carrier brakes until its periapsis is under
   the surface (or deep in the atmosphere). A vessel that brakes first
   falls *back* and then drops below; the decouplers may have pushed a
   probe behind it, so it brakes 30 degrees off retrograde, towards the
   side of the orbital plane with fewer probes, and leaves the plane of
   the group while it falls back.
5. **Circularization**, once per periapsis passage: the rearmost probe of
   the group brakes into the circular orbit, so nobody is behind it to
   catch up with it (braking makes a vessel fall back). Its period is
   trimmed to the final one and it is renamed with its position on the
   ring. Right after the first one, the probes still waiting stop
   drifting: each one gets a period within a few seconds of the resonant
   one, chosen so that it reaches the periapsis exactly when its slot
   comes round (the decouplers and the drift leave it slightly early or
   late, which would put it a fraction of a degree off its slot).

Before each braking the use case predicts where the probe would land and
checks that the slot is free: a periapsis passage that was missed (the
player was busy elsewhere) leaves a slot empty, the following passages fill
the next slots, and the last probe waits the laps it takes for the empty
slot to come round.

Waiting: when the next maneuver is less than ``alarm_threshold`` away and
the player is on one of the constellation's vessels, time warp gets there.
Otherwise a game alarm is set a few minutes before, and the use case waits
(or stops, with ``wait=False``, to be run again later). When the moment
comes:

* on a vessel of the constellation: it goes ahead;
* in the space center or the tracking station: it takes command of the
  vessel it needs;
* on another vessel (or in a building or an editor): it warns in red,
  on screen and in the log, and does nothing. If the maneuver is missed,
  the plan adapts as explained above.

Every decision is taken again from the orbits in the game on every step,
so a run can be stopped at any time and another one (another day, after
restarting the game) goes on from where things are.
"""

from __future__ import annotations

import logging
import math
import time
from collections.abc import Callable
from dataclasses import dataclass, field

from ksp_control.application.ports import (
    ConstellationGamePort,
    Direction,
    GameUnavailableError,
    Notice,
    NoticeLevel,
    NoticePlacement,
    VesselOrbit,
)
from ksp_control.domain.constellation import (
    ApsisBurn,
    ConstellationError,
    ConstellationPlan,
    Vector,
    angle_in_plane,
    apsis_burn_delta_v,
    carrier_name,
    distance,
    format_duration,
    landing_slot_free,
    max_los_gap,
    opposite_apsis_radius,
    parse_probe_name,
    period_sensitivity,
    plan_transfer,
    probe_name,
    probe_name_prefix,
    rearmost,
    ring_gaps,
    ring_position,
    signed_angle,
    slot_error,
)
from ksp_control.domain.model import GameScene
from ksp_control.domain.tourism import burn_throttle, burn_time

logger = logging.getLogger(__name__)

# Game seconds the autopilot gets to turn before a timed burn; time warp
# stops this long before the burn starts.
TURN_MARGIN = 30.0
# Game seconds before a maneuver at which the vessel is taken over, to
# prepare it (switch, ignite, turn) and still start the burn on time.
PREPARE_MARGIN = 150.0
# A burn starts once the nose is within this angle of the target, degrees.
ALIGNED_DEGREES = 3.0
# Real seconds the autopilot gets to turn before burning anyway.
ALIGN_TIMEOUT = 90.0
# A probe is on the final orbit when its semi-major axis is closer to the
# final radius than this share of the way to the resonant one.
CIRCULAR_SHARE = 0.5
# Lowest throttle of a fine period trim (thrust is still proportional).
FINE_THROTTLE = 0.002
# Real seconds without progress, throttle open, after which a burn is
# considered out of fuel.
STALL_SECONDS = 4.0
# Real seconds the probes get to float away from the carrier after the
# release before it brakes anyway.
CLEARANCE_TIMEOUT = 180.0
# A period is trimmed again only when it is off by more than this many
# tolerances, and a trim that ends further off than that has failed. The
# game's physics make the period of a loaded vessel jitter a little, so
# demanding the tolerance itself could start the same trim over and over.
RETRIM_FACTOR = 2.0
# The carrier brakes out of the orbit this many degrees off retrograde,
# towards the side of the orbital plane with fewer probes.
DISPOSAL_TILT_DEGREES = 30
# Probes closer than this to the carrier decide which side it tilts to, m.
NEARBY_DISTANCE = 5_000.0


class DeploymentError(RuntimeError):
    """The deployment cannot go on without the player (no carrier, no fuel...)."""


class DeploymentPaused(Exception):
    """The next maneuver is far away: an alarm is set and the run stops.

    Raised only when the options ask not to wait. Running again after the
    alarm goes off resumes the deployment.

    Attributes:
        alarm_ut: Universal time of the alarm.
        label: What the maneuver is.
    """

    def __init__(self, alarm_ut: float, label: str) -> None:
        """Create the exception.

        Args:
            alarm_ut: Universal time of the alarm.
            label: What the maneuver is.
        """
        super().__init__(f"alarm set for {label}")
        self.alarm_ut = alarm_ut
        self.label = label


@dataclass(frozen=True)
class DeploymentOptions:
    """How to deploy.

    Attributes:
        body_label: Name of the body in the probe names (``Muna``).
        spacing: Metres each probe pulls ahead of the next one per lap
            while waiting on the resonant orbit (0: no drift).
        period_tolerance: Allowed error of the final periods, in seconds.
            Probes whose periods differ drift apart forever, so it is
            small.
        resonant_tolerance: Allowed error of the periods on the resonant
            orbit, in seconds.
        alarm_threshold: Game seconds of waiting beyond which an alarm is
            set instead of warping.
        alarm_lead: Game seconds between the alarm and the maneuver.
        wait: Keep running until the alarm goes off (else stop).
        warp: Use time warp at all.
        clearance: Metres every probe must be from the carrier before the
            carrier fires its engine.
        poll: Real seconds between checks while waiting.
        warning_interval: Real seconds between two red warnings.
    """

    body_label: str
    spacing: float = 200.0
    period_tolerance: float = 0.05
    resonant_tolerance: float = 0.1
    alarm_threshold: float = 3600.0
    alarm_lead: float = 300.0
    wait: bool = True
    warp: bool = True
    clearance: float = 100.0
    poll: float = 5.0
    warning_interval: float = 30.0


@dataclass(frozen=True)
class ProbeStatus:
    """One probe of the finished (or growing) ring.

    Attributes:
        name: Vessel name.
        position: Position on the ring.
        periapsis: Periapsis altitude, in metres.
        apoapsis: Apoapsis altitude, in metres.
        period_error: Period minus the planned one, in seconds.
        gap_ahead: Angle to the next probe ahead, in degrees.
        slot_error: Distance from its ideal slot, in degrees (positive:
            ahead).
    """

    name: str
    position: int
    periapsis: float
    apoapsis: float
    period_error: float
    gap_ahead: float
    slot_error: float


@dataclass(frozen=True)
class DeploymentReport:
    """State of the ring when the deployment ends.

    Attributes:
        probes: The probes on the final orbit, in ring order.
        warnings: Problems found (gaps out of sight, short antennas...).
        lasting_days: Kerbin days (6 h) before the period errors open a
            gap beyond line of sight; ``math.inf`` when they never do.
    """

    probes: list[ProbeStatus]
    warnings: list[str] = field(default_factory=list)
    lasting_days: float = math.inf


@dataclass
class _Survey:
    """What the game shows at the start of a step.

    Attributes:
        ut: Universal time of the reading.
        probes: Orbits of the constellation's probes around the body.
        numbers: Position number in each probe's name.
        circular: Probes on the final orbit.
        waiting: Probes still on the resonant orbit.
        carrier: Orbit of the renamed carrier, while it is still up.
        mounted: Probes still attached to the active vessel.
        active: The active vessel (``None`` outside flight).
    """

    ut: float
    probes: dict[str, VesselOrbit]
    numbers: dict[str, int]
    circular: list[str]
    waiting: list[str]
    carrier: VesselOrbit | None
    mounted: int
    active: str | None


class ConstellationDeployer:
    """Deploys a relay constellation following a plan.

    Responsibilities: survey the constellation in the game, pick the next
    step (see the module docstring), wait for it (time warp, alarms,
    warnings, taking command), fly it, and report the finished ring.

    Usage notes: ``run`` blocks until the ring is complete, or raises
    ``DeploymentPaused`` (alarm set, ``wait=False``) or ``DeploymentError``.
    It can be called again later, from a new instance: everything is read
    from the game. ``sleep`` and ``clock`` are injectable so tests can run
    against a simulated game.
    """

    def __init__(
        self,
        game: ConstellationGamePort,
        plan: ConstellationPlan,
        options: DeploymentOptions,
        person_name: Callable[[], str],
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
        interval: float = 0.2,
        burn_interval: float = 0.05,
    ) -> None:
        """Create the deployer.

        Args:
            game: Port to the game.
            plan: Orbits of the constellation.
            options: How to deploy.
            person_name: Gives a new invented person's name for each probe.
            sleep: Waits a number of real seconds.
            clock: Monotonic real clock, in seconds.
            interval: Real seconds between readings during coarse burns.
            burn_interval: Real seconds between readings during period
                trims.
        """
        self._game = game
        self._plan = plan
        self._options = options
        self._person_name = person_name
        self._sleep = sleep
        self._clock = clock
        self._interval = interval
        self._burn_interval = burn_interval
        self._prefix = probe_name_prefix(plan.altitude, options.body_label)
        self._carrier = carrier_name(plan.altitude, options.body_label)
        self._last_warning = -math.inf

    # ------------------------------------------------------------ public

    @property
    def prefix(self) -> str:
        """Start of the names of the probes (``CA-40-Muna-``)."""
        return self._prefix

    def run(self) -> DeploymentReport:
        """Deploy until every probe is in its slot.

        Returns:
            The report of the finished ring.

        Raises:
            DeploymentPaused: When an alarm was set and the options say not
                to wait for it.
            DeploymentError: When the deployment needs the player.
            GameUnavailableError: When the game stops answering during a
                maneuver.
        """
        logger.info("Constellation plan:\n%s", self._plan.describe())
        while True:
            survey = self._survey()
            if self._step(survey):
                break
        report = self.report()
        logger.info("Ring complete: %d probes", len(report.probes))
        return report

    def status(self) -> list[str]:
        """Describe the constellation and the next step, commanding nothing.

        Returns:
            Lines of text for the player.
        """
        s = self._survey()
        lines = [f"Probes named {self._prefix}..: {len(s.probes)} "
                 f"({len(s.circular)} on the final orbit, {len(s.waiting)} waiting)"]
        for name in sorted(s.probes, key=lambda n: s.numbers[n]):
            o = s.probes[name]
            lines.append(f"  {name}: periapsis {o.periapsis / 1000:,.1f} km, apoapsis "
                         f"{o.apoapsis / 1000:,.1f} km, period {format_duration(o.period)}")
        if s.carrier is not None:
            lines.append(f"Carrier {self._carrier}: periapsis {s.carrier.periapsis / 1000:,.1f} km")
        if s.mounted:
            lines.append(f"Active vessel {s.active} carries {s.mounted} probe(s)")
        lines.append(f"Next step: {self._describe_next(s)}")
        return lines

    def report(self) -> DeploymentReport:
        """Measure the ring: positions, gaps, periods, line of sight, range.

        Returns:
            The report (only the probes on the final orbit).
        """
        s = self._survey()
        plan = self._plan
        names = [n for n in s.circular if s.probes[n].elements is not None]
        if not names:
            return DeploymentReport([], ["no probe on the final orbit"])
        now = s.ut
        positions = {n: s.probes[n].elements.position(now) for n in names}
        normal = s.probes[names[0]].elements.normal
        reference = positions[names[0]]
        angles = {n: angle_in_plane(p, normal, reference) for n, p in positions.items()}
        first = min(names, key=lambda n: s.numbers[n])
        gaps = {name: gap for name, _, gap in ring_gaps(angles)}
        statuses = []
        warnings: list[str] = []
        for name, ahead, gap in ring_gaps(angles):
            o = s.probes[name]
            statuses.append(ProbeStatus(
                name=name,
                position=s.numbers[name],
                periapsis=o.periapsis,
                apoapsis=o.apoapsis,
                period_error=o.period - plan.period,
                gap_ahead=math.degrees(gap),
                slot_error=math.degrees(slot_error(angles[name], angles[first], plan.count)),
            ))
            # Line of sight, cautiously from the lower of the two periapses.
            low = plan.body.radius + min(o.periapsis, s.probes[ahead].periapsis)
            limit = max_los_gap(plan.body.radius, low, plan.occlusion)
            if len(names) > 1 and gap > limit:
                warnings.append(f"{name} and {ahead} are {math.degrees(gap):.1f} deg apart: out "
                                f"of sight (at most {math.degrees(limit):.1f} deg)")
            # Antenna range against their distance at the higher apoapsis.
            power = self._game.comms_power(name) * self._game.comms_power(ahead)
            far = plan.body.radius + max(o.apoapsis, s.probes[ahead].apoapsis)
            needed = 2.0 * far * math.sin(gap / 2.0)
            if len(names) > 1 and power > 0 and math.sqrt(power) < needed:
                warnings.append(f"{name} and {ahead} reach {math.sqrt(power) / 1000:,.0f} km but "
                                f"are up to {needed / 1000:,.0f} km apart: bigger relay antennas "
                                "are needed")
            if abs(o.period - plan.period) > plan.period * 1e-4:
                warnings.append(f"{name} has a period {o.period - plan.period:+.2f} s off")
        lasting = self._lasting_days(s, names, gaps)
        if len(names) < plan.count:
            warnings.append(f"only {len(names)} of {plan.count} probes on the final orbit")
        return DeploymentReport(sorted(statuses, key=lambda p: p.position), warnings, lasting)

    # ----------------------------------------------------------- survey

    def _survey(self) -> _Survey:
        """Read the constellation from the game.

        Returns:
            The survey.
        """
        plan = self._plan
        ut = self._game.ut()
        probes: dict[str, VesselOrbit] = {}
        numbers: dict[str, int] = {}
        carrier: VesselOrbit | None = None
        for name in self._game.vessel_names():
            parsed = parse_probe_name(name, self._prefix)
            if parsed is None and name != self._carrier:
                continue
            orbit = self._game.orbit(name)
            if orbit is None or orbit.body != plan.body.name or orbit.situation in (
                    "landed", "splashed"):
                continue
            if parsed is None:
                if orbit.periapsis > plan.body.disposal_periapsis + 1_000.0 and orbit.closed:
                    carrier = orbit
                continue
            probes[name] = orbit
            numbers[name] = parsed[0]
        # Halfway between the final and the resonant semi-major axes.
        threshold = CIRCULAR_SHARE * (plan.resonant_semi_major_axis - plan.radius)
        circular = [n for n, o in probes.items()
                    if o.closed and abs(o.semi_major_axis - plan.radius) < threshold]
        waiting = [n for n in probes if n not in circular]
        active = self._game.active_vessel() if self._game.scene() is GameScene.FLIGHT else None
        mounted = 0
        if active is not None and active not in probes:
            # Counting the probes walks the parts: only for a vessel that
            # may be the carrier.
            active_orbit = self._game.orbit(active)
            if active_orbit is not None and active_orbit.body == plan.body.name:
                mounted = self._game.mounted_probes()
        return _Survey(ut, probes, numbers, circular, waiting, carrier, mounted, active)

    def _describe_next(self, s: _Survey) -> str:
        """Say what the next step would be.

        Args:
            s: Current survey.

        Returns:
            A sentence.
        """
        if not s.probes and not s.mounted:
            return (f"nothing to do: fly the carrier loaded with probes into orbit around "
                    f"{self._plan.body.name} and make it the active vessel")
        if s.mounted:
            return (f"put {s.active} on the resonant orbit and release {s.mounted} probe(s)"
                    if not s.probes else f"release the {s.mounted} probe(s) left on {s.active}")
        if self._needs_drift(s):
            return "trim the released probes so that they line up ahead of the carrier"
        if s.carrier is not None:
            return f"bring {self._carrier} down"
        if self._needs_settle(s):
            return "stop the waiting probes drifting"
        if self._needs_finish(s):
            return "trim the period of a probe on the final orbit"
        if s.waiting:
            probe, pass_ut = self._next_brake(s)
            return (f"brake {probe} into the final orbit in "
                    f"{format_duration(pass_ut - s.ut)} (game time)")
        return "nothing: the ring is complete"

    # ------------------------------------------------------------- steps

    def _step(self, s: _Survey) -> bool:
        """Take the next step of the deployment.

        Args:
            s: Current survey.

        Returns:
            True when the ring is complete.

        Raises:
            DeploymentError: When nothing can be done without the player.
        """
        if not s.probes and not s.mounted:
            raise DeploymentError(
                f"no probes named {self._prefix}.. around {self._plan.body.name} and the active "
                f"vessel carries none: fly the loaded carrier into orbit around "
                f"{self._plan.body.name} and run again with it as the active vessel")
        if s.mounted:
            if not s.probes:
                self._insert(s.active)
            self._release(s)
            return False
        if self._needs_drift(s):
            self._drift(s)
            return False
        if s.carrier is not None:
            self._dispose(s.carrier)
            return False
        if self._needs_settle(s):
            self._settle(s)
            return False
        if self._needs_finish(s):
            self._finish(s)
            return False
        if s.waiting:
            probe, pass_ut = self._next_brake(s)
            self._brake(probe, pass_ut)
            return False
        return True

    def _drift_targets(self, s: _Survey) -> dict[str, float]:
        """Period each waiting probe must have before the first braking.

        Args:
            s: Current survey.

        Returns:
            Target period of each waiting probe.
        """
        ordered = sorted(s.waiting, key=lambda n: s.numbers[n])
        return {name: self._plan.drift_period(rank, self._options.spacing)
                for rank, name in enumerate(ordered, start=1)}

    def _needs_drift(self, s: _Survey) -> bool:
        """Tell whether released probes still have to line up.

        Args:
            s: Current survey.

        Returns:
            True before the first braking, while a waiting probe is not on
            its drift period.
        """
        if s.circular:
            return False
        targets = self._drift_targets(s)
        limit = RETRIM_FACTOR * self._options.resonant_tolerance
        return any(abs(s.probes[n].period - targets[n]) > limit for n in s.waiting)

    def _needs_settle(self, s: _Survey) -> bool:
        """Tell whether waiting probes still drift after the first braking.

        Args:
            s: Current survey.

        Returns:
            True when a probe is on the ring and a waiting one is not on
            its settle period.
        """
        limit = RETRIM_FACTOR * self._options.resonant_tolerance
        targets = self._settle_targets(s)
        return any(abs(s.probes[n].period - targets[n]) > limit for n in targets)

    def _settle_targets(self, s: _Survey) -> dict[str, float]:
        """Period each waiting probe keeps until it brakes, once the ring exists.

        Nearly the resonant period: the decouplers' push and the drift
        leave each probe a few seconds early or late, which would put it a
        fraction of a degree off its slot. Probe ``k`` of the braking order
        (1 brakes at the next periapsis, 2 at the one after...) spreads the
        correction over the ``k`` laps it still waits: arriving ``dt`` later
        at the periapsis shifts its landing back by ``n dt`` (``n`` being
        the angular speed of the final orbit). The rule gives the same
        targets on every lap, so a later run trims nothing that is fine.

        Args:
            s: Current survey.

        Returns:
            Target period of each waiting probe (empty before the first
            braking).
        """
        plan = self._plan
        if not s.circular or not s.waiting:
            return {}
        waiting = {n: s.probes[n] for n in s.waiting}
        circular = {n: s.probes[n] for n in s.circular}
        reference = min(s.circular, key=lambda n: s.numbers[n])
        first = waiting[min(waiting, key=lambda n: s.numbers[n])].elements
        assert first is not None
        # Braking order: rearmost first, as seen at the next passage.
        upcoming = first.next_periapsis(s.ut)
        angles = self._angles(waiting, upcoming)
        base = next(iter(angles.values()))
        order = sorted(waiting, key=lambda n: signed_angle(angles[n] - base))
        mean_motion = 2.0 * math.pi / plan.period
        targets = {}
        for laps, name in enumerate(order, start=1):
            orbit = waiting[name].elements
            assert orbit is not None
            # The passage where it brakes, flying its current period until
            # then; the error there already counts that period, so a probe
            # on its target has no error left and keeps its period.
            pass_ut = (orbit.last_periapsis(upcoming + orbit.period / 2.0)
                       + (laps - 1) * orbit.period)
            at_pass = self._angles({reference: circular[reference], name: waiting[name]},
                                   pass_ut)
            error = slot_error(at_pass[name], at_pass[reference], plan.count)
            targets[name] = orbit.period + error / (mean_motion * laps)
        return targets

    def _needs_finish(self, s: _Survey) -> bool:
        """Tell whether a probe on the final orbit needs a trim or a new name.

        Args:
            s: Current survey.

        Returns:
            True when one is off its period or misnamed.
        """
        return bool(self._unfinished(s))

    def _unfinished(self, s: _Survey) -> dict[str, int]:
        """Probes on the final orbit that need work, with their right number.

        Args:
            s: Current survey.

        Returns:
            Probe name -> the position number it should carry.
        """
        expected = self._expected_positions(s)
        limit = RETRIM_FACTOR * self._options.period_tolerance
        return {
            name: expected[name] for name in s.circular
            if expected[name] != s.numbers[name]
            or abs(s.probes[name].period - self._plan.period) > limit
        }

    def _expected_positions(self, s: _Survey) -> dict[str, int]:
        """Position number each probe on the final orbit should carry.

        The first probe to brake is number 1. Once there are several, the
        one with the lowest number keeps it and the others are counted
        from it, one per slot behind.

        Args:
            s: Current survey.

        Returns:
            Probe name -> position.
        """
        if not s.circular:
            return {}
        if len(s.circular) == 1:
            return {s.circular[0]: 1}
        reference = min(s.circular, key=lambda n: s.numbers[n])
        angles = self._angles({n: s.probes[n] for n in s.circular}, s.ut)
        return {n: ring_position(angles[n], angles[reference], s.numbers[reference],
                                 self._plan.count)
                for n in s.circular}

    # --------------------------------------------------------- insertion

    def _insert(self, carrier: str) -> None:
        """Put the carrier on the resonant orbit.

        Args:
            carrier: Name of the active vessel carrying the probes.

        Raises:
            DeploymentError: When the carrier is not on a usable orbit.
        """
        plan, body = self._plan, self._plan.body
        target_periapsis = plan.radius
        target_apoapsis = body.radius + plan.resonant_apoapsis
        tolerance = max(200.0, 0.003 * plan.radius)
        for _ in range(6):
            o = self._require_orbit(carrier)
            apoapsis = body.radius + o.apoapsis if o.closed else math.inf
            try:
                burns = plan_transfer(body.mu, body.radius + o.periapsis, apoapsis,
                                      target_periapsis, target_apoapsis,
                                      min_radius=body.radius + body.min_safe_altitude,
                                      max_radius=0.95 * body.sphere_of_influence,
                                      tolerance=tolerance)
            except ConstellationError as error:
                raise DeploymentError(f"{carrier}: {error}") from error
            if not burns:
                break
            total = sum(abs(b.delta_v) for b in burns)
            propulsion = self._game.propulsion()
            logger.info("Transfer to the resonant orbit: %d burn(s), %.0f m/s (KSP counts %.0f m/s "
                        "left)", len(burns), total, propulsion.delta_v)
            if propulsion.delta_v < total:
                logger.warning("KSP's delta-v readout (%.0f m/s) is below the %.0f m/s of the "
                               "transfer; it may not count every tank", propulsion.delta_v, total)
            self._apsis_burn(carrier, burns[0])
        else:
            raise DeploymentError(f"{carrier} did not reach the resonant orbit after 6 burns")
        error = self._trim_period(carrier, plan.resonant_period, self._options.resonant_tolerance,
                                  "Resonant period")
        self._check_trim(carrier, error, self._options.resonant_tolerance)
        o = self._require_orbit(carrier)
        logger.info("%s on the resonant orbit: periapsis %.1f km, apoapsis %.1f km, period off "
                    "by %+.2f s", carrier, o.periapsis / 1000, o.apoapsis / 1000, error)

    def _apsis_burn(self, vessel: str, burn: ApsisBurn) -> None:
        """Burn along the velocity at an apsis until the other one is in place.

        Args:
            vessel: The active vessel.
            burn: Where to burn and the wanted distance of the other apsis.
        """
        body = self._plan.body
        o = self._require_orbit(vessel)
        to_apsis = o.time_to_periapsis if burn.at_periapsis else o.time_to_apoapsis
        apsis_ut = o.ut + to_apsis
        propulsion = self._game.propulsion()
        duration = burn_time(burn.delta_v, propulsion.thrust, propulsion.isp, propulsion.mass)
        if not math.isfinite(duration):
            raise DeploymentError(f"{vessel} has no engine with fuel for the transfer")
        direction = Direction.PROGRADE if burn.delta_v > 0 else Direction.RETROGRADE
        logger.info("Burn %s at the %s: %.0f m/s, about %.0f s, other apsis to %.1f km",
                    direction.value, "periapsis" if burn.at_periapsis else "apoapsis",
                    abs(burn.delta_v), duration, (burn.target_radius - body.radius) / 1000)
        self._wait_for_burn(direction, apsis_ut - duration / 2.0)
        raising = burn.delta_v > 0
        throttle = 0.0
        stall = _StallWatch(self._clock)
        while True:
            o = self._require_orbit(vessel)
            opposite = (opposite_apsis_radius(o.semi_major_axis, o.radius)
                        if o.semi_major_axis > 0 else math.inf)
            if (opposite >= burn.target_radius) if raising else (opposite <= burn.target_radius):
                break
            remaining = abs(apsis_burn_delta_v(body.mu, o.radius, o.semi_major_axis,
                                               burn.target_radius))
            wanted = burn_throttle(remaining, propulsion.thrust, propulsion.mass)
            if abs(wanted - throttle) >= 0.02:
                self._game.set_throttle(wanted)
                throttle = wanted
            if stall.stalled(abs(opposite - burn.target_radius)):
                logger.warning("%s: no more thrust, %.0f m/s short", vessel, remaining)
                break
            self._sleep(self._interval)
        self._game.set_throttle(0.0)

    # ----------------------------------------------------------- release

    def _release(self, s: _Survey) -> None:
        """Release every probe from the carrier, near the apoapsis.

        Args:
            s: Current survey (the active vessel is the carrier).
        """
        carrier = s.active
        assert carrier is not None
        o = self._require_orbit(carrier)
        # Near the apoapsis the first periapsis is half a lap away: time
        # for every probe to trim and for the carrier to leave.
        if o.closed and 0.1 * o.period < o.time_to_apoapsis < 0.9 * o.period:
            target = o.ut + o.time_to_apoapsis - TURN_MARGIN
            if self._options.warp:
                logger.info("Warping to the apoapsis to release the probes")
                self._game.warp_to(target)
            else:
                self._wait_until(target)
        if carrier != self._carrier:
            logger.info("Renaming the carrier %s to %s", carrier, self._carrier)
            self._game.rename(carrier, self._carrier)
            carrier = self._carrier
        # Along the normal, decouplers push the probes sideways and along
        # the orbit, never straight into the line of flight.
        self._game.point(Direction.NORMAL)
        self._wait_aligned()
        used_numbers = set(s.numbers.values())
        used_people = {parse_probe_name(n, self._prefix)[1] for n in s.probes}
        released = []
        number = 1
        while True:
            while number in used_numbers:
                number += 1
            person = self._new_person(used_people)
            name = probe_name(self._prefix, number, person)
            if not self._game.release_probe(name):
                break
            logger.info("Released %s", name)
            released.append(name)
            used_numbers.add(number)
            used_people.add(person)
            self._sleep(1.0)
        self._game.release_controls()
        self._wait_for_clearance(carrier, released)

    def _new_person(self, used: set[str]) -> str:
        """Invent a person's name not used by another probe.

        Args:
            used: Names already taken.

        Returns:
            A new name.
        """
        for _ in range(100):
            person = self._person_name().strip()
            if person and person not in used:
                return person
        return f"Probe {len(used) + 1}"

    def _wait_for_clearance(self, carrier: str, probes: list[str]) -> None:
        """Wait until every released probe floated away from the carrier.

        Args:
            carrier: Name of the carrier.
            probes: Names of the released probes.
        """
        end = self._clock() + CLEARANCE_TIMEOUT
        while True:
            ut = self._game.ut()
            here = self._position(carrier, ut)
            gaps = [distance(here, self._position(p, ut)) for p in probes]
            closest = min(gaps) if gaps else math.inf
            if closest >= self._options.clearance:
                logger.info("Every probe is at least %.0f m from the carrier", closest)
                return
            if self._clock() > end:
                logger.warning("A probe is still only %.0f m from the carrier: going on", closest)
                return
            self._sleep(2.0)

    # -------------------------------------------- drift, disposal, settle

    def _drift(self, s: _Survey) -> None:
        """Trim every waiting probe to its drift period.

        Args:
            s: Current survey.
        """
        for name, target in self._drift_targets(s).items():
            orbit = self._require_orbit(name)
            if abs(orbit.period - target) <= self._options.resonant_tolerance:
                continue
            if not self._acquire(name, f"line up {name}", self._game.ut()):
                return
            self._prepare(name)
            error = self._trim_period(name, target, self._options.resonant_tolerance,
                                      "Drift period")
            self._check_trim(name, error, self._options.resonant_tolerance)
            logger.info("%s on its drift period (%+.2f s)", name, error)
            # Lined up: only now, away from the carrier and the others,
            # do the solar panels come out.
            self._deploy_solar_panels(name)

    def _settle(self, s: _Survey) -> None:
        """Put every waiting probe on its settle period (see ``_settle_targets``).

        Args:
            s: Current survey.
        """
        for name, target in self._settle_targets(s).items():
            orbit = self._require_orbit(name)
            if abs(orbit.period - target) <= self._options.resonant_tolerance:
                continue
            if not self._acquire(name, f"stop {name} drifting", self._game.ut()):
                return
            self._prepare(name)
            self._deploy_solar_panels(name)
            error = self._trim_period(name, target, self._options.resonant_tolerance,
                                      "Settle period")
            self._check_trim(name, error, self._options.resonant_tolerance)
            logger.info("%s waits on a period %+.2f s from the resonant one, to reach its slot "
                        "(%+.2f s off)", name, target - self._plan.resonant_period, error)

    def _dispose(self, carrier: VesselOrbit) -> None:
        """Bring the empty carrier down, braking away from the probes.

        Args:
            carrier: Its orbit.
        """
        body = self._plan.body
        name = carrier.name
        if not self._acquire(name, f"bring {name} down", self._game.ut()):
            return
        target = body.radius + body.disposal_periapsis
        side = self._escape_side(name)
        logger.info("Bringing %s down: periapsis to %.1f km, braking %d degrees towards the %s",
                    name, body.disposal_periapsis / 1000, DISPOSAL_TILT_DEGREES,
                    "normal" if side > 0 else "anti-normal")
        self._game.prepare_engines()
        propulsion = self._game.propulsion()
        # Braking makes the carrier fall back first, possibly towards a
        # probe the decouplers pushed behind it; the sideways component
        # moves it out of the plane of the group while it does.
        tilt = math.radians(DISPOSAL_TILT_DEGREES)
        self._game.point_between(-math.cos(tilt), side * math.sin(tilt))
        self._wait_aligned()
        throttle = 0.0
        stall = _StallWatch(self._clock)
        while True:
            o = self._require_orbit(name)
            periapsis_radius = body.radius + o.periapsis
            if periapsis_radius <= target or o.situation in ("landed", "splashed"):
                break
            remaining = abs(apsis_burn_delta_v(body.mu, o.radius, o.semi_major_axis, target))
            wanted = burn_throttle(remaining, propulsion.thrust, propulsion.mass)
            if abs(wanted - throttle) >= 0.02:
                self._game.set_throttle(wanted)
                throttle = wanted
            if stall.stalled(periapsis_radius):
                logger.warning("%s ran out of fuel with its periapsis at %.1f km: it stays up, "
                               "bring it down by hand", name, o.periapsis / 1000)
                self._game.set_throttle(0.0)
                self._game.release_controls()
                # Renamed so later runs stop trying.
                self._game.rename(name, f"{name} (empty)")
                return
            self._sleep(self._interval)
        self._game.set_throttle(0.0)
        self._game.release_controls()
        logger.info("%s is coming down", name)

    def _escape_side(self, carrier: str) -> float:
        """Side of the orbital plane with fewer probes near the carrier.

        Args:
            carrier: Name of the carrier.

        Returns:
            +1 to tilt the disposal burn towards the orbit normal, -1 towards
            the anti-normal.
        """
        probes = [n for n in self._game.vessel_names()
                  if parse_probe_name(n, self._prefix) is not None]
        offsets = self._game.relative_positions(carrier, probes)
        # Each nearby probe counts by its side, not by how far it is: one
        # probe far out of the plane must not outweigh several close in.
        balance = sum(math.copysign(1.0, normal) for along, normal in offsets.values()
                      if math.hypot(along, normal) < NEARBY_DISTANCE and normal != 0)
        return -1.0 if balance > 0 else 1.0

    # ----------------------------------------------------------- braking

    def _next_brake(self, s: _Survey) -> tuple[str, float]:
        """Choose the probe and the periapsis passage of the next braking.

        The probe is the rearmost of the waiting group at the passage; the
        passage is the next one (or the one just past, if the brake can
        still start) where it would land in a free slot.

        Args:
            s: Current survey.

        Returns:
            The probe and the universal time of its periapsis passage.
        """
        plan = self._plan
        waiting = {n: s.probes[n] for n in s.waiting}
        circular = {n: s.probes[n] for n in s.circular}
        first = waiting[min(waiting, key=lambda n: s.numbers[n])].elements
        assert first is not None
        # The passage just past still counts while a late brake lands close
        # enough to its slot.
        candidate = first.last_periapsis(s.ut)
        if s.ut - candidate > plan.max_brake_delay:
            candidate = first.next_periapsis(s.ut)
        for _ in range(2 * plan.count + 2):
            probe = rearmost(self._angles(waiting, candidate))
            orbit = waiting[probe].elements
            assert orbit is not None
            # That probe's own passage nearest to the candidate.
            pass_ut = orbit.last_periapsis(candidate + orbit.period / 2.0)
            angles = self._angles({**circular, probe: waiting[probe]}, pass_ut)
            occupied = [angles[n] for n in circular]
            if pass_ut >= s.ut - plan.max_brake_delay and landing_slot_free(
                    angles[probe], occupied, plan.count):
                return probe, pass_ut
            if pass_ut > s.ut + TURN_MARGIN:
                logger.info("Periapsis in %s would put %s in a taken slot: waiting a lap",
                            format_duration(pass_ut - s.ut), probe)
            candidate = pass_ut + orbit.period
        raise DeploymentError("no free slot found for the waiting probes")

    def _brake(self, probe: str, pass_ut: float) -> None:
        """Brake a probe into the final orbit at its periapsis passage.

        Args:
            probe: Name of the probe.
            pass_ut: Universal time of its periapsis passage.
        """
        plan = self._plan
        label = f"brake {probe} into the final orbit"
        logger.info("Next: %s, periapsis in %s", label, format_duration(pass_ut - self._game.ut()))
        if not self._acquire(probe, label, pass_ut - PREPARE_MARGIN,
                             due_ut=pass_ut + plan.max_brake_delay):
            logger.warning("Missed the periapsis of %s: the next passage fills the next slot",
                           probe)
            return
        self._prepare(probe)
        self._deploy_solar_panels(probe)
        propulsion = self._game.propulsion()
        duration = burn_time(plan.injection_delta_v, propulsion.thrust, propulsion.isp,
                             propulsion.mass)
        logger.info("Braking %s: %.1f m/s, about %.1f s, centred on the periapsis", probe,
                    plan.injection_delta_v, duration)
        self._wait_for_burn(Direction.RETROGRADE, pass_ut - duration / 2.0)
        error = self._trim_period(probe, plan.period, self._options.period_tolerance, "Braking")
        logger.info("%s on the final orbit, period off by %+.3f s", probe, error)
        self._game.release_controls()
        self._rename_on_ring(probe)

    def _finish(self, s: _Survey) -> None:
        """Trim and rename the probes on the final orbit that need it.

        Args:
            s: Current survey.
        """
        for name in self._unfinished(s):
            if (abs(s.probes[name].period - self._plan.period)
                    > RETRIM_FACTOR * self._options.period_tolerance):
                if not self._acquire(name, f"trim the period of {name}", self._game.ut()):
                    return
                self._prepare(name)
                self._deploy_solar_panels(name)
                error = self._trim_period(name, self._plan.period, self._options.period_tolerance,
                                          "Final period")
                self._check_trim(name, error, self._options.period_tolerance)
                logger.info("%s period off by %+.3f s", name, error)
                self._game.release_controls()
            self._rename_on_ring(name)

    def _rename_on_ring(self, probe: str) -> None:
        """Give a probe on the final orbit the number of its ring position.

        Args:
            probe: Its current name.
        """
        s = self._survey()
        if probe not in s.circular:
            return
        expected = self._expected_positions(s).get(probe)
        if expected is None or expected == s.numbers[probe]:
            return
        old_number = s.numbers[probe]
        person = parse_probe_name(probe, self._prefix)[1]
        # A waiting probe that still carries this number from the release
        # gets the old number of this one, so no two probes share one.
        for other, number in s.numbers.items():
            if other != probe and number == expected:
                if other in s.circular:
                    logger.warning("%s should be number %d, but %s already is", probe, expected,
                                   other)
                    return
                other_person = parse_probe_name(other, self._prefix)[1]
                swapped = probe_name(self._prefix, old_number, other_person)
                logger.info("%s gives its number to %s: renamed %s", other, probe, swapped)
                self._game.rename(other, swapped)
        new_name = probe_name(self._prefix, expected, person)
        logger.info("%s takes position %d: renamed %s", probe, expected, new_name)
        self._game.rename(probe, new_name)

    # ------------------------------------------------------ burn helpers

    def _check_trim(self, vessel: str, error: float, tolerance: float) -> None:
        """Stop the deployment when a period trim could not be completed.

        Args:
            vessel: The vessel that was trimmed.
            error: Its final period error, in seconds.
            tolerance: The tolerance it was trimmed to, in seconds.

        Raises:
            DeploymentError: When the error is beyond what a later survey
                accepts (which would start the same trim forever).
        """
        if not abs(error) <= RETRIM_FACTOR * tolerance:
            raise DeploymentError(
                f"{vessel} could not trim its period (still {error:+.2f} s off): out of fuel, "
                "or its engine is too strong for the tolerance")

    def _prepare(self, vessel: str) -> None:
        """Check that the active probe can fly and ignite its engines.

        Args:
            vessel: Name of the active vessel.

        Raises:
            DeploymentError: When it cannot be controlled or has no engine.
        """
        if not self._game.has_control():
            raise DeploymentError(f"{vessel} cannot be controlled: no power for its probe core?")
        if self._game.prepare_engines() == 0:
            raise DeploymentError(f"{vessel} has no engine with fuel")

    def _deploy_solar_panels(self, vessel: str) -> None:
        """Deploy the solar panels of the active probe, once it has lined up.

        The drift step calls it right after each trim. The later steps call
        it too whenever they take command of a probe, so a probe whose drift
        needed no trim, or a run stopped in between, still gets its panels
        out; on a probe whose panels are already out it does nothing.

        Args:
            vessel: Name of the active probe.
        """
        deployed = self._game.deploy_solar_panels()
        if deployed:
            logger.info("%s: %d solar panel(s) deployed", vessel, deployed)

    def _wait_for_burn(self, direction: Direction, start_ut: float) -> None:
        """Turn the vessel and wait (warping) until a burn starts.

        Args:
            direction: Where the burn points.
            start_ut: Universal time to start burning.
        """
        self._game.point(direction)
        self._wait_aligned()
        if self._options.warp and start_ut - TURN_MARGIN > self._game.ut():
            self._game.warp_to(start_ut - TURN_MARGIN)
            # Orientation is frozen during warp: aim again.
            self._game.point(direction)
            self._wait_aligned()
        self._wait_until(start_ut)

    def _wait_until(self, ut: float) -> None:
        """Let game time run (without warp) until a universal time.

        Args:
            ut: Universal time to reach.
        """
        while self._game.ut() < ut:
            self._sleep(self._interval)

    def _wait_aligned(self) -> None:
        """Wait until the autopilot points where it was asked."""
        end = self._clock() + ALIGN_TIMEOUT
        error = math.inf
        while self._clock() < end:
            error = self._game.pointing_error()
            if error < ALIGNED_DEGREES:
                return
            self._sleep(self._interval)
        logger.warning("Still %.0f degrees off target: burning anyway", error)

    def _trim_period(self, vessel: str, target: float, tolerance: float, label: str) -> float:
        """Burn along the orbit until the period of the active vessel is right.

        The throttle eases as the period nears the target, down to a tiny
        fraction, so that the last reading before stopping lands within
        the tolerance even with a strong engine. An overshoot is corrected
        by turning around.

        Args:
            vessel: Name of the active vessel.
            target: Wanted period, in seconds.
            tolerance: Allowed error, in seconds.
            label: What the burn is, for the log.

        Returns:
            The final period error, in seconds.
        """
        error = self._game.period() - target
        for _ in range(4):
            if abs(error) <= tolerance:
                break
            sign = 1.0 if error > 0 else -1.0
            direction = Direction.RETROGRADE if sign > 0 else Direction.PROGRADE
            self._game.point(direction)
            self._wait_aligned()
            propulsion = self._game.propulsion()
            if propulsion.thrust <= 0:
                logger.warning("%s: %s needs thrust and has none", vessel, label)
                break
            orbit = self._require_orbit(vessel)
            sensitivity = period_sensitivity(self._plan.body.mu, max(orbit.semi_major_axis, 1.0),
                                             orbit.speed)
            refreshed = self._clock()
            throttle = 0.0
            stall = _StallWatch(self._clock)
            while True:
                error = self._game.period() - target
                if sign * error <= 0.3 * tolerance:
                    break
                if self._clock() - refreshed > 2.0:
                    orbit = self._require_orbit(vessel)
                    if orbit.semi_major_axis > 0:
                        sensitivity = period_sensitivity(self._plan.body.mu,
                                                         orbit.semi_major_axis, orbit.speed)
                    refreshed = self._clock()
                remaining = (sign * error / sensitivity) if math.isfinite(error) else math.inf
                wanted = burn_throttle(remaining, propulsion.thrust, propulsion.mass,
                                       settle_time=2.0, floor=FINE_THROTTLE)
                if throttle == 0.0 or abs(wanted - throttle) > 0.1 * throttle:
                    self._game.set_throttle(wanted)
                    throttle = wanted
                if stall.stalled(sign * error if math.isfinite(error) else math.inf):
                    logger.warning("%s: %s ran out of thrust %.2f s from the target", vessel,
                                   label, error)
                    self._game.set_throttle(0.0)
                    return error
                self._sleep(self._burn_interval)
            self._game.set_throttle(0.0)
            self._sleep(self._burn_interval)
            error = self._game.period() - target
        return error

    # ------------------------------------------------------------ waiting

    def _acquire(self, vessel: str, label: str, act_ut: float,
                 due_ut: float | None = None) -> bool:
        """Wait until a vessel can be commanded for a maneuver, and switch to it.

        Args:
            vessel: Vessel the maneuver is for.
            label: What the maneuver is, for messages.
            act_ut: Universal time to take the vessel over.
            due_ut: Universal time after which the maneuver is missed
                (``None``: never).

        Returns:
            True with the vessel active; False when the maneuver was missed.

        Raises:
            DeploymentPaused: When an alarm was set and the options say not
                to wait.
        """
        options = self._options
        alarm_title = f"{self._prefix.rstrip('-')}: "
        alarm_ut = act_ut - options.alarm_lead
        alarm_set = False
        offline = False
        while True:
            try:
                now = self._game.ut()
                scene = self._game.scene()
                active = self._game.active_vessel() if scene is GameScene.FLIGHT else None
            except GameUnavailableError as error:
                if not offline:
                    logger.warning("The game does not answer (%s): waiting for it", error)
                    offline = True
                self._sleep(options.poll)
                continue
            if offline:
                logger.info("The game answers again")
                offline = False
            if due_ut is not None and now > due_ut:
                self._game.remove_alarms(alarm_title)
                return False
            ours = active is not None and self._is_ours(active)
            if now >= act_ut:
                if ours:
                    self._game.remove_alarms(alarm_title)
                    if active != vessel:
                        self._game.switch_to(vessel)
                    return True
                if scene in (GameScene.SPACE_CENTER, GameScene.TRACKING_STATION):
                    logger.info("Taking command of %s from the %s to %s", vessel,
                                scene.value.replace("_", " "), label)
                    self._game.switch_to(vessel)
                    continue
                self._warn(f"Time to {label}: switch to {vessel}"
                           + (f" ({format_duration(due_ut - now)} left)" if due_ut else ""))
                self._sleep(options.poll)
                continue
            wait = act_ut - now
            if ours and options.warp and (wait <= options.alarm_threshold or now >= alarm_ut):
                logger.info("Warping %s to %s", format_duration(wait), label)
                self._game.warp_to(act_ut)
                continue
            if not alarm_set:
                ring_ut = max(alarm_ut, now + 5.0)
                self._game.remove_alarms(alarm_title)
                self._game.set_alarm(ring_ut, vessel, f"{alarm_title}{label}",
                                     f"In {format_duration(act_ut - ring_ut)} the relay "
                                     f"constellation script will {label}.")
                alarm_set = True
                logger.info("Alarm set in %s (game time) to %s", format_duration(ring_ut - now),
                            label)
                if not options.wait:
                    raise DeploymentPaused(ring_ut, label)
            if now >= alarm_ut:
                if scene in (GameScene.SPACE_CENTER, GameScene.TRACKING_STATION):
                    logger.info("Taking command of %s from the %s to %s", vessel,
                                scene.value.replace("_", " "), label)
                    self._game.switch_to(vessel)
                    continue
                if not ours:
                    self._warn(f"In {format_duration(wait)}: {label}. Switch to {vessel}")
            self._sleep(options.poll)

    def _warn(self, text: str) -> None:
        """Warn the player in red, on screen and in the log, now and then.

        Args:
            text: The warning.
        """
        if self._clock() - self._last_warning < self._options.warning_interval:
            return
        self._last_warning = self._clock()
        logger.warning(text)
        self._game.show(Notice(text, NoticeLevel.ERROR, NoticePlacement.BANNER, 10.0))

    def _is_ours(self, vessel: str) -> bool:
        """Tell whether a vessel belongs to the constellation.

        Args:
            vessel: Its name.

        Returns:
            True for its probes and its carrier.
        """
        return vessel == self._carrier or parse_probe_name(vessel, self._prefix) is not None

    # ------------------------------------------------------------ helpers

    def _require_orbit(self, vessel: str) -> VesselOrbit:
        """Read the orbit of a vessel that must exist.

        Args:
            vessel: Its name.

        Returns:
            The orbit.

        Raises:
            DeploymentError: When the vessel is gone.
        """
        orbit = self._game.orbit(vessel)
        if orbit is None:
            raise DeploymentError(f"{vessel} is gone")
        return orbit

    def _position(self, vessel: str, ut: float) -> Vector:
        """Predicted position of a vessel.

        Args:
            vessel: Its name.
            ut: Universal time.

        Returns:
            The position in the body's inertial frame.
        """
        orbit = self._require_orbit(vessel)
        if orbit.elements is None:
            return (math.inf, math.inf, math.inf)
        return orbit.elements.position(ut)

    def _angles(self, orbits: dict[str, VesselOrbit], ut: float) -> dict[str, float]:
        """Angles of vessels around the orbit at a time, in a common frame.

        Args:
            orbits: Their orbits (all closed).
            ut: Universal time.

        Returns:
            Angle of each one, increasing in the direction of motion.
        """
        positions = {n: o.elements.position(ut) for n, o in orbits.items() if o.elements}
        first = next(iter(orbits.values())).elements
        assert first is not None
        normal = first.normal
        reference = first.position(ut)
        return {n: angle_in_plane(p, normal, reference) for n, p in positions.items()}

    def _lasting_days(self, s: _Survey, names: list[str], gaps: dict[str, float]) -> float:
        """Estimate how long the ring stays in sight with its period errors.

        Args:
            s: Current survey.
            names: Probes on the final orbit.
            gaps: Gap ahead of each probe, in radians.

        Returns:
            Kerbin days (6 h) before the widest gap goes out of sight.
        """
        if len(names) < 2:
            return math.inf
        periods = [s.probes[n].period for n in names]
        spread = max(periods) - min(periods)
        if spread <= 0:
            return math.inf
        # Two probes whose periods differ by dT drift apart by
        # 2 pi dT / T^2 radians per second.
        rate = 2.0 * math.pi * spread / self._plan.period ** 2
        margin = self._plan.max_gap - max(gaps.values())
        if margin <= 0:
            return 0.0
        return margin / rate / 21_600.0


class _StallWatch:
    """Notices when a burn stops making progress (no fuel left).

    Usage: call ``stalled`` every reading with a quantity that should keep
    decreasing while the engines push.
    """

    def __init__(self, clock: Callable[[], float]) -> None:
        """Create the watch.

        Args:
            clock: Monotonic real clock, in seconds.
        """
        self._clock = clock
        self._best = math.inf
        self._since = clock()

    def stalled(self, value: float) -> bool:
        """Record a reading.

        Args:
            value: The quantity that should decrease.

        Returns:
            True when it has not decreased for ``STALL_SECONDS``.
        """
        now = self._clock()
        if value < self._best - 1e-9:
            self._best = value
            self._since = now
            return False
        return now - self._since > STALL_SECONDS
