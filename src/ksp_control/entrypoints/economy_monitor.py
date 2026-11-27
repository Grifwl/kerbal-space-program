"""Run the career economy monitor and its web dashboard.

Examples:
    Monitor a real game (KSP running with the kRPC server started)::

        ksp-economy-monitor --db data/my-career.sqlite

    Try the dashboard without KSP::

        ksp-economy-monitor --demo
"""

from __future__ import annotations

import argparse
import logging
import tempfile
from pathlib import Path

from ksp_control.adapters.game.krpc_connection import KrpcEndpoint
from ksp_control.adapters.game.krpc_telemetry import KrpcTelemetry
from ksp_control.adapters.game.simulated_telemetry import SimulatedTelemetry
from ksp_control.adapters.persistence.json_vessel_designs import JsonVesselDesigns
from ksp_control.adapters.persistence.sqlite_repository import SqliteEconomyRepository
from ksp_control.adapters.polling import PollingLoop
from ksp_control.adapters.web.app import create_app
from ksp_control.application.economy_monitor import EconomyMonitorService
from ksp_control.application.ports import GameTelemetryPort


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    """Parse the command line.

    Args:
        argv: Arguments without the program name; ``None`` uses ``sys.argv``.

    Returns:
        The parsed options.
    """
    parser = argparse.ArgumentParser(
        description="Track KSP career funds, science and reputation in a live web dashboard."
    )
    parser.add_argument("--db", default="data/career.sqlite",
                        help="SQLite file for this career (use one file per save game).")
    parser.add_argument("--designs", default="data/vessel-designs.json",
                        help="Craft designs of vessels renamed by other scripts (ksp-tourism "
                             "writes it), to add up the flights of one design.")
    parser.add_argument("--krpc-address", default="127.0.0.1", help="kRPC server address.")
    parser.add_argument("--krpc-port", type=int, default=50000,
                        help="Port of the kRPC TCP/IP server.")
    parser.add_argument("--interval", type=float, default=2.0, help="Seconds between polls.")
    parser.add_argument("--host", default="127.0.0.1",
                        help="Web server interface (0.0.0.0 to open it to your LAN).")
    parser.add_argument("--port", type=int, default=8080, help="Web server port.")
    parser.add_argument("--demo", action="store_true",
                        help="Use a simulated career instead of KSP (temporary database).")
    parser.add_argument("-v", "--verbose", action="store_true", help="Debug logging.")
    return parser.parse_args(argv)


def build_telemetry(args: argparse.Namespace) -> GameTelemetryPort:
    """Choose the game adapter from the options.

    Args:
        args: Parsed options.

    Returns:
        The simulator in demo mode, the kRPC adapter otherwise.
    """
    if args.demo:
        return SimulatedTelemetry()
    return KrpcTelemetry(KrpcEndpoint(args.krpc_address, args.krpc_port))


def main(argv: list[str] | None = None) -> None:
    """Wire everything together and serve the dashboard until Ctrl+C.

    Args:
        argv: Arguments without the program name; ``None`` uses ``sys.argv``.
    """
    args = parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
    )
    # Werkzeug logs every dashboard refresh; keep the console readable.
    logging.getLogger("werkzeug").setLevel(logging.WARNING)

    # The demo writes to a throwaway database so it never pollutes a career.
    db_path = Path(tempfile.mkdtemp()) / "demo.sqlite" if args.demo else Path(args.db)
    repository = SqliteEconomyRepository(db_path)
    service = EconomyMonitorService(build_telemetry(args), repository,
                                    designs=JsonVesselDesigns(args.designs))
    poller = PollingLoop(service, interval=args.interval)

    logging.getLogger(__name__).info(
        "Dashboard on http://%s:%d  (database: %s)", args.host, args.port, db_path
    )
    poller.start()
    try:
        create_app(service).run(host=args.host, port=args.port, threaded=True)
    finally:
        poller.stop()
        service.close()
        repository.close()


if __name__ == "__main__":
    main()
