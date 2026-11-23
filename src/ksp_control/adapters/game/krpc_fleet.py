"""Fleet adapter: lists and recovers vessels through kRPC.

Notes on kRPC 0.5.x that shape this adapter:

* Vessel procedures are only available in the flight scene.
* ``Part.cost`` returns KSP's catalog cost, which includes full tanks; the
  domain subtracts the missing fuel using the resource amounts read here.
* Parts of vessels outside physics range (unloaded) are not readable, and
  outside the flight scene neither parts nor positions are; the value is
  then reported as unknown rather than guessed.
"""

from __future__ import annotations

import logging
from typing import Any

from ksp_control.adapters.game.krpc_connection import (
    RECOVERABLE_ERRORS,
    KrpcEndpoint,
    connect,
    is_connection_problem,
    read_scene,
)
from ksp_control.application.ports import FleetPort, GameUnavailableError
from ksp_control.domain.model import GameScene
from ksp_control.domain.recovery import PartValue, ResourceLoad, VesselReport

logger = logging.getLogger(__name__)


class KrpcFleet(FleetPort):
    """Reads and recovers vessels of a running KSP game.

    Responsibilities: translate remote ``Vessel`` objects into
    ``VesselReport`` values and keep the mapping from report ids back to the
    remote objects, so a later ``recover`` acts on the same vessel.

    Usage notes: not thread safe; meant for one short-lived script.
    """

    def __init__(self, endpoint: KrpcEndpoint, client_name: str = "KSP vessel recovery") -> None:
        """Configure the adapter; the connection is opened on first use.

        Args:
            endpoint: kRPC server location.
            client_name: Name shown in the kRPC server window in KSP.
        """
        self._endpoint = endpoint
        self._client_name = client_name
        self._conn: Any = None
        self._vessels: dict[str, Any] = {}

    def scene(self) -> GameScene:
        """Return the scene the game is in.

        Returns:
            The current scene.
        """
        return self._call(lambda conn: read_scene(conn))

    def funds(self) -> float:
        """Return the current funds balance.

        Returns:
            Funds.
        """
        return self._call(lambda conn: float(conn.space_center.funds))

    def list_vessels(self) -> list[VesselReport]:
        """Read every vessel of the save.

        Returns:
            One report per vessel.
        """
        def read(conn: Any) -> list[VesselReport]:
            space_center = conn.space_center
            # None outside the flight scene.
            active = space_center.active_vessel
            reports = []
            self._vessels.clear()
            for index, vessel in enumerate(space_center.vessels):
                vessel_id = str(index)
                self._vessels[vessel_id] = vessel
                reports.append(self._report(vessel_id, vessel, vessel == active))
            return reports

        return self._call(read)

    def vessel_exists(self, vessel_id: str) -> bool:
        """Tell whether a listed vessel is still in the game.

        Args:
            vessel_id: Identifier from ``list_vessels``.

        Returns:
            False once it has been recovered or destroyed.
        """
        vessel = self._vessels[vessel_id]
        return self._call(lambda conn: vessel in conn.space_center.vessels)

    def recover(self, vessel_id: str) -> None:
        """Ask KSP to recover a vessel.

        Args:
            vessel_id: Identifier from ``list_vessels``.
        """
        vessel = self._vessels[vessel_id]
        self._call(lambda conn: vessel.recover())

    def close(self) -> None:
        """Close the kRPC connection, if open."""
        if self._conn is not None:
            try:
                self._conn.close()
            except Exception:  # noqa: BLE001 - closing must never fail
                pass
        self._conn = None
        self._vessels.clear()

    def _call(self, action: Any) -> Any:
        """Run an action against the game, translating kRPC errors.

        Args:
            action: Callable receiving the kRPC client.

        Returns:
            Whatever the action returns.

        Raises:
            GameUnavailableError: On any kRPC failure.
        """
        if self._conn is None:
            self._conn = connect(self._endpoint, self._client_name)
        try:
            return action(self._conn)
        except RECOVERABLE_ERRORS as error:
            if is_connection_problem(error):
                self.close()
            # kRPC appends the C# stack trace; the first line is enough.
            message = str(error).splitlines()[0] if str(error) else type(error).__name__
            raise GameUnavailableError(message) from error

    @staticmethod
    def _report(vessel_id: str, vessel: Any, is_active: bool) -> VesselReport:
        """Read the facts needed to value one vessel.

        Args:
            vessel_id: Identifier to give to the report.
            vessel: Remote ``Vessel`` object.
            is_active: Whether it is the active vessel.

        Returns:
            The vessel report.
        """
        body = vessel.orbit.body
        latitude, longitude = _read_position(vessel)
        return VesselReport(
            vessel_id=vessel_id,
            name=str(vessel.name),
            vessel_type=_enum_name(vessel.type),
            situation=_enum_name(vessel.situation),
            body=str(body.name),
            body_radius=float(body.equatorial_radius),
            latitude=latitude,
            longitude=longitude,
            recoverable=bool(vessel.recoverable),
            is_active=is_active,
            crew=int(vessel.crew_count),
            parts=_read_parts(vessel),
        )


def _read_position(vessel: Any) -> tuple[float | None, float | None]:
    """Read where a vessel is, when the scene allows it.

    Args:
        vessel: Remote ``Vessel`` object.

    Returns:
        ``(latitude, longitude)`` in degrees, or ``(None, None)`` outside the
        flight scene, where kRPC does not provide positions.
    """
    try:
        flight = vessel.flight()
        return float(flight.latitude), float(flight.longitude)
    except RECOVERABLE_ERRORS as error:
        if is_connection_problem(error):
            raise
        return None, None


def _read_parts(vessel: Any) -> tuple[PartValue, ...] | None:
    """Read the cost and tanks of every part of a vessel.

    Args:
        vessel: Remote ``Vessel`` object.

    Returns:
        The part values, or ``None`` when the parts are not accessible.
        kRPC 0.5.x returns an *empty* part list for vessels outside physics
        range (about 2.5 km from the active vessel), which is reported as
        unknown rather than as a vessel worth nothing.
    """
    try:
        parts = []
        for part in vessel.parts.all:
            tanks = tuple(
                ResourceLoad(str(r.name), float(r.amount), float(r.max))
                for r in part.resources.all
            )
            parts.append(PartValue(float(part.cost), tanks))
        return tuple(parts) or None
    except RECOVERABLE_ERRORS as error:
        if is_connection_problem(error):
            raise
        logger.debug("Parts of %s unavailable: %s", vessel, error)
        return None


def _enum_name(value: Any) -> str:
    """Lower-case name of a kRPC enum value.

    Args:
        value: kRPC enum member.

    Returns:
        Its name, e.g. ``"debris"`` or ``"splashed"``.
    """
    return str(getattr(value, "name", value)).lower()
