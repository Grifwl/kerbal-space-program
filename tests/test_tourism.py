"""Tests of the suborbital tourism rules, flight director and campaign."""

from __future__ import annotations

import math
from dataclasses import dataclass, field, replace

import pytest

from ksp_control.adapters.game.krpc_tourism import craft_file_name
from ksp_control.application.ports import TourismGamePort
from ksp_control.application.suborbital_flight import (
    FlightError,
    FlightReport,
    SuborbitalFlightDirector,
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
    Tourist,
    TourismContract,
    choose_pilot,
    is_suborbital_hop,
    match_kerbal,
    plan_passengers,
    should_stage_on_ascent,
)

SUBORBITAL = "Vuelo espacial suborbital en Kerbin"


def contract(cid: str, names: list[str], status=ContractStatus.OFFERED, reward=None,
             destinations=(SUBORBITAL,)) -> TourismContract:
    """Build a tourism contract whose tourists all share the destinations.

    Args:
        cid: Contract id.
        names: Full names of the tourists.
        status: Contract status.
        reward: Total reward (default: 13,000 per tourist).
        destinations: Destinations of every tourist.

    Returns:
        The contract.
    """
    tourists = tuple(Tourist(f"Itinerario de viaje de {n.split()[0]}", tuple(destinations), n)
                     for n in names)
    return TourismContract(cid, f"Contract {cid}", status, tourists,
                           13_000.0 * len(names) if reward is None else reward)


# ------------------------------------------------------------------ domain


def test_only_suborbital_kerbin_itineraries_are_hops():
    """A single other destination rules the contract out."""
    assert is_suborbital_hop(contract("a", ["Samroda Kerman"]))
    assert is_suborbital_hop(contract("a", ["X Kerman"], destinations=("Suborbital spaceflight at Kerbin",)))
    assert not is_suborbital_hop(contract("b", ["X Kerman"], destinations=(SUBORBITAL, "Sobrevuelo de Mun")))
    assert not is_suborbital_hop(contract("c", ["X Kerman"], destinations=("Vuelo espacial suborbital en Mun",)))
    assert not is_suborbital_hop(contract("d", []))


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
    assert {c.contract_id for c in plan.accept} == {"p1", "p2"}
    assert plan.seats_used == 4 and plan.reward == 52_000


def test_plan_carries_accepted_contracts_first():
    """Accepted contracts board before any offer, offers fill the rest."""
    contracts = [contract("p1", ["B Kerman", "C Kerman"]),
                 contract("act", ["A Kerman"], ContractStatus.ACTIVE),
                 contract("p2", ["D Kerman", "E Kerman"]),
                 contract("moon", ["F Kerman"], destinations=("Sobrevuelo de Mun",))]
    plan = plan_passengers(contracts, seats=4, available={"A Kerman"})
    assert [c.contract_id for c in plan.carry] == ["act"]
    assert [c.contract_id for c in plan.accept] in (["p1"], ["p2"])
    assert plan.seats_used == 3


def test_plan_skips_accepted_contracts_whose_tourists_cannot_board():
    """Tourists that are not at the space center cannot fly."""
    contracts = [contract("act", ["A Kerman"], ContractStatus.ACTIVE)]
    assert plan_passengers(contracts, seats=4, available=set()).is_empty
    assert plan_passengers(contracts, seats=4, available={"A Kerman"}, allow_offers=False).carry


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

    def release_controls(self) -> None:
        self.throttle = 0.0

    def warp_to(self, ut: float) -> None:
        self.warped = True
        self.advance(ut - self.t)

    def set_physics_warp(self, factor: int) -> None:
        self.physics_warp = factor

    # Unused by the director.
    def funds(self) -> float: raise NotImplementedError
    def read_tourism_contracts(self): raise NotImplementedError
    def accept_contract(self, contract_id): raise NotImplementedError
    def contract_status(self, contract_id): raise NotImplementedError
    def roster(self): raise NotImplementedError
    def launch(self, craft, crew): raise NotImplementedError
    def close(self) -> None: pass


def _director(sim: SimulatedHop, **profile) -> SuborbitalFlightDirector:
    """A director whose waits advance the simulation."""
    clock = {"now": 0.0}

    def sleep(seconds: float) -> None:
        clock["now"] += seconds
        sim.advance(seconds)

    return SuborbitalFlightDirector(sim, FlightProfile(**profile), interval=0.2,
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
                 complete: bool = True) -> None:
        self.contracts = {c.contract_id: c for c in contracts}
        self._funds = funds
        self.complete = complete
        self.launches: list[tuple[str, list[str]]] = []
        self.pilots = [Kerbal("Jebediah Kerman", "crew", "Pilot", True),
                       Kerbal("Valentina Kerman", "crew", "Pilot", True)]

    def funds(self) -> float:
        return self._funds

    def read_tourism_contracts(self) -> list[TourismContract]:
        return [c for c in self.contracts.values()
                if c.status in (ContractStatus.OFFERED, ContractStatus.ACTIVE)]

    def accept_contract(self, contract_id: str) -> None:
        c = self.contracts[contract_id]
        self.contracts[contract_id] = TourismContract(c.contract_id, c.title, ContractStatus.ACTIVE,
                                                      c.tourists, c.reward)

    def contract_status(self, contract_id: str) -> ContractStatus:
        return self.contracts[contract_id].status

    def roster(self) -> list[Kerbal]:
        tourists = [Kerbal(t.kerbal, "tourist", "Tourist", True)
                    for c in self.contracts.values() if c.status is ContractStatus.ACTIVE
                    for t in c.tourists]
        return self.pilots + tourists

    def launch(self, craft: str, crew: list[str]) -> str:
        self.launches.append((craft, crew))
        self._funds -= 12_000  # the vessel is paid at launch
        return "Passatgers v1.1"

    def finish_flight(self) -> None:
        """What recovering the tourists does: complete and pay."""
        for cid, c in list(self.contracts.items()):
            if c.status is ContractStatus.ACTIVE and self.complete:
                self.contracts[cid] = TourismContract(cid, c.title, ContractStatus.COMPLETED,
                                                      c.tourists, c.reward)
                self._funds += c.reward

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


def _campaign(game: FakeGame, target: float, reached_space: bool = True, **options):
    """A campaign with a fake flight and recovery."""
    recovery = FakeRecovery(game, ["Passatgers v1.1", "Escombros de Passatgers v1.1", "Rover"])
    fly = lambda: FlightReport("Passatgers v1.1", reached_space, 75_000.0, "splashed")  # noqa: E731
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
    with pytest.raises(CampaignError, match="contract"):
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


def test_ascent_pitch_turns_gradually():
    """Vertical, then a linear turn, then the final pitch."""
    from ksp_control.domain.tourism import ascent_pitch

    assert ascent_pitch(500, 1_000, 10_000, 80) == 90
    assert ascent_pitch(5_500, 1_000, 10_000, 80) == pytest.approx(85)
    assert ascent_pitch(30_000, 1_000, 10_000, 80) == 80
