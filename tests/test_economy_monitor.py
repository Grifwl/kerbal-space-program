"""Tests of the use cases wired with in-memory/fake adapters."""

from __future__ import annotations

from datetime import timedelta

from ksp_control.adapters.game.simulated_telemetry import SimulatedTelemetry
from ksp_control.adapters.persistence.sqlite_repository import SqliteEconomyRepository
from ksp_control.adapters.web.app import create_app
from ksp_control.application.economy_monitor import EconomyMonitorService
from ksp_control.application.ports import GameTelemetryPort, GameUnavailableError
from ksp_control.domain.model import GameSnapshot


class ScriptedTelemetry(GameTelemetryPort):
    """Fake telemetry returning a predefined list of snapshots or errors."""

    def __init__(self, script: list) -> None:
        """Store the script.

        Args:
            script: Items are ``GameSnapshot`` objects or exceptions to raise.
        """
        self._script = list(script)

    def read_snapshot(self) -> GameSnapshot:
        """Return (or raise) the next scripted item.

        Returns:
            The next snapshot.
        """
        item = self._script.pop(0)
        if isinstance(item, Exception):
            raise item
        return item

    def close(self) -> None:
        """Nothing to release."""


def test_simulated_career_ledger_matches_balances():
    """End to end: the ledger always adds up to the funds actually gained."""
    repository = SqliteEconomyRepository(":memory:")
    service = EconomyMonitorService(SimulatedTelemetry(seed=7, steps_per_action=1), repository)
    for _ in range(300):
        service.poll_once()

    view = service.dashboard()
    observed = view.current.resources.funds - view.baseline.resources.funds
    assert abs(view.summary.total - observed) < 1.0
    assert view.summary.launch_costs < 0
    assert view.summary.contracts_net > 0
    assert view.status.connected


def test_unavailable_game_is_reported_not_raised():
    """A missing game sets the status instead of crashing the loop."""
    service = EconomyMonitorService(
        ScriptedTelemetry([GameUnavailableError("KSP is not running")]),
        SqliteEconomyRepository(":memory:"),
    )
    assert service.poll_once() == []
    assert not service.status.connected
    assert "KSP is not running" in service.status.message


def test_snapshot_without_contracts_is_not_double_counted():
    """Contracts unreadable for one poll: rewards still booked exactly once."""
    from tests.test_ledger import ORBIT, snap

    script = [
        snap(0, 10_000, active=(ORBIT,)),
        snap(1, 10_000, active=(ORBIT,)),
        snap(2, 40_000, 8, 15, contracts=False),          # scene loading
        snap(3, 40_000, 8, 15, completed=(ORBIT,)),
        snap(4, 40_000, 8, 15, completed=(ORBIT,)),
    ]
    repository = SqliteEconomyRepository(":memory:")
    service = EconomyMonitorService(ScriptedTelemetry(script), repository)
    for _ in script:
        service.poll_once()
    events = repository.list_events()
    assert [e.kind.value for e in events] == ["contract_completed"]


def test_samples_are_throttled_when_nothing_changes():
    """Identical readings are only stored once per heartbeat."""
    from tests.test_ledger import snap

    script = [snap(i, 1_000) for i in range(10)]  # 2 s apart
    repository = SqliteEconomyRepository(":memory:")
    service = EconomyMonitorService(
        ScriptedTelemetry(script), repository, heartbeat=timedelta(seconds=10)
    )
    for _ in script:
        service.poll_once()
    assert len(repository.list_samples()) == 2  # t=0 and t=10 s


def test_dashboard_endpoint_serves_json():
    """The web adapter exposes the read model."""
    service = EconomyMonitorService(SimulatedTelemetry(steps_per_action=1),
                                    SqliteEconomyRepository(":memory:"))
    for _ in range(20):
        service.poll_once()
    client = create_app(service).test_client()
    data = client.get("/api/dashboard").get_json()
    assert data["status"]["connected"] is True
    assert {"mission_balance", "breakdown", "vessels"} <= set(data["summary"])
    assert data["samples"] and "funds" in data["samples"][0]
    assert client.get("/").status_code == 200


def test_launch_with_transient_empty_contracts_is_a_single_launch():
    """Real KSP sequence seen on launch: the contract lists come back empty
    for one poll while the flight scene loads, and the vessel cost is
    charged one poll after the scene change. Only the launch must be booked.
    """
    from tests.test_ledger import ORBIT, TEST, snap
    from ksp_control.domain.model import GameScene

    book = {"active": (TEST,), "completed": (ORBIT, ORBIT)}
    flight = {"scene": GameScene.FLIGHT, "vessel": "Untitled Space Craft"}
    script = [
        snap(0, 363_411, scene=GameScene.EDITOR_VAB, **book),
        snap(1, 363_411, scene=GameScene.EDITOR_VAB, **book),
        snap(2, 363_411, **flight, vessel_prelaunch=True),          # empty lists
        snap(3, 345_220, **flight, vessel_prelaunch=True, **book),  # charged late
        snap(4, 345_220, **flight, vessel_prelaunch=True, **book),
        snap(5, 345_220, **flight, **book),
    ]
    repository = SqliteEconomyRepository(":memory:")
    service = EconomyMonitorService(ScriptedTelemetry(script), repository)
    for _ in script:
        service.poll_once()
    events = repository.list_events()
    assert [(e.kind.value, e.delta.funds, e.vessel) for e in events] == [
        ("vessel_launched", -18_191, "Untitled Space Craft")
    ]


