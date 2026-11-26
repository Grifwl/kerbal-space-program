"""Tests of the tourism rules, flight director (suborbital and orbital) and campaign."""

from __future__ import annotations

import math
from dataclasses import dataclass, field, replace

import pytest

from ksp_control.adapters.game.krpc_tourism import craft_file_name
from ksp_control.application.ports import TourismGamePort
from ksp_control.application.tourism_flight import (
    FlightError,
    FlightReport,
    NotEnoughDeltaVError,
    TourismFlightDirector,
)
from ksp_control.application.tourism_campaign import (
    CampaignError,
    CampaignOptions,
    StopReason,
    TourismCampaign,
)
from ksp_control.application.vessel_recovery import RecoveryOutcome
from ksp_control.domain.recovery import RecoveryDecision, RecoveryPolicy, VesselReport
from ksp_control.domain.tourism import (
    ContractStatus,
    EngineState,
    FlightProfile,
    FlightTelemetry,
    Kerbal,
    Mission,
    OfferedContract,
    Propulsion,
    Tourist,
    TourismContract,
    apsis_burn_dv,
    burn_throttle,
    burn_time,
    choose_pilot,
    contract_mission,
    destination_mission,
    match_kerbal,
    orbit_visited,
    plan_passengers,
    should_stage_on_ascent,
    time_to_radius,
)

SUBORBITAL = "Vuelo espacial suborbital en Kerbin"
ORBIT = "Orbita alrededor de Kerbin"


def contract(cid: str, names: list[str], status=ContractStatus.OFFERED, reward=None,
             destinations=(SUBORBITAL,), delivered=()) -> TourismContract:
    """Build a tourism contract whose tourists all share the destinations.

    Like in the game, the rewards sit on the tourists' itineraries.

    Args:
        cid: Contract id.
        names: Full names of the tourists.
        status: Contract status.
        reward: Total reward (default: 13,000 per tourist), split evenly
            between the tourists.
        destinations: Destinations of every tourist.
        delivered: Names of the tourists whose itinerary is completed.

    Returns:
        The contract.
    """
    total = 13_000.0 * len(names) if reward is None else reward
    each = total / len(names) if names else 0.0
    tourists = tuple(Tourist(f"Itinerario de viaje de {n.split()[0]}", tuple(destinations), n,
                             reward=each, completed=n in delivered)
                     for n in names)
    return TourismContract(cid, f"Contract {cid}", status, tourists, total)


def ids(boardings) -> list[str]:
    """Contract ids of some boardings, in order."""
    return [b.contract.contract_id for b in boardings]


def five() -> list[str]:
    """The tourists of the five-tourist offer seen in the real game."""
    return ["Ribelia Kerman", "Munmoro Kerman", "Shernando Kerman", "Lubera Kerman",
            "Chutona Kerman"]


# ------------------------------------------------------------------ domain


def test_destinations_are_classified_by_mission():
    """Suborbital and orbit of Kerbin, in Spanish or English, with accents."""
    assert destination_mission(SUBORBITAL) is Mission.SUBORBITAL
    assert destination_mission("Suborbital spaceflight at Kerbin") is Mission.SUBORBITAL
    assert destination_mission(ORBIT) is Mission.ORBITAL
    assert destination_mission("Órbita alrededor de Kerbin") is Mission.ORBITAL
    assert destination_mission("Orbit Kerbin") is Mission.ORBITAL
    assert destination_mission("Orbita alrededor de Mun") is None
    assert destination_mission("Vuelo espacial suborbital en Mun") is None
    assert destination_mission("Sobrevuelo de Mun") is None


def test_contracts_need_the_mission_of_their_most_demanding_tourist():
    """One orbit makes the contract orbital; one flyby rules it out."""
    assert contract_mission(contract("a", ["Samroda Kerman"])) is Mission.SUBORBITAL
    assert contract_mission(contract("b", ["X Kerman"], destinations=(SUBORBITAL, ORBIT))) \
        is Mission.ORBITAL
    mixed = TourismContract("m", "Mixed", ContractStatus.OFFERED, (
        Tourist("Itinerario de viaje de Jebtono", (SUBORBITAL, ORBIT), "Jebtono Kerman"),
        Tourist("Itinerario de viaje de Bobneio", (ORBIT,), "Bobneio Kerman"),
    ))
    assert contract_mission(mixed) is Mission.ORBITAL
    assert contract_mission(contract("c", ["X Kerman"], destinations=(SUBORBITAL, "Sobrevuelo de Mun"))) is None
    assert contract_mission(contract("d", [])) is None
    # Only the tourists still waiting count.
    split = TourismContract("s", "Split", ContractStatus.ACTIVE, (
        Tourist("Itinerario de viaje de A", (ORBIT,), "A Kerman", completed=True),
        Tourist("Itinerario de viaje de B", (SUBORBITAL,), "B Kerman"),
    ))
    assert contract_mission(split) is Mission.SUBORBITAL


def test_orbit_visited_follows_the_destination_flags():
    """Only orbit destinations matter; a suborbital tourist is always done."""
    tourist = Tourist("Itinerario de viaje de A", (SUBORBITAL, ORBIT), "A Kerman")
    assert not orbit_visited(tourist)
    assert not orbit_visited(replace(tourist, visited=(SUBORBITAL,)))
    assert orbit_visited(replace(tourist, visited=(ORBIT,)))
    assert orbit_visited(Tourist("Itinerario de viaje de B", (SUBORBITAL,), "B Kerman"))


def test_tourists_are_matched_by_first_name():
    """Itinerary titles carry only the first name."""
    roster = ["Samroda Kerman", "Robala Kerman"]
    assert match_kerbal("Itinerario de viaje de Samroda", roster) == "Samroda Kerman"
    assert match_kerbal("Itinerario de viaje de Nobody", roster) is None
    assert match_kerbal("Itinerario de viaje de Bob", ["Bob Kerman", "Bob Smith"]) is None


def test_plan_fills_the_seats_with_the_best_offers():
    """Two pairs beat a pair plus a single."""
    contracts = [contract("vip", ["A Kerman"]), contract("p1", ["B Kerman", "C Kerman"]),
                 contract("p2", ["D Kerman", "E Kerman"])]
    plan = plan_passengers(contracts, seats=4, available=set())
    assert set(ids(plan.accept)) == {"p1", "p2"}
    assert plan.seats_used == 4 and plan.reward == 52_000


def test_plan_carries_accepted_contracts_first():
    """Accepted contracts board before any offer, offers fill the rest."""
    contracts = [contract("p1", ["B Kerman", "C Kerman"]),
                 contract("act", ["A Kerman"], ContractStatus.ACTIVE),
                 contract("p2", ["D Kerman", "E Kerman"]),
                 contract("moon", ["F Kerman"], destinations=("Sobrevuelo de Mun",))]
    plan = plan_passengers(contracts, seats=4, available={"A Kerman"})
    assert ids(plan.carry) == ["act"]
    # Three seats left: a pair whole, and one of the other pair, whose
    # second tourist flies on the next flight.
    assert ids(plan.accept) == ["p1", "p2"]
    assert [b.waiting for b in plan.accept] == [0, 1]
    assert plan.seats_used == 4


def test_plan_skips_accepted_contracts_whose_tourists_cannot_board():
    """Tourists that are not at the space center cannot fly."""
    contracts = [contract("act", ["A Kerman"], ContractStatus.ACTIVE)]
    assert plan_passengers(contracts, seats=4, available=set()).is_empty
    assert plan_passengers(contracts, seats=4, available={"A Kerman"}, allow_offers=False).carry


def test_plan_accepts_an_offer_bigger_than_the_craft():
    """Five tourists and four seats: four fly now, one waits."""
    plan = plan_passengers([contract("five", five())], seats=4, available=set())
    assert ids(plan.accept) == ["five"]
    boarding = plan.accept[0]
    assert [t.kerbal for t in boarding.tourists] == five()[:4]
    assert boarding.waiting == 1 and not boarding.completes_contract
    assert [t.kerbal for t in boarding.left_behind] == ["Chutona Kerman"]
    # Only the itineraries of the tourists on board are paid this flight.
    assert plan.reward == 4 * 13_000


def test_plan_splits_an_offer_that_pays_more_per_seat():
    """Seen in the game: five tourists at 19,500 beat two pairs at 13,000."""
    contracts = [contract("pair1", ["A Kerman", "B Kerman"]),
                 contract("pair2", ["C Kerman", "D Kerman"]),
                 contract("five", five(), reward=97_500)]
    plan = plan_passengers(contracts, seats=4, available=set())
    assert ids(plan.accept) == ["five"]
    assert plan.reward == 4 * 19_500 and plan.accept[0].waiting == 1


