"""Tests of the relay constellation rules and of its deployment, in a simulated game."""

from __future__ import annotations

import math
from collections.abc import Callable
from dataclasses import dataclass, field

import pytest

from ksp_control.application.ports import (
    ConstellationGamePort,
    Direction,
    Notice,
    NoticeLevel,
    VesselOrbit,
)
from ksp_control.application.relay_constellation import (
    ConstellationDeployer,
    DeploymentOptions,
    DeploymentPaused,
)
from ksp_control.domain.constellation import (
    Body,
    ConstellationError,
    Orbit,
    angle_in_plane,
    existing_constellations,
    landing_slot_free,
    min_los_altitude,
    orbit_from_state,
    parse_probe_name,
    plan_constellation,
    plan_transfer,
    probe_name,
    probe_name_prefix,
    rearmost,
    ring_position,
    signed_angle,
)
from ksp_control.domain.model import GameScene
from ksp_control.domain.tourism import Propulsion

MUN = Body("Mun", 6.5138398e10, 200_000.0, 2_429_559.0)
MINMUS = Body("Minmus", 1.7658e9, 60_000.0, 2_247_428.0)
KERBIN = Body("Kerbin", 3.5316e12, 600_000.0, 84_159_286.0, 70_000.0)


# ------------------------------------------------------------- domain rules


def test_mun_plan_matches_the_resonant_orbit_calculator():
    plan = plan_constellation(MUN, 6)
    assert plan.los_altitude == pytest.approx(30_940, abs=10)
    assert plan.altitude == 40_000
    assert plan.resonant_period == pytest.approx(plan.period * 7 / 6)
    assert plan.resonant_apoapsis == pytest.approx(91_900, abs=150)
    assert plan.injection_delta_v == pytest.approx(24.8, abs=0.1)
    assert math.degrees(plan.max_gap) == pytest.approx(67.1, abs=0.1)


def test_plan_stays_above_atmosphere_and_mountains():
    # Kerbin: 6 probes see each other from 92.8 km, above the atmosphere.
    assert plan_constellation(KERBIN, 6).altitude == 100_000
    # 3 probes at Minmus would see each other from 60 km, which is fine;
    # many probes would allow a very low orbit, but the mountains do not.
    plan = plan_constellation(MINMUS, 12)
    assert plan.altitude >= MINMUS.highest_terrain


def test_plan_rejects_impossible_constellations():
    with pytest.raises(ConstellationError):
        plan_constellation(MUN, 2)
    with pytest.raises(ConstellationError):
        plan_constellation(MUN, 6, altitude=25_000)
    tiny_soi = Body("Rock", 1e9, 10_000.0, 30_000.0)
    with pytest.raises(ConstellationError):
        plan_constellation(tiny_soi, 3)


def test_min_los_altitude_formula():
    assert min_los_altitude(200_000, 6) == pytest.approx(200_000 / math.cos(math.pi / 6) - 200_000)
    assert min_los_altitude(200_000, 6, occlusion=0.9) < min_los_altitude(200_000, 6)


def test_transfer_from_a_high_circular_orbit_takes_two_burns():
    plan = plan_constellation(MUN, 6)
    r = MUN.radius + 100_000
    burns = plan_transfer(MUN.mu, r, r, plan.radius, MUN.radius + plan.resonant_apoapsis,
                          min_radius=MUN.radius + MUN.min_safe_altitude)
    assert len(burns) == 2
    assert all(b.delta_v < 0 for b in burns)
    assert {round(b.target_radius) for b in burns} == {
        round(plan.radius), round(MUN.radius + plan.resonant_apoapsis)}


def test_transfer_refuses_an_unsafe_path_and_skips_matching_apsides():
    plan = plan_constellation(MUN, 6)
    ap = MUN.radius + plan.resonant_apoapsis
    assert plan_transfer(MUN.mu, plan.radius, ap, plan.radius, ap, min_radius=MUN.radius) == []
    one = plan_transfer(MUN.mu, plan.radius, ap + 20_000, plan.radius, ap, min_radius=MUN.radius)
    assert len(one) == 1 and one[0].at_periapsis
    # A periapsis under the ground is raised from the apoapsis...
    rescue = plan_transfer(MUN.mu, MUN.radius - 1000, ap, plan.radius, ap, min_radius=MUN.radius)
    assert len(rescue) == 1 and not rescue[0].at_periapsis
    # ...but an escape trajectory into the ground has no apoapsis to use.
    with pytest.raises(ConstellationError):
        plan_transfer(MUN.mu, MUN.radius - 1000, math.inf, plan.radius, ap, min_radius=MUN.radius)


