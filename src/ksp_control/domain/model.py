"""Value objects describing the state of a KSP career economy.

All classes are immutable dataclasses: a snapshot of the game is a fact that
happened at a given moment and must never be mutated afterwards. Immutability
also makes them safe to share between the polling thread and the web thread.
"""

from __future__ import annotations

import hashlib
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum


class GameScene(str, Enum):
    """The KSP scene the player is currently in.

    The scene matters to the economy rules because KSP charges and refunds
    vessel costs on scene transitions (editor -> flight charges the vessel,
    flight -> space center recovers it, flight -> editor reverts it).
    """

    SPACE_CENTER = "space_center"
    FLIGHT = "flight"
    TRACKING_STATION = "tracking_station"
    EDITOR_VAB = "editor_vab"
    EDITOR_SPH = "editor_sph"
    UNKNOWN = "unknown"

    @property
    def is_editor(self) -> bool:
        """Whether the scene is one of the vessel editors (VAB or SPH).

        Returns:
            True for the Vehicle Assembly Building and the Space Plane Hangar.
        """
        return self in (GameScene.EDITOR_VAB, GameScene.EDITOR_SPH)

    @classmethod
    def from_name(cls, name: str | None) -> "GameScene":
        """Build a scene from a loosely formatted name.

        Args:
            name: Scene name such as ``"flight"`` or ``"EditorVAB"``. Case,
                underscores and spaces are ignored.

        Returns:
            The matching scene, or ``GameScene.UNKNOWN`` when nothing matches.
        """
        if not name:
            return cls.UNKNOWN
        normalized = name.replace("_", "").replace(" ", "").lower()
        for scene in cls:
            if scene.value.replace("_", "") == normalized:
                return scene
        return cls.UNKNOWN


@dataclass(frozen=True)
class Resources:
    """An amount (or a change) of the three career currencies.

    The same type is used both for absolute balances and for deltas, which
    keeps the arithmetic in the ledger rules short and readable.

    Attributes:
        funds: Money, in KSP funds (√).
        science: Science points.
        reputation: Reputation points.
    """

    funds: float = 0.0
    science: float = 0.0
    reputation: float = 0.0

    # Changes smaller than these are floating point noise (KSP stores the
    # balances as doubles and reputation in particular drifts by tiny amounts).
    FUNDS_EPSILON = 0.5
    SCIENCE_EPSILON = 0.01
    REPUTATION_EPSILON = 0.01

    def __add__(self, other: "Resources") -> "Resources":
        """Add two amounts currency by currency.

        Args:
            other: The amount to add.

        Returns:
            A new ``Resources`` holding the sum.
        """
        return Resources(
            self.funds + other.funds,
            self.science + other.science,
            self.reputation + other.reputation,
        )

    def __sub__(self, other: "Resources") -> "Resources":
        """Subtract two amounts currency by currency.

        Args:
            other: The amount to subtract.

        Returns:
            A new ``Resources`` holding the difference.
        """
        return Resources(
            self.funds - other.funds,
            self.science - other.science,
            self.reputation - other.reputation,
        )

    def only_funds(self) -> "Resources":
        """Keep the funds component and zero the others.

        Returns:
            A new ``Resources`` with only ``funds`` set.
        """
        return Resources(funds=self.funds)

    def without_funds(self) -> "Resources":
        """Zero the funds component and keep the others.

        Returns:
            A new ``Resources`` with ``funds`` set to zero.
        """
        return Resources(science=self.science, reputation=self.reputation)

    def cleaned(self) -> "Resources":
        """Round each currency and drop values that are only numeric noise.

        Returns:
            A new ``Resources`` where negligible components are exactly zero.
        """
        return Resources(
            _clean(self.funds, self.FUNDS_EPSILON),
            _clean(self.science, self.SCIENCE_EPSILON),
            _clean(self.reputation, self.REPUTATION_EPSILON),
        )

    @property
    def is_negligible(self) -> bool:
        """Whether every component is below its noise threshold.

        Returns:
            True when the amount can be safely treated as zero.
        """
        return (
            abs(self.funds) < self.FUNDS_EPSILON
            and abs(self.science) < self.SCIENCE_EPSILON
            and abs(self.reputation) < self.REPUTATION_EPSILON
        )


def _clean(value: float, epsilon: float) -> float:
    """Round a currency value and snap noise to zero.

    Args:
        value: Raw value.
        epsilon: Magnitude below which the value is considered noise.

    Returns:
        ``0.0`` for noise, otherwise the value rounded to two decimals.
    """
    return 0.0 if abs(value) < epsilon else round(value, 2)


@dataclass(frozen=True)
class ParameterTerms:
    """The rewards of one contract parameter (an objective of a contract).

    Many contracts pay most of their money per objective rather than on
    completion: tourism contracts, for instance, pay each tourist's
    itinerary and each of its stops, while the contract itself pays 0.

    Attributes:
        title: Human readable objective.
        funds: Funds paid when the objective is completed.
        science: Science granted when the objective is completed.
        reputation: Nominal reputation granted when it is completed.
        optional: Whether the contract can complete without it.
    """

    title: str
    funds: float = 0.0
    science: float = 0.0
    reputation: float = 0.0
    optional: bool = False

    @property
    def reward(self) -> Resources:
        """Resources received when completing the objective.

        Returns:
            The objective rewards.
        """
        return Resources(self.funds, self.science, self.reputation)


