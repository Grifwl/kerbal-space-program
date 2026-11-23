"""Value objects describing the state of a KSP career economy.

All classes are immutable dataclasses: a snapshot of the game is a fact that
happened at a given moment and must never be mutated afterwards. Immutability
also makes them safe to share between the polling thread and the web thread.
"""

from __future__ import annotations

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
    """

    contract_type: str
    title: str
    funds_advance: float = 0.0
    funds_completion: float = 0.0
    funds_failure: float = 0.0
    science_completion: float = 0.0
    reputation_completion: float = 0.0
    reputation_failure: float = 0.0

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


@dataclass(frozen=True)
class ContractBook:
    """The player's contracts grouped by state.

    Each group is stored as a tuple of terms but compared as a multiset, so
    the order in which kRPC returns contracts is irrelevant.

    Attributes:
        active: Contracts accepted and in progress.
        completed: Contracts completed successfully.
        failed: Contracts that failed (deadline expired or explicitly failed).
    """

    active: tuple[ContractTerms, ...] = ()
    completed: tuple[ContractTerms, ...] = ()
    failed: tuple[ContractTerms, ...] = ()

    def counts(self) -> tuple[Counter, Counter, Counter]:
        """Count how many times each contract appears in every group.

        Returns:
            Three ``Counter`` objects: active, completed and failed.
        """
        return Counter(self.active), Counter(self.completed), Counter(self.failed)


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
    """

    taken_at: datetime
    universal_time: float
    scene: GameScene
    resources: Resources
    active_vessel: str | None = None
    contracts: ContractBook | None = None


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
    """

    occurred_at: datetime
    universal_time: float
    kind: EventKind
    description: str
    delta: Resources = field(default_factory=Resources)
    vessel: str | None = None