def test_plan_prefers_offers_that_fit_when_they_pay_the_same():
    """Splitting is only chosen when it fills more seats or pays more."""
    contracts = [contract("five", five()), contract("pair1", ["A Kerman", "B Kerman"]),
                 contract("pair2", ["C Kerman", "D Kerman"])]
    plan = plan_passengers(contracts, seats=4, available=set())
    assert set(ids(plan.accept)) == {"pair1", "pair2"}
    # One pair and the big one: the pair boards whole, the big one is split.
    plan = plan_passengers(contracts[:2], seats=4, available=set())
    assert ids(plan.accept) == ["five"]  # four seats filled beat two
    # A pair and a trio: the pair boards whole, two of the trio join it.
    trio = contract("trio", ["E Kerman", "F Kerman", "G Kerman"])
    plan = plan_passengers([trio, contracts[1]], seats=4, available=set())
    assert ids(plan.accept) == ["pair1", "trio"]
    assert [b.waiting for b in plan.accept] == [0, 1]


def test_plan_carries_the_tourists_left_by_a_split_contract():
    """The tourist left behind flies first; offers fill the other seats."""
    names = five()
    split = contract("five", names, ContractStatus.ACTIVE, delivered=names[:4])
    contracts = [split, contract("trio", ["E Kerman", "F Kerman", "G Kerman"]),
                 contract("pair", ["A Kerman", "B Kerman"])]
    plan = plan_passengers(contracts, seats=4, available=set(names))
    assert ids(plan.carry) == ["five"]
    assert [t.kerbal for t in plan.carry[0].tourists] == ["Chutona Kerman"]
    assert plan.carry[0].completes_contract
    assert ids(plan.accept) == ["trio"]
    assert plan.seats_used == 4
    # A delivered tourist never boards again, even if still at the space center.
    done = contract("done", ["A Kerman"], ContractStatus.ACTIVE, delivered=["A Kerman"])
    assert plan_passengers([done], seats=4, available={"A Kerman"}).is_empty


def test_plan_finishes_the_smallest_accepted_contracts_first():
    """Fewer tourists left go first, so contracts are completed sooner."""
    big = contract("big", five(), ContractStatus.ACTIVE)
    pair = contract("pair", ["A Kerman", "B Kerman"], ContractStatus.ACTIVE)
    plan = plan_passengers([big, pair], seats=4, available=set(five()) | {"A Kerman", "B Kerman"})
    assert ids(plan.carry) == ["pair", "big"]
    assert [len(b.tourists) for b in plan.carry] == [2, 2]
    assert plan.carry[1].waiting == 3


def test_plan_mixes_suborbital_and_orbital_tourists():
    """The flight is orbital as soon as one tourist on board wants an orbit."""
    orbital = contract("orb", ["A Kerman", "B Kerman"], reward=65_000,
                       destinations=(SUBORBITAL, ORBIT))
    hop = contract("hop", ["C Kerman", "D Kerman"])
    plan = plan_passengers([orbital, hop], seats=4, available=set())
    assert set(ids(plan.accept)) == {"orb", "hop"} and plan.mission is Mission.ORBITAL
    assert plan_passengers([hop], seats=4, available=set()).mission is Mission.SUBORBITAL
    # A craft limited to suborbital hops leaves the orbital tourists alone.
    plan = plan_passengers([orbital, hop], seats=4, available=set(),
                           max_mission=Mission.SUBORBITAL)
    assert ids(plan.accept) == ["hop"] and plan.mission is Mission.SUBORBITAL


def test_pilots_take_turns_and_unavailable_ones_are_skipped():
    """Rotation over the hired pilots, in name order unless given."""
    roster = [Kerbal("Valentina Kerman", "crew", "Pilot", True),
              Kerbal("Jebediah Kerman", "crew", "Pilot", True),
              Kerbal("Bill Kerman", "crew", "Engineer", True),
              Kerbal("Hanbarto Kerman", "applicant", "Pilot", True)]
    assert [choose_pilot(roster, n) for n in range(3)] == [
        "Jebediah Kerman", "Valentina Kerman", "Jebediah Kerman"]
    assert choose_pilot(roster, 0, ("Valentina Kerman",)) == "Valentina Kerman"
    roster[1] = Kerbal("Jebediah Kerman", "crew", "Pilot", False)
    assert choose_pilot(roster, 0) == "Valentina Kerman"


KERBIN_MU = 3.5316e12
KERBIN_RADIUS = 600_000.0


def test_apsis_burns_circularize_and_deorbit():
    """Vis-viva numbers for a Kerbin orbit at 80 km."""
    apo = KERBIN_RADIUS + 80_000
    # Already circular: nothing to do.
    assert apsis_burn_dv(KERBIN_MU, apo, apo, apo) == pytest.approx(0.0)
    # From the top of a hop that skims the surface to a circular orbit.
    hop_a = (apo + KERBIN_RADIUS) / 2
    circular_speed = math.sqrt(KERBIN_MU / apo)
    assert apsis_burn_dv(KERBIN_MU, apo, hop_a, apo) == pytest.approx(
        circular_speed - math.sqrt(KERBIN_MU * (2 / apo - 1 / hop_a)))
    assert 2_200 < circular_speed < 2_300
    # Deorbiting to a 35 km periapsis is a small retrograde burn.
    deorbit = apsis_burn_dv(KERBIN_MU, apo, apo, KERBIN_RADIUS + 35_000)
    assert -45 < deorbit < -35


def test_burn_time_follows_the_rocket_equation():
    """The vessel gets lighter, so the burn is shorter than dv / a."""
    # 1,000 m/s with 100 kN on 10 t and Isp 300 s.
    seconds = burn_time(1_000.0, 100_000.0, 300.0, 10_000.0)
    exhaust = 300.0 * 9.80665
    assert seconds == pytest.approx(10_000 * (1 - math.exp(-1_000 / exhaust)) * exhaust / 100_000)
    assert seconds < 1_000.0 / (100_000.0 / 10_000.0)
    assert burn_time(-1_000.0, 100_000.0, 300.0, 10_000.0) == seconds
    assert burn_time(100.0, 0.0, 300.0, 10_000.0) == math.inf
    # Full power while far from the target, easing off at the end.
    assert burn_throttle(500.0, 100_000.0, 10_000.0) == 1.0
    assert burn_throttle(5.0, 100_000.0, 10_000.0) == pytest.approx(0.25)
    assert burn_throttle(0.1, 100_000.0, 10_000.0) == 0.05


def test_time_to_radius_matches_a_simulated_fall():
    """The prediction agrees with integrating the trajectory."""
    sim = SimulatedOrbit(launched=True)
    # 75 km up, climbing steeply with some eastward speed: a suborbital arc.
    sim.y, sim.vx, sim.vy = KERBIN_RADIUS + 75_000, 1_200.0, 600.0
    t = sim.read_flight()
    predicted = time_to_radius(KERBIN_MU, t.semi_major_axis, t.eccentricity, t.radius, True,
                               KERBIN_RADIUS + 70_000)
    while not (sim.altitude() < 70_000 and sim.vertical_speed() < 0):
        sim.advance(0.5)
    assert predicted == pytest.approx(sim.t, abs=1.0)
    assert time_to_radius(KERBIN_MU, t.semi_major_axis, t.eccentricity, t.radius, True,
                          KERBIN_RADIUS + 900_000) is None  # never that high


def test_staging_waits_for_every_dropped_engine():
    """Boosters are dropped only when all of them burnt out."""
    srb_out = EngineState(True, False, 3)
    srb_on = EngineState(True, True, 3)
    core = EngineState(True, True, 1)
    assert should_stage_on_ascent(4, 0, [srb_out, srb_out, core])
    assert not should_stage_on_ascent(4, 0, [srb_out, srb_on, core])
    # Next stage drops nothing: only when nothing pushes at all.
    assert not should_stage_on_ascent(2, 0, [core])
    assert should_stage_on_ascent(2, 0, [EngineState(True, False, 1)])
    # The parachute stage is never activated on the way up.
    assert not should_stage_on_ascent(1, 0, [EngineState(True, False, -1)])


def test_craft_file_names_have_dots_replaced():
    """KSP saves "Passatgers v1.1" as "Passatgers v1_1.craft"."""
    files = ["Passatgers v1_0", "Passatgers v1_1"]
    assert craft_file_name("Passatgers v1.1", files) == "Passatgers v1_1"
    assert craft_file_name("Passatgers v1_0", files) == "Passatgers v1_0"
    assert craft_file_name("Passatgers v2", files) is None