def test_capture_from_an_escape_trajectory_burns_at_the_periapsis():
    plan = plan_constellation(MUN, 6)
    burns = plan_transfer(MUN.mu, plan.radius, math.inf, plan.radius,
                          MUN.radius + plan.resonant_apoapsis, min_radius=MUN.radius)
    assert len(burns) == 1 and burns[0].at_periapsis and burns[0].delta_v < 0


def test_orbit_round_trips_through_a_state():
    orbit = Orbit(MUN.mu, 266_000.0, 0.1, 0.2, 1.0, 0.5, 2.0, 100.0)
    for ut in (100.0, 900.0, 2500.0):
        again = orbit_from_state(MUN.mu, orbit.position(ut), orbit.velocity(ut), ut)
        assert again.semi_major_axis == pytest.approx(orbit.semi_major_axis, rel=1e-9)
        assert again.eccentricity == pytest.approx(orbit.eccentricity, abs=1e-9)
        for later in (ut + 10.0, ut + 1234.0):
            assert math.dist(again.position(later), orbit.position(later)) < 1e-3


def test_periapsis_passages_are_found():
    orbit = Orbit(MUN.mu, 266_000.0, 0.1, 0.0, 0.0, 0.0, 1.0, 0.0)
    nxt = orbit.next_periapsis(50.0)
    assert orbit.radius_at(nxt) == pytest.approx(orbit.periapsis_radius, rel=1e-9)
    assert orbit.last_periapsis(nxt + 10.0) == pytest.approx(nxt)
    assert orbit.radius_at(orbit.next_apoapsis(50.0)) == pytest.approx(orbit.apoapsis_radius)


def test_rearmost_and_slots():
    assert rearmost({"a": 0.10, "b": 0.05, "c": 0.12}) == "b"
    # Across the zero angle.
    assert rearmost({"a": 0.01, "b": 2 * math.pi - 0.02}) == "b"
    slot = 2 * math.pi / 6
    taken = [0.0, -slot % (2 * math.pi)]
    assert not landing_slot_free(0.01, taken, 6)
    assert landing_slot_free(-2 * slot % (2 * math.pi), taken, 6)
    assert landing_slot_free(1.0, [], 6)
    # Positions grow backwards along the orbit.
    assert ring_position(-slot % (2 * math.pi), 0.0, 1, 6) == 2
    assert ring_position(slot, 0.0, 1, 6) == 6
    assert signed_angle(3 * math.pi / 2) == pytest.approx(-math.pi / 2)


def test_probe_names():
    prefix = probe_name_prefix(40_000, "Muna")
    assert prefix == "CA-40-Muna-"
    name = probe_name(prefix, 1, "John Doe")
    assert name == "CA-40-Muna-01 John Doe"
    assert parse_probe_name(name, prefix) == (1, "John Doe")
    assert parse_probe_name("CA-40-Muna Carrier", prefix) is None
    assert parse_probe_name("CA-50-Muna-01 John Doe", prefix) is None
    assert probe_name_prefix(12_500, "Minmus") == "CA-12.5-Minmus-"


def test_existing_constellations_are_found_by_name():
    names = ["CA-40-Muna-01 John Doe", "CA-40-Muna-02 Jane Roe", "CA-40-Muna Carrier",
             "CA-80-Muna-01 Max Power", "CA-10-Minmus-01 Ann Lee", "Cruise John Doe"]
    assert existing_constellations(names, "Muna") == {
        40_000.0: ["CA-40-Muna-01 John Doe", "CA-40-Muna-02 Jane Roe"],
        80_000.0: ["CA-80-Muna-01 Max Power"],
    }


# --------------------------------------------------------- simulated game


def _add(a, b):
    return (a[0] + b[0], a[1] + b[1], a[2] + b[2])


