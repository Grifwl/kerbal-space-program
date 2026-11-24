"""Fly tourists on suborbital hops until the funds reach a target.

Each flight accepts the suborbital tourism contracts that fit in the craft,
launches it with a pilot (taking turns) and the tourists, flies above the
atmosphere, lands under parachutes, recovers the vessel and its debris and
starts again. Stop it at any time with Ctrl+C (the vessel in flight is then
left to you).

Examples:
    See what the next flight would carry, without touching the game::

        ksp-tourism --craft "Passatgers v1.1" --target 500000 --dry-run

    Fly a single flight first, to check the craft::

        ksp-tourism --craft "Passatgers v1.1" --target 500000 --max-flights 1

    Fly until 500,000 funds or no contracts are left::

        ksp-tourism --craft "Passatgers v1.1" --target 500000
"""

from __future__ import annotations

import argparse
import logging
import sys

from ksp_control.adapters.game.krpc_connection import KrpcEndpoint
from ksp_control.adapters.game.krpc_fleet import KrpcFleet
from ksp_control.adapters.game.krpc_tourism import KrpcTourismGame
from ksp_control.application.ports import GameUnavailableError
from ksp_control.application.suborbital_flight import FlightError, SuborbitalFlightDirector
from ksp_control.application.tourism_campaign import (
    CampaignError,
    CampaignOptions,
    FlightSummary,
    TourismCampaign,
)
from ksp_control.application.vessel_recovery import VesselRecoveryService
from ksp_control.domain.tourism import FlightProfile, choose_pilot

logger = logging.getLogger("ksp_control.tourism")


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    """Parse the command line.

    Args:
        argv: Arguments without the program name; ``None`` uses ``sys.argv``.

    Returns:
        The parsed options.
    """
    parser = argparse.ArgumentParser(
        description="Fly suborbital tourism contracts in a loop until the funds reach a target."
    )
    parser.add_argument("--craft", required=True,
                        help="Name of the VAB craft to launch, e.g. \"Passatgers v1.1\".")
    parser.add_argument("--target", type=float, required=True,
                        help="Stop when the funds reach this amount.")
    parser.add_argument("--seats", type=int, default=4,
                        help="Passenger seats of the craft, pilot excluded (default: 4).")
    parser.add_argument("--pilot", action="append", default=[], metavar="NAME",
                        help="Pilot to use; repeat to take turns in that order "
                             "(default: every hired pilot, in turns).")
    parser.add_argument("--max-flights", type=int, default=None,
                        help="Stop after this many flights.")
    parser.add_argument("--apoapsis", type=float, default=75_000.0,
                        help="Target apoapsis in metres; must be above 70,000 (default: 75,000).")
    parser.add_argument("--pitch", type=float, default=80.0,
                        help="Pitch reached at 10 km after a gradual turn from vertical at 1 km; "
                             "90 = straight up, lower = lands further east (default: 80).")
    parser.add_argument("--heading", type=float, default=90.0,
                        help="Heading of the tilt, 90 = east, towards the sea (default: 90).")
    parser.add_argument("--max-q", type=float, default=20.0,
                        help="Throttle down while the dynamic pressure exceeds this, in kPa; "
                             "0 = never (default: 20).")
    parser.add_argument("--max-drag", type=float, default=1.0,
                        help="Throttle down while the drag exceeds this multiple of the "
                             "weight; 1 = climb at terminal velocity, 0 = never (default: 1).")
    parser.add_argument("--min-throttle", type=float, default=0.1,
                        help="Lowest throttle the air limits may set (default: 0.1).")
    parser.add_argument("--no-warp", action="store_true",
                        help="Never use time warp.")
    parser.add_argument("--physics-warp", type=int, default=2, choices=range(0, 4),
                        help="Physics warp under the parachutes: 0-3 (default: 2, i.e. 3x).")
    parser.add_argument("--dry-run", action="store_true",
                        help="Only show the next flight; change nothing.")
    parser.add_argument("--krpc-address", default="127.0.0.1", help="kRPC server address.")
    parser.add_argument("--krpc-port", type=int, default=50000,
                        help="Port of the kRPC TCP/IP server.")
    parser.add_argument("-v", "--verbose", action="store_true", help="Debug logging.")
    args = parser.parse_args(argv)
    if args.apoapsis <= 70_000:
        parser.error("--apoapsis must be above 70,000 m, where space begins at Kerbin")
    if not 0.0 <= args.min_throttle <= 1.0:
        parser.error("--min-throttle must be between 0 and 1")
    if args.seats < 1:
        parser.error("--seats must be at least 1")
    return args