def test_contract_status_from_game_names():
    """kRPC enum names map to statuses."""
    assert ContractStatus.from_name("ContractState.active") is ContractStatus.ACTIVE
    assert ContractStatus.from_name("deadline_expired") is ContractStatus.OTHER


# ------------------------------------------------------------ flight (simulated)


# Drag coefficient (per unit mass) giving a terminal velocity of 500 m/s
# at sea level, like the real 14 t tourist rocket; the air thins with a
# 5.6 km scale height, as on Kerbin.
DRAG_K = 9.8 / 500.0 ** 2
SEA_LEVEL_DENSITY = 1.2  # kg/m3


def _density(altitude: float) -> float:
    """Relative air density of the simulated atmosphere."""
    return math.exp(-altitude / 5_600.0) if altitude < 70_000 else 0.0


@dataclass
class _Engine:
    """A simulated engine: burns while its stage is lit and fuel lasts.

    Liquid engines follow the throttle (and burn fuel in proportion);
    solid boosters always burn at full power.
    """

    ignite_stage: int
    decouple_stage: int
    burn_time: float
    thrust: float = 8.0  # acceleration it gives, m/s2
    solid: bool = False
    attached: bool = True


@dataclass
class SimulatedHop(TourismGamePort):
    """A one-dimensional rocket good enough to exercise the director.

    Stages like the real craft: 4 ignites everything, 3 drops the boosters,
    2 the side engines, 1 the core, 0 opens the parachutes. The vessel has
    unit mass, so the weight is 9.8 and forces are accelerations.
    """

    t: float = 0.0
    altitude: float = 0.0
    vs: float = 0.0
    throttle: float = 0.0
    current_stage: int = 5
    chutes_open: bool = False
    staged_at: list[tuple[int, float]] = field(default_factory=list)
    warped: bool = False
    physics_warp: int = 0
    engines: list[_Engine] = field(default_factory=lambda: [
        _Engine(4, 3, 25.0, 10.0, solid=True), _Engine(4, 3, 25.0, 10.0, solid=True),
        _Engine(4, 2, 45.0, 6.0), _Engine(4, 2, 45.0, 6.0), _Engine(4, 1, 90.0, 14.0),
    ])
    control: bool = True
    throttles: list[float] = field(default_factory=list)
    rcs: list[bool] = field(default_factory=list)
    # (altitude, dynamic pressure in Pa) while climbing under power.
    climb_q: list[tuple[float, float]] = field(default_factory=list)

    # Physics -------------------------------------------------------------
    def _burning(self) -> list[_Engine]:
        """Engines lit and with fuel."""
        lit = self.current_stage <= 4
        return [e for e in self.engines if e.attached and lit and e.burn_time > 0]

    def _drag(self) -> float:
        """Drag acceleration (always positive)."""
        return DRAG_K * self.vs ** 2 * _density(self.altitude)

    def _q(self) -> float:
        """Dynamic pressure, in pascals."""
        return 0.5 * SEA_LEVEL_DENSITY * _density(self.altitude) * self.vs ** 2

    def advance(self, dt: float) -> None:
        """Move the simulation forward."""
        steps = max(1, int(dt / 0.05))
        for _ in range(steps):
            h = dt / steps
            thrust = 0.0
            for engine in self._burning():
                level = 1.0 if engine.solid else self.throttle
                thrust += engine.thrust * level
                engine.burn_time -= h * level
            drag = self._drag() * (1 if self.vs > 0 else -1)
            if thrust > 0 and self.vs > 0:
                self.climb_q.append((self.altitude, self._q()))
            self.vs += (thrust - 9.8 - drag) * h
            if self.chutes_open and self.altitude < 5_000:
                self.vs = max(self.vs, -7.0)  # under the canopies
            self.altitude = max(0.0, self.altitude + self.vs * h)
            if self.altitude == 0.0 and self.t > 5:
                self.vs = 0.0
            self.t += h

    # Port ------------------------------------------------------------------
    def read_flight(self) -> FlightTelemetry:
        reading = self._reading()
        # Heat grows with speed in the air: a rough stand-in for the game.
        return replace(reading, heat_ratio=min(1.0, self._q() / 100_000.0))

    def _reading(self) -> FlightTelemetry:
        """Telemetry without the heat."""
        apo = self.altitude + (self.vs ** 2 / 19.6 if self.vs > 0 else 0.0)
        on_ground = self.altitude == 0.0 and self.t > 5
        return FlightTelemetry(
            vessel="Hopper", altitude=self.altitude, vertical_speed=self.vs,
            situation=("pre_launch" if self.current_stage == 5 else
                       "splashed" if on_ground else
                       "sub_orbital" if self.altitude > 70_000 else "flying"),
            surface_speed=abs(self.vs), apoapsis=apo,
            time_to_apoapsis=max(self.vs / 9.8, 0.0), ut=self.t,
            current_stage=self.current_stage, parachute_stage=0, atmosphere_depth=70_000.0,
            drag=self._drag(), weight=9.8, dynamic_pressure=self._q(),
        )

    def read_engines(self) -> list[EngineState]:
        lit = self.current_stage <= 4
        return [EngineState(lit, e.burn_time > 0, e.decouple_stage)
                for e in self.engines if e.attached]



    def parachutes_safe(self) -> bool:
        return self.altitude < 20_000 and self.vs > -260

    def parachutes_open(self) -> bool:
        return self.chutes_open and self.altitude < 5_000

    def has_control(self) -> bool:
        return self.control

    def stage(self) -> None:
        self.current_stage -= 1
        self.staged_at.append((self.current_stage, self.altitude))
        for engine in self.engines:
            if engine.decouple_stage == self.current_stage:
                engine.attached = False
        if self.current_stage == 0:
            self.chutes_open = True

    def set_throttle(self, value: float) -> None:
        self.throttle = value
        self.throttles.append(value)

    def point(self, pitch: float, heading: float) -> None:
        pass

    def hold_retrograde(self) -> None:
        pass

    def set_rcs(self, on: bool) -> None:
        self.rcs.append(on)

    def release_controls(self) -> None:
        self.throttle = 0.0

    def warp_to(self, ut: float) -> None:
        self.warped = True
        self.advance(ut - self.t)

    def set_physics_warp(self, factor: int) -> None:
        self.physics_warp = factor

    def read_propulsion(self) -> Propulsion:
        left = sum(e.thrust * e.burn_time for e in self.engines if e.attached)
        return Propulsion(thrust=10.0, isp=300.0, mass=1.0, delta_v=left)

    # Unused by a suborbital hop.
    def read_offers(self): raise NotImplementedError
    def decline_contract(self, contract_id): raise NotImplementedError
    def read_crew(self): raise NotImplementedError
    def add_node(self, ut, prograde): raise NotImplementedError
    def node_remaining_dv(self): raise NotImplementedError
    def aim_at_node(self): raise NotImplementedError
    def remove_nodes(self): raise NotImplementedError
    def pointing_error(self): raise NotImplementedError
    def funds(self) -> float: raise NotImplementedError
    def read_tourism_contracts(self): raise NotImplementedError
    def accept_contract(self, contract_id): raise NotImplementedError
    def contract_status(self, contract_id): raise NotImplementedError
    def roster(self): raise NotImplementedError
    def launch(self, craft, crew): raise NotImplementedError
    def close(self) -> None: pass


def _director(sim: TourismGamePort, **profile) -> TourismFlightDirector:
    """A director whose waits advance the simulation."""
    clock = {"now": 0.0}

    def sleep(seconds: float) -> None:
        clock["now"] += seconds
        sim.advance(seconds)

    return TourismFlightDirector(sim, FlightProfile(**profile), interval=0.2,
                                 sleep=sleep, clock=lambda: clock["now"])


def test_hop_reaches_space_jettisons_and_lands():
    """The whole profile: boosters, side engines, cut-off, space, chutes."""
    sim = SimulatedHop()
    report = _director(sim, target_apoapsis=75_000).fly()
    assert report.reached_space
    assert 70_000 < report.max_altitude < 80_000
    assert report.situation == "splashed"
    stages = [stage for stage, _ in sim.staged_at]
    assert stages == [4, 3, 2, 1, 0]
    # The boosters burn out on the way up; whatever is left above the
    # parachutes is jettisoned at the latest in space; chutes armed low.
    altitude = dict(sim.staged_at)
    assert altitude[3] < 70_000
    assert altitude[2] <= altitude[1] and altitude[1] >= 70_000
    assert altitude[0] < 20_000
    assert sim.warped and sim.physics_warp == 0
    # RCS on for the flight, off once the parachutes hold the capsule.
    assert sim.rcs == [True, False]
    # Heat peaks are reported for the climb and for the way down.
    assert 0 < report.ascent_heat <= 1 and 0 < report.reentry_heat <= 1


