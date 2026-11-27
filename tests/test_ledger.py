"""Unit tests for the ledger rules (pure domain, no I/O)."""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta, timezone

import pytest

from ksp_control.domain.ledger import LedgerAnalyzer, LedgerSummary
from ksp_control.domain.model import (
    ContractBook,
    ContractTerms,
    EventKind,
    GameScene,
    GameSnapshot,
    ParameterTerms,
    Resources,
)

T0 = datetime(2026, 11, 23, 20, 0, tzinfo=timezone.utc)
ORBIT = ContractTerms("Exploration", "Orbit Kerbin", 5_000, 30_000, 10_000, 8, 15, 12)
TEST = ContractTerms("PartTest", "Test a part", 1_000, 9_000, 3_000, 2, 5, 4)


def snap(
    step: int,
    funds: float,
    science: float = 0.0,
    reputation: float = 0.0,
    scene: GameScene = GameScene.SPACE_CENTER,
    vessel: str | None = None,
    active: tuple = (),
    completed: tuple = (),
    failed: tuple = (),
    contracts: bool = True,
    vessel_prelaunch: bool = False,
) -> GameSnapshot:
    """Build a snapshot with sensible defaults.

    Args:
        step: Index used to derive increasing timestamps.
        funds: Funds balance.
        science: Science balance.
        reputation: Reputation balance.
        scene: Game scene.
        vessel: Active vessel name.
        active: Active contracts.
        completed: Completed contracts.
        failed: Failed contracts.
        contracts: Whether contracts were readable.
        vessel_prelaunch: Whether the vessel is still on the pad.

    Returns:
        The snapshot.
    """
    return GameSnapshot(
        taken_at=T0 + timedelta(seconds=2 * step),
        universal_time=100.0 * step,
        scene=scene,
        resources=Resources(funds, science, reputation),
        active_vessel=vessel,
        contracts=ContractBook(active, completed, failed) if contracts else None,
        vessel_prelaunch=vessel_prelaunch,
    )


@pytest.fixture
def analyzer() -> LedgerAnalyzer:
    """Provide a fresh analyzer.

    Returns:
        The analyzer under test.
    """
    return LedgerAnalyzer()


def kinds(events) -> list[EventKind]:
    """Extract event kinds.

    Args:
        events: Ledger events.

    Returns:
        Their kinds, in order.
    """
    return [e.kind for e in events]


def test_no_change_produces_no_events(analyzer):
    """Identical snapshots are not an event."""
    assert analyzer.compare(snap(0, 1000), snap(1, 1000)) == []


def test_accepting_a_contract_books_the_advance(analyzer):
    """A new active contract explains the advance payment exactly."""
    events = analyzer.compare(snap(0, 10_000), snap(1, 15_000, active=(ORBIT,)))
    assert kinds(events) == [EventKind.CONTRACT_ACCEPTED]
    assert events[0].delta == Resources(funds=5_000)


def test_completing_a_contract_books_all_rewards(analyzer):
    """Completion moves the contract and pays funds, science, reputation."""
    before = snap(0, 10_000, 1, 1, active=(ORBIT, TEST))
    after = snap(1, 40_000, 9, 16, active=(TEST,), completed=(ORBIT,))
    events = analyzer.compare(before, after)
    assert kinds(events) == [EventKind.CONTRACT_COMPLETED]
    assert events[0].delta == Resources(30_000, 8, 15)


def test_reputation_scaling_goes_to_other(analyzer):
    """KSP scales reputation; the difference is kept as an OTHER residual."""
    before = snap(0, 10_000, 0, 50, active=(ORBIT,))
    after = snap(1, 40_000, 8, 60, completed=(ORBIT,))  # 10 instead of 15
    events = analyzer.compare(before, after)
    assert kinds(events) == [EventKind.CONTRACT_COMPLETED, EventKind.OTHER]
    assert events[1].delta == Resources(reputation=-5)


def test_failure_and_cancellation_charge_penalties(analyzer):
    """Failed goes to the failed list, cancelled simply disappears."""
    before = snap(0, 50_000, 0, 30, active=(ORBIT, TEST))
    after = snap(1, 37_000, 0, 14, failed=(ORBIT,))
    events = analyzer.compare(before, after)
    by_kind = {e.kind: e.delta for e in events}
    assert by_kind[EventKind.CONTRACT_FAILED] == Resources(funds=-10_000, reputation=-12)
    assert by_kind[EventKind.CONTRACT_CANCELLED] == Resources(funds=-3_000, reputation=-4)
    assert EventKind.OTHER not in by_kind


