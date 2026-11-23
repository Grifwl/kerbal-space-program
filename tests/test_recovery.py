"""Tests of the vessel recovery rules and use case."""

from __future__ import annotations

import pytest

from ksp_control.application.ports import FleetPort, GameUnavailableError
from ksp_control.application.vessel_recovery import VesselRecoveryService
from ksp_control.domain.model import GameScene
from ksp_control.domain.recovery import (
    KSC_LATITUDE,
    KSC_LONGITUDE,
    PartValue,
    RecoveryPolicy,
    ResourceLoad,
    VesselReport,
    distance_to_ksc,
    plan_recovery,
    recovery_factor,
)

KERBIN_RADIUS = 600_000.0


def vessel(
    vessel_id: str,
    name: str = "Kerbal X Debris",
    vessel_type: str = "debris",
    situation: str = "splashed",
    latitude: float = KSC_LATITUDE,
    longitude: float = KSC_LONGITUDE + 1.0,
    recoverable: bool = True,
    is_active: bool = False,
    parts: tuple[PartValue, ...] | None = (PartValue(1_000.0),),
    body: str = "Kerbin",
) -> VesselReport:
    """Build a vessel report with sensible defaults (1 part worth 1,000).

    Args:
        vessel_id: Identifier.
        name: Vessel name.
        vessel_type: KSP type.
        situation: KSP situation.
        latitude: Degrees.
        longitude: Degrees (default: about 10 km east of the KSC).
        recoverable: KSP's recoverable flag.
        is_active: Whether it is being flown.
        parts: Part values, or ``None`` when unreadable.
        body: Body name.

    Returns:
        The report.
    """
    return VesselReport(
        vessel_id, name, vessel_type, situation, body, KERBIN_RADIUS,
        latitude, longitude, recoverable, is_active, 0, parts,
    )


def test_recovery_factor_follows_ksp_curve():
    """100% at the KSC, 98% next to it, 10% at the antipode."""
    assert recovery_factor(500, KERBIN_RADIUS) == 1.0
    assert recovery_factor(10_000, KERBIN_RADIUS) == pytest.approx(0.975, abs=0.001)
    antipode = 3.14159265 * KERBIN_RADIUS
    assert recovery_factor(antipode, KERBIN_RADIUS) == pytest.approx(0.1)


def test_distance_to_ksc_is_zero_on_the_pad():
    """The pad is the reference point."""
    assert distance_to_ksc(KSC_LATITUDE, KSC_LONGITUDE, KERBIN_RADIUS) == pytest.approx(0)


def test_part_value_discounts_burnt_fuel():
    """Catalog cost includes full tanks; spent fuel is not paid back."""
    tank = PartValue(1_000.0, (ResourceLoad("LiquidFuel", 0, 100), ResourceLoad("Oxidizer", 50, 100)))
    assert tank.value == pytest.approx(1_000 - 100 * 0.8 - 50 * 0.18)


def test_plan_recovers_only_what_pays_and_is_safe():
    """Every skip reason, and payouts sorted highest first."""
    vessels = [
        vessel("cheap", parts=(PartValue(50.0),)),
        vessel("rich", parts=(PartValue(5_000.0),)),
        vessel("orbit", situation="orbiting", recoverable=False),
        vessel("base", name="Mun Base", vessel_type="base"),
        vessel("active", name="Kerbal X", vessel_type="ship", is_active=True),
        vessel("far", parts=None),
        vessel("ok"),
    ]
    decisions = plan_recovery(vessels, RecoveryPolicy(min_value=100))
    chosen = [d.vessel.vessel_id for d in decisions if d.recover]
    assert chosen == ["rich", "ok", "far"]  # unknown value: recovered by default
    reasons = {d.vessel.vessel_id: d.reason for d in decisions}
    assert "minimum" in reasons["cheap"]
    assert "not recoverable" in reasons["orbit"]
    assert "protected" in reasons["base"]
    assert "active vessel" in reasons["active"]
    assert "unknown" in reasons["far"]
    strict = plan_recovery(vessels, RecoveryPolicy(min_value=100, recover_unknown=False))
    assert [d.vessel.vessel_id for d in strict if d.recover] == ["rich", "ok"]