def _peak_q(sim: SimulatedHop) -> float:
    """Highest dynamic pressure while climbing under power."""
    return max(q for _, q in sim.climb_q)


def test_air_limit_throttles_down_in_thick_air():
    """The liquid engines are throttled so the dynamic pressure stays low.

    The simulated rocket, like the real one, has a terminal velocity of
    500 m/s: the drag never reaches its weight, so only the dynamic
    pressure limit can slow it down.
    """
    limited = SimulatedHop()
    _director(limited).fly()
    free = SimulatedHop()
    _director(free, max_dynamic_pressure=0, max_drag_ratio=0).fly()
    drag_only = SimulatedHop()
    _director(drag_only, max_dynamic_pressure=0).fly()

    assert min(limited.throttles[:-1]) < 0.5      # it really throttled down
    assert all(v in (0.0, 1.0) for v in free.throttles)
    assert all(v in (0.0, 1.0) for v in drag_only.throttles)
    # Solid boosters cannot be throttled, so q may pass the limit while
    # they burn, but it stays well under the full power peak.
    assert _peak_q(limited) < 0.5 * _peak_q(free)
    assert _peak_q(free) > 60_000


def test_air_limited_throttle_rule():
    """Integral rule on the worst limit: down above it, up below, clamped."""
    from ksp_control.domain.tourism import air_limited_throttle

    def rule(previous, q=0.0, drag=0.0, max_q=20_000.0, max_drag=1.0, dt=0.2):
        return air_limited_throttle(previous, q, max_q, drag, 1.0, max_drag, dt)

    assert rule(1.0, q=40_000) == pytest.approx(0.8)          # q twice the limit
    assert rule(1.0, drag=2.0) == pytest.approx(0.8)          # drag twice the weight
    assert rule(1.0, q=40_000, drag=3.0) == pytest.approx(0.6)  # the worst one counts
    assert rule(0.5, q=10_000) == pytest.approx(0.6)          # thin air: back up
    assert rule(0.12, q=200_000, dt=1.0) == 0.1               # never below the floor
    assert rule(0.99, dt=1.0) == 1.0
    assert rule(0.3, q=90_000, drag=9.0, max_q=0, max_drag=0) == 1.0  # both disabled
    # The heat limit: hottest part at 96% with 80% allowed is 1.2 times over.
    assert air_limited_throttle(1.0, 0.0, 20_000.0, 0.0, 1.0, 1.0, 0.2,
                                heat_ratio=0.96, max_heat_ratio=0.8) == pytest.approx(0.96)
    assert air_limited_throttle(1.0, 0.0, 20_000.0, 0.0, 1.0, 1.0, 0.2,
                                heat_ratio=0.96, max_heat_ratio=0.0) == 1.0


class _EvaHop(SimulatedHop):
    """A hop during which the player goes on EVA for a while in space."""

    eva: tuple[float, float] = (0.0, 0.0)  # (start, end) simulated time
    stages_during_eva: int = 0

    def read_flight(self) -> FlightTelemetry:
        reading = super().read_flight()
        start, end = self.eva
        if start <= self.t < end:
            return replace(reading, vessel="Jebediah Kerman")
        return reading

    def stage(self) -> None:
        start, end = self.eva
        if start <= self.t < end:
            self.stages_during_eva += 1
        super().stage()


def test_no_commands_while_the_player_is_on_another_vessel():
    """An EVA in space: the director waits, then finishes the flight.

    Without the EVA the simulated rocket leaves the atmosphere and
    jettisons its last stages about 128 s after launch; the EVA (60-180 s)
    covers that moment, so the jettison must wait until it ends.
    """
    sim = _EvaHop()
    sim.eva = (60.0, 180.0)
    stage = sim.stage
    stage_times: list[float] = []

    def timed_stage() -> None:
        stage_times.append(sim.t)
        stage()

    sim.stage = timed_stage
    report = _director(sim, warp=False).fly()
    assert report.reached_space and report.situation == "splashed"
    assert sim.stages_during_eva == 0
    assert [stage for stage, _ in sim.staged_at] == [4, 3, 2, 1, 0]
    assert stage_times[2] >= 180.0  # jettison delayed until the EVA ended


def test_hop_refuses_a_vessel_without_control():
    """Without a pilot in the pod the rocket cannot be flown."""
    with pytest.raises(FlightError, match="cannot be controlled"):
        _director(SimulatedHop(control=False)).fly()


def test_hop_that_cannot_reach_space_still_lands():
    """Out of fuel below 70 km: chutes anyway, reported as not in space."""
    sim = SimulatedHop(engines=[_Engine(4, 1, 15.0, 20.0)])
    report = _director(sim).fly()
    assert not report.reached_space
    assert report.situation == "splashed"
    assert sim.chutes_open


# ------------------------------------------------------------ campaign


class FakeGame(TourismGamePort):
    """Space center side of the game: contracts, roster and funds."""

    def __init__(self, contracts: list[TourismContract], funds: float = 100_000.0,
                 complete: bool = True, capacity: int = 5) -> None:
        self.contracts = {c.contract_id: c for c in contracts}
        self._funds = funds
        self.complete = complete
        self.capacity = capacity  # pilot seat included
        self.launches: list[tuple[str, list[str]]] = []
        self.aboard: list[str] = []
        # Offers that are not tourism, and what KSP offers after a decline.
        self.other_offers: list[OfferedContract] = []
        self.replacements: list[TourismContract | OfferedContract] = []
        self.declined: list[str] = []
        self.pilots = [Kerbal("Jebediah Kerman", "crew", "Pilot", True),
                       Kerbal("Valentina Kerman", "crew", "Pilot", True)]

    def funds(self) -> float:
        return self._funds

    def read_tourism_contracts(self) -> list[TourismContract]:
        return [c for c in self.contracts.values()
                if c.status in (ContractStatus.OFFERED, ContractStatus.ACTIVE)]

    def accept_contract(self, contract_id: str) -> None:
        c = self.contracts[contract_id]
        self.contracts[contract_id] = replace(c, status=ContractStatus.ACTIVE)

    def contract_status(self, contract_id: str) -> ContractStatus:
        return self.contracts[contract_id].status

    def read_offers(self) -> list[OfferedContract]:
        tourism = [OfferedContract(c.contract_id, c.title, "FinePrint.Contracts.TourismContract", True)
                   for c in self.contracts.values() if c.status is ContractStatus.OFFERED]
        return self.other_offers + tourism

    def decline_contract(self, contract_id: str) -> None:
        self.declined.append(contract_id)
        self.other_offers = [o for o in self.other_offers if o.contract_id != contract_id]
        if self.replacements:  # KSP fills the free offer slot
            new = self.replacements.pop(0)
            if isinstance(new, OfferedContract):
                self.other_offers.append(new)
            else:
                self.contracts[new.contract_id] = new

    def roster(self) -> list[Kerbal]:
        # Delivered tourists stay at the space center until their contract
        # ends: only the itinerary flag tells they already flew.
        tourists = [Kerbal(t.kerbal, "tourist", "Tourist", True)
                    for c in self.contracts.values() if c.status is ContractStatus.ACTIVE
                    for t in c.tourists]
        return self.pilots + tourists

    def launch(self, craft: str, crew: list[str]) -> str:
        self.launches.append((craft, crew))
        self.aboard = crew[:self.capacity]  # who does not fit stays behind
        self._funds -= 12_000  # the vessel is paid at launch
        return "Passatgers v1.1"

    def finish_flight(self) -> None:
        """What recovering the tourists does: complete and pay.

        Each tourist on board has their itinerary completed and paid; a
        contract is completed once all its tourists are.
        """
        if not self.complete:
            return
        on_board = set(self.aboard)
        for cid, c in list(self.contracts.items()):
            if c.status is not ContractStatus.ACTIVE:
                continue
            tourists = []
            for t in c.tourists:
                if not t.completed and t.kerbal in on_board:
                    self._funds += t.reward
                    t = replace(t, completed=True)
                tourists.append(t)
            status = (ContractStatus.COMPLETED if all(t.completed for t in tourists)
                      else ContractStatus.ACTIVE)
            self.contracts[cid] = replace(c, status=status, tourists=tuple(tourists))

    def read_crew(self) -> tuple[int, list[str]]:
        return self.capacity, list(self.aboard)

    def read_flight(self) -> FlightTelemetry | None:
        return self.flight

    flight: FlightTelemetry | None = None

    def read_engines(self): raise NotImplementedError
    def parachutes_safe(self): raise NotImplementedError
    def parachutes_open(self): raise NotImplementedError
    def has_control(self): raise NotImplementedError
    def stage(self): raise NotImplementedError
    def set_throttle(self, value): raise NotImplementedError
    def point(self, pitch, heading): raise NotImplementedError
    def hold_retrograde(self): raise NotImplementedError
    def release_controls(self): raise NotImplementedError
    def warp_to(self, ut): raise NotImplementedError
    def set_physics_warp(self, factor): raise NotImplementedError
    def read_propulsion(self): raise NotImplementedError
    def add_node(self, ut, prograde): raise NotImplementedError
    def node_remaining_dv(self): raise NotImplementedError
    def aim_at_node(self): raise NotImplementedError
    def remove_nodes(self): raise NotImplementedError
    def pointing_error(self): raise NotImplementedError
    def set_rcs(self, on): raise NotImplementedError
    def close(self) -> None: pass