def test_duplicate_contracts_are_counted(analyzer):
    """Two identical contracts are tracked as a multiset."""
    before = snap(0, 0, active=(TEST, TEST))
    after = snap(1, 9_000, 2, 5, active=(TEST,), completed=(TEST,))
    events = analyzer.compare(before, after)
    assert kinds(events) == [EventKind.CONTRACT_COMPLETED]


def test_launch_is_charged_on_entering_flight(analyzer):
    """Funds lost when leaving the editor are the vessel cost."""
    events = analyzer.compare(
        snap(0, 100_000, scene=GameScene.EDITOR_VAB),
        snap(1, 64_000, scene=GameScene.FLIGHT, vessel="Kerbal X"),
    )
    assert kinds(events) == [EventKind.VESSEL_LAUNCHED]
    assert events[0].delta == Resources(funds=-36_000)
    assert events[0].vessel == "Kerbal X"


def test_late_charge_on_the_pad_is_still_the_launch(analyzer):
    """KSP may deduct the cost one reading after the flight scene opens."""
    events = analyzer.compare(
        snap(0, 100_000, scene=GameScene.FLIGHT, vessel="Kerbal X", vessel_prelaunch=True),
        snap(1, 64_000, scene=GameScene.FLIGHT, vessel="Kerbal X", vessel_prelaunch=True),
    )
    assert kinds(events) == [EventKind.VESSEL_LAUNCHED]


def test_expense_during_flight_is_not_a_launch(analyzer):
    """Once the vessel has lifted off, a funds drop is something else."""
    events = analyzer.compare(
        snap(0, 100_000, scene=GameScene.FLIGHT, vessel="Kerbal X"),
        snap(1, 90_000, scene=GameScene.FLIGHT, vessel="Kerbal X"),
    )
    assert kinds(events) == [EventKind.OTHER]


def test_revert_to_editor_refunds_the_vessel(analyzer):
    """Flight -> editor with more funds is a reverted launch."""
    events = analyzer.compare(
        snap(0, 64_000, scene=GameScene.FLIGHT, vessel="Kerbal X"),
        snap(1, 100_000, scene=GameScene.EDITOR_VAB),
    )
    assert kinds(events) == [EventKind.LAUNCH_REVERTED]
    assert events[0].vessel == "Kerbal X"


def test_recovery_keeps_science_and_contract_rewards_apart(analyzer):
    """A recovery that also completes a contract yields both events."""
    before = snap(0, 10_000, 5, 0, scene=GameScene.FLIGHT, vessel="Lander", active=(ORBIT,))
    after = snap(1, 52_000, 25, 15, scene=GameScene.SPACE_CENTER, completed=(ORBIT,))
    events = analyzer.compare(before, after)
    assert kinds(events) == [EventKind.CONTRACT_COMPLETED, EventKind.VESSEL_RECOVERED]
    assert events[1].delta == Resources(funds=12_000, science=12)
    assert events[1].vessel == "Lander"


def test_unexplained_change_is_other(analyzer):
    """A facility upgrade in the space center is OTHER."""
    events = analyzer.compare(snap(0, 200_000), snap(1, 125_000))
    assert kinds(events) == [EventKind.OTHER]
    assert events[0].delta.funds == -75_000


def test_events_always_add_up_to_observed_change(analyzer):
    """Whatever the attribution, deltas sum to the real balance change."""
    before = snap(0, 10_000, 3, 7, scene=GameScene.EDITOR_SPH, active=(TEST,))
    after = snap(1, 1_234, 1, 2, scene=GameScene.FLIGHT, vessel="Plane", failed=(TEST,))
    events = analyzer.compare(before, after)
    total = sum((e.delta for e in events), Resources())
    assert total.funds == pytest.approx(1_234 - 10_000)
    assert total.science == pytest.approx(1 - 3)
    assert total.reputation == pytest.approx(2 - 7)


def test_quickload_reverts_a_completion(analyzer):
    """A completed contract going back to active undoes its reward."""
    before = snap(0, 40_000, 8, 15, completed=(ORBIT,))
    after = snap(1, 10_000, active=(ORBIT,))
    events = analyzer.compare(before, after)
    assert kinds(events) == [EventKind.CONTRACT_REVERTED]
    assert events[0].delta.funds == -30_000


