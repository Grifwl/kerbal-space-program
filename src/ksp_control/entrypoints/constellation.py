"""Deploy a relay constellation around a body from a carrier full of probes.

Fly the carrier, loaded with the probes, into any orbit around the body and
leave it as the active vessel. The script puts it on the resonant orbit,
releases the probes, brings the empty carrier down and brakes one probe per
periapsis passage into the final circular orbit, evenly spaced. Each probe
is named ``CA-<km>-<body>-<nn> <person>``. Stop it at any time with Ctrl+C
and run the same command again later: it carries on from the orbits in the
game.

Examples:
    See the plan and the next step, commanding nothing::

        ksp-constellation --body Mun --body-label Muna --dry-run

    Deploy (the number of probes is read from the carrier)::

        ksp-constellation --body Mun --body-label Muna

    Set an alarm when the next step is far away and stop; run again when
    it rings::

        ksp-constellation --body Mun --body-label Muna --no-wait
"""

from __future__ import annotations

import argparse
import logging
import math
import sys

from ksp_control.adapters.game.krpc_connection import KrpcEndpoint
from ksp_control.adapters.game.krpc_constellation import KrpcConstellationGame
from ksp_control.adapters.names import person_names
from ksp_control.application.ports import GameUnavailableError
from ksp_control.application.relay_constellation import (
    ConstellationDeployer,
    DeploymentError,
    DeploymentOptions,
    DeploymentPaused,
    DeploymentReport,
)
from ksp_control.domain.constellation import (
    ALTITUDE_STEP,
    ConstellationError,
    ConstellationPlan,
    existing_constellations,
    format_duration,
    plan_constellation,
)
from ksp_control.domain.model import GameScene

logger = logging.getLogger("ksp_control.constellation")


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    """Parse the command line.

    Args:
        argv: Arguments without the program name; ``None`` uses ``sys.argv``.

    Returns:
        The parsed options.
    """
    parser = argparse.ArgumentParser(
        description="Spread relay probes evenly around a body with a resonant orbit."
    )
    parser.add_argument("--body", required=True,
                        help="Internal name of the body to orbit, e.g. Mun or Minmus.")
    parser.add_argument("--body-label", default=None,
                        help="Name of the body in the probe names, e.g. Muna (default: --body).")
    parser.add_argument("--altitude", type=float, default=None,
                        help="Altitude of the constellation in metres (default: the lowest one "
                             "where neighbours see each other, rounded up to --round, or the "
                             "one of a deployment in progress).")
    parser.add_argument("--count", type=int, default=None,
                        help="Number of probes (default: those on the carrier, or of the "
                             "deployment in progress).")
    parser.add_argument("--round", type=float, default=ALTITUDE_STEP,
                        help="Rounding of the automatic altitude in metres (default: 10,000).")
    parser.add_argument("--occlusion", type=float, default=1.0,
                        help="Share of the body radius that blocks the signal when choosing "
                             "the altitude; CommNet uses less than 1 (default: 1, cautious).")
    parser.add_argument("--spacing", type=float, default=200.0,
                        help="Metres each waiting probe pulls ahead of the next one per lap, so "
                             "that they line up ahead of the carrier (default: 200; 0: off).")
    parser.add_argument("--period-tolerance", type=float, default=0.05,
                        help="Allowed error of the final periods, in seconds (default: 0.05).")
    parser.add_argument("--alarm-after", type=float, default=60.0,
                        help="Minutes of game time to wait beyond which an alarm is set "
                             "instead of warping (default: 60).")
    parser.add_argument("--alarm-lead", type=float, default=5.0,
                        help="Minutes between the alarm and the maneuver (default: 5).")
    parser.add_argument("--no-wait", action="store_true",
                        help="When an alarm is set, stop instead of waiting for it; run again "
                             "when it rings.")
    parser.add_argument("--no-warp", action="store_true", help="Never use time warp.")
    parser.add_argument("--dry-run", action="store_true",
                        help="Show the plan and the next step, and command nothing.")
    parser.add_argument("--report", action="store_true",
                        help="Only measure the ring: gaps, periods, line of sight.")
    parser.add_argument("--krpc-address", default="127.0.0.1", help="kRPC server address.")
    parser.add_argument("--krpc-port", type=int, default=50000, help="kRPC server port.")
    parser.add_argument("--verbose", action="store_true", help="Log debug messages.")
    return parser.parse_args(argv)