class FakeRecovery:
    """Stands in for ``VesselRecoveryService``; recovering pays the game."""

    def __init__(self, game: FakeGame, vessels: list[str]) -> None:
        self.game = game
        self.vessels = vessels
        self.recovered: list[str] = []

    def plan(self, policy: RecoveryPolicy) -> list[RecoveryDecision]:
        reports = [VesselReport(str(i), name, "ship", "splashed", "Kerbin", 600_000.0, 0, 0,
                                True, i == 0, 0, None)
                   for i, name in enumerate(self.vessels)]
        return [RecoveryDecision(r, True, "ok") for r in reports]

    def execute(self, decisions: list[RecoveryDecision]) -> list[RecoveryOutcome]:
        outcomes = []
        for d in decisions:
            if d.recover:
                self.recovered.append(d.vessel.name)
                if d.vessel.is_active:
                    self.game._funds += 10_000  # the vessel's value
                    self.game.finish_flight()
                outcomes.append(RecoveryOutcome(d, True, 1_000.0, "recovered"))
        return outcomes

    def close(self) -> None:
        pass


class FakeFlight:
    """Stands in for ``TourismFlightDirector.fly``; remembers each order."""

    def __init__(self, reached_space: bool = True, reached_orbit: bool = True) -> None:
        self.reached_space = reached_space
        self.reached_orbit = reached_orbit
        self.missions: list[Mission] = []
        self.checks: list = []
        self.hop_helps: list[bool] = []
        self.short_of_delta_v = False

    def __call__(self, mission: Mission, orbit_visited, hop_helps: bool = True) -> FlightReport:
        self.missions.append(mission)
        self.checks.append(orbit_visited)
        self.hop_helps.append(hop_helps)
        if self.short_of_delta_v and mission is Mission.ORBITAL and not hop_helps:
            raise NotEnoughDeltaVError("Passatgers v1.1 has 3,269 m/s of delta-v")
        orbit = self.reached_orbit and mission is Mission.ORBITAL
        return FlightReport("Passatgers v1.1", self.reached_space, 75_000.0, "splashed",
                            reached_orbit=orbit, delta_v_orbit=None if orbit else 120.0)


def _campaign(game: FakeGame, target: float, reached_space: bool = True,
              flight: FakeFlight | None = None, **options):
    """A campaign with a fake flight and recovery."""
    recovery = FakeRecovery(game, ["Passatgers v1.1", "Escombros de Passatgers v1.1", "Rover"])
    fly = flight or FakeFlight(reached_space)
    campaign = TourismCampaign(game, fly, recovery,
                               CampaignOptions("Passatgers v1.1", target, **options),
                               sleep=lambda s: None)
    return campaign, recovery


def _offers() -> list[TourismContract]:
    """The three offers seen in the real game."""
    return [contract("p1", ["Samroda Kerman", "Robala Kerman"]),
            contract("p2", ["Rongera Kerman", "Halponda Kerman"]),
            contract("vip", ["Samemy Kerman"])]


def test_campaign_flies_until_no_contract_is_left():
    """Two pairs first, then the VIP; pilots take turns."""
    game = FakeGame(_offers())
    campaign, recovery = _campaign(game, target=1_000_000)
    result = campaign.run()
    assert result.reason is StopReason.NO_CONTRACTS
    assert [crew[0] for _, crew in game.launches] == ["Jebediah Kerman", "Valentina Kerman"]
    assert sorted(game.launches[0][1][1:]) == sorted(
        ["Samroda Kerman", "Robala Kerman", "Rongera Kerman", "Halponda Kerman"])
    assert game.launches[1][1][1:] == ["Samemy Kerman"]
    # Rewards 52,000 + 13,000, two launches of 12,000, two recoveries of 10,000.
    assert result.end_funds - result.start_funds == 65_000 - 2 * 2_000
    first = result.flights[0]
    assert (first.launch_cost, first.contract_rewards) == (12_000, 52_000)
    assert first.vessel_recovery == 10_000 and first.net == 50_000
    # Only this craft and its debris are recovered, never the rover.
    assert "Rover" not in recovery.recovered
    assert (first.tourists, first.delivered, first.completed, first.waiting) == (4, 4, 2, 0)


def test_campaign_flies_a_contract_bigger_than_the_craft_in_two_flights():
    """Five tourists, four seats: the fifth flies next, with other tourists."""
    game = FakeGame([contract("five", five()), contract("trio", ["E Kerman", "F Kerman",
                                                                "G Kerman"])])
    result = _campaign(game, target=1_000_000)[0].run()
    assert result.reason is StopReason.NO_CONTRACTS
    assert [crew[1:] for _, crew in game.launches] == [
        five()[:4], ["Chutona Kerman", "E Kerman", "F Kerman", "G Kerman"]]
    first, second = result.flights
    # The big contract stays open after the first flight, paying per tourist.
    assert (first.delivered, first.completed, first.waiting) == (4, 0, 1)
    assert first.contract_rewards == 4 * 13_000
    assert (second.delivered, second.completed, second.waiting) == (4, 2, 0)
    assert second.contract_rewards == 4 * 13_000
    assert all(c.status is ContractStatus.COMPLETED for c in game.contracts.values())


def test_campaign_learns_the_seats_of_a_bigger_craft():
    """Four seats assumed for the first flight, six from then on."""
    four = ["A Kerman", "B Kerman", "C Kerman", "D Kerman"]
    six = ["E Kerman", "F Kerman", "G Kerman", "H Kerman", "I Kerman", "J Kerman"]
    game = FakeGame([contract("four", four), contract("six", six)], capacity=7)
    campaign = _campaign(game, target=1_000_000)[0]
    assert campaign.seats == 4
    result = campaign.run()
    assert [len(crew) - 1 for _, crew in game.launches] == [4, 6]
    assert campaign.seats == 6 and len(result.flights) == 2


def test_campaign_flies_who_fits_in_a_smaller_craft():
    """Two seats, four boarded: two fly, two wait for the next flight."""
    four = ["A Kerman", "B Kerman", "C Kerman", "D Kerman"]
    game = FakeGame([contract("four", four)], capacity=3)
    result = _campaign(game, target=1_000_000)[0].run()
    first, second = result.flights
    assert (first.tourists, first.delivered, first.completed) == (2, 2, 0)
    assert (second.tourists, second.delivered, second.completed) == (2, 2, 1)
    assert game.launches[1][1][1:] == ["C Kerman", "D Kerman"]


def test_campaign_reports_the_flight_that_stops_it():
    """The summary is shown even when a check stops the campaign."""
    game = FakeGame(_offers(), complete=False)
    shown = []
    with pytest.raises(CampaignError):
        _campaign(game, target=1_000_000)[0].run(on_flight=shown.append)
    assert len(shown) == 1 and shown[0].delivered == 0


def _part_test(cid: str) -> OfferedContract:
    """A "test a part" offer."""
    return OfferedContract(cid, f"Prueba {cid}", "Contracts.Templates.PartTest", True)


def test_campaign_declines_part_tests_one_at_a_time_until_the_seats_are_full():
    """Decline, look again, decline again: KSP may offer another part test."""
    from ksp_control.domain.tourism import PART_TEST_TYPE

    game = FakeGame([contract("pair", ["A Kerman", "B Kerman"])])
    game.other_offers = [_part_test("pt1"), _part_test("pt2"), _part_test("pt3"),
                         OfferedContract("sat", "Satellite", "FinePrint.Contracts.SatelliteContract", True)]
    game.replacements = [_part_test("pt9"), contract("new", ["C Kerman", "D Kerman"])]
    _campaign(game, target=1_000_000, max_flights=1, decline_types=(PART_TEST_TYPE,))[0].run()
    # The first decline brought another part test, the second the tourists:
    # then the seats were full, so pt3 and pt9 stay, and never the satellite.
    assert game.declined == ["pt1", "pt2"]
    assert sorted(game.launches[0][1][1:]) == ["A Kerman", "B Kerman", "C Kerman", "D Kerman"]