def test_active_vessel_goes_last_when_included():
    """Recovering the active vessel ends the flight, so it must be last."""
    vessels = [
        vessel("active", parts=(PartValue(90_000.0),), is_active=True, vessel_type="ship"),
        vessel("debris"),
    ]
    decisions = plan_recovery(vessels, RecoveryPolicy(include_active=True))
    assert [d.vessel.vessel_id for d in decisions] == ["debris", "active"]


class FakeFleet(FleetPort):
    """In-memory game: recovering a vessel removes it and pays its value."""

    def __init__(self, vessels: list[VesselReport], leave_flight_on_recover: bool = False):
        """Store the fleet.

        Args:
            vessels: Vessels in the game.
            leave_flight_on_recover: Simulate kRPC 0.5.x sending the game to
                the space center when a vessel is recovered.
        """
        self.vessels = {v.vessel_id: v for v in vessels}
        self.balance = 10_000.0
        self.current_scene = GameScene.FLIGHT
        self.leave_flight = leave_flight_on_recover

    def scene(self) -> GameScene:
        """Return the scene."""
        return self.current_scene

    def funds(self) -> float:
        """Return the balance."""
        return self.balance

    def list_vessels(self) -> list[VesselReport]:
        """Return the vessels."""
        return list(self.vessels.values())

    def vessel_exists(self, vessel_id: str) -> bool:
        """Tell whether the vessel is still there."""
        return vessel_id in self.vessels

    def recover(self, vessel_id: str) -> None:
        """Remove the vessel and pay its parts."""
        recovered = self.vessels.pop(vessel_id)
        self.balance += sum(p.value for p in recovered.parts or ())
        if self.leave_flight:
            self.current_scene = GameScene.SPACE_CENTER

    def close(self) -> None:
        """Nothing to release."""


def make_service(fleet: FleetPort) -> VesselRecoveryService:
    """Service with a fake clock so tests never wait.

    Args:
        fleet: The fleet port.

    Returns:
        The service.
    """
    now = [0.0]

    def sleep(seconds: float) -> None:
        now[0] += seconds

    return VesselRecoveryService(fleet, timeout=5, sleep=sleep, clock=lambda: now[0])


def test_service_recovers_and_measures_payout():
    """Each chosen vessel is recovered and its real payout reported."""
    fleet = FakeFleet([vessel("a"), vessel("b", parts=(PartValue(2_000.0),)), vessel("c", recoverable=False)])
    service = make_service(fleet)
    outcomes = service.execute(service.plan(RecoveryPolicy()))
    assert [(o.decision.vessel.vessel_id, o.recovered, o.payout) for o in outcomes] == [
        ("b", True, 2_000), ("a", True, 1_000),
    ]
    assert set(fleet.vessels) == {"c"}


def test_service_stops_when_the_game_leaves_flight():
    """kRPC 0.5.x may switch to the space center: report the rest as skipped."""
    fleet = FakeFleet([vessel("a"), vessel("b")], leave_flight_on_recover=True)
    service = make_service(fleet)
    outcomes = service.execute(service.plan(RecoveryPolicy()))
    assert [o.recovered for o in outcomes] == [True, False]
    assert "changed scene" in outcomes[1].message


def test_unknown_position_means_unknown_value():
    """Outside flight kRPC gives no position: value unknown, recovered anyway."""
    blind = vessel("sc", latitude=None, longitude=None)
    (decision,) = plan_recovery([blind], RecoveryPolicy())
    assert decision.recover and decision.estimated_value is None


def test_service_works_from_the_space_center():
    """kRPC 0.8 lists vessels outside flight: recover from the space center."""
    fleet = FakeFleet([vessel("a"), vessel("b")])
    fleet.current_scene = GameScene.SPACE_CENTER
    service = make_service(fleet)
    outcomes = service.execute(service.plan(RecoveryPolicy()))
    assert [o.recovered for o in outcomes] == [True, True]


def test_listing_errors_are_reported():
    """A scene without vessel access surfaces as GameUnavailableError."""

    class Blind(FakeFleet):
        def list_vessels(self):
            raise GameUnavailableError("Procedure not available in game scene")

    with pytest.raises(GameUnavailableError):
        make_service(Blind([])).plan(RecoveryPolicy())