def _scale(a, k):
    return (a[0] * k, a[1] * k, a[2] * k)


def _unit(a):
    n = math.sqrt(a[0] ** 2 + a[1] ** 2 + a[2] ** 2)
    return (a[0] / n, a[1] / n, a[2] / n)


def _cross(a, b):
    return (a[1] * b[2] - a[2] * b[1], a[2] * b[0] - a[0] * b[2], a[0] * b[1] - a[1] * b[0])


@dataclass
class SimVessel:
    """A vessel of the simulated game: a Kepler orbit and a simple engine."""

    orbit: Orbit
    mass: float
    thrust: float
    isp: float
    delta_v: float
    engines_on: bool = False
    mounted: int = 0
    power: float = 5e8
    pointing: Direction | tuple[float, float] | None = None
    panels_out: bool = False


@dataclass
class SimGame(ConstellationGamePort):
    """A game where vessels follow Kepler orbits and burn in small steps.

    Time only runs through ``sleep`` (real time = game time) and
    ``warp_to``. Every brake is checked: nobody may be behind the probe that
    brakes.
    """

    planet: Body = MUN
    vessels: dict[str, SimVessel] = field(default_factory=dict)
    now: float = 1_000.0
    real: float = 0.0
    current_scene: GameScene = GameScene.FLIGHT
    active: str | None = None
    throttle: float = 0.0
    alarms: list[tuple[float, str, str]] = field(default_factory=list)
    notices: list[Notice] = field(default_factory=list)
    switches: list[tuple[GameScene, str]] = field(default_factory=list)
    warps: list[float] = field(default_factory=list)
    brake_violations: list[str] = field(default_factory=list)
    brakes: list[str] = field(default_factory=list)
    panels: list[tuple[str, int]] = field(default_factory=list)
    on_time: Callable[["SimGame"], None] | None = None
    kick: float = 0.6
    plan_radius: float = 0.0
    resonant_axis: float = 0.0
    approaches: dict[str, float] = field(default_factory=dict)
    last_sleep: float = 0.0
    _measure_after: str | None = None

    # -- time --------------------------------------------------------------

    def sleep(self, seconds: float) -> None:
        """Let real (and game) time run, burning the active engine."""
        self.last_sleep = seconds
        self.real += seconds
        left = seconds
        while left > 1e-9:
            step = min(left, 0.02 if self.throttle > 0 else left)
            self._burn(step)
            self.now += step
            left -= step
        if self.on_time:
            self.on_time(self)

    def clock(self) -> float:
        """Real time elapsed."""
        return self.real

    def _burn(self, dt: float) -> None:
        if self.throttle <= 0 or self.active is None:
            return
        v = self.vessels[self.active]
        if not v.engines_on or v.delta_v <= 0 or v.pointing is None:
            return
        accel = v.thrust * self.throttle / v.mass
        dv = min(accel * dt, v.delta_v)
        v.delta_v -= dv
        r, vel = v.orbit.position(self.now), v.orbit.velocity(self.now)
        before = v.orbit.semi_major_axis
        vel = _add(vel, _scale(self._direction(v, r, vel), dv))
        v.orbit = orbit_from_state(self.planet.mu, r, vel, self.now)
        middle = (self.plan_radius + self.resonant_axis) / 2
        if before >= middle > v.orbit.semi_major_axis:
            self._check_brake(self.active)

    def _direction(self, v: SimVessel, r, vel):
        if v.pointing is Direction.PROGRADE:
            return _unit(vel)
        if v.pointing is Direction.RETROGRADE:
            return _scale(_unit(vel), -1.0)
        if isinstance(v.pointing, tuple):
            prograde, normal = v.pointing
            return _unit(_add(_scale(_unit(vel), prograde),
                              _scale(_unit(_cross(r, vel)), normal)))
        return _unit(_cross(r, vel))

    def _check_brake(self, name: str) -> None:
        """Record a violation when a braking probe has a waiting probe behind it.

        The carrier is not held to that rule (it brakes away sideways);
        for every braking vessel the closest approach is measured once the
        engine stops.
        """
        v = self.vessels[name]
        middle = (self.plan_radius + self.resonant_axis) / 2
        self.brakes.append(name)
        self._measure_after = name
        if "Carrier" in name:
            return
        me = v.orbit.position(self.now)
        normal = v.orbit.normal
        for other, o in self.vessels.items():
            if other == name or not other.startswith("CA-") or "Carrier" in other:
                continue
            if o.orbit.semi_major_axis < middle:
                continue
            angle = signed_angle(angle_in_plane(o.orbit.position(self.now), normal, me))
            if angle < 0:
                self.brake_violations.append(f"{other} behind {name}")

    def _closest_approach(self, name: str) -> None:
        """Smallest distance from a vessel that just braked to the others, over half a lap."""
        middle = (self.plan_radius + self.resonant_axis) / 2
        me = self.vessels[name].orbit
        others = [o.orbit for n, o in self.vessels.items() if n != name and n.startswith("CA-")
                  and o.orbit.semi_major_axis > middle]
        closest = math.inf
        for step in range(0, 1800, 2):
            ut = self.now + step
            here = me.position(ut)
            for other in others:
                closest = min(closest, math.dist(here, other.position(ut)))
        self.approaches[name] = closest

    # -- port --------------------------------------------------------------

    def scene(self) -> GameScene:
        return self.current_scene

    def ut(self) -> float:
        return self.now

    def body(self, name: str) -> Body:
        return self.planet

    def active_vessel(self) -> str | None:
        return self.active if self.current_scene is GameScene.FLIGHT else None

    def vessel_names(self) -> list[str]:
        return list(self.vessels)

    def orbit(self, vessel: str) -> VesselOrbit | None:
        v = self.vessels.get(vessel)
        if v is None:
            return None
        o = v.orbit
        r = o.radius_at(self.now)
        speed = math.sqrt(self.planet.mu * (2 / r - 1 / o.semi_major_axis))
        periapsis = o.periapsis_radius - self.planet.radius
        return VesselOrbit(
            name=vessel, body=self.planet.name,
            situation="orbiting" if periapsis > 0 else "sub_orbital",
            ut=self.now, periapsis=periapsis, apoapsis=o.apoapsis_radius - self.planet.radius,
            semi_major_axis=o.semi_major_axis, period=o.period, radius=r, speed=speed,
            time_to_periapsis=o.next_periapsis(self.now) - self.now,
            time_to_apoapsis=o.next_apoapsis(self.now) - self.now, elements=o,
        )

    def period(self) -> float:
        return self.vessels[self.active].orbit.period

    def mounted_probes(self) -> int:
        return self.vessels[self.active].mounted

    def release_probe(self, name: str) -> bool:
        carrier = self.vessels[self.active]
        if carrier.mounted == 0:
            return False
        assert carrier.pointing is Direction.NORMAL
        index = carrier.mounted
        carrier.mounted -= 1
        r, vel = carrier.orbit.position(self.now), carrier.orbit.velocity(self.now)
        # Decouplers push sideways, in the plane of the orbit (the carrier
        # points along the normal), each probe in another direction.
        prograde = _unit(vel)
        radial = _unit(r)
        angle = index * 1.1
        push = _add(_scale(prograde, math.cos(angle)), _scale(radial, math.sin(angle)))
        self.vessels[name] = SimVessel(
            orbit=orbit_from_state(self.planet.mu, r, _add(vel, _scale(push, self.kick)), self.now),
            mass=500.0, thrust=2_000.0, isp=315.0, delta_v=300.0)
        return True

    def rename(self, vessel: str, new_name: str) -> None:
        assert new_name not in self.vessels, new_name
        self.vessels[new_name] = self.vessels.pop(vessel)
        if self.active == vessel:
            self.active = new_name

    def switch_to(self, vessel: str) -> None:
        assert vessel in self.vessels
        assert self.throttle == 0.0
        self.switches.append((self.current_scene, vessel))
        self.current_scene = GameScene.FLIGHT
        self.active = vessel

    def has_control(self) -> bool:
        return True

    def prepare_engines(self) -> int:
        v = self.vessels[self.active]
        v.engines_on = True
        return 1 if v.delta_v > 0 else 0

    def deploy_solar_panels(self) -> int:
        """Unfold the panels of the active vessel, noting how many brakes came before."""
        v = self.vessels[self.active]
        if v.panels_out:
            return 0
        v.panels_out = True
        self.panels.append((self.active, len(self.brakes)))
        return 1

    def propulsion(self) -> Propulsion:
        v = self.vessels[self.active]
        return Propulsion(v.thrust if v.engines_on else 0.0, v.isp, v.mass, v.delta_v)

    def point(self, direction: Direction) -> None:
        self.vessels[self.active].pointing = direction

    def point_between(self, prograde: float, normal: float) -> None:
        self.vessels[self.active].pointing = (prograde, normal)

    def relative_positions(self, origin: str, others: list[str]) -> dict[str, tuple[float, float]]:
        o = self.vessels[origin].orbit
        here, vel = o.position(self.now), o.velocity(self.now)
        along, normal = _unit(vel), _unit(_cross(here, vel))
        result = {}
        for name in others:
            if name in self.vessels:
                d = tuple(a - b for a, b in zip(self.vessels[name].orbit.position(self.now), here))
                result[name] = (sum(x * y for x, y in zip(d, along)),
                                sum(x * y for x, y in zip(d, normal)))
        return result

    def pointing_error(self) -> float:
        return 0.0

    def set_throttle(self, value: float) -> None:
        assert self.current_scene is GameScene.FLIGHT
        self.throttle = value
        if value == 0.0 and self._measure_after in self.vessels:
            self._closest_approach(self._measure_after)
            self._measure_after = None

    def release_controls(self) -> None:
        self.throttle = 0.0
        if self.active in self.vessels:
            self.vessels[self.active].pointing = None

    def warp_to(self, ut: float) -> None:
        assert self.throttle == 0.0
        assert ut >= self.now - 1e-6
        self.warps.append(ut - self.now)
        self.now = max(self.now, ut)
        if self.on_time:
            self.on_time(self)

    def set_alarm(self, ut: float, vessel: str, title: str, description: str) -> None:
        self.alarms.append((ut, vessel, title))

    def remove_alarms(self, title_prefix: str) -> int:
        return 0

    def show(self, notice: Notice) -> None:
        self.notices.append(notice)

    def comms_power(self, vessel: str) -> float:
        return self.vessels[vessel].power if vessel in self.vessels else 0.0

    def close(self) -> None:
        pass