def test_campaign_stops_declining_after_the_limit():
    """KSP offering part tests over and over does not drain the reputation."""
    from ksp_control.domain.tourism import PART_TEST_TYPE

    game = FakeGame([contract("pair", ["A Kerman", "B Kerman"])])
    game.other_offers = [_part_test("pt0")]
    game.replacements = [_part_test(f"pt{i}") for i in range(1, 20)]
    _campaign(game, target=1_000_000, max_flights=1, decline_types=(PART_TEST_TYPE,),
              max_declines=3)[0].run()
    assert len(game.declined) == 3 and len(game.launches) == 1


def test_campaign_stops_when_declining_brings_nobody():
    """No tourist at all and nothing left to decline: stop, do not fly empty."""
    from ksp_control.domain.tourism import PART_TEST_TYPE

    game = FakeGame([])
    game.other_offers = [_part_test("pt1"), _part_test("pt2")]
    result = _campaign(game, target=1_000_000, decline_types=(PART_TEST_TYPE,))[0].run()
    assert result.reason is StopReason.NO_CONTRACTS
    assert game.declined == ["pt1", "pt2"] and game.launches == []


def test_campaign_declines_nothing_when_the_seats_are_full_or_it_is_off():
    """Declining costs reputation: only when it can fill seats, only if asked."""
    from ksp_control.domain.tourism import PART_TEST_TYPE

    full = FakeGame([contract("four", ["A Kerman", "B Kerman", "C Kerman", "D Kerman"])])
    full.other_offers = [_part_test("pt1")]
    _campaign(full, target=1_000_000, max_flights=1, decline_types=(PART_TEST_TYPE,))[0].run()
    off = FakeGame([contract("pair", ["A Kerman", "B Kerman"])])
    off.other_offers = [_part_test("pt1")]
    _campaign(off, target=1_000_000, max_flights=1)[0].run()
    assert full.declined == [] and off.declined == []


def test_campaign_flies_anyway_when_no_new_tourists_come():
    """After the wait, the flight goes with the tourists there are."""
    from ksp_control.domain.tourism import PART_TEST_TYPE

    game = FakeGame([contract("pair", ["A Kerman", "B Kerman"])])
    game.other_offers = [_part_test("pt1")]
    _campaign(game, target=1_000_000, max_flights=1, decline_types=(PART_TEST_TYPE,))[0].run()
    assert game.declined == ["pt1"]
    assert sorted(game.launches[0][1][1:]) == ["A Kerman", "B Kerman"]


def test_campaign_stops_at_the_target():
    """Once the funds reach the target no more flights are made."""
    game = FakeGame(_offers(), funds=100_000)
    result = _campaign(game, target=130_000)[0].run()
    assert result.reason is StopReason.TARGET_REACHED
    assert len(result.flights) == 1


def test_campaign_respects_the_flight_limit():
    """--max-flights stops the loop."""
    result = _campaign(FakeGame(_offers()), target=1_000_000, max_flights=1)[0].run()
    assert result.reason is StopReason.FLIGHT_LIMIT and len(result.flights) == 1


def test_campaign_stops_when_contracts_are_not_completed():
    """Something went wrong in the game: do not fly again blindly."""
    game = FakeGame(_offers(), complete=False)
    with pytest.raises(CampaignError, match="itinerary"):
        _campaign(game, target=1_000_000)[0].run()


def test_campaign_stops_when_the_craft_cannot_reach_space():
    """A craft that falls short would fail every flight the same way."""
    with pytest.raises(CampaignError, match="space"):
        _campaign(FakeGame(_offers()), target=1_000_000, reached_space=False)[0].run()


def test_campaign_never_launches_while_a_vessel_is_flying():
    """Launching would abandon the flying vessel (and its tourists)."""
    game = FakeGame(_offers())
    game.flight = FlightTelemetry("Passatgers v1.1", "flying", 5_000, 100, 100, 20_000, 30,
                                  0.0, 4, 0, 70_000)
    with pytest.raises(CampaignError, match="in flight"):
        _campaign(game, target=1_000_000)[0].run()
    assert not game.launches
    assert all(c.status is ContractStatus.OFFERED for c in game.contracts.values())


def _orbital_offers() -> list[TourismContract]:
    """An orbital pair and a suborbital pair, like in the real game."""
    return [contract("orb", ["Duddreda Kerman", "Dansy Kerman"], reward=64_998,
                     destinations=(SUBORBITAL, ORBIT)),
            contract("hop", ["Sigorio Kerman", "Macbuno Kerman"])]


def test_campaign_flies_to_orbit_when_a_tourist_wants_it():
    """Both pairs together, on an orbital flight; the orbit check reads flags."""
    game = FakeGame(_orbital_offers())
    flight = FakeFlight()
    result = _campaign(game, target=1_000_000, flight=flight, max_flights=1)[0].run()
    assert flight.missions == [Mission.ORBITAL]
    assert result.flights[0].mission is Mission.ORBITAL
    assert sorted(game.launches[0][1][1:]) == sorted(
        ["Duddreda Kerman", "Dansy Kerman", "Sigorio Kerman", "Macbuno Kerman"])
    # The check given to the flight follows the game's destination flags.
    check = flight.checks[0]
    game.contracts["orb"] = replace(game.contracts["orb"], status=ContractStatus.ACTIVE)
    assert not check()
    orb = game.contracts["orb"]
    game.contracts["orb"] = replace(orb, tourists=tuple(replace(t, visited=(ORBIT,))
                                                        for t in orb.tourists))
    assert check()


def test_campaign_orbit_test_boards_only_suborbital_tourists():
    """No contract is at risk while the craft is being checked."""
    game = FakeGame(_orbital_offers())
    flight = FakeFlight()
    _campaign(game, target=1_000_000, flight=flight, max_flights=1, orbit_test=True)[0].run()
    assert flight.missions == [Mission.ORBITAL]
    assert sorted(game.launches[0][1][1:]) == ["Macbuno Kerman", "Sigorio Kerman"]
    assert game.contracts["orb"].status is ContractStatus.OFFERED


def test_campaign_suborbital_only_leaves_orbital_contracts():
    """--suborbital-only: hops only, orbital offers untouched."""
    game = FakeGame(_orbital_offers())
    flight = FakeFlight()
    _campaign(game, target=1_000_000, flight=flight, max_mission=Mission.SUBORBITAL)[0].run()
    assert flight.missions == [Mission.SUBORBITAL]
    assert game.contracts["orb"].status is ContractStatus.OFFERED


def test_campaign_stays_on_the_pad_when_a_hop_helps_nobody():
    """Orbital tourists already went suborbital: no pointless hop."""
    went_suborbital = [replace(t, visited=(SUBORBITAL,))
                       for t in _orbital_offers()[0].tourists]
    orbital = replace(_orbital_offers()[0], status=ContractStatus.ACTIVE,
                      tourists=tuple(went_suborbital))
    game = FakeGame([orbital], complete=False)  # a pad recovery completes nothing
    flight = FakeFlight()
    flight.short_of_delta_v = True
    campaign, recovery = _campaign(game, target=1_000_000, flight=flight)
    with pytest.raises(CampaignError, match="recovered from the launch pad"):
        campaign.run()
    assert flight.hop_helps == [False]
    assert "Passatgers v1.1" in recovery.recovered and "Rover" not in recovery.recovered
    # Their contract is still open for a better craft.
    assert game.contracts["orb"].status is ContractStatus.ACTIVE


def test_campaign_hops_when_it_still_helps_someone():
    """A tourist who never went suborbital still gains from a hop."""
    game = FakeGame(_orbital_offers())
    flight = FakeFlight()
    flight.short_of_delta_v = True
    _campaign(game, target=1_000_000, flight=flight, max_flights=1)[0].run()
    assert flight.hop_helps == [True]


def test_campaign_stops_when_an_orbital_flight_misses_the_orbit():
    """The craft needs work: do not fail more orbital contracts."""
    game = FakeGame(_orbital_offers())
    with pytest.raises(CampaignError, match="did not reach orbit"):
        _campaign(game, target=1_000_000, flight=FakeFlight(reached_orbit=False),
                  orbit_test=True)[0].run()