def build_plan(game: KrpcConstellationGame, args: argparse.Namespace,
               label: str) -> ConstellationPlan:
    """Work out the constellation, from the options and the game.

    The number of probes comes from ``--count``, else from the deployment
    in progress (probes already named) plus those still on the active
    vessel. The altitude comes from ``--altitude``, else from the
    deployment in progress, else from the plan.

    Args:
        game: The game adapter.
        args: Command line options.
        label: Name of the body in the probe names.

    Returns:
        The plan.

    Raises:
        ConstellationError: When the constellation cannot be built or is
            ambiguous.
    """
    body = game.body(args.body)
    groups = existing_constellations(game.vessel_names(), label)
    altitude = args.altitude
    if altitude is None and len(groups) > 1:
        found = ", ".join(f"{a / 1000:g} km" for a in sorted(groups))
        raise ConstellationError(f"several constellations around {label} ({found}): choose one "
                                 "with --altitude")
    if altitude is None and groups:
        altitude = next(iter(groups))
        logger.info("Deployment in progress found at %.0f km (%d probes)", altitude / 1000,
                    len(groups[altitude]))
    count = args.count
    if count is None:
        named = len(groups.get(altitude, [])) if altitude is not None else 0
        mounted = 0
        active = game.active_vessel() if game.scene() is GameScene.FLIGHT else None
        if active is not None:
            orbit = game.orbit(active)
            if orbit is not None and orbit.body == body.name:
                mounted = game.mounted_probes()
        count = named + mounted
        if count == 0:
            raise ConstellationError(
                f"no probes found: make the loaded carrier, in orbit around {body.name}, the "
                "active vessel (or give --count)")
    return plan_constellation(body, count, altitude, step=args.round, occlusion=args.occlusion)


def print_report(report: DeploymentReport, plan: ConstellationPlan) -> None:
    """Log the measured ring.

    Args:
        report: The measurements.
        plan: The plan, for the ideal figures.
    """
    for probe in report.probes:
        logger.info("  %s: periapsis %.2f km, apoapsis %.2f km, period %+.3f s, %.2f deg to the "
                    "next (ideal %.1f), %+.2f deg from its slot", probe.name,
                    probe.periapsis / 1000, probe.apoapsis / 1000, probe.period_error,
                    probe.gap_ahead, math.degrees(plan.slot_angle), probe.slot_error)
    if math.isfinite(report.lasting_days):
        logger.info("With these periods the ring stays in sight for about %s Kerbin days",
                    f"{report.lasting_days:,.0f}")
    for warning in report.warnings:
        logger.warning(warning)


def main(argv: list[str] | None = None) -> int:
    """Run the deployment.

    Args:
        argv: Command line arguments; ``None`` uses ``sys.argv``.

    Returns:
        Process exit code: 0 when done (or paused on an alarm), 1 when the
        deployment needs the player, 2 when the game cannot be reached.
    """
    args = parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(message)s",
        datefmt="%H:%M:%S",
    )
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")

    label = args.body_label or args.body
    game = KrpcConstellationGame(KrpcEndpoint(args.krpc_address, args.krpc_port))
    try:
        plan = build_plan(game, args, label)
        options = DeploymentOptions(
            body_label=label,
            spacing=args.spacing,
            period_tolerance=args.period_tolerance,
            alarm_threshold=args.alarm_after * 60.0,
            alarm_lead=args.alarm_lead * 60.0,
            wait=not args.no_wait,
            warp=not args.no_warp,
        )
        deployer = ConstellationDeployer(game, plan, options, person_names())
        if args.dry_run:
            print(plan.describe())
            for line in deployer.status():
                print(line)
            return 0
        if args.report:
            print_report(deployer.report(), plan)
            return 0
        report = deployer.run()
        print_report(report, plan)
        return 0
    except DeploymentPaused as pause:
        logger.info("Alarm set: %s. Run this command again when it rings (in %s of game time)",
                    pause.label, format_duration(pause.alarm_ut - game.ut()))
        return 0
    except (ConstellationError, DeploymentError, KeyError) as error:
        logger.error("Stopped: %s", error)
        return 1
    except GameUnavailableError as error:
        logger.error("Cannot talk to the game: %s", error)
        return 2
    except KeyboardInterrupt:
        logger.warning("Interrupted: the active vessel is yours now; run again to carry on")
        _release(game)
        return 130
    except Exception:  # noqa: BLE001 - a bug must not leave an engine burning
        logger.exception("Unexpected error: the active vessel is yours now (autopilot off, "
                         "throttle at zero)")
        _release(game)
        return 1
    finally:
        game.close()


def _release(game: KrpcConstellationGame) -> None:
    """Hand the active vessel back to the player, if there is one.

    Args:
        game: The game adapter.
    """
    try:
        game.release_controls()
    except Exception:  # noqa: BLE001 - outside flight there is nothing to release
        pass


if __name__ == "__main__":
    sys.exit(main())