def _game(probes: int = 6) -> SimGame:
    """A carrier in a 100 km orbit of the Mun, slightly inclined."""
    plan = plan_constellation(MUN, probes)
    game = SimGame(plan_radius=plan.radius, resonant_axis=plan.resonant_semi_major_axis)
    r = MUN.radius + 100_000
    speed = math.sqrt(MUN.mu / r)
    # Slightly eccentric so the apsides are well defined.
    orbit = orbit_from_state(MUN.mu, (r, 0.0, 0.0), (0.0, speed * 1.002 * math.cos(0.1),
                                                     speed * 1.002 * math.sin(0.1)), game.now)
    game.vessels["Relay Carrier"] = SimVessel(orbit, mass=6_000.0, thrust=30_000.0, isp=320.0,
                                              delta_v=600.0, engines_on=True, mounted=probes)
    game.active = "Relay Carrier"
    return game


def _deployer(game: SimGame, probes: int = 6, **options) -> ConstellationDeployer:
    plan = plan_constellation(MUN, probes)
    people = iter(f"Person {i}" for i in range(1, 1000))
    defaults = dict(body_label="Muna", poll=5.0)
    defaults.update(options)
    return ConstellationDeployer(game, plan, DeploymentOptions(**defaults), lambda: next(people),
                                 sleep=game.sleep, clock=game.clock)