def test_summary_answers_earning_or_losing(analyzer):
    """The summary splits contracts from vessels and per vessel."""
    s = [
        snap(0, 100_000, scene=GameScene.SPACE_CENTER),
        snap(1, 105_000, active=(ORBIT,)),
        snap(2, 105_000, scene=GameScene.EDITOR_VAB, active=(ORBIT,)),
        snap(3, 65_000, scene=GameScene.FLIGHT, vessel="Kerbal X", active=(ORBIT,)),
        snap(4, 95_000, 8, 15, scene=GameScene.FLIGHT, vessel="Kerbal X", completed=(ORBIT,)),
        snap(5, 103_000, 8, 15, scene=GameScene.SPACE_CENTER, completed=(ORBIT,)),
    ]
    events = [e for a, b in zip(s, s[1:]) for e in analyzer.compare(a, b)]
    summary = LedgerSummary.from_events(events)
    assert summary.contracts_net == 35_000
    assert summary.launch_costs == -40_000
    assert summary.recoveries == 8_000
    assert summary.mission_balance == 3_000
    assert summary.total == 3_000
    (vessel,) = summary.vessels
    assert (vessel.name, vessel.launches, vessel.net) == ("Kerbal X", 1, -32_000)


def test_reverted_launch_does_not_count_as_flight():
    """Launch + revert nets to zero flights and zero cost."""
    analyzer = LedgerAnalyzer()
    s = [
        snap(0, 100_000, scene=GameScene.EDITOR_VAB),
        snap(1, 60_000, scene=GameScene.FLIGHT, vessel="Oops"),
        snap(2, 100_000, scene=GameScene.EDITOR_VAB),
    ]
    events = [e for a, b in zip(s, s[1:]) for e in analyzer.compare(a, b)]
    summary = LedgerSummary.from_events(events)
    (vessel,) = summary.vessels
    assert (vessel.launches, vessel.cost, vessel.recovered, vessel.net) == (0, 0, 0, 0)
    assert (summary.launch_costs, summary.recoveries) == (0, 0)


# Tourism contract as seen in a real career: the contract itself pays 0,
# each tourist's itinerary pays 9,750 and each stop on it pays 3,250.
TOURISTS = ContractTerms(
    "TourismContract", "Ferry two tourists", reputation_completion=18,
    parameters=(
        ParameterTerms("Itinerary of Hadpondo", funds=9_750),
        ParameterTerms("Suborbital flight", funds=3_250),
        ParameterTerms("Itinerary of Podtreya", funds=9_750),
        ParameterTerms("Suborbital flight", funds=3_250),
    ),
)


def test_objectives_completed_in_flight_are_contract_income(analyzer):
    """Both tourists reach their suborbital stop: 2 x 3,250."""
    flight = {"scene": GameScene.FLIGHT, "vessel": "Bus"}
    before = snap(0, 345_220, **flight, active=(TOURISTS,))
    after = snap(1, 351_720, **flight, active=(TOURISTS,))
    before = _with_progress(before, (False, False, False, False))
    after = _with_progress(after, (False, True, False, True))
    events = analyzer.compare(before, after)
    assert kinds(events) == [EventKind.CONTRACT_PROGRESS]
    assert events[0].delta.funds == 6_500


def test_completion_pays_pending_objectives_then_recovery(analyzer):
    """Recovering the bus completes both itineraries and the contract."""
    before = _with_progress(
        snap(0, 351_720, 44, 81, scene=GameScene.FLIGHT, vessel="Bus", active=(TOURISTS,)),
        (False, True, False, True),
    )
    after = snap(1, 378_553, 62.8, 99, completed=(TOURISTS,))
    events = analyzer.compare(before, after)
    assert kinds(events) == [EventKind.CONTRACT_COMPLETED, EventKind.VESSEL_RECOVERED]
    assert events[0].delta == Resources(funds=19_500, reputation=18)
    assert events[1].delta == Resources(funds=7_333, science=18.8)
    summary = LedgerSummary.from_events(events)
    assert summary.contract_rewards == 19_500


def _with_progress(snapshot: GameSnapshot, flags: tuple[bool, ...]) -> GameSnapshot:
    """Attach objective progress to the single active contract of a snapshot.

    Args:
        snapshot: Snapshot with exactly one active contract.
        flags: Completion flag of each of its parameters.

    Returns:
        A copy of the snapshot carrying the progress.
    """
    book = snapshot.contracts
    return replace(snapshot, contracts=replace(book, active_progress=(flags,)))