def format_summary(summary: FlightSummary) -> str:
    """Render the result of one flight.

    Args:
        summary: The flight.

    Returns:
        A text line.
    """
    report = summary.report
    return (
        f"Flight {summary.number} ({summary.pilot}): {report.situation} after "
        f"{report.max_altitude:,.0f} m; {summary.completed}/{len(summary.contracts)} "
        f"contract(s) completed. Launch -{summary.launch_cost:,.0f}, contracts "
        f"+{summary.contract_rewards:,.0f}, recovery and other {summary.vessel_recovery:+,.0f}: "
        f"net {summary.net:+,.0f}; funds {summary.funds:,.0f}"
    )


def main(argv: list[str] | None = None) -> int:
    """Run the campaign.

    Args:
        argv: Arguments without the program name; ``None`` uses ``sys.argv``.

    Returns:
        Process exit code: 0 when it stopped normally, 1 when a flight or
        contract needs attention, 2 when the game cannot be reached.
    """
    args = parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(message)s",
        datefmt="%H:%M:%S",
    )
    # Contract titles and names are localized (accents).
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")

    endpoint = KrpcEndpoint(args.krpc_address, args.krpc_port)
    game = KrpcTourismGame(endpoint)
    recovery = VesselRecoveryService(KrpcFleet(endpoint, "KSP tourism recovery"))
    profile = FlightProfile(
        target_apoapsis=args.apoapsis,
        pitch=args.pitch,
        heading=args.heading,
        warp=not args.no_warp,
        physics_warp=args.physics_warp,
        max_dynamic_pressure=args.max_q * 1000.0,
        max_drag_ratio=args.max_drag,
        min_throttle=args.min_throttle,
    )
    director = SuborbitalFlightDirector(game, profile)
    options = CampaignOptions(
        craft=args.craft,
        target_funds=args.target,
        seats=args.seats,
        pilots=tuple(args.pilot),
        max_flights=args.max_flights,
    )
    campaign = TourismCampaign(game, director.fly, recovery, options)
    try:
        if args.dry_run:
            return _dry_run(game, campaign, options)
        result = campaign.run(on_flight=lambda s: logger.info(format_summary(s)))
        # %-style logging has no thousands separator: format it here.
        logger.info("Stopped: %s. %d flight(s), funds %s -> %s (%s)",
                    result.reason.value, len(result.flights),
                    f"{result.start_funds:,.0f}", f"{result.end_funds:,.0f}",
                    f"{result.end_funds - result.start_funds:+,.0f}")
        return 0
    except (CampaignError, FlightError) as error:
        logger.error("Stopped: %s", error)
        return 1
    except GameUnavailableError as error:
        logger.error("Cannot talk to the game: %s", error)
        return 2
    except KeyboardInterrupt:
        logger.warning("Interrupted: if a vessel is flying, it is yours now")
        _release(game)
        return 130
    except Exception:  # noqa: BLE001 - a bug must not leave the rocket burning
        logger.exception("Unexpected error: if a vessel is flying, it is yours now "
                         "(autopilot off, throttle at zero)")
        _release(game)
        return 1
    finally:
        game.close()
        recovery.close()


def _release(game: KrpcTourismGame) -> None:
    """Hand the vessel back to the player, if there is one.

    Args:
        game: The game adapter.
    """
    try:
        game.release_controls()
    except Exception:  # noqa: BLE001 - outside flight there is nothing to release
        pass


def _dry_run(game: KrpcTourismGame, campaign: TourismCampaign, options: CampaignOptions) -> int:
    """Show what the next flight would carry.

    Args:
        game: The game adapter.
        campaign: The campaign.
        options: Campaign options.

    Returns:
        Process exit code.
    """
    funds = game.funds()
    print(f"Funds: {funds:,.0f} (target {options.target_funds:,.0f})")
    contracts = game.read_tourism_contracts()
    print(f"Tourism contracts: {len(contracts)}")
    for contract in contracts:
        tourists = ", ".join(t.kerbal or f"? ({t.itinerary})" for t in contract.tourists)
        print(f"  [{contract.status.value}] {contract.title} - {tourists} - "
              f"{contract.reward:,.0f} funds")
        for tourist in contract.tourists:
            print(f"      {tourist.itinerary}: {'; '.join(tourist.destinations)}")
    plan = campaign.next_plan()
    if plan.is_empty:
        print("Next flight: nothing to fly.")
        return 0
    pilot = choose_pilot(game.roster(), 0, options.pilots)
    print(f"Next flight: pilot {pilot or '(none available!)'}, "
          f"{plan.seats_used}/{options.seats} seat(s), {plan.reward:,.0f} funds")
    for contract in plan.carry:
        print(f"  already accepted: {contract.title}")
    for contract in plan.accept:
        print(f"  to accept:        {contract.title}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