def _check_ring(game: SimGame, report, probes: int = 6) -> None:
    assert len(report.probes) == probes, report
    assert sorted(p.position for p in report.probes) == list(range(1, probes + 1))
    for status in report.probes:
        assert status.gap_ahead == pytest.approx(360 / probes, abs=1.0), report
        assert abs(status.period_error) <= 0.1, report
        assert parse_probe_name(status.name, "CA-40-Muna-")[0] == status.position
    assert not report.warnings, report.warnings
    assert not game.brake_violations, game.brake_violations
    # Nobody passes close to a vessel that has just braked.
    assert min(game.approaches.values()) > 100.0, game.approaches


def test_full_deployment_spreads_six_probes_around_the_mun():
    game = _game()
    report = _deployer(game).run()
    _check_ring(game, report)
    # The carrier was renamed and brought down below the surface.
    carrier = game.vessels["CA-40-Muna Carrier"]
    assert carrier.orbit.periapsis_radius < MUN.radius
    # Laps take under an hour: time warp, never an alarm.
    assert game.alarms == []
    assert game.brakes[0] == "CA-40-Muna Carrier"
    probes = game.brakes[1:]
    assert len(probes) == 6
    # The first probe to brake took position 1 (people's names never change).
    first_person = parse_probe_name(probes[0], "CA-40-Muna-")[1]
    assert probe_name("CA-40-Muna-", 1, first_person) in game.vessels