# ------------------------------------------------------------ orbital flight (simulated)


@dataclass
class SimulatedOrbit(TourismGamePort):
    """A rocket flying in a plane around a non-rotating Kerbin.

    Point mass of unit mass (forces are accelerations), inverse-square
    gravity, drag in the same exponential atmosphere as ``SimulatedHop`` and
    a perfect autopilot. Stages like the real craft: 4 ignites everything,
    3 drops the boosters, 2 the side engines, 1 the core, 0 opens the
    parachutes. A maneuver node is flown by thrusting along (or against)
    the velocity until its delta-v is delivered. The orbital elements come
    from the state vector, independently of the code under test.
    """

    core_burn: float = 200.0
    launched: bool = False
    t: float = 0.0
    x: float = 0.0
    y: float = KERBIN_RADIUS
    vx: float = 0.0
    vy: float = 0.0
    throttle: float = 0.0
    current_stage: int = 5
    chutes_open: bool = False
    landed: bool = False
    mode: str = "pitch"
    pitch: float = 90.0
    node: list | None = None  # [ut, prograde delta-v, delivered]
    burns: list[float] = field(default_factory=list)  # prograde delta-v of each node
    staged_at: list[tuple[int, float]] = field(default_factory=list)
    max_periapsis: float = -KERBIN_RADIUS
    pitches: list[tuple[int, float]] = field(default_factory=list)  # (stage, pitch)
    engines: list[_Engine] = field(default_factory=list)

    def __post_init__(self) -> None:
        """Fit the engines; the core burn sets how much delta-v there is."""
        self.engines = [
            _Engine(4, 3, 30.0, 10.0, solid=True), _Engine(4, 3, 30.0, 10.0, solid=True),
            _Engine(4, 2, 60.0, 6.0), _Engine(4, 2, 60.0, 6.0),
            _Engine(4, 1, self.core_burn, 14.0),
        ]
        if self.launched:
            self.current_stage = 2
            for engine in self.engines[:4]:
                engine.attached = False

    # Physics -------------------------------------------------------------
    def altitude(self) -> float:
        """Altitude above the surface."""
        return math.hypot(self.x, self.y) - KERBIN_RADIUS

    def vertical_speed(self) -> float:
        """Speed away from the centre."""
        return (self.x * self.vx + self.y * self.vy) / math.hypot(self.x, self.y)

    def _burning(self) -> list[_Engine]:
        """Engines lit and with fuel."""
        lit = self.current_stage <= 4
        return [e for e in self.engines if e.attached and lit and e.burn_time > 0]

    def advance(self, dt: float) -> None:
        """Move the simulation forward (semi-implicit Euler)."""
        if self.current_stage == 5:  # held on the pad
            self.t += dt
            return
        left = dt
        while left > 1e-9:
            altitude = self.altitude()
            fine = self.throttle > 0 or altitude < 72_000 or self._burning() and any(
                e.solid for e in self._burning())
            h = min(left, 0.05 if fine else 1.0)
            left -= h
            self.t += h
            if self.landed:
                continue
            r = math.hypot(self.x, self.y)
            up = (self.x / r, self.y / r)
            east = (up[1], -up[0])
            speed = math.hypot(self.vx, self.vy)
            along = (self.vx / speed, self.vy / speed) if speed > 0 else up
            if self.mode == "node" and self.node is not None:
                direction = along if self.node[1] >= 0 else (-along[0], -along[1])
            elif self.mode == "retro":
                direction = (-along[0], -along[1])
            else:
                p = math.radians(self.pitch)
                direction = (math.cos(p) * east[0] + math.sin(p) * up[0],
                             math.cos(p) * east[1] + math.sin(p) * up[1])
            thrust = 0.0
            for engine in self._burning():
                level = 1.0 if engine.solid else self.throttle
                thrust += engine.thrust * level
                engine.burn_time -= h * level
            if self.node is not None and self.mode == "node":
                self.node[2] += thrust * h
            gravity = KERBIN_MU / r ** 2
            drag = DRAG_K * speed ** 2 * _density(altitude)
            self.vx += (thrust * direction[0] - gravity * up[0] - drag * along[0]) * h
            self.vy += (thrust * direction[1] - gravity * up[1] - drag * along[1]) * h
            if self.chutes_open and altitude < 5_000:
                speed = math.hypot(self.vx, self.vy)
                if speed > 7.0:  # under the canopies
                    self.vx, self.vy = self.vx * 7.0 / speed, self.vy * 7.0 / speed
            self.x += self.vx * h
            self.y += self.vy * h
            if math.hypot(self.x, self.y) <= KERBIN_RADIUS and self.t > 5:
                self.landed = True
                self.vx = self.vy = 0.0
            self.max_periapsis = max(self.max_periapsis, self._elements()["periapsis"])

    def _elements(self) -> dict[str, float]:
        """Orbital elements of the current state vector."""
        r = math.hypot(self.x, self.y)
        speed2 = self.vx ** 2 + self.vy ** 2
        energy = speed2 / 2 - KERBIN_MU / r
        if energy >= 0 or speed2 == 0:
            return dict(a=r / 2, e=1.0, apoapsis=self.altitude(), periapsis=-KERBIN_RADIUS,
                        to_apo=0.0, to_peri=0.0, period=0.0)
        a = -KERBIN_MU / (2 * energy)
        h = self.x * self.vy - self.y * self.vx
        e = math.sqrt(max(0.0, 1 + 2 * energy * h ** 2 / KERBIN_MU ** 2))
        n = math.sqrt(KERBIN_MU / a ** 3)
        if e < 1e-9:
            mean = 0.0
        else:
            cos_nu = max(-1.0, min(1.0, (a * (1 - e ** 2) / r - 1) / e))
            nu = math.acos(cos_nu)
            if self.vertical_speed() < 0:
                nu = 2 * math.pi - nu
            ecc = 2 * math.atan2(math.sqrt(1 - e) * math.sin(nu / 2),
                                 math.sqrt(1 + e) * math.cos(nu / 2))
            mean = (ecc - e * math.sin(ecc)) % (2 * math.pi)
        return dict(a=a, e=e, apoapsis=a * (1 + e) - KERBIN_RADIUS,
                    periapsis=a * (1 - e) - KERBIN_RADIUS,
                    to_apo=((math.pi - mean) % (2 * math.pi)) / n,
                    to_peri=((2 * math.pi - mean) % (2 * math.pi)) / n,
                    period=2 * math.pi / n)

    # Port ------------------------------------------------------------------
    def read_flight(self) -> FlightTelemetry:
        altitude = self.altitude()
        el = self._elements()
        speed = math.hypot(self.vx, self.vy)
        r = math.hypot(self.x, self.y)
        situation = ("pre_launch" if self.current_stage == 5 else
                     "splashed" if self.landed else
                     "flying" if altitude < 70_000 else
                     "orbiting" if el["periapsis"] > 70_000 else "sub_orbital")
        return FlightTelemetry(
            vessel="Orbiter", situation=situation, altitude=altitude,
            vertical_speed=self.vertical_speed(), surface_speed=speed,
            apoapsis=el["apoapsis"], time_to_apoapsis=el["to_apo"], ut=self.t,
            current_stage=self.current_stage, parachute_stage=0, atmosphere_depth=70_000.0,
            drag=DRAG_K * speed ** 2 * _density(altitude), weight=KERBIN_MU / r ** 2,
            dynamic_pressure=0.5 * SEA_LEVEL_DENSITY * _density(altitude) * speed ** 2,
            periapsis=el["periapsis"], semi_major_axis=el["a"], eccentricity=el["e"],
            radius=r, time_to_periapsis=el["to_peri"], period=el["period"], mu=KERBIN_MU,
            body_radius=KERBIN_RADIUS, thrust=sum(e.thrust for e in self._burning()), mass=1.0,
        )

    def read_engines(self) -> list[EngineState]:
        lit = self.current_stage <= 4
        return [EngineState(lit, e.burn_time > 0, e.decouple_stage)
                for e in self.engines if e.attached]

    def read_propulsion(self) -> Propulsion:
        left = sum(e.thrust * max(e.burn_time, 0.0) for e in self.engines if e.attached)
        thrust = sum(e.thrust for e in self.engines if e.attached and e.burn_time > 0)
        return Propulsion(thrust=thrust, isp=300.0, mass=1.0, delta_v=left)

    def parachutes_safe(self) -> bool:
        return self.altitude() < 20_000 and math.hypot(self.vx, self.vy) < 300

    def parachutes_open(self) -> bool:
        return self.chutes_open and self.altitude() < 5_000

    def has_control(self) -> bool:
        return True

    def stage(self) -> None:
        self.current_stage -= 1
        self.staged_at.append((self.current_stage, self.altitude()))
        for engine in self.engines:
            if engine.decouple_stage == self.current_stage:
                engine.attached = False
        if self.current_stage == 0:
            self.chutes_open = True

    def set_throttle(self, value: float) -> None:
        self.throttle = value

    def point(self, pitch: float, heading: float) -> None:
        self.mode, self.pitch = "pitch", pitch
        self.pitches.append((self.current_stage, pitch))

    def hold_retrograde(self) -> None:
        self.mode = "retro"

    def set_rcs(self, on: bool) -> None:
        pass

    def release_controls(self) -> None:
        self.throttle = 0.0

    def warp_to(self, ut: float) -> None:
        self.advance(ut - self.t)

    def set_physics_warp(self, factor: int) -> None:
        pass

    def add_node(self, ut: float, prograde: float) -> None:
        self.node = [ut, prograde, 0.0]
        self.burns.append(prograde)

    def node_remaining_dv(self) -> float | None:
        return None if self.node is None else abs(abs(self.node[1]) - self.node[2])

    def aim_at_node(self) -> None:
        self.mode = "node"

    def remove_nodes(self) -> None:
        self.node = None

    def pointing_error(self) -> float:
        return 0.0

    # Unused by the director.
    def read_offers(self): raise NotImplementedError
    def decline_contract(self, contract_id): raise NotImplementedError
    def read_crew(self): raise NotImplementedError
    def funds(self) -> float: raise NotImplementedError
    def read_tourism_contracts(self): raise NotImplementedError
    def accept_contract(self, contract_id): raise NotImplementedError
    def contract_status(self, contract_id): raise NotImplementedError
    def roster(self): raise NotImplementedError
    def launch(self, craft, crew): raise NotImplementedError
    def close(self) -> None: pass


