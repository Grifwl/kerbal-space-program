"""Telemetry adapter backed by a real KSP instance through kRPC.

This is the only module of the package that imports ``krpc``. Everything
kRPC-specific is handled here:

* lazy (re)connection, so the monitor can be started before KSP and
  survives KSP restarts;
* differences between kRPC versions (e.g. ``KRPC.CurrentGameScene`` was
  renamed ``KRPC.GameScene`` in kRPC 0.7.0);
* translation of remote contract objects into immutable ``ContractTerms``.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

from ksp_control.application.ports import GameTelemetryPort, GameUnavailableError
from ksp_control.domain.model import (
    ContractBook,
    ContractTerms,
    GameScene,
    GameSnapshot,
    Resources,
)

logger = logging.getLogger(__name__)

# Exceptions raised by kRPC when the server is unreachable or a call fails.
# ConnectionError/OSError cover sockets; RuntimeError covers krpc.error.RPCError
# and the "procedure not available in this game scene" errors.
_RECOVERABLE_ERRORS = (ConnectionError, OSError, RuntimeError, EOFError, ValueError)


class KrpcTelemetry(GameTelemetryPort):
    """Reads funds, science, reputation, scene and contracts through kRPC.

    Responsibilities:

    * Own the kRPC connection and reconnect transparently after failures.
    * Cache contract terms by remote object, because reading a contract costs
      eight remote calls and a long career has hundreds of finished
      contracts. Only contracts never seen before are fetched, so a normal
      poll costs a handful of calls.

    Usage notes: not thread safe; use it from the polling thread only.
    """

    def __init__(
        self,
        address: str = "127.0.0.1",
        rpc_port: int = 50000,
        stream_port: int = 50001,
        client_name: str = "KSP economy monitor",
    ) -> None:
        """Configure the adapter; the connection is opened on first use.

        Args:
            address: Host where KSP and the kRPC server run.
            rpc_port: kRPC RPC port.
            stream_port: kRPC stream port.
            client_name: Name shown in the kRPC server window in KSP.
        """
        self._address = address
        self._rpc_port = rpc_port
        self._stream_port = stream_port
        self._client_name = client_name
        self._conn: Any = None
        self._contract_cache: dict[Any, ContractTerms] = {}
        self._warned_missing: set[str] = set()

    def read_snapshot(self) -> GameSnapshot:
        """Read the current state of the game.

        Returns:
            The current snapshot.

        Raises:
            GameUnavailableError: When KSP/kRPC cannot be reached or the
                current scene does not expose the career balances.
        """
        conn = self._connection()
        try:
            space_center = conn.space_center
            scene = self._read_scene(conn)
            snapshot = GameSnapshot(
                taken_at=datetime.now(timezone.utc),
                universal_time=float(space_center.ut),
                scene=scene,
                resources=Resources(
                    funds=self._read_currency(space_center, "funds"),
                    science=self._read_currency(space_center, "science"),
                    reputation=self._read_currency(space_center, "reputation"),
                ),
                active_vessel=self._read_active_vessel(space_center, scene),
                contracts=self._read_contracts(space_center),
            )
        except _RECOVERABLE_ERRORS as error:
            # A dropped socket leaves the client unusable: start afresh.
            if _is_connection_problem(error):
                self.close()
            raise GameUnavailableError(f"Cannot read the game: {error}") from error
        return snapshot

    def close(self) -> None:
        """Close the kRPC connection, if open."""
        if self._conn is not None:
            try:
                self._conn.close()
            except Exception:  # noqa: BLE001 - closing must never fail
                pass
        self._conn = None
        # Remote object ids are only meaningful within a server session.
        self._contract_cache.clear()

    def _connection(self) -> Any:
        """Return the open connection, connecting first if needed.

        Returns:
            A ``krpc`` client.

        Raises:
            GameUnavailableError: When the server cannot be reached.
        """
        if self._conn is not None:
            return self._conn
        try:
            # Imported lazily so the rest of the package (tests, demo mode)
            # works even when the kRPC client is not installed.
            import krpc
        except ImportError as error:  # pragma: no cover - environment issue
            raise GameUnavailableError("The 'krpc' Python package is not installed") from error
        try:
            self._conn = krpc.connect(
                name=self._client_name,
                address=self._address,
                rpc_port=self._rpc_port,
                stream_port=self._stream_port,
            )
        except _RECOVERABLE_ERRORS as error:
            raise GameUnavailableError(
                f"Cannot connect to kRPC at {self._address}:{self._rpc_port} "
                f"(is KSP running with the kRPC server started?): {error}"
            ) from error
        logger.info("Connected to kRPC server %s", self._conn.krpc.get_status().version)
        return self._conn

    @staticmethod
    def _read_scene(conn: Any) -> GameScene:
        """Read the current game scene in a version independent way.

        Args:
            conn: Open kRPC client.

        Returns:
            The current scene.
        """
        krpc_service = conn.krpc
        # kRPC >= 0.7.0 exposes ``game_scene``; older versions expose
        # ``current_game_scene``. Both return an enum with the same names.
        for attribute in ("game_scene", "current_game_scene"):
            if hasattr(type(krpc_service), attribute) or hasattr(krpc_service, attribute):
                value = getattr(krpc_service, attribute)
                return GameScene.from_name(getattr(value, "name", str(value)))
        return GameScene.UNKNOWN

    def _read_currency(self, space_center: Any, attribute: str) -> float:
        """Read one career currency.

        Science and sandbox saves do not have every currency; in that case
        the value is reported as zero and a warning is logged once.

        Args:
            space_center: The SpaceCenter service.
            attribute: ``"funds"``, ``"science"`` or ``"reputation"``.

        Returns:
            The balance, or ``0.0`` when the save does not use it.
        """
        try:
            return float(getattr(space_center, attribute))
        except _RECOVERABLE_ERRORS as error:
            if _is_connection_problem(error):
                raise
            if attribute not in self._warned_missing:
                self._warned_missing.add(attribute)
                logger.warning("'%s' is not available in this save (%s)", attribute, error)
            return 0.0

    @staticmethod
    def _read_active_vessel(space_center: Any, scene: GameScene) -> str | None:
        """Read the active vessel name, only meaningful in the flight scene.

        Args:
            space_center: The SpaceCenter service.
            scene: Current scene.

        Returns:
            The vessel name, or ``None`` outside flight.
        """
        if scene is not GameScene.FLIGHT:
            return None
        try:
            return str(space_center.active_vessel.name)
        except _RECOVERABLE_ERRORS as error:
            if _is_connection_problem(error):
                raise
            return None

    def _read_contracts(self, space_center: Any) -> ContractBook | None:
        """Read active, completed and failed contracts.

        Args:
            space_center: The SpaceCenter service.

        Returns:
            The contract book, or ``None`` when contracts are unavailable
            (non career save, or a scene that is still loading).
        """
        try:
            manager = space_center.contract_manager
            return ContractBook(
                active=self._terms_of(manager.active_contracts),
                completed=self._terms_of(manager.completed_contracts),
                failed=self._terms_of(manager.failed_contracts),
            )
        except _RECOVERABLE_ERRORS as error:
            if _is_connection_problem(error):
                raise
            logger.debug("Contracts unavailable: %s", error)
            return None

    def _terms_of(self, contracts: list[Any]) -> tuple[ContractTerms, ...]:
        """Convert remote contracts to terms, using the cache when possible.

        Args:
            contracts: Remote ``Contract`` objects.

        Returns:
            The terms of each contract, in the same order.
        """
        result = []
        for contract in contracts:
            # Remote objects hash and compare by their server object id.
            terms = self._contract_cache.get(contract)
            if terms is None:
                terms = ContractTerms(
                    contract_type=str(contract.type),
                    title=str(contract.title),
                    funds_advance=float(contract.funds_advance),
                    funds_completion=float(contract.funds_completion),
                    funds_failure=float(contract.funds_failure),
                    science_completion=float(contract.science_completion),
                    reputation_completion=float(contract.reputation_completion),
                    reputation_failure=float(contract.reputation_failure),
                )
                self._contract_cache[contract] = terms
            result.append(terms)
        return tuple(result)


def _is_connection_problem(error: BaseException) -> bool:
    """Tell socket-level failures apart from errors raised by a single RPC.

    Args:
        error: The exception raised while talking to kRPC.

    Returns:
        True when the connection itself is broken and must be reopened.
    """
    # Sockets raise OSError (socket.error) when the server goes away. kRPC
    # defines its own ``krpc.error.ConnectionError`` deriving from
    # RuntimeError (not from the built-in one), so it is matched by name to
    # avoid importing krpc at module import time.
    return isinstance(error, (OSError, EOFError)) or type(error).__name__ == "ConnectionError"