@dataclass(frozen=True)
class ContractTerms:
    """The economic terms of one contract.

    kRPC does not expose a stable contract identifier (the remote object ids
    may change when KSP reloads the save on a scene change), so the terms
    themselves act as the identity of a contract. Two different contracts
    with identical title *and* identical rewards are indistinguishable, which
    is harmless: they are economically identical too. Contract lists are
    therefore compared as multisets of terms (see ``ContractBook``).

    Attributes:
        contract_type: KSP contract type name (e.g. ``"PartTest"``).
        title: Human readable contract title.
        funds_advance: Funds paid when the contract is accepted.
        funds_completion: Funds paid when the contract is completed.
        funds_failure: Funds charged when the contract fails or is cancelled,
            stored as a positive number like KSP does.
        science_completion: Science granted on completion.
        reputation_completion: Nominal reputation granted on completion.
        reputation_failure: Nominal reputation lost on failure, positive.
        parameters: Every objective of the contract, nested ones included,
            flattened depth-first. Their rewards are paid on top of the
            contract's own completion reward.
    """

    contract_type: str
    title: str
    funds_advance: float = 0.0
    funds_completion: float = 0.0
    funds_failure: float = 0.0
    science_completion: float = 0.0
    reputation_completion: float = 0.0
    reputation_failure: float = 0.0
    parameters: tuple[ParameterTerms, ...] = ()

    @property
    def key(self) -> str:
        """Stable identifier of these terms, usable across program runs.

        Titles repeat (KSP offers many "Ferry a VIP..." contracts), so the
        key covers the whole terms, rewards and objectives included.

        Returns:
            A short hexadecimal digest.
        """
        return hashlib.sha1(repr(self).encode("utf-8")).hexdigest()[:16]

    @property
    def advance(self) -> Resources:
        """Resources received when accepting the contract.

        Returns:
            The advance payment.
        """
        return Resources(funds=self.funds_advance)

    @property
    def reward(self) -> Resources:
        """Resources received when completing the contract.

        Returns:
            The completion rewards.
        """
        return Resources(
            self.funds_completion, self.science_completion, self.reputation_completion
        )

    @property
    def penalty(self) -> Resources:
        """Resources lost when the contract fails or is cancelled.

        Returns:
            A negative amount (abs() protects against either sign convention).
        """
        return Resources(
            funds=-abs(self.funds_failure), reputation=-abs(self.reputation_failure)
        )

    def objectives_paid(self, progress: tuple[bool, ...]) -> Resources:
        """Rewards of the objectives already completed.

        Args:
            progress: Completion flag of each parameter, aligned with
                ``parameters``. Missing flags count as not completed.

        Returns:
            The sum of the rewards of the completed objectives.
        """
        return sum(
            (p.reward for p, done in zip(self.parameters, progress) if done), Resources()
        )

    def objectives_pending(self, progress: tuple[bool, ...]) -> Resources:
        """Rewards of the mandatory objectives not completed yet.

        Completing the contract implies completing all of them, so this is
        what KSP still pays on top of the contract reward at completion.

        Args:
            progress: Completion flag of each parameter, aligned with
                ``parameters``. Missing flags count as not completed.

        Returns:
            The sum of the rewards of the pending non-optional objectives.
        """
        flags = tuple(progress) + (False,) * (len(self.parameters) - len(progress))
        return sum(
            (p.reward for p, done in zip(self.parameters, flags) if not done and not p.optional),
            Resources(),
        )


@dataclass(frozen=True)
class ContractBook:
    """The player's contracts grouped by state.

    Each group is stored as a tuple of terms but compared as a multiset, so
    the order in which kRPC returns contracts is irrelevant.

    Attributes:
        active: Contracts accepted and in progress.
        completed: Contracts completed successfully.
        failed: Contracts that failed (deadline expired or explicitly failed).
        active_progress: For each active contract (same order as
            ``active``), the completion flag of each of its parameters.
            Only active contracts carry progress: once a contract is
            finished, KSP no longer reports its objectives reliably.
    """

    active: tuple[ContractTerms, ...] = ()
    completed: tuple[ContractTerms, ...] = ()
    failed: tuple[ContractTerms, ...] = ()
    active_progress: tuple[tuple[bool, ...], ...] = ()

    def progress_by_contract(self) -> dict[ContractTerms, list[tuple[bool, ...]]]:
        """Objective progress of every active copy of each contract.

        Returns:
            For each active contract, the progress flags of every copy, the
            most advanced copy first (the one most likely to finish first).
        """
        progress: dict[ContractTerms, list[tuple[bool, ...]]] = {}
        for index, terms in enumerate(self.active):
            flags = self.active_progress[index] if index < len(self.active_progress) else ()
            progress.setdefault(terms, []).append(tuple(flags))
        for copies in progress.values():
            copies.sort(key=sum, reverse=True)
        return progress

    def counts(self) -> tuple[Counter, Counter, Counter]:
        """Count how many times each contract appears in every group.

        Returns:
            Three ``Counter`` objects: active, completed and failed.
        """
        return Counter(self.active), Counter(self.completed), Counter(self.failed)

    @property
    def is_empty(self) -> bool:
        """Whether there are no contracts at all in any group.

        Returns:
            True when active, completed and failed are all empty.
        """
        return not (self.active or self.completed or self.failed)

    def same_as(self, other: "ContractBook") -> bool:
        """Compare two books ignoring the order of the contracts.

        Args:
            other: The book to compare with.

        Returns:
            True when every group holds the same contracts.
        """
        return self.counts() == other.counts() and Counter(
            zip(self.active, self.active_progress)
        ) == Counter(zip(other.active, other.active_progress))


