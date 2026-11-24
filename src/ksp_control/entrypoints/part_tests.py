"""Keep an assistant running that helps fly part test contracts.

Start it once per game session and leave it running: it waits for the
game, follows every flight, and reconnects when KSP or the kRPC server is
restarted. Stop it with Ctrl+C.

Examples:
    Full assistance (throttle control and automatic "Run Test")::

        ksp-part-tests

    Only messages, never touch the vessel::

        ksp-part-tests --no-throttle --no-run-test
"""

from __future__ import annotations

import argparse
import logging
import sys
import time

from ksp_control.adapters.game.krpc_connection import KrpcEndpoint
from ksp_control.adapters.game.krpc_part_tests import KrpcPartTestGame
from ksp_control.application.part_test_assistant import AssistantOptions, PartTestAssistant
from ksp_control.application.ports import GameUnavailableError

logger = logging.getLogger("ksp_control.part_tests")


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    """Parse the command line.

    Args:
        argv: Arguments without the program name; ``None`` uses ``sys.argv``.

    Returns:
        The parsed options.
    """
    parser = argparse.ArgumentParser(
        description="Background assistant for 'test a part' contracts: plans the tests, "
                    "holds the speed window with the throttle, runs tests and tells you "
                    "when to stage."
    )
    parser.add_argument("--no-throttle", action="store_true",
                        help="Never touch the throttle; only show messages.")
    parser.add_argument("--no-run-test", action="store_true",
                        help="Do not press 'Run Test' automatically; tell me instead.")
    parser.add_argument("--speed-aim", type=float, default=0.4,
                        help="Where to hold the speed inside its window: 0 = minimum, "
                             "1 = maximum (default: 0.4).")
    parser.add_argument("--interval", type=float, default=0.25,
                        help="Seconds between readings during a flight (default: 0.25).")
    parser.add_argument("--retry", type=float, default=5.0,
                        help="Seconds between connection attempts while the game is "
                             "unavailable (default: 5).")
    parser.add_argument("--krpc-address", default="127.0.0.1", help="kRPC server address.")
    parser.add_argument("--krpc-port", type=int, default=50000,
                        help="Port of the kRPC TCP/IP server.")
    parser.add_argument("-v", "--verbose", action="store_true", help="Debug logging.")
    args = parser.parse_args(argv)
    if not 0.0 <= args.speed_aim <= 1.0:
        parser.error("--speed-aim must be between 0 and 1")
    return args


def main(argv: list[str] | None = None) -> int:
    """Run the assistant until interrupted.

    Args:
        argv: Arguments without the program name; ``None`` uses ``sys.argv``.

    Returns:
        Process exit code.
    """
    args = parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(message)s",
        datefmt="%H:%M:%S",
    )
    # Console output may contain localized part names (accents).
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")

    game = KrpcPartTestGame(KrpcEndpoint(args.krpc_address, args.krpc_port))
    assistant = PartTestAssistant(game, AssistantOptions(
        control_throttle=not args.no_throttle,
        run_tests=not args.no_run_test,
        speed_aim=args.speed_aim,
    ))
    logger.info("Part test assistant running; press Ctrl+C to stop")
    waiting = False
    try:
        while True:
            try:
                assistant.tick()
                if waiting:
                    logger.info("Connected to the game")
                    waiting = False
                time.sleep(args.interval)
            except GameUnavailableError as error:
                # Expected while KSP starts, loads a scene or is closed:
                # report it once and keep trying.
                if not waiting:
                    logger.info("Waiting for the game: %s", error)
                    waiting = True
                game.close()
                time.sleep(args.retry)
    except KeyboardInterrupt:
        logger.info("Stopped")
    finally:
        game.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
