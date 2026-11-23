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
        snap(1, 40_000, 8, 15, contracts=False),          # scene loading
        snap(2, 40_000, 8, 15, completed=(ORBIT,)),
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