def test_empty_contract_lists_are_ignored_even_if_repeated():
    """A loading glitch that lasts two polls must not erase the contracts."""
    from tests.test_ledger import ORBIT, snap

    script = [snap(i, 1_000, completed=(ORBIT,)) for i in range(2)]
    script += [snap(i, 1_000) for i in range(2, 4)]  # empty twice
    script += [snap(i, 1_000, completed=(ORBIT,)) for i in range(4, 6)]
    repository = SqliteEconomyRepository(":memory:")
    service = EconomyMonitorService(ScriptedTelemetry(script), repository)
    for _ in script:
        service.poll_once()
    assert repository.list_events() == []


def test_restart_records_changes_made_while_stopped():
    """The ledger keeps adding up across monitor restarts."""
    from tests.test_ledger import snap

    repository = SqliteEconomyRepository(":memory:")
    first = EconomyMonitorService(ScriptedTelemetry([snap(0, 1_000), snap(1, 1_000)]), repository)
    first.poll_once()
    first.poll_once()
    second = EconomyMonitorService(ScriptedTelemetry([snap(5, 4_000), snap(6, 4_000)]), repository)
    second.poll_once()
    second.poll_once()
    (event,) = repository.list_events()
    assert event.delta.funds == 3_000
    assert "not running" in event.description


def test_vip_mission_with_unflagged_objective_across_a_restart():
    """Real VIP mission: kRPC reports the paid suborbital objective as still
    pending. The 3,250 must be recognized by amount, remembered across a
    monitor restart, and not paid again when the contract completes."""
    from tests.test_ledger import snap
    from ksp_control.domain.model import ContractTerms, GameScene, ParameterTerms

    vip = ContractTerms(
        "TourismContract", "Ferry a VIP", reputation_completion=19.05,
        parameters=(
            ParameterTerms("Itinerary of Lenusa", funds=9_750),
            ParameterTerms("Suborbital flight", funds=3_250),
        ),
    )
    flight = {"scene": GameScene.FLIGHT, "vessel": "Bus"}
    repository = SqliteEconomyRepository(":memory:")

    def run(script):
        service = EconomyMonitorService(ScriptedTelemetry(script), repository)
        for _ in script:
            service.poll_once()

    run([snap(i, 345_220, **flight, active=(vip,)) for i in range(2)]
        + [snap(i, 348_470, **flight, active=(vip,)) for i in range(2, 4)])
    # Monitor restarted while the VIP is still flying.
    run([snap(i, 348_470, **flight, active=(vip,)) for i in range(4, 6)]
        + [snap(i, 365_691.88, 25.92, 19.05, completed=(vip,)) for i in range(6, 8)])

    events = [(e.kind.value, e.delta.funds) for e in reversed(repository.list_events())]
    assert events == [
        ("contract_progress", 3_250),
        ("contract_completed", 9_750),
        ("vessel_recovered", 7_471.88),
    ]
    assert repository.paid_objectives() == set()  # forgotten once finished


def test_dashboard_groups_renamed_vessels_by_their_recorded_design(tmp_path):
    """A cruise renamed by ksp-tourism shows under its craft in the vessel table."""
    from tests.test_ledger import ORBIT, TEST, snap
    from ksp_control.adapters.persistence.json_vessel_designs import JsonVesselDesigns
    from ksp_control.domain.model import GameScene

    book = {"active": (TEST,), "completed": (ORBIT,)}
    script = [
        snap(0, 100_000, scene=GameScene.EDITOR_VAB, **book),
        snap(1, 100_000, scene=GameScene.EDITOR_VAB, **book),
        snap(2, 80_000, scene=GameScene.FLIGHT, vessel="Cruise John Doe", **book),
        snap(3, 84_000, scene=GameScene.SPACE_CENTER, **book),
    ]
    designs = JsonVesselDesigns(tmp_path / "designs.json")
    repository = SqliteEconomyRepository(":memory:")
    service = EconomyMonitorService(ScriptedTelemetry(script), repository, designs=designs)
    for _ in script:
        service.poll_once()
    # Recorded by the tourism script after the monitor booked the launch.
    designs.record("Cruise John Doe", "Passatgers v1.3")
    (vessel,) = service.dashboard().summary.vessels
    assert (vessel.name, vessel.launches, vessel.net) == ("Passatgers v1.3", 1, -16_000)