def test_solar_panels_come_out_once_each_probe_has_lined_up():
    game = _game()
    _deployer(game).run()
    ring = {n: v for n, v in game.vessels.items() if parse_probe_name(n, "CA-40-Muna-")}
    assert len(ring) == 6 and all(v.panels_out for v in ring.values())
    # Never on the carrier, and every probe during the line-up: after its
    # drift trim, before the carrier brakes out of the orbit.
    assert not game.vessels["CA-40-Muna Carrier"].panels_out
    assert len(game.panels) == 6
    assert all(brakes == 0 for _, brakes in game.panels)


def test_alarm_and_red_warning_while_the_player_is_elsewhere_then_a_missed_pass():
    game = _game()
    game.vessels["Other ship"] = SimVessel(
        Orbit(MUN.mu, 900_000.0, 0.01, 0.0, 0.0, 0.0, 0.0, 0.0), 1.0, 0.0, 1.0, 0.0)
    state = {"away_until": None}

    def player(sim: SimGame) -> None:
        circular = [n for n, v in sim.vessels.items() if n.startswith("CA-40-Muna-")
                    and abs(v.orbit.semi_major_axis - sim.plan_radius) < 5_000]
        if state["away_until"] is None and len(circular) == 2 and sim.throttle == 0:
            # The player leaves for another vessel for two laps.
            state["away_until"] = sim.now + 2 * 3378.0
            sim.active = "Other ship"
        elif state["away_until"] is not None and sim.active == "Other ship" \
                and sim.now > state["away_until"]:
            sim.active = next(n for n in sim.vessels if n.startswith("CA-40-Muna-"))

    game.on_time = player
    report = _deployer(game).run()
    _check_ring(game, report)
    assert state["away_until"] is not None
    assert game.alarms, "an alarm is set while the player is elsewhere"
    assert any(n.level is NoticeLevel.ERROR for n in game.notices)


def test_takes_command_from_the_space_center_when_the_alarm_rings():
    game = _game()
    state = {"done": False}

    def player(sim: SimGame) -> None:
        # Only while the script waits between maneuvers (its long polls).
        if not state["done"] and len(sim.brakes) == 2 and sim.last_sleep >= 5.0:
            state["done"] = True
            sim.current_scene = GameScene.SPACE_CENTER
            sim.active = None

    game.on_time = player
    # Every wait sets an alarm: the player is in the space center when it rings.
    report = _deployer(game, alarm_threshold=0.0).run()
    _check_ring(game, report)
    assert any(scene is GameScene.SPACE_CENTER for scene, _ in game.switches)
    assert game.alarms


def test_long_laps_set_an_alarm_instead_of_warping_and_can_stop_and_resume():
    game = _game()
    with pytest.raises(DeploymentPaused):
        # Every wait counts as long: the first one sets an alarm and stops.
        _deployer(game, alarm_threshold=0.0, wait=False).run()
    assert len(game.alarms) == 1
    # Another run, another day: it picks up from the orbits in the game.
    report = _deployer(game, alarm_threshold=0.0).run()
    _check_ring(game, report)
    assert len([b for b in game.brakes if "Carrier" not in b]) == 6


def test_status_describes_the_next_step_without_commanding():
    game = _game()
    lines = _deployer(game).status()
    assert any("resonant orbit" in line and "release 6" in line for line in lines)
    assert game.throttle == 0.0 and game.warps == []