@dataclass(frozen=True)
class GameSnapshot:
    """Everything the economy monitor reads from the game in a single poll.

    Attributes:
        taken_at: Wall clock time (timezone aware, UTC) of the reading.
        universal_time: KSP universal time in seconds since the career start.
        scene: Scene the player was in.
        resources: Funds, science and reputation balances.
        active_vessel: Name of the active vessel, only meaningful in flight.
        contracts: Contract lists, or ``None`` when they could not be read
            (contract diffs are then skipped instead of being misread).
        vessel_prelaunch: Whether the active vessel is sitting on the launch
            pad or runway, not launched yet. KSP charges the vessel cost a
            moment *after* the flight scene opens, so this is what lets a
            late charge still be recognized as the launch cost.
        vessels: Names of every vessel in the save, or ``None`` when they
            cannot be read (kRPC 0.5.x only lists them in flight). Used to
            recognize recoveries of non-active vessels during a flight.
    """

    taken_at: datetime
    universal_time: float
    scene: GameScene
    resources: Resources
    active_vessel: str | None = None
    contracts: ContractBook | None = None
    vessel_prelaunch: bool = False
    vessels: tuple[str, ...] | None = None


@dataclass(frozen=True)
class ResourceSample:
    """A persisted point of the funds/science/reputation time series.

    This is a ``GameSnapshot`` without the contract details: contracts are
    only needed to detect events, not to draw the charts.

    Attributes:
        taken_at: Wall clock time of the reading (UTC).
        universal_time: KSP universal time in seconds.
        scene: Scene the player was in.
        resources: Balances at that moment.
        active_vessel: Name of the active vessel, if any.
    """

    taken_at: datetime
    universal_time: float
    scene: GameScene
    resources: Resources
    active_vessel: str | None = None

    @classmethod
    def from_snapshot(cls, snapshot: GameSnapshot) -> "ResourceSample":
        """Drop the contract details of a snapshot.

        Args:
            snapshot: Full game snapshot.

        Returns:
            The sample to persist for the charts.
        """
        return cls(
            snapshot.taken_at,
            snapshot.universal_time,
            snapshot.scene,
            snapshot.resources,
            snapshot.active_vessel,
        )


class EventKind(str, Enum):
    """What caused a change in the career balances."""

    CONTRACT_ACCEPTED = "contract_accepted"
    CONTRACT_COMPLETED = "contract_completed"
    CONTRACT_PROGRESS = "contract_progress"
    CONTRACT_FAILED = "contract_failed"
    CONTRACT_CANCELLED = "contract_cancelled"
    CONTRACT_REVERTED = "contract_reverted"
    VESSEL_LAUNCHED = "vessel_launched"
    VESSEL_RECOVERED = "vessel_recovered"
    LAUNCH_REVERTED = "launch_reverted"
    OTHER = "other"

    @property
    def is_contract(self) -> bool:
        """Whether the event comes from the contract system.

        Returns:
            True for every ``CONTRACT_*`` kind.
        """
        return self.value.startswith("contract_")

    @property
    def is_vessel(self) -> bool:
        """Whether the event is a vessel cost or refund.

        Returns:
            True for launches, recoveries and reverted launches.
        """
        return self in (
            EventKind.VESSEL_LAUNCHED,
            EventKind.VESSEL_RECOVERED,
            EventKind.LAUNCH_REVERTED,
        )


@dataclass(frozen=True)
class LedgerEvent:
    """One entry of the career ledger.

    Attributes:
        occurred_at: Wall clock time when the event was detected (UTC).
        universal_time: KSP universal time when the event was detected.
        kind: What caused the change.
        description: Human readable explanation (contract title, vessel...).
        delta: Change in funds, science and reputation caused by the event.
        vessel: Vessel involved, for vessel events.
        contract: Key of the contract involved, for contract events. Only
            used while running (not persisted with the event).
        objectives: Indices of the contract objectives this event paid, for
            ``CONTRACT_PROGRESS`` events. Only used while running.
    """

    occurred_at: datetime
    universal_time: float
    kind: EventKind
    description: str
    delta: Resources = field(default_factory=Resources)
    vessel: str | None = None
    contract: str | None = None
    objectives: tuple[int, ...] = ()
