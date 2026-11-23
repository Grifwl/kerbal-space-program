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

from ksp_control.adapters.game.krpc_connection import (
    RECOVERABLE_ERRORS as _RECOVERABLE_ERRORS,
    KrpcEndpoint,
    connect,
    is_connection_problem as _is_connection_problem,
    read_scene,
)
from ksp_control.application.ports import GameTelemetryPort, GameUnavailableError
from ksp_control.domain.model import (
    ContractBook,
    ContractTerms,
    GameScene,
    GameSnapshot,
    ParameterTerms,
    Resources,
)

logger = logging.getLogger(__name__)

class KrpcTelemetry(GameTelemetryPort):
    """Reads funds, science, reputation, scene and contracts through kRPC.

    Responsibilities:

    * Own the kRPC connection and reconnect transparently after failures.
    * Cache contract terms (rewards and objective tree) by remote object,
      because reading a contract costs dozens of remote calls and a long
      career has hundreds of finished contracts. Only contracts never seen
      before are fetched; afterwards a poll only reads the completion flag
      of the objectives of the *active* contracts.

    Usage notes: not thread safe; use it from the polling thread only.
    """

    def __init__(
        self,
        endpoint: KrpcEndpoint | None = None,
        client_name: str = "KSP economy monitor",
    ) -> None:
        """Configure the adapter; the connection is opened on first use.

        Args:
            endpoint: kRPC server location (local default port if omitted).
            client_name: Name shown in the kRPC server window in KSP.
        """
        self._endpoint = endpoint or KrpcEndpoint()
        self._client_name = client_name
        self._conn: Any = None
        # Remote contract -> (terms, flattened remote parameters).
        self._contract_cache: dict[Any, tuple[ContractTerms, list[Any]]] = {}
        self._warned_missing: set[str] = set()
        # Remote vessel -> name, so each poll costs one call for the list.
        self._vessel_names: dict[Any, str] = {}

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
            scene = read_scene(conn)
            vessel_name, prelaunch = self._read_active_vessel(space_center, scene)
            snapshot = GameSnapshot(
                taken_at=datetime.now(timezone.utc),
                universal_time=float(space_center.ut),
                scene=scene,
                resources=Resources(
                    funds=self._read_currency(space_center, "funds"),
                    science=self._read_currency(space_center, "science"),
                    reputation=self._read_currency(space_center, "reputation"),
                ),
                active_vessel=vessel_name,
                contracts=self._read_contracts(space_center),
                vessel_prelaunch=prelaunch,
                vessels=self._read_vessel_names(space_center, scene),
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
        self._vessel_names.clear()

    def _connection(self) -> Any:
        """Return the open connection, connecting first if needed.

        Returns:
            A ``krpc`` client.

        Raises:
            GameUnavailableError: When the server cannot be reached.
        """
        if self._conn is None:
            self._conn = connect(self._endpoint, self._client_name)
        return self._conn

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
    def _read_active_vessel(space_center: Any, scene: GameScene) -> tuple[str | None, bool]:
        """Read the active vessel, only meaningful in the flight scene.

        Args:
            space_center: The SpaceCenter service.
            scene: Current scene.

        Returns:
            The vessel name (``None`` outside flight) and whether the vessel
            is still waiting on the launch pad or runway (``pre_launch``).
        """
        if scene is not GameScene.FLIGHT:
            return None, False
        try:
            vessel = space_center.active_vessel
            situation = getattr(vessel.situation, "name", str(vessel.situation))
            return str(vessel.name), situation.lower() == "pre_launch"
        except _RECOVERABLE_ERRORS as error:
            if _is_connection_problem(error):
                raise
            return None, False

    def _read_vessel_names(self, space_center: Any, scene: GameScene) -> tuple[str, ...] | None:
        """Read the names of every vessel.

        kRPC 0.8 lists vessels in every scene where the game has them loaded
        (flight, space center, tracking station); older versions only in
        flight. The editors have no vessel list at all.

        Args:
            space_center: The SpaceCenter service.
            scene: Current scene.

        Returns:
            The names, or ``None`` when they cannot be read.
        """
        if scene.is_editor:
            return None
        try:
            names = []
            for vessel in space_center.vessels:
                name = self._vessel_names.get(vessel)
                if name is None:
                    name = self._vessel_names[vessel] = str(vessel.name)
                names.append(name)
            return tuple(names)
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
            active = manager.active_contracts
            return ContractBook(
                active=self._terms_of(active),
                completed=self._terms_of(manager.completed_contracts),
                failed=self._terms_of(manager.failed_contracts),
                active_progress=tuple(self._progress_of(contract) for contract in active),
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
        return tuple(self._cached(contract)[0] for contract in contracts)

    def _progress_of(self, contract: Any) -> tuple[bool, ...]:
        """Read which objectives of an active contract are completed.

        Args:
            contract: Remote ``Contract`` object (already cached).

        Returns:
            One flag per objective, aligned with ``ContractTerms.parameters``.
        """
        _, parameters = self._cached(contract)
        return tuple(bool(parameter.completed) for parameter in parameters)

    def _cached(self, contract: Any) -> tuple[ContractTerms, list[Any]]:
        """Return the terms and flattened objectives of a contract.

        Args:
            contract: Remote ``Contract`` object. Remote objects hash and
                compare by their server object id, so they are valid keys.

        Returns:
            The contract terms and its remote parameters, depth-first.
        """
        entry = self._contract_cache.get(contract)
        if entry is None:
            parameters = list(_flatten(contract.parameters))
            terms = ContractTerms(
                contract_type=str(contract.type),
                title=str(contract.title),
                funds_advance=float(contract.funds_advance),
                funds_completion=float(contract.funds_completion),
                funds_failure=float(contract.funds_failure),
                science_completion=float(contract.science_completion),
                reputation_completion=float(contract.reputation_completion),
                reputation_failure=float(contract.reputation_failure),
                parameters=tuple(
                    ParameterTerms(
                        title=str(p.title),
                        funds=float(p.funds_completion),
                        science=float(p.science_completion),
                        reputation=float(p.reputation_completion),
                        optional=bool(p.optional),
                    )
                    for p in parameters
                ),
            )
            entry = (terms, parameters)
            self._contract_cache[contract] = entry
        return entry


def _flatten(parameters: list[Any]) -> list[Any]:
    """Flatten a tree of contract parameters depth-first.

    Args:
        parameters: Remote ``ContractParameter`` objects.

    Returns:
        Every parameter, each one followed by its descendants.
    """
    result = []
    for parameter in parameters:
        result.append(parameter)
        result.extend(_flatten(parameter.children))
    return result