def test_orbital_flight_circularizes_waits_deorbits_and_lands():
    """The whole orbital profile, in order."""
    sim = SimulatedOrbit()
    checks = []

    def orbit_counted() -> bool:
        checks.append(sim.read_flight().situation)
        return len(checks) >= 3  # the game takes a moment

    report = _director(sim).fly(Mission.ORBITAL, orbit_counted)
    assert report.reached_space and report.reached_orbit
    assert report.situation == "splashed"
    assert sim.max_periapsis > 70_000
    # Circularization (prograde) then deorbit (retrograde, small).
    assert len(sim.burns) == 2 and 100 < sim.burns[0] < 1_500 and -60 < sim.burns[1] < 0
    # The orbit is waited for while orbiting.
    assert checks and set(checks) == {"orbiting"}
    # The core is only dropped after the deorbit burn, in space.
    altitude = dict(sim.staged_at)
    assert [stage for stage, _ in sim.staged_at] == [4, 3, 2, 1, 0]
    assert altitude[1] > 70_000 and altitude[0] < 20_000
    assert report.delta_v_orbit is not None and report.delta_v_orbit > 39.0


def test_orbital_flight_short_of_delta_v_stays_on_the_pad_if_a_hop_helps_nobody():
    """Nothing is ignited when the flight would reach no destination."""
    sim = SimulatedOrbit(core_burn=60.0)
    with pytest.raises(NotEnoughDeltaVError, match="not enough for an orbit"):
        _director(sim).fly(Mission.ORBITAL, lambda: True, hop_helps=False)
    assert sim.current_stage == 5 and sim.staged_at == []


def test_orbital_flight_short_of_delta_v_on_the_pad_flies_a_hop():
    """Too little delta-v for an orbit: straight up, so space is reached."""
    sim = SimulatedOrbit(core_burn=60.0)  # 2,160 m/s, like the real v1.2
    report = _director(sim).fly(Mission.ORBITAL, lambda: True)
    assert report.reached_space and not report.reached_orbit
    assert report.orbit_delta_v_needed == 3_600 and report.delta_v_launch == pytest.approx(2_160)
    assert report.situation == "splashed" and sim.burns == []


def test_orbital_flight_without_enough_delta_v_stays_suborbital():
    """Never circularize without the fuel to come back."""
    sim = SimulatedOrbit(core_burn=110.0)
    # The pad check is off, to reach the check made before circularizing.
    report = _director(sim, orbit_delta_v=0).fly(Mission.ORBITAL, lambda: True)
    assert report.reached_space and not report.reached_orbit
    assert report.situation == "splashed"
    assert sim.burns == [] and sim.max_periapsis < 70_000


def test_turn_after_first_stage_and_periapsis_top_up():
    """Vertical until the boosters drop; the short circularization is topped up.

    Climbing straight up for longer leaves the rocket on a steeper path:
    in this simulation the circularization burn, centred on the apoapsis,
    leaves the periapsis just inside the atmosphere, and the top-up burn
    lifts it out.
    """
    sim = SimulatedOrbit()
    report = _director(sim, turn_after_first_stage=True).fly(Mission.ORBITAL, lambda: True)
    assert report.reached_orbit and report.situation == "splashed"
    # Straight up while the boosters (stage 4) are on.
    assert all(pitch == 90.0 for stage, pitch in sim.pitches if stage >= 4)
    assert any(pitch < 90.0 for stage, pitch in sim.pitches if stage < 4)
    # Circularization, top-up, deorbit.
    assert len(sim.burns) == 3 and sim.burns[1] > 0 > sim.burns[2]


@dataclass
class _NoTopUpOrbit(SimulatedOrbit):
    """After the first burn, KSP's delta-v figure leaves nothing for a top-up."""

    nodes_done: int = 0

    def remove_nodes(self) -> None:
        self.nodes_done += 1
        super().remove_nodes()

    def read_propulsion(self) -> Propulsion:
        reading = super().read_propulsion()
        return replace(reading, delta_v=100.0) if self.nodes_done else reading


def test_grazing_periapsis_is_lowered_before_jettisoning():
    """No orbit and a periapsis just under 70 km: deorbit, then jettison.

    The circularization comes out short (see the test above) and nothing
    is left for a top-up: the vessel would otherwise skim the atmosphere.
    """
    sim = _NoTopUpOrbit()
    report = _director(sim, turn_after_first_stage=True).fly(Mission.ORBITAL, lambda: True)
    assert report.reached_space and not report.reached_orbit
    assert report.situation == "splashed"
    assert len(sim.burns) == 2 and sim.burns[1] < 0
    # The engines were only jettisoned after the deorbit burn lowered the periapsis.
    assert dict(sim.staged_at)[1] > 70_000


def test_orbital_flight_stays_in_orbit_when_the_game_does_not_count_it():
    """The tourists' orbit must be counted before coming back."""
    sim = SimulatedOrbit()
    with pytest.raises(FlightError, match="left in orbit"):
        _director(sim).fly(Mission.ORBITAL, lambda: False)
    assert sim.read_flight().situation == "orbiting"


def test_apoapsis_pitch_is_flat_at_the_target_apoapsis():
    """Vertical at the start, horizontal at the target, sooner below 1."""
    from ksp_control.domain.tourism import apoapsis_pitch

    assert apoapsis_pitch(2_000, 2_000, 80_000) == 90
    assert apoapsis_pitch(41_000, 2_000, 80_000) == pytest.approx(45)
    assert apoapsis_pitch(80_000, 2_000, 80_000) == 0
    assert apoapsis_pitch(95_000, 2_000, 80_000) == 0
    # A quarter of the way up with shape 0.5: already halfway over.
    assert apoapsis_pitch(21_500, 2_000, 80_000, shape=0.5) == pytest.approx(45)


def test_engines_relight_only_when_the_apoapsis_drops_clearly():
    """A few metres lost to the air do not flicker the engines."""
    from ksp_control.domain.tourism import ascent_throttle

    assert ascent_throttle(79_990, 80_000) == 1.0            # still climbing
    assert ascent_throttle(80_010, 80_000) == 0.0            # reached: cut
    assert ascent_throttle(79_990, 80_000, cut=True) == 0.0  # air took 10 m
    assert ascent_throttle(79_400, 80_000, cut=True) == 1.0  # clearly lower


def test_ascent_pitch_turns_gradually():
    """Vertical, then a linear turn, then the final pitch."""
    from ksp_control.domain.tourism import ascent_pitch

    assert ascent_pitch(500, 1_000, 10_000, 80) == 90
    assert ascent_pitch(5_500, 1_000, 10_000, 80) == pytest.approx(85)
    assert ascent_pitch(30_000, 1_000, 10_000, 80) == 80
