"""Rules for deploying a relay constellation around a body (pure, no I/O).

A relay constellation is ``N`` probes sharing one circular orbit, evenly
spaced around it, so that each one always sees its two neighbours over the
horizon of the body. They are carried together by one vessel and spread
with a *resonant orbit* (see Eric Meyer's resonant orbit calculator,
https://meyerweb.com/eric/ksp/resonant-orbits/):

* The carrier flies an orbit whose periapsis is at the altitude of the
  constellation and whose period is ``(N + 1) / N`` times the period of the
  final circular orbit (its apoapsis is therefore higher).
* A probe that brakes at the periapsis into the circular orbit completes a
  lap in ``T``; the ones still on the resonant orbit come back to the
  periapsis ``T / N`` later. The next probe that brakes there lands
  ``1 / N`` of a lap behind the first, and so on: after ``N`` laps the
  ring is complete.

The lowest altitude at which ``N`` evenly spaced probes still see their
neighbours is the one where the straight line between two neighbours just
grazes the body: ``r cos(pi / N) = R``. The constellation altitude defaults
to that, rounded up to the next multiple of 10 km, and never below the
atmosphere or the highest mountain of the body.

This module also holds the orbital mechanics the deployment needs: Kepler
propagation of an orbit from its elements (so positions can be predicted
in any game scene, for vessels far away), the two-burn transfer from any
orbit to the resonant one, how sensitive a period is to a burn, which
probe is the rearmost of a group and which slot of the ring a probe would
land in.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

Vector = tuple[float, float, float]

# Highest terrain of the stock bodies, in metres above the datum (KSP
# wiki, rounded up). The constellation must clear it; Jool and the Sun have
# no surface to hit.
HIGHEST_TERRAIN: dict[str, float] = {
    "Moho": 6_820.0,
    "Eve": 7_530.0,
    "Gilly": 6_410.0,
    "Kerbin": 6_770.0,
    "Mun": 7_070.0,
    "Minmus": 5_730.0,
    "Duna": 8_270.0,
    "Ike": 12_730.0,
    "Dres": 5_700.0,
    "Laythe": 6_080.0,
    "Vall": 7_980.0,
    "Tylo": 12_910.0,
    "Bop": 21_760.0,
    "Pol": 5_590.0,
    "Eeloo": 3_880.0,
    "Jool": 0.0,
    "Sun": 0.0,
}
# For a body missing from the table (a modded one): a tenth of its radius,
# and at least 5 km. Pass an explicit altitude when this is too cautious.
UNKNOWN_TERRAIN_FRACTION = 0.1
UNKNOWN_TERRAIN_MINIMUM = 5_000.0
# Margin kept above the highest terrain or the top of the atmosphere.
SAFETY_CLEARANCE = 1_000.0
# The resonant apoapsis must stay this far inside the sphere of influence.
SOI_MARGIN = 0.9
# Default rounding of the automatic altitude, in metres.
ALTITUDE_STEP = 10_000.0
# Below this margin between the ideal gap and the widest one in sight, the
# plan warns: small placement errors or drift would break the ring.
LOW_MARGIN_DEGREES = 3.0


class ConstellationError(ValueError):
    """The constellation asked for cannot be built (bad count, altitude...)."""


# ------------------------------------------------------------------ bodies


@dataclass(frozen=True)
class Body:
    """A celestial body the constellation orbits.

    Attributes:
        name: Internal name of the body (``Mun``, ``Minmus``...).
        mu: Gravitational parameter, in m^3/s^2.
        radius: Equatorial radius, in metres.
        sphere_of_influence: Radius of its sphere of influence, in metres.
        atmosphere_depth: Height of its atmosphere, in metres (0 without).
    """

    name: str
    mu: float
    radius: float
    sphere_of_influence: float
    atmosphere_depth: float = 0.0

    @property
    def highest_terrain(self) -> float:
        """Altitude of its highest mountain, in metres.

        Returns:
            The value from ``HIGHEST_TERRAIN``, or a cautious estimate for
            a body missing from it.
        """
        if self.name in HIGHEST_TERRAIN:
            return HIGHEST_TERRAIN[self.name]
        return max(UNKNOWN_TERRAIN_MINIMUM, UNKNOWN_TERRAIN_FRACTION * self.radius)

    @property
    def min_safe_altitude(self) -> float:
        """Lowest altitude an orbit can keep without hitting anything.

        Returns:
            Above the atmosphere and the highest terrain, with a margin.
        """
        return max(self.atmosphere_depth, self.highest_terrain) + SAFETY_CLEARANCE

    @property
    def disposal_periapsis(self) -> float:
        """Periapsis altitude that brings a vessel down for good.

        In the atmosphere (a third of its height) a vessel burns up or
        lands; on an airless body the periapsis goes well under the
        surface, so the vessel hits the ground.

        Returns:
            The altitude, in metres (negative: below the surface).
        """
        if self.atmosphere_depth > 0:
            return self.atmosphere_depth / 3.0
        return -0.1 * self.radius


# --------------------------------------------------------- orbit formulas


def circular_period(mu: float, semi_major_axis: float) -> float:
    """Period of an orbit, which only depends on its semi-major axis.

    Args:
        mu: Gravitational parameter of the body, in m^3/s^2.
        semi_major_axis: Semi-major axis (the radius of a circular orbit),
            in metres.

    Returns:
        The period, in seconds.
    """
    return 2.0 * math.pi * math.sqrt(semi_major_axis ** 3 / mu)


def semi_major_axis_for_period(mu: float, period: float) -> float:
    """Semi-major axis of the orbits that take a given time per lap.

    Args:
        mu: Gravitational parameter of the body, in m^3/s^2.
        period: Wanted period, in seconds.

    Returns:
        The semi-major axis, in metres.
    """
    return (mu * (period / (2.0 * math.pi)) ** 2) ** (1.0 / 3.0)


def orbital_speed(mu: float, radius: float, semi_major_axis: float) -> float:
    """Speed at a distance from the centre (vis-viva equation).

    Args:
        mu: Gravitational parameter of the body, in m^3/s^2.
        radius: Distance from the centre, in metres.
        semi_major_axis: Semi-major axis of the orbit (negative for a
            hyperbola), in metres.

    Returns:
        The speed, in m/s.
    """
    return math.sqrt(max(0.0, mu * (2.0 / radius - 1.0 / semi_major_axis)))


def period_sensitivity(mu: float, semi_major_axis: float, speed: float) -> float:
    """How many seconds of period one m/s along the velocity adds.

    From ``T = 2 pi sqrt(a^3 / mu)`` and the vis-viva equation:
    ``dT/dv = 3 T a v / mu``. The higher and faster the vessel, the more a
    small burn changes its period; that is what makes trimming a period to
    a fraction of a second delicate.

    Args:
        mu: Gravitational parameter of the body, in m^3/s^2.
        semi_major_axis: Semi-major axis now, in metres.
        speed: Orbital speed now, in m/s.

    Returns:
        Seconds of period per m/s of prograde delta-v (positive).
    """
    return 3.0 * circular_period(mu, semi_major_axis) * semi_major_axis * speed / mu


def opposite_apsis_radius(semi_major_axis: float, radius: float) -> float:
    """Distance of the apsis across the orbit from a vessel at an apsis.

    Burning along the velocity at an apsis keeps that apsis where it is
    and moves the other one, whose distance is ``2a - r``. Watching it
    tells when such a burn is done, whichever of the two apsides ends up
    being the periapsis.

    Args:
        semi_major_axis: Semi-major axis now, in metres.
        radius: Distance from the centre now (at the apsis), in metres.

    Returns:
        The distance of the other apsis, in metres.
    """
    return 2.0 * semi_major_axis - radius


def apsis_burn_delta_v(mu: float, radius: float, semi_major_axis: float,
                       new_opposite_radius: float) -> float:
    """Prograde delta-v that moves the opposite apsis, burning at an apsis.

    Args:
        mu: Gravitational parameter of the body, in m^3/s^2.
        radius: Distance of the apsis where the burn happens, in metres.
        semi_major_axis: Semi-major axis now (negative for a hyperbola), in
            metres.
        new_opposite_radius: Wanted distance of the other apsis, in metres.

    Returns:
        The delta-v, in m/s: positive prograde, negative retrograde.
    """
    new_semi_major_axis = (radius + new_opposite_radius) / 2.0
    return (orbital_speed(mu, radius, new_semi_major_axis)
            - orbital_speed(mu, radius, semi_major_axis))


# ------------------------------------------------------------ line of sight


def los_radius(body_radius: float, count: int, occlusion: float = 1.0) -> float:
    """Smallest orbit radius where evenly spaced neighbours see each other.

    The line between two neighbours ``2 pi / N`` apart passes closest to
    the centre at ``r cos(pi / N)``; it must stay above the body.

    Args:
        body_radius: Radius of the body, in metres.
        count: Number of probes in the ring (3 or more).
        occlusion: Fraction of the radius that blocks the signal. CommNet
            uses less than 1 (its occlusion modifiers), so 1 is the
            cautious choice.

    Returns:
        The radius, in metres.
    """
    return body_radius * occlusion / math.cos(math.pi / count)


def min_los_altitude(body_radius: float, count: int, occlusion: float = 1.0) -> float:
    """Lowest altitude where evenly spaced neighbours see each other.

    Args:
        body_radius: Radius of the body, in metres.
        count: Number of probes in the ring (3 or more).
        occlusion: See ``los_radius``.

    Returns:
        The altitude, in metres (30.9 km for 6 probes at the Mun).
    """
    return los_radius(body_radius, count, occlusion) - body_radius


def max_los_gap(body_radius: float, radius: float, occlusion: float = 1.0) -> float:
    """Widest angle between two probes at a radius that still see each other.

    Args:
        body_radius: Radius of the body, in metres.
        radius: Distance of both probes from the centre, in metres.
        occlusion: See ``los_radius``.

    Returns:
        The angle, in radians (0 when the radius is inside the body).
    """
    ratio = body_radius * occlusion / radius
    if ratio >= 1.0:
        return 0.0
    return 2.0 * math.acos(ratio)


def neighbour_distance(radius: float, count: int) -> float:
    """Straight-line distance between two neighbours of the ring.

    Args:
        radius: Radius of the circular orbit, in metres.
        count: Number of probes.

    Returns:
        The distance, in metres; their antennas must reach it.
    """
    return 2.0 * radius * math.sin(math.pi / count)


# --------------------------------------------------------------- the plan


@dataclass(frozen=True)
class ConstellationPlan:
    """Everything decided before flying: the final and the resonant orbit.

    Attributes:
        body: Body the probes orbit.
        count: Number of probes.
        altitude: Altitude of the final circular orbit, in metres.
        los_altitude: Lowest altitude where neighbours see each other, in
            metres.
        occlusion: Fraction of the body radius taken as blocking the
            signal.
        period: Period of the final circular orbit, in seconds.
        resonant_period: Period of the resonant orbit, ``(N + 1) / N``
            times ``period``, in seconds.
        resonant_apoapsis: Apoapsis altitude of the resonant orbit (its
            periapsis is ``altitude``), in metres.
    """

    body: Body
    count: int
    altitude: float
    los_altitude: float
    occlusion: float
    period: float
    resonant_period: float
    resonant_apoapsis: float

    @property
    def radius(self) -> float:
        """Radius of the final circular orbit, in metres."""
        return self.body.radius + self.altitude

    @property
    def resonant_semi_major_axis(self) -> float:
        """Semi-major axis of the resonant orbit, in metres."""
        return semi_major_axis_for_period(self.body.mu, self.resonant_period)

    @property
    def periapsis_speed(self) -> float:
        """Speed at the periapsis of the resonant orbit, in m/s."""
        return orbital_speed(self.body.mu, self.radius, self.resonant_semi_major_axis)

    @property
    def circular_speed(self) -> float:
        """Speed on the final circular orbit, in m/s."""
        return orbital_speed(self.body.mu, self.radius, self.radius)

    @property
    def injection_delta_v(self) -> float:
        """Braking each probe needs at the periapsis, in m/s."""
        return self.periapsis_speed - self.circular_speed

    @property
    def slot_angle(self) -> float:
        """Angle between two neighbours of the finished ring, in radians."""
        return 2.0 * math.pi / self.count

    @property
    def max_gap(self) -> float:
        """Widest gap between neighbours that keeps them in sight, in radians."""
        return max_los_gap(self.body.radius, self.radius, self.occlusion)

    @property
    def neighbour_distance(self) -> float:
        """Distance between neighbours of the finished ring, in metres."""
        return neighbour_distance(self.radius, self.count)

    @property
    def max_brake_delay(self) -> float:
        """Seconds a brake may start late and still land close to its slot.

        Braking after the periapsis puts the probe a little further along
        than its slot: it gains on the slot at the difference between its
        angular speed at the periapsis and that of the circular orbit.
        Half a degree of error is allowed.

        Returns:
            The delay, in seconds.
        """
        gain = (self.periapsis_speed - self.circular_speed) / self.radius
        return math.radians(0.5) / gain if gain > 0 else 0.0

    def drift_period(self, rank: int, spacing: float) -> float:
        """Period that makes a probe pull ahead of the carrier while waiting.

        Right after the release every probe is close to the carrier. Each
        one shortens its period a little, more the higher its rank, so the
        group stretches into a line with the carrier at the back and
        ``spacing`` metres between probes after one lap. The carrier can
        then brake out of the orbit backwards, away from all of them, and
        the probe that brakes at each periapsis is always the rearmost.

        Args:
            rank: 1 for the probe that drifts least, up to ``count``.
            spacing: Metres gained per lap per rank, measured at the
                periapsis; 0 disables the drift.

        Returns:
            The period to trim the probe to, in seconds.
        """
        return self.resonant_period - rank * spacing / self.periapsis_speed

    def describe(self) -> str:
        """Summarize the plan for the log.

        Returns:
            One line per figure.
        """
        return "\n".join([
            f"Body: {self.body.name} (radius {self.body.radius / 1000:,.1f} km)",
            f"Probes: {self.count}, lowest altitude where neighbours see each other "
            f"{self.los_altitude / 1000:,.1f} km",
            f"Constellation: circular orbit at {self.altitude / 1000:,.1f} km, period "
            f"{format_duration(self.period)}, {self.neighbour_distance / 1000:,.0f} km between "
            f"neighbours, gaps up to {math.degrees(self.max_gap):.1f} deg keep them in sight "
            f"(ideal {math.degrees(self.slot_angle):.1f} deg)",
            f"Resonant orbit: periapsis {self.altitude / 1000:,.1f} km, apoapsis "
            f"{self.resonant_apoapsis / 1000:,.1f} km, period {format_duration(self.resonant_period)}",
            f"Braking per probe at the periapsis: {self.injection_delta_v:,.1f} m/s",
        ] + ([
            f"Warning: only {math.degrees(self.margin):.1f} deg of margin before neighbours lose "
            "sight of each other; a higher altitude gives more"
        ] if math.degrees(self.margin) < LOW_MARGIN_DEGREES else []))

    @property
    def margin(self) -> float:
        """How much a gap may grow beyond the ideal before sight is lost.

        Returns:
            The angle, in radians.
        """
        return self.max_gap - self.slot_angle


def plan_constellation(body: Body, count: int, altitude: float | None = None,
                       step: float = ALTITUDE_STEP, occlusion: float = 1.0) -> ConstellationPlan:
    """Choose the orbits of a constellation.

    Args:
        body: Body to orbit.
        count: Number of probes (3 or more: two probes cannot see each
            other across a body).
        altitude: Altitude of the circular orbit in metres; ``None`` picks
            the lowest one where neighbours see each other, rounded up to
            a multiple of ``step``, above the atmosphere and the mountains.
        step: Rounding of the automatic altitude, in metres.
        occlusion: Fraction of the body radius taken as blocking the signal.

    Returns:
        The plan.

    Raises:
        ConstellationError: When the count is too small, the altitude too
            low, or the resonant orbit would leave the sphere of influence.
    """
    if count < 3:
        raise ConstellationError(f"a ring needs at least 3 probes, not {count}")
    los_altitude = min_los_altitude(body.radius, count, occlusion)
    floor = max(los_altitude, body.min_safe_altitude)
    if altitude is None:
        altitude = math.ceil(floor / step) * step
    elif altitude < los_altitude:
        raise ConstellationError(
            f"at {altitude / 1000:,.1f} km {count} probes would not see their neighbours over "
            f"{body.name}: at least {los_altitude / 1000:,.1f} km is needed")
    elif altitude < body.min_safe_altitude:
        raise ConstellationError(
            f"{altitude / 1000:,.1f} km is too low over {body.name}: the orbit must stay above "
            f"{body.min_safe_altitude / 1000:,.1f} km (atmosphere or mountains)")
    radius = body.radius + altitude
    period = circular_period(body.mu, radius)
    resonant_period = period * (count + 1) / count
    apoapsis_radius = 2.0 * semi_major_axis_for_period(body.mu, resonant_period) - radius
    if apoapsis_radius > SOI_MARGIN * body.sphere_of_influence:
        raise ConstellationError(
            f"the resonant orbit would reach {(apoapsis_radius - body.radius) / 1000:,.0f} km, "
            f"too close to the edge of the sphere of influence of {body.name}: choose a lower "
            "altitude")
    return ConstellationPlan(
        body=body, count=count, altitude=altitude, los_altitude=los_altitude,
        occlusion=occlusion, period=period, resonant_period=resonant_period,
        resonant_apoapsis=apoapsis_radius - body.radius,
    )


# ----------------------------------------------------------- the transfer


@dataclass(frozen=True)
class ApsisBurn:
    """One burn along the velocity at an apsis.

    Attributes:
        at_periapsis: Whether it happens at the periapsis (else at the
            apoapsis).
        target_radius: Wanted distance of the apsis on the other side, in
            metres.
        delta_v: Estimated delta-v, positive prograde, in m/s.
    """

    at_periapsis: bool
    target_radius: float
    delta_v: float


def plan_transfer(mu: float, periapsis_radius: float, apoapsis_radius: float,
                  target_periapsis_radius: float, target_apoapsis_radius: float,
                  min_radius: float, max_radius: float = math.inf,
                  tolerance: float = 100.0) -> list[ApsisBurn]:
    """Cheapest pair of apsis burns from one orbit to another in its plane.

    A burn at an apsis keeps it and moves the other one. The first burn,
    at the periapsis or the apoapsis of the current orbit, puts the other
    apsis at one of the two target distances; the second, at that new
    apsis, puts the other one at the remaining target. That gives four
    candidates (two places times two orders); the cheapest one whose
    intermediate orbit stays clear of the body (and inside the sphere of
    influence) wins. Burns that would change less than ``tolerance`` are
    left out, so an orbit that already matches needs none.

    Args:
        mu: Gravitational parameter of the body, in m^3/s^2.
        periapsis_radius: Distance of the current periapsis, in metres.
        apoapsis_radius: Distance of the current apoapsis, in metres;
            ``math.inf`` for an escape trajectory (only the periapsis can
            then be used for the first burn: that is the capture).
        target_periapsis_radius: Wanted periapsis distance, in metres.
        target_apoapsis_radius: Wanted apoapsis distance, in metres.
        min_radius: Lowest periapsis allowed on the way, in metres.
        max_radius: Highest apoapsis allowed on the way, in metres.
        tolerance: Apsides closer than this to their target are kept, in
            metres.

    Returns:
        The burns, in order (empty when nothing has to change).

    Raises:
        ConstellationError: When no candidate is safe (the current
            periapsis is too low, say).
    """
    closed = math.isfinite(apoapsis_radius)
    semi_major_axis = (periapsis_radius + apoapsis_radius) / 2.0 if closed else -math.inf
    if (abs(periapsis_radius - target_periapsis_radius) <= tolerance
            and closed and abs(apoapsis_radius - target_apoapsis_radius) <= tolerance):
        return []

    best: tuple[float, list[ApsisBurn]] | None = None
    places = [(True, periapsis_radius)] + ([(False, apoapsis_radius)] if closed else [])
    targets = (target_periapsis_radius, target_apoapsis_radius)
    for at_periapsis, burn_radius in places:
        for first, second in (targets, targets[::-1]):
            low, high = min(burn_radius, first), max(burn_radius, first)
            if low < min_radius or high > max_radius:
                continue
            burns: list[ApsisBurn] = []
            if abs(first - (apoapsis_radius if at_periapsis else periapsis_radius)) > tolerance:
                dv1 = (apsis_burn_delta_v(mu, burn_radius, semi_major_axis, first)
                       if closed else _capture_delta_v(mu, burn_radius, periapsis_radius,
                                                       semi_major_axis, first))
                burns.append(ApsisBurn(at_periapsis, first, dv1))
            # The second burn happens at the apsis now at ``first``; it is
            # the periapsis of the intermediate orbit if ``first`` is the
            # lower of the two.
            middle = (burn_radius + first) / 2.0
            if abs(burn_radius - second) > tolerance:
                dv2 = apsis_burn_delta_v(mu, first, middle, second)
                burns.append(ApsisBurn(first <= burn_radius, second, dv2))
            if not closed and not burns:
                continue
            cost = sum(abs(b.delta_v) for b in burns)
            if best is None or cost < best[0]:
                best = (cost, burns)
    if best is None:
        raise ConstellationError(
            "no safe transfer to the resonant orbit: the current periapsis is too low or the "
            "orbit too high")
    return best[1]


def _capture_delta_v(mu: float, radius: float, periapsis_radius: float,
                     semi_major_axis: float, new_opposite_radius: float) -> float:
    """Delta-v of a burn at the periapsis of an escape trajectory.

    Args:
        mu: Gravitational parameter of the body, in m^3/s^2.
        radius: Distance of the periapsis, in metres.
        periapsis_radius: Same as ``radius`` (kept for symmetry).
        semi_major_axis: Semi-major axis of the hyperbola (negative), or
            ``-inf`` when unknown (taken as a parabola, a lower bound).
        new_opposite_radius: Wanted apoapsis distance, in metres.

    Returns:
        The (negative) delta-v, in m/s.
    """
    del periapsis_radius
    if math.isinf(semi_major_axis):
        speed = math.sqrt(2.0 * mu / radius)
    else:
        speed = orbital_speed(mu, radius, semi_major_axis)
    return orbital_speed(mu, radius, (radius + new_opposite_radius) / 2.0) - speed


# ------------------------------------------------------ Kepler propagation


@dataclass(frozen=True)
class Orbit:
    """A closed (elliptical) orbit given by its Keplerian elements.

    Positions are computed from the elements, so they can be predicted for
    any time and for vessels the game has not loaded, in any scene. They
    come out in an inertial frame centred on the body; every orbit around
    the same body uses the same frame, which is all that comparing
    vessels needs.

    Attributes:
        mu: Gravitational parameter of the body, in m^3/s^2.
        semi_major_axis: In metres.
        eccentricity: Below 1.
        inclination: In radians.
        longitude_of_ascending_node: In radians.
        argument_of_periapsis: In radians.
        mean_anomaly_at_epoch: In radians.
        epoch: Universal time of ``mean_anomaly_at_epoch``, in seconds.
    """

    mu: float
    semi_major_axis: float
    eccentricity: float
    inclination: float
    longitude_of_ascending_node: float
    argument_of_periapsis: float
    mean_anomaly_at_epoch: float
    epoch: float

    @property
    def period(self) -> float:
        """Seconds per lap."""
        return circular_period(self.mu, self.semi_major_axis)

    @property
    def mean_motion(self) -> float:
        """Average angular speed, in radians per second."""
        return 2.0 * math.pi / self.period

    @property
    def periapsis_radius(self) -> float:
        """Distance of the periapsis from the centre, in metres."""
        return self.semi_major_axis * (1.0 - self.eccentricity)

    @property
    def apoapsis_radius(self) -> float:
        """Distance of the apoapsis from the centre, in metres."""
        return self.semi_major_axis * (1.0 + self.eccentricity)

    def mean_anomaly(self, ut: float) -> float:
        """Mean anomaly at a time, in radians within [0, 2 pi).

        Args:
            ut: Universal time, in seconds.

        Returns:
            The mean anomaly.
        """
        return (self.mean_anomaly_at_epoch + self.mean_motion * (ut - self.epoch)) % (2.0 * math.pi)

    def true_anomaly(self, ut: float) -> float:
        """True anomaly at a time (angle from the periapsis), in radians.

        Args:
            ut: Universal time, in seconds.

        Returns:
            The true anomaly, within [0, 2 pi).
        """
        mean = self.mean_anomaly(ut)
        e = self.eccentricity
        # Kepler's equation M = E - e sin E, by Newton's method from M.
        eccentric = mean if e < 0.8 else math.pi
        for _ in range(50):
            step = (eccentric - e * math.sin(eccentric) - mean) / (1.0 - e * math.cos(eccentric))
            eccentric -= step
            if abs(step) < 1e-12:
                break
        true = 2.0 * math.atan2(math.sqrt(1.0 + e) * math.sin(eccentric / 2.0),
                                math.sqrt(1.0 - e) * math.cos(eccentric / 2.0))
        return true % (2.0 * math.pi)

    def radius_at(self, ut: float) -> float:
        """Distance from the centre at a time, in metres.

        Args:
            ut: Universal time, in seconds.

        Returns:
            The distance.
        """
        true = self.true_anomaly(ut)
        e = self.eccentricity
        return self.semi_major_axis * (1.0 - e * e) / (1.0 + e * math.cos(true))

    def position(self, ut: float) -> Vector:
        """Position at a time, in the body's inertial frame.

        Args:
            ut: Universal time, in seconds.

        Returns:
            The position vector, in metres.
        """
        true = self.true_anomaly(ut)
        radius = self.radius_at(ut)
        return self._to_inertial(radius * math.cos(true), radius * math.sin(true))

    def velocity(self, ut: float) -> Vector:
        """Velocity at a time, in the body's inertial frame.

        Args:
            ut: Universal time, in seconds.

        Returns:
            The velocity vector, in m/s.
        """
        true = self.true_anomaly(ut)
        e = self.eccentricity
        p = self.semi_major_axis * (1.0 - e * e)
        factor = math.sqrt(self.mu / p)
        return self._to_inertial(-factor * math.sin(true), factor * (e + math.cos(true)))

    @property
    def normal(self) -> Vector:
        """Unit vector along the angular momentum (the orbit normal).

        Returns:
            The normal, in the body's inertial frame.
        """
        i, node = self.inclination, self.longitude_of_ascending_node
        return (math.sin(node) * math.sin(i), -math.cos(node) * math.sin(i), math.cos(i))

    def next_periapsis(self, ut: float) -> float:
        """Universal time of the first periapsis passage after a time.

        Args:
            ut: Universal time, in seconds.

        Returns:
            The time of the passage, in seconds.
        """
        mean = self.mean_anomaly(ut)
        return ut + (2.0 * math.pi - mean) / self.mean_motion

    def last_periapsis(self, ut: float) -> float:
        """Universal time of the latest periapsis passage before a time.

        Args:
            ut: Universal time, in seconds.

        Returns:
            The time of the passage, in seconds.
        """
        return ut - self.mean_anomaly(ut) / self.mean_motion

    def next_apoapsis(self, ut: float) -> float:
        """Universal time of the first apoapsis passage after a time.

        Args:
            ut: Universal time, in seconds.

        Returns:
            The time of the passage, in seconds.
        """
        mean = self.mean_anomaly(ut)
        return ut + ((math.pi - mean) % (2.0 * math.pi)) / self.mean_motion

    def _to_inertial(self, x: float, y: float) -> Vector:
        """Rotate a vector from the orbit's plane to the inertial frame.

        Args:
            x: Component towards the periapsis.
            y: Component 90 degrees ahead, in the direction of motion.

        Returns:
            The vector in the inertial frame.
        """
        i = self.inclination
        node = self.longitude_of_ascending_node
        arg = self.argument_of_periapsis
        cos_node, sin_node = math.cos(node), math.sin(node)
        cos_arg, sin_arg = math.cos(arg), math.sin(arg)
        cos_i, sin_i = math.cos(i), math.sin(i)
        return (
            (cos_node * cos_arg - sin_node * sin_arg * cos_i) * x
            + (-cos_node * sin_arg - sin_node * cos_arg * cos_i) * y,
            (sin_node * cos_arg + cos_node * sin_arg * cos_i) * x
            + (-sin_node * sin_arg + cos_node * cos_arg * cos_i) * y,
            (sin_arg * sin_i) * x + (cos_arg * sin_i) * y,
        )


def orbit_from_state(mu: float, position: Vector, velocity: Vector, ut: float) -> Orbit:
    """Keplerian elements of the orbit through a position and a velocity.

    Args:
        mu: Gravitational parameter of the body, in m^3/s^2.
        position: Position in the body's inertial frame, in metres.
        velocity: Velocity in the same frame, in m/s.
        ut: Universal time of the state, which becomes the epoch.

    Returns:
        The orbit.

    Raises:
        ConstellationError: When the state is not on a closed orbit.
    """
    r = _norm(position)
    v2 = _dot(velocity, velocity)
    h = _cross(position, velocity)
    energy = v2 / 2.0 - mu / r
    if energy >= 0:
        raise ConstellationError("not a closed orbit")
    a = -mu / (2.0 * energy)
    e_vec = tuple(c / mu for c in _sub(_cross(velocity, h), tuple(mu * p / r for p in position)))
    e = _norm(e_vec)
    h_norm = _norm(h)
    i = math.acos(max(-1.0, min(1.0, h[2] / h_norm)))
    node_vec = (-h[1], h[0], 0.0)
    node_norm = _norm(node_vec)
    if node_norm < 1e-9:
        # Equatorial: measure from the x axis.
        node = 0.0
        node_vec = (1.0, 0.0, 0.0)
        node_norm = 1.0
    else:
        node = math.atan2(node_vec[1], node_vec[0]) % (2.0 * math.pi)
    # Directions in the plane: towards the node, and 90 degrees ahead.
    x_axis = tuple(c / node_norm for c in node_vec)
    y_axis = _cross(tuple(c / h_norm for c in h), x_axis)
    if e < 1e-9:
        arg = 0.0
        true = math.atan2(_dot(position, y_axis), _dot(position, x_axis))
    else:
        arg = math.atan2(_dot(e_vec, y_axis), _dot(e_vec, x_axis))
        p_angle = math.atan2(_dot(position, y_axis), _dot(position, x_axis))
        true = p_angle - arg
    eccentric = 2.0 * math.atan2(math.sqrt(1.0 - e) * math.sin(true / 2.0),
                                 math.sqrt(1.0 + e) * math.cos(true / 2.0))
    mean = (eccentric - e * math.sin(eccentric)) % (2.0 * math.pi)
    return Orbit(mu=mu, semi_major_axis=a, eccentricity=e, inclination=i,
                 longitude_of_ascending_node=node, argument_of_periapsis=arg % (2.0 * math.pi),
                 mean_anomaly_at_epoch=mean, epoch=ut)


# ------------------------------------------------------------- the ring


def angle_in_plane(position: Vector, normal: Vector, reference: Vector) -> float:
    """Angle of a position around an orbit, in the direction of motion.

    Args:
        position: The position, in the body's inertial frame.
        normal: Normal of the orbit (motion is counter-clockwise about it).
        reference: Direction that counts as angle 0 (projected onto the
            plane).

    Returns:
        The angle, in radians within [0, 2 pi).
    """
    n = _unit(normal)
    x_axis = _unit(_sub(reference, tuple(_dot(reference, n) * c for c in n)))
    y_axis = _cross(n, x_axis)
    return math.atan2(_dot(position, y_axis), _dot(position, x_axis)) % (2.0 * math.pi)


def signed_angle(angle: float) -> float:
    """Wrap an angle into (-pi, pi].

    Args:
        angle: In radians.

    Returns:
        The same direction, between -pi (excluded) and pi.
    """
    wrapped = math.fmod(angle + math.pi, 2.0 * math.pi)
    if wrapped <= 0:
        wrapped += 2.0 * math.pi
    return wrapped - math.pi


def rearmost(angles: dict[str, float]) -> str:
    """The member of a group that is furthest behind along the orbit.

    The group must span less than half a lap, which holds for probes still
    flying together on the resonant orbit.

    Args:
        angles: Angle of each member around the orbit (``angle_in_plane``),
            increasing in the direction of motion.

    Returns:
        The name of the rearmost member.

    Raises:
        ValueError: When the group is empty.
    """
    if not angles:
        raise ValueError("empty group")
    reference = next(iter(angles.values()))
    return min(angles, key=lambda name: signed_angle(angles[name] - reference))


def slot_of(angle: float, reference: float, count: int) -> int:
    """Slot of the ring an angle falls in, counted from a reference angle.

    Args:
        angle: Angle of a probe, in radians.
        reference: Angle of a probe that defines slot 0, in radians.
        count: Number of slots.

    Returns:
        The slot, from 0 to ``count - 1``.
    """
    step = 2.0 * math.pi / count
    return round(((angle - reference) % (2.0 * math.pi)) / step) % count


def slot_error(angle: float, reference: float, count: int) -> float:
    """How far an angle is from the centre of its slot.

    Args:
        angle: Angle of a probe, in radians.
        reference: Angle of a probe that defines slot 0, in radians.
        count: Number of slots.

    Returns:
        The signed error, in radians (positive: ahead of the slot).
    """
    step = 2.0 * math.pi / count
    offset = (angle - reference) % (2.0 * math.pi)
    return offset - round(offset / step) * step


def landing_slot_free(landing: float, occupied: list[float], count: int) -> bool:
    """Tell whether a probe braking now would land in an empty slot.

    The first probe defines the ring: any landing is fine. Later ones must
    land in a slot no probe holds. After a missed periapsis the next
    passes fill the following slots, and the last probe waits the laps
    needed for the empty one to come round.

    Args:
        landing: Angle where the probe would end up, in radians.
        occupied: Angles of the probes already on the ring, at the same
            time, in radians.
        count: Number of slots.

    Returns:
        True when the slot is free.
    """
    if not occupied:
        return True
    reference = occupied[0]
    taken = {slot_of(angle, reference, count) for angle in occupied}
    return slot_of(landing, reference, count) not in taken


def ring_gaps(angles: dict[str, float]) -> list[tuple[str, str, float]]:
    """Gaps between neighbours of a ring, in the direction of motion.

    Args:
        angles: Angle of each probe at one time, in radians.

    Returns:
        ``(probe, the one ahead of it, gap in radians)`` for every probe,
        in ring order.
    """
    ordered = sorted(angles, key=lambda name: angles[name])
    gaps = []
    for index, name in enumerate(ordered):
        ahead = ordered[(index + 1) % len(ordered)]
        gap = (angles[ahead] - angles[name]) % (2.0 * math.pi)
        if len(ordered) == 1:
            gap = 2.0 * math.pi
        gaps.append((name, ahead, gap))
    return gaps


def ring_position(angle: float, reference_angle: float, reference_position: int,
                  count: int) -> int:
    """Position number of a probe on the ring, from one whose number is known.

    Positions are numbered in the order a resonant deployment fills them
    when no periapsis is missed: each probe lands one slot *behind* the
    previous one, so the numbers grow against the direction of motion.

    Args:
        angle: Angle of the probe, in radians.
        reference_angle: Angle of a probe whose position is known, at the
            same time, in radians.
        reference_position: Position of that probe, from 1 to ``count``.
        count: Number of probes.

    Returns:
        The position, from 1 to ``count``.
    """
    behind = slot_of(reference_angle, angle, count)
    return (reference_position - 1 + behind) % count + 1


# ------------------------------------------------------------------ names

# "Communication Array": first part of every probe name.
NAME_TAG = "CA"


def altitude_label(altitude: float) -> str:
    """Altitude as it appears in the probe names, in kilometres.

    Args:
        altitude: Altitude of the constellation, in metres.

    Returns:
        ``40`` for 40,000 m, ``12.5`` for 12,500 m.
    """
    return f"{altitude / 1000:g}"


def probe_name_prefix(altitude: float, body_label: str) -> str:
    """Start shared by the names of every probe of a constellation.

    Args:
        altitude: Altitude of the constellation, in metres.
        body_label: Name of the body as the player wants it written
            (``Muna``).

    Returns:
        Text such as ``CA-40-Muna-``.
    """
    return f"{NAME_TAG}-{altitude_label(altitude)}-{body_label}-"


def probe_name(prefix: str, position: int, person: str) -> str:
    """Full name of a probe.

    Args:
        prefix: See ``probe_name_prefix``.
        position: Its position number (two digits at least).
        person: An invented person's name, which never changes.

    Returns:
        Text such as ``CA-40-Muna-01 John Doe``.
    """
    return f"{prefix}{position:02d} {person}"


def parse_probe_name(name: str, prefix: str) -> tuple[int, str] | None:
    """Read the position number and the person's name of a probe.

    Args:
        name: Vessel name.
        prefix: See ``probe_name_prefix``.

    Returns:
        ``(position, person)``, or ``None`` when the vessel is not one of
        the constellation's probes.
    """
    if not name.startswith(prefix):
        return None
    number, _, person = name[len(prefix):].partition(" ")
    if not number.isdigit() or not person:
        return None
    return int(number), person


def existing_constellations(names: list[str], body_label: str) -> dict[float, list[str]]:
    """Group the probe names of a body's constellations by altitude.

    Lets a later run find a deployment in progress without being told its
    altitude.

    Args:
        names: Names of every vessel in the game.
        body_label: Name of the body in the probe names (``Muna``).

    Returns:
        Altitude in metres -> names of its probes.
    """
    groups: dict[float, list[str]] = {}
    head = f"{NAME_TAG}-"
    for name in names:
        if not name.startswith(head):
            continue
        altitude_text = name[len(head):].partition("-")[0]
        try:
            altitude = float(altitude_text) * 1000.0
        except ValueError:
            continue
        if parse_probe_name(name, probe_name_prefix(altitude, body_label)) is not None:
            groups.setdefault(altitude, []).append(name)
    return groups


def carrier_name(altitude: float, body_label: str) -> str:
    """Name given to the carrier once it starts releasing probes.

    It lets a later run find the carrier again to bring it down.

    Args:
        altitude: Altitude of the constellation, in metres.
        body_label: See ``probe_name_prefix``.

    Returns:
        Text such as ``CA-40-Muna Carrier``.
    """
    return f"{NAME_TAG}-{altitude_label(altitude)}-{body_label} Carrier"


def format_duration(seconds: float) -> str:
    """Format a game duration for people.

    Args:
        seconds: The duration.

    Returns:
        Text such as ``48 min 12 s`` or ``3 h 05 min``.
    """
    if not math.isfinite(seconds):
        return "never"
    seconds = max(0.0, seconds)
    if seconds < 60:
        return f"{seconds:.1f} s"
    if seconds < 3600:
        return f"{int(seconds // 60)} min {int(seconds % 60):02d} s"
    hours = int(seconds // 3600)
    return f"{hours} h {int((seconds % 3600) // 60):02d} min"


# ------------------------------------------------------------ vector maths


def _dot(a: Vector, b: Vector) -> float:
    """Dot product of two vectors."""
    return a[0] * b[0] + a[1] * b[1] + a[2] * b[2]


def _cross(a: Vector, b: Vector) -> Vector:
    """Cross product of two vectors."""
    return (a[1] * b[2] - a[2] * b[1], a[2] * b[0] - a[0] * b[2], a[0] * b[1] - a[1] * b[0])


def _sub(a: Vector, b: Vector) -> Vector:
    """Difference of two vectors."""
    return (a[0] - b[0], a[1] - b[1], a[2] - b[2])


def _norm(a: Vector) -> float:
    """Length of a vector."""
    return math.sqrt(_dot(a, a))


def _unit(a: Vector) -> Vector:
    """Vector of length 1 in the same direction."""
    length = _norm(a)
    return (a[0] / length, a[1] / length, a[2] / length)


def distance(a: Vector, b: Vector) -> float:
    """Distance between two points.

    Args:
        a: First point.
        b: Second point.

    Returns:
        The distance, in the units of the points.
    """
    return _norm(_sub(a, b))