def test_recovering_debris_in_flight_is_a_recovery(analyzer):
    """A stage vanishes from the list and funds go up, without scene change."""
    flight = {"scene": GameScene.FLIGHT, "vessel": "Kerbal X"}
    before = replace(snap(0, 50_000, **flight), vessels=("Kerbal X", "Kerbal X Debris"))
    after = replace(snap(1, 53_500, 2, 0, **flight), vessels=("Kerbal X",))
    events = analyzer.compare(before, after)
    assert kinds(events) == [EventKind.VESSEL_RECOVERED]
    assert events[0].vessel == "Kerbal X Debris"
    assert events[0].delta == Resources(funds=3_500, science=2)


def test_owner_vessel_handles_localized_and_nested_debris():
    """English suffix, Spanish prefix, repeated markers."""
    from ksp_control.domain.ledger import owner_vessel

    assert owner_vessel("Kerbal X Debris Debris") == "Kerbal X"
    assert owner_vessel("Escombros de Escombros de Nave") == "Nave"
    assert owner_vessel("Kerbal X") == "Kerbal X"


def test_recovering_from_the_space_center_names_the_vessel(analyzer):
    """kRPC 0.8 lists vessels outside flight too."""
    before = replace(snap(0, 1_000), vessels=("A", "Escombros de A"))
    after = replace(snap(1, 1_300), vessels=("A",))
    (event,) = analyzer.compare(before, after)
    assert (event.kind, event.vessel) == (EventKind.VESSEL_RECOVERED, "Escombros de A")


def test_debris_recoveries_count_for_their_vessel(analyzer):
    """The per-vessel table merges "X Debris" into "X"."""
    s = [
        snap(0, 100_000, scene=GameScene.EDITOR_VAB),
        replace(snap(1, 60_000, scene=GameScene.FLIGHT, vessel="Kerbal X"),
                vessels=("Kerbal X", "Kerbal X Debris")),
        replace(snap(2, 64_000, scene=GameScene.FLIGHT, vessel="Kerbal X"),
                vessels=("Kerbal X",)),
    ]
    events = [e for a, b in zip(s, s[1:]) for e in analyzer.compare(a, b)]
    (vessel,) = LedgerSummary.from_events(events).vessels
    assert (vessel.name, vessel.cost, vessel.recovered) == ("Kerbal X", -40_000, 4_000)


def test_renamed_vessels_add_up_under_their_design(analyzer):
    """Two cruises of one craft, and the debris of one, make one line."""
    s = [
        snap(0, 100_000, scene=GameScene.EDITOR_VAB),
        # The monitor may read the craft name before the script renames it.
        replace(snap(1, 80_000, scene=GameScene.FLIGHT, vessel="Passatgers v1.3"),
                vessels=("Passatgers v1.3",)),
        replace(snap(2, 81_000, scene=GameScene.FLIGHT, vessel="Cruise John Doe"),
                vessels=("Cruise John Doe", "Escombros de Cruise John Doe")),
        replace(snap(3, 81_000, scene=GameScene.FLIGHT, vessel="Cruise John Doe"),
                vessels=("Cruise John Doe",)),
        snap(4, 85_000, scene=GameScene.SPACE_CENTER),
        snap(5, 85_000, scene=GameScene.EDITOR_VAB),
        snap(6, 65_000, scene=GameScene.FLIGHT, vessel="Cruise Jane Roe"),
        snap(7, 69_000, scene=GameScene.SPACE_CENTER),
    ]
    s[2] = replace(s[2], resources=s[1].resources)  # debris still up
    s[3] = replace(s[3], resources=replace(s[1].resources, funds=81_000))
    events = [e for a, b in zip(s, s[1:]) for e in analyzer.compare(a, b)]
    designs = {"Cruise John Doe": "Passatgers v1.3", "Cruise Jane Roe": "Passatgers v1.3"}
    (vessel,) = LedgerSummary.from_events(events, designs).vessels
    assert vessel.name == "Passatgers v1.3"
    assert (vessel.launches, vessel.cost, vessel.recovered) == (2, -40_000, 9_000)
    # Without the designs, each cruise is a line of its own.
    assert len(LedgerSummary.from_events(events).vessels) == 3
