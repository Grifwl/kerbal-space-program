"""Rules to decide which vessels are worth recovering.

In stock KSP recovering a vessel never costs funds: the game pays the value
of its parts and of the resources left on board, scaled by a *recovery
factor* that depends on how far from the Kerbal Space Center it landed.
The "is it worth it?" question therefore becomes:

* is the estimated payout above a minimum the player cares about, and
* would recovering it cost something that is not funds? A landed base or
  station may be the subject of a contract, and recovering the active
  vessel ends the flight, so those are excluded unless asked for.

kRPC does not expose KSP's own recovery value, so it is estimated here with
the same formula the game uses.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field

# Kerbal Space Center launch pad, in degrees (stock Kerbin).
KSC_LATITUDE = -0.0972
KSC_LONGITUDE = -74.5577
HOME_BODY = "Kerbin"
# Anything landed this close to the launch pad is on KSC grounds, where KSP
# refunds 100% of the value.
KSC_GROUNDS_RADIUS_M = 3_000.0

# Unit cost (funds per unit) of the stock resources. kRPC 0.5.x does not
# expose it; unknown resources are treated as free, which can only make the
# estimate slightly higher than the real payout (never lower).
STOCK_RESOURCE_UNIT_COST: dict[str, float] = {
    "LiquidFuel": 0.8,
    "Oxidizer": 0.18,
    "SolidFuel": 0.6,
    "MonoPropellant": 1.2,
    "XenonGas": 4.0,
    "Ore": 0.02,
    "Ablator": 0.5,
    "ElectricCharge": 0.0,
    "IntakeAir": 0.0,
    "EVA Propellant": 0.0,
}

# Vessel types that are usually placed on purpose (often for a contract).
DEFAULT_PROTECTED_TYPES = frozenset({"base", "station", "relay", "space_object"})


@dataclass(frozen=True)
class ResourceLoad:
    """A resource tank of a part.

    Attributes:
        name: KSP resource name (e.g. ``"LiquidFuel"``).
        amount: Units currently on board.
        capacity: Maximum units.
    """

    name: str
    amount: float
    capacity: float


@dataclass(frozen=True)
class PartValue:
    """What a part is worth before the recovery factor is applied.

    Attributes:
        cost: KSP catalog cost of the part, *including* full tanks (this is
            what kRPC's ``Part.cost`` returns in 0.5.x).
        resources: The part's tanks.
    """

    cost: float
    resources: tuple[ResourceLoad, ...] = ()

    @property
    def value(self) -> float:
        """Current value: catalog cost minus the cost of the missing resources.

        Returns:
            Funds the part is worth with its current fuel level.
        """
        missing = sum(
            (r.capacity - r.amount) * STOCK_RESOURCE_UNIT_COST.get(r.name, 0.0)
            for r in self.resources
        )
        return max(self.cost - missing, 0.0)


@dataclass(frozen=True)
class VesselReport:
    """Raw facts about one vessel, as read from the game.

    Attributes:
        vessel_id: Identifier valid for the adapter that produced it.
        name: Vessel name.
        vessel_type: KSP type in lower case (``"debris"``, ``"ship"``...).
        situation: KSP situation in lower case (``"landed"``...).
        body: Name of the body it is on or orbiting.
        body_radius: Radius of that body, in meters.
        latitude: Degrees, or ``None`` when unknown (kRPC only gives the
            position of vessels in the flight scene).
        longitude: Degrees, or ``None`` when unknown.
        recoverable: What KSP says (landed or splashed on the home world).
        is_active: Whether it is the vessel being flown.
        crew: Number of kerbals on board.
        parts: Part values, or ``None`` when they could not be read.
    """

    vessel_id: str
    name: str
    vessel_type: str
    situation: str
    body: str
    body_radius: float
    latitude: float | None
    longitude: float | None
    recoverable: bool
    is_active: bool = False
    crew: int = 0
    parts: tuple[PartValue, ...] | None = None


@dataclass(frozen=True)
class RecoveryPolicy:
    """What the player wants recovered.

    Attributes:
        min_value: Minimum estimated payout, in funds, to bother recovering.
        include_active: Also recover the vessel being flown (ends the flight).
        recover_unknown: Recover vessels whose value cannot be estimated
            (outside physics range kRPC cannot read their parts). Safe by
            default because recovering never costs funds in stock KSP.
        protected_types: Vessel types never recovered automatically.
    """

    min_value: float = 1.0
    include_active: bool = False
    recover_unknown: bool = True
    protected_types: frozenset[str] = field(default_factory=lambda: DEFAULT_PROTECTED_TYPES)


@dataclass(frozen=True)
class RecoveryDecision:
    """The verdict for one vessel.

    Attributes:
        vessel: The vessel.
        recover: Whether it should be recovered.
        reason: Short human readable explanation.
        estimated_value: Estimated payout in funds, if computable.
        distance_km: Great-circle distance to the KSC, if on the home world.
    """

    vessel: VesselReport
    recover: bool
    reason: str
    estimated_value: float | None = None
    distance_km: float | None = None


def distance_to_ksc(latitude: float, longitude: float, body_radius: float) -> float:
    """Great-circle distance from a point on Kerbin to the KSC launch pad.

    Args:
        latitude: Degrees.
        longitude: Degrees.
        body_radius: Kerbin radius in meters.

    Returns:
        Distance in meters.
    """
    lat1, lon1 = math.radians(latitude), math.radians(longitude)
    lat2, lon2 = math.radians(KSC_LATITUDE), math.radians(KSC_LONGITUDE)
    # Haversine formula: numerically stable for the short distances that
    # matter most here (stages landing a few km from the pad).
    a = (
        math.sin((lat2 - lat1) / 2) ** 2
        + math.cos(lat1) * math.cos(lat2) * math.sin((lon2 - lon1) / 2) ** 2
    )
    return 2 * body_radius * math.asin(min(1.0, math.sqrt(a)))


def recovery_factor(distance_m: float, body_radius: float) -> float:
    """Fraction of the value KSP pays back, by distance to the KSC.

    KSP pays 100% on KSC grounds and otherwise interpolates linearly from
    98% next to the KSC down to 10% at the opposite side of Kerbin.

    Args:
        distance_m: Great-circle distance to the KSC.
        body_radius: Kerbin radius in meters.

    Returns:
        A factor between 0.1 and 1.0.
    """
    if distance_m <= KSC_GROUNDS_RADIUS_M:
        return 1.0
    half_circumference = math.pi * body_radius
    t = min(max(distance_m / half_circumference, 0.0), 1.0)
    return 0.98 + (0.1 - 0.98) * t


def estimate_recovery_value(vessel: VesselReport) -> tuple[float | None, float | None]:
    """Estimate what recovering a vessel would pay.

    Args:
        vessel: The vessel.

    Returns:
        ``(value, distance_m)``; either may be ``None`` when unknown.
    """
    if vessel.body != HOME_BODY or vessel.latitude is None or vessel.longitude is None:
        return None, None
    distance = distance_to_ksc(vessel.latitude, vessel.longitude, vessel.body_radius)
    if vessel.parts is None:
        return None, distance
    raw = sum(part.value for part in vessel.parts)
    return raw * recovery_factor(distance, vessel.body_radius), distance


def plan_recovery(vessels: list[VesselReport], policy: RecoveryPolicy) -> list[RecoveryDecision]:
    """Decide, vessel by vessel, what to recover and in which order.

    The vessels to recover come first, highest payout first, and the active
    vessel always last: recovering it ends the flight scene, which would
    leave the rest of the list unreachable.

    Args:
        vessels: Every vessel the game reports.
        policy: The player's preferences.

    Returns:
        One decision per vessel.
    """
    decisions = [_decide(vessel, policy) for vessel in vessels]
    return sorted(
        decisions,
        key=lambda d: (
            not d.recover,
            d.vessel.is_active,
            -(d.estimated_value or 0.0),
            d.vessel.name,
        ),
    )


def _decide(vessel: VesselReport, policy: RecoveryPolicy) -> RecoveryDecision:
    """Apply the policy to one vessel.

    Args:
        vessel: The vessel.
        policy: The player's preferences.

    Returns:
        The decision with its reason.
    """
    value, distance = estimate_recovery_value(vessel)
    distance_km = distance / 1000 if distance is not None else None

    def verdict(recover: bool, reason: str) -> RecoveryDecision:
        return RecoveryDecision(vessel, recover, reason, value, distance_km)

    if not vessel.recoverable:
        return verdict(False, f"not recoverable ({vessel.situation} at {vessel.body})")
    if vessel.vessel_type in policy.protected_types:
        return verdict(False, f"{vessel.vessel_type} vessels are protected (may be a contract)")
    if vessel.is_active and not policy.include_active:
        return verdict(False, "active vessel (use --include-active to end the flight)")
    if value is None:
        if policy.recover_unknown:
            return verdict(True, "value unknown; recovery never costs funds")
        return verdict(False, "value unknown")
    if value < policy.min_value:
        return verdict(False, f"worth less than the minimum of {policy.min_value:,.0f}")
    return verdict(True, "worth recovering")
