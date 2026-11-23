"""Shared helpers to open and diagnose kRPC connections.

Every kRPC adapter needs the same things: a lazy connection that reports a
missing game as ``GameUnavailableError``, the list of exceptions that mean
"try again later", and a way to tell a broken socket from a failed call.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

from ksp_control.application.ports import GameUnavailableError
from ksp_control.domain.model import GameScene

logger = logging.getLogger(__name__)

# Exceptions raised by kRPC when the server is unreachable or a call fails.
# OSError covers sockets; RuntimeError covers krpc.error.RPCError and the
# "procedure not available in this game scene" errors.
RECOVERABLE_ERRORS = (ConnectionError, OSError, RuntimeError, EOFError, ValueError)


@dataclass(frozen=True)
class KrpcEndpoint:
    """Where the kRPC server listens.

    Since kRPC 0.8.0 a single TCP port carries both calls and streams (the
    second port shown in the server window is a separate WebSocket server).

    Attributes:
        address: Host where KSP and the kRPC server run.
        port: Port of the kRPC TCP/IP server.
    """

    address: str = "127.0.0.1"
    port: int = 50000


def connect(endpoint: KrpcEndpoint, client_name: str) -> Any:
    """Open a kRPC connection.

    Args:
        endpoint: Server location.
        client_name: Name shown in the kRPC server window in KSP.

    Returns:
        A ``krpc`` client.

    Raises:
        GameUnavailableError: When the package is missing or the server
            cannot be reached.
    """
    try:
        # Imported lazily so the rest of the package (tests, demo mode)
        # works even when the kRPC client is not installed.
        import krpc
    except ImportError as error:  # pragma: no cover - environment issue
        raise GameUnavailableError("The 'krpc' Python package is not installed") from error
    try:
        conn = krpc.connect(name=client_name, address=endpoint.address, port=endpoint.port)
    except RECOVERABLE_ERRORS as error:
        raise GameUnavailableError(
            f"Cannot connect to kRPC at {endpoint.address}:{endpoint.port} "
            f"(is KSP running with the kRPC server started?): {error}"
        ) from error
    logger.info("Connected to kRPC server %s", conn.krpc.get_status().version)
    return conn


def read_scene(conn: Any) -> GameScene:
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


def is_connection_problem(error: BaseException) -> bool:
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
