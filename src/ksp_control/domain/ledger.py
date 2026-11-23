"""Rules that turn consecutive game snapshots into a ledger of economic events.

kRPC only tells us the *balances* (funds, science, reputation), not *why*
they changed: KSP's own transaction reasons are not exposed. This module
reconstructs the reasons by combining three sources of evidence:

1. Contract lists. A contract that moves from "active" to "completed" between
   two snapshots explains a known amount of funds/science/reputation.
2. Scene transitions. KSP charges a vessel when it leaves the editor for the
   launch pad, refunds it on "revert to editor" and pays its recovery value
   when it returns to the space center.
3. The residual. Whatever the contracts do not explain is attributed to the
   scene transition (launch, recovery...) or, failing that, to ``OTHER``
   (milestones, facility upgrades, part purchases, strategies...).

The residual approach guarantees that the ledger always adds up to the real
change in balances, even when an attribution is imperfect.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass

from ksp_control.domain.model import (
    ContractBook,
    ContractTerms,
    EventKind,
    GameScene,
    GameSnapshot,
    LedgerEvent,
    Resources,
)


class LedgerAnalyzer:
    """Compares two snapshots and explains the difference as ledger events.

    The analyzer is stateless: the caller keeps the previous snapshot and
    passes both snapshots on every call, which keeps the class trivial to
    test and safe to reuse.
    """

    def compare(self, previous: GameSnapshot, current: GameSnapshot) -> list[LedgerEvent]:
        """Explain how the career balances changed between two snapshots.

        Args:
            previous: The earlier snapshot.
            current: The later snapshot.

        Returns:
            The detected events. Their deltas always add up to the observed
            change in balances (within the noise thresholds).
        """
        events: list[LedgerEvent] = []

        # Contracts first: they are the only source with exact, known amounts.
        if previous.contracts is not None and current.contracts is not None:
            for kind, terms, delta in _diff_contracts(previous.contracts, current.contracts):
                events.append(self._event(current, kind, terms.title, delta))

        explained = sum((event.delta for event in events), Resources())
        residual = (current.resources - previous.resources - explained).cleaned()
        if not residual.is_negligible:
            events.extend(self._explain_residual(previous, current, residual))
        return events

    def _explain_residual(
        self, previous: GameSnapshot, current: GameSnapshot, residual: Resources
    ) -> list[LedgerEvent]:
        """Attribute the unexplained change to a vessel operation or to OTHER.

        Args:
            previous: The earlier snapshot.
            current: The later snapshot.
            residual: Change not explained by contracts.

        Returns:
            One or two events whose deltas add up to ``residual``.
        """
        before, after = previous.scene, current.scene
        funds = residual.funds

        # Funds are charged when the vessel is rolled out to the launch pad or
        # runway, i.e. on the transition into the flight scene.
        if after is GameScene.FLIGHT and before is not GameScene.FLIGHT and funds < 0:
            vessel = current.active_vessel or "Unknown vessel"
            return self._split(
                current, residual, EventKind.VESSEL_LAUNCHED, f"Launched {vessel}", vessel,
                keep_science=False,
            )

        # "Revert to VAB/SPH" refunds the whole vessel cost.
        if before is GameScene.FLIGHT and after.is_editor and funds > 0:
            vessel = previous.active_vessel or "Unknown vessel"
            return self._split(
                current, residual, EventKind.LAUNCH_REVERTED, f"Reverted {vessel}", vessel,
                keep_science=False,
            )

        # Recovering from flight pays the vessel's recovery value plus any
        # science stored on board, so the science residual stays attached.
        recovered_from_flight = before is GameScene.FLIGHT and after in (
            GameScene.SPACE_CENTER,
            GameScene.TRACKING_STATION,
        )
        recovered_in_tracking = (
            before is GameScene.TRACKING_STATION and after is GameScene.TRACKING_STATION
        )
        if (recovered_from_flight or recovered_in_tracking) and funds > 0:
            vessel = previous.active_vessel if recovered_from_flight else None
            label = f"Recovered {vessel}" if vessel else "Recovered a vessel"
            return self._split(
                current, residual, EventKind.VESSEL_RECOVERED, label, vessel,
                keep_science=True,
            )

        return [self._event(current, EventKind.OTHER, _describe_other(residual), residual)]

    def _split(
        self,
        current: GameSnapshot,
        residual: Resources,
        kind: EventKind,
        description: str,
        vessel: str | None,
        keep_science: bool,
    ) -> list[LedgerEvent]:
        """Assign the funds of a residual to a vessel event, the rest to OTHER.

        Args:
            current: Snapshot used for timestamps.
            residual: Unexplained change.
            kind: Vessel event kind.
            description: Text of the vessel event.
            vessel: Vessel name, if known.
            keep_science: Whether the science part also belongs to the vessel
                event (true for recoveries, which return experiment data).

        Returns:
            The vessel event plus, when needed, an OTHER event with the rest.
        """
        main = (
            Resources(funds=residual.funds, science=residual.science)
            if keep_science
            else residual.only_funds()
        )
        events = [self._event(current, kind, description, main, vessel)]
        rest = (residual - main).cleaned()
        if not rest.is_negligible:
            events.append(self._event(current, EventKind.OTHER, _describe_other(rest), rest))
        return events

    @staticmethod
    def _event(
        snapshot: GameSnapshot,
        kind: EventKind,
        description: str,
        delta: Resources,
        vessel: str | None = None,
    ) -> LedgerEvent:
        """Create an event stamped with the snapshot's times.

        Args:
            snapshot: Snapshot in which the event was detected.
            kind: Event kind.
            description: Human readable text.
            delta: Change in balances.
            vessel: Vessel involved, if any.

        Returns:
            The new ledger event.
        """
        return LedgerEvent(
            occurred_at=snapshot.taken_at,
            universal_time=snapshot.universal_time,
            kind=kind,
            description=description,
            delta=delta.cleaned(),
            vessel=vessel,
        )


def _diff_contracts(
    previous: ContractBook, current: ContractBook
) -> list[tuple[EventKind, ContractTerms, Resources]]:
    """Detect contract state changes by comparing contract multisets.

    For every distinct contract the counts in each group are compared:

    * more completed / failed copies -> completed / failed events;
    * fewer completed / failed copies -> the game travelled back in time
      ("revert to launch" or a quickload), so the reward is undone;
    * a change in active copies not explained by the above is an acceptance
      (more) or a cancellation (fewer).

    Args:
        previous: Contracts in the earlier snapshot.
        current: Contracts in the later snapshot.

    Returns:
        Tuples of (kind, contract, expected change in balances).
    """
    prev_active, prev_done, prev_failed = previous.counts()
    cur_active, cur_done, cur_failed = current.counts()
    every_contract = set(prev_active) | set(prev_done) | set(prev_failed)
    every_contract |= set(cur_active) | set(cur_done) | set(cur_failed)

    changes: list[tuple[EventKind, ContractTerms, Resources]] = []
    for terms in sorted(every_contract, key=lambda t: (t.title, t.contract_type)):
        d_active = _delta(prev_active, cur_active, terms)
        d_done = _delta(prev_done, cur_done, terms)
        d_failed = _delta(prev_failed, cur_failed, terms)

        completed, uncompleted = max(d_done, 0), max(-d_done, 0)
        failed, unfailed = max(d_failed, 0), max(-d_failed, 0)

        changes += [(EventKind.CONTRACT_COMPLETED, terms, terms.reward)] * completed
        changes += [(EventKind.CONTRACT_FAILED, terms, terms.penalty)] * failed
        changes += [(EventKind.CONTRACT_REVERTED, terms, _negate(terms.reward))] * uncompleted
        changes += [(EventKind.CONTRACT_REVERTED, terms, _negate(terms.penalty))] * unfailed

        # A completion or failure removes the contract from the active list;
        # a revert puts it back. Whatever active change remains is the
        # player accepting or cancelling contracts.
        unexplained_active = d_active + completed + failed - uncompleted - unfailed
        if unexplained_active > 0:
            changes += [(EventKind.CONTRACT_ACCEPTED, terms, terms.advance)] * unexplained_active
        elif unexplained_active < 0:
            changes += [(EventKind.CONTRACT_CANCELLED, terms, terms.penalty)] * -unexplained_active
    return changes


def _delta(before: Counter, after: Counter, terms: ContractTerms) -> int:
    """Change in the number of copies of a contract in one group.

    Args:
        before: Counts in the earlier snapshot.
        after: Counts in the later snapshot.
        terms: The contract.

    Returns:
        Positive when copies were added, negative when removed.
    """
    return after.get(terms, 0) - before.get(terms, 0)


def _negate(amount: Resources) -> Resources:
    """Flip the sign of every currency.

    Args:
        amount: Amount to negate.

    Returns:
        The opposite amount.
    """
    return Resources() - amount


def _describe_other(delta: Resources) -> str:
    """Pick a helpful description for a change that could not be attributed.

    Args:
        delta: The unexplained change.

    Returns:
        A short text hinting at the most likely causes.
    """
    if delta.funds > 0:
        return "Other income (milestones, strategies...)"
    if delta.funds < 0:
        return "Other expense (facilities, part purchases...)"
    if delta.science > 0:
        return "Science gained (experiments)"
    if delta.science < 0:
        return "Science spent (R&D)"
    return "Reputation change"


@dataclass(frozen=True)
class BreakdownItem:
    """One line of the funds breakdown.

    Attributes:
        key: Stable machine identifier (used by the web UI).
        label: Human readable name.
        funds: Total funds of the category (negative for costs).
    """

    key: str
    label: str
    funds: float


@dataclass(frozen=True)
class VesselEconomics:
    """Money spent and recovered for all launches of one vessel design.

    Attributes:
        name: Vessel name.
        launches: Number of launches that were not reverted.
        cost: Total funds charged at launch (negative).
        recovered: Funds returned by recoveries and reverts (positive).
    """

    name: str
    launches: int
    cost: float
    recovered: float

    @property
    def net(self) -> float:
        """Net funds spent on the vessel.

        Returns:
            ``cost + recovered`` (negative means money spent).
        """
        return self.cost + self.recovered


@dataclass(frozen=True)
class LedgerSummary:
    """Aggregated answer to "am I earning or losing money?".

    Attributes:
        contract_advances: Funds received when accepting contracts.
        contract_rewards: Funds received when completing contracts.
        contract_penalties: Funds lost to failures, cancellations and reverts.
        launch_costs: Funds charged for launching vessels (negative).
        recoveries: Funds recovered from vessels and reverted launches.
        other: Funds changes that are neither contracts nor vessels.
        contract_science: Science earned from contracts.
        contract_reputation: Nominal reputation earned from contracts.
        vessels: Per-vessel economics, most expensive first.
    """

    contract_advances: float
    contract_rewards: float
    contract_penalties: float
    launch_costs: float
    recoveries: float
    other: float
    contract_science: float
    contract_reputation: float
    vessels: tuple[VesselEconomics, ...]

    @property
    def contracts_net(self) -> float:
        """Net funds from the contract system.

        Returns:
            Advances + rewards + penalties.
        """
        return self.contract_advances + self.contract_rewards + self.contract_penalties

    @property
    def vessels_net(self) -> float:
        """Net funds spent on vessels.

        Returns:
            Launch costs + recoveries (usually negative).
        """
        return self.launch_costs + self.recoveries

    @property
    def mission_balance(self) -> float:
        """The headline figure: contracts income minus vessel spending.

        Returns:
            Positive when contracts pay for the rockets.
        """
        return self.contracts_net + self.vessels_net

    @property
    def total(self) -> float:
        """Every funds change in the ledger.

        Returns:
            Mission balance plus the unattributed changes.
        """
        return self.mission_balance + self.other

    def breakdown(self) -> list[BreakdownItem]:
        """Funds per category, in a fixed order for the UI.

        Returns:
            The breakdown lines.
        """
        return [
            BreakdownItem("contract_advances", "Contract advances", self.contract_advances),
            BreakdownItem("contract_rewards", "Contract rewards", self.contract_rewards),
            BreakdownItem("contract_penalties", "Contract penalties", self.contract_penalties),
            BreakdownItem("launch_costs", "Vessel launches", self.launch_costs),
            BreakdownItem("recoveries", "Recoveries and reverts", self.recoveries),
            BreakdownItem("other", "Other", self.other),
        ]

    @classmethod
    def from_events(cls, events: list[LedgerEvent]) -> "LedgerSummary":
        """Aggregate a list of ledger events.

        Args:
            events: Events in any order.

        Returns:
            The summary of all the events.
        """
        totals = Counter[str]()
        vessels: dict[str, dict[str, float]] = {}

        for event in events:
            funds = event.delta.funds
            kind = event.kind
            if kind is EventKind.CONTRACT_ACCEPTED:
                totals["advances"] += funds
            elif kind is EventKind.CONTRACT_COMPLETED:
                totals["rewards"] += funds
            elif kind.is_contract:
                totals["penalties"] += funds
            elif kind is EventKind.VESSEL_LAUNCHED:
                totals["launches"] += funds
            elif kind.is_vessel:
                totals["recoveries"] += funds
            else:
                totals["other"] += funds

            if kind.is_contract:
                totals["science"] += event.delta.science
                totals["reputation"] += event.delta.reputation

            if kind.is_vessel:
                stats = vessels.setdefault(
                    event.vessel or "Unknown vessel",
                    {"launches": 0, "cost": 0.0, "recovered": 0.0},
                )
                if kind is EventKind.VESSEL_LAUNCHED:
                    stats["launches"] += 1
                    stats["cost"] += funds
                else:
                    stats["recovered"] += funds
                    # A revert cancels the launch: it should not count as a flight.
                    if kind is EventKind.LAUNCH_REVERTED:
                        stats["launches"] -= 1

        per_vessel = sorted(
            (
                VesselEconomics(name, int(max(s["launches"], 0)), s["cost"], s["recovered"])
                for name, s in vessels.items()
            ),
            key=lambda v: v.net,
        )
        return cls(
            contract_advances=totals["advances"],
            contract_rewards=totals["rewards"],
            contract_penalties=totals["penalties"],
            launch_costs=totals["launches"],
            recoveries=totals["recoveries"],
            other=totals["other"],
            contract_science=totals["science"],
            contract_reputation=totals["reputation"],
            vessels=tuple(per_vessel),
        )
