"""Fly tourists to space (suborbital or orbit) until a funds or science target.

Each flight accepts the tourism contracts of Kerbin (suborbital flights and
orbits) that fill the craft, launches it with a pilot (taking turns) and
the tourists, flies above the atmosphere (and into orbit when a tourist
wants it), lands under parachutes, recovers the vessel and its debris and
starts again. Ctrl+C asks it to stop once the flight under way has landed
and been recovered (no more contracts are accepted); a second Ctrl+C stops
it at once, leaving the vessel in flight to you.

Examples:
    See what the next flight would carry, without touching the game::

        ksp-tourism --craft "Passatgers v1.2" --funds-target 500000 --dry-run

    Check that the craft reaches orbit and comes back, with suborbital
    tourists only (no contract at risk if it falls short)::

        ksp-tourism --craft "Passatgers v1.2" --funds-target 500000 --orbit-test --max-flights 1

    Fly until 500,000 funds or no contracts are left::

        ksp-tourism --craft "Passatgers v1.2" --funds-target 500000

    Fly until 160 science points (to research a technology), or 500,000
    funds, whichever comes first::

        ksp-tourism --craft "Passatgers v1.2" --science-target 160 --funds-target 500000
"""

from __future__ import annotations

import argparse
import logging
import math
import sys

from ksp_control.adapters.game.krpc_connection import KrpcEndpoint
from ksp_control.adapters.game.krpc_fleet import KrpcFleet
from ksp_control.adapters.game.krpc_tourism import KrpcTourismGame
from ksp_control.adapters.interrupt import TwoStepInterrupt
from ksp_control.adapters.names import person_names
from ksp_control.adapters.persistence.json_vessel_designs import JsonVesselDesigns
from ksp_control.application.ports import GameUnavailableError
from ksp_control.application.tourism_flight import FlightError, TourismFlightDirector
from ksp_control.application.tourism_campaign import (
    CampaignError,
    CampaignOptions,
    FlightSummary,
    TourismCampaign,
)
from ksp_control.application.vessel_recovery import VesselRecoveryService
from ksp_control.domain.tourism import (
    PART_TEST_TYPE,
    FlightProfile,
    Mission,
    choose_pilot,
    contract_mission,
    offers_to_decline,
)

logger = logging.getLogger("ksp_control.tourism")


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    """Parse the command line.

    Args:
        argv: Arguments without the program name; ``None`` uses ``sys.argv``.

    Returns:
        The parsed options.
    """
    parser = argparse.ArgumentParser(
        description="Fly tourism contracts (suborbital and orbital) in a loop until the funds "
                    "reach a target."
    )
    parser.add_argument("--craft", required=True,
                        help="Name of the VAB craft to launch, e.g. \"Passatgers v1.1\".")
    parser.add_argument("--funds-target", type=float, default=None,
                        help="Stop when the funds reach this amount.")
    parser.add_argument("--science-target", type=float, default=None,
                        help="Stop when the science points reach this amount, e.g. the cost "
                             "of a technology to research.")
    parser.add_argument("--seats", type=int, default=None,
                        help="Passenger seats of the craft, pilot excluded, for the first "
                             "flight; every launch reads the real number from the craft for "
                             "the next flights (default: 4).")
    parser.add_argument("--pilot", action="append", default=[], metavar="NAME",
                        help="Pilot to use; repeat to take turns in that order "
                             "(default: every hired pilot, in turns).")
    parser.add_argument("--max-flights", type=int, default=None,
                        help="Stop after this many flights.")
    parser.add_argument("--suborbital-only", action="store_true",
                        help="Never board tourists that want an orbit (for a craft that cannot "
                             "reach orbit).")
    parser.add_argument("--orbit-test", action="store_true",
                        help="Fly to orbit and back, but board only suborbital tourists: checks "
                             "the craft without risking a contract.")
    parser.add_argument("--decline-part-tests", action="store_true",
                        help="While the next flight would leave seats empty, decline offered "
                             "part test contracts one at a time so that KSP offers others, maybe "
                             "tourists (costs reputation).")
    parser.add_argument("--offer-wait", type=float, default=60.0,
                        help="Seconds to wait for a new offer after each decline (default: 60).")
    parser.add_argument("--vessel-prefix", default="Cruise",
                        help="Each launched vessel is named this plus an invented person's name, "
                             "e.g. \"Cruise John Doe\" (default: Cruise).")
    parser.add_argument("--keep-craft-name", action="store_true",
                        help="Keep the craft's name for every launched vessel.")
    parser.add_argument("--designs", default="data/vessel-designs.json",
                        help="File where the craft of each renamed vessel is recorded, so the "
                             "economy monitor adds up the flights of one design.")
    parser.add_argument("--alarm-margin", type=float, default=60.0,
                        help="Minutes of game time that must be free of game alarms for a "
                             "flight to start; an orbital flight takes about 45 (default: 60, "
                             "0: ignore alarms).")
    parser.add_argument("--max-declines", type=int, default=10,
                        help="Most offers declined before one flight (default: 10).")
    parser.add_argument("--apoapsis", type=float, default=75_000.0,
                        help="Target apoapsis of a suborbital hop in metres; must be above "
                             "70,000 (default: 75,000).")
    parser.add_argument("--orbit", type=float, default=80_000.0,
                        help="Altitude of the orbit in metres; must be above 70,000 "
                             "(default: 80,000).")
    parser.add_argument("--turn-shape", type=float, default=1.0,
                        help="Shape of the orbital gravity turn, which goes from vertical at "
                             "1 km to horizontal when the apoapsis reaches --orbit: 1 = tilt in "
                             "proportion to the apoapsis rise, lower = tilt sooner (cheaper "
                             "circularization, but more time fast in thick, hot air) "
                             "(default: 1).")
    parser.add_argument("--turn-after-first-stage", action="store_true",
                        help="Start the orbital gravity turn only once the first stage (the "
                             "boosters) has been dropped, for a rocket that loses control when "
                             "they separate while it is tilted; costs some delta-v.")
    parser.add_argument("--orbit-delta-v", type=float, default=3_600.0,
                        help="Vacuum delta-v the craft needs on the pad to try an orbit, in m/s; "
                             "with less it flies a suborbital hop (default: 3,600).")
    parser.add_argument("--deorbit-periapsis", type=float, default=35_000.0,
                        help="Periapsis of the deorbit burn, in metres (default: 35,000).")
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
    parser.add_argument("--max-heat", type=float, default=80.0,
                        help="Throttle down while the hottest part is above this percentage of "
                             "the temperature it withstands; 0 = never (default: 80).")
    parser.add_argument("--min-throttle", type=float, default=0.1,
                        help="Lowest throttle the air limits may set (default: 0.1).")
    parser.add_argument("--no-rcs", action="store_true",
                        help="Leave the RCS off (by default it is switched on from the launch "
                             "until the parachutes open, so the autopilot can use it).")
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
    if args.funds_target is None and args.science_target is None and args.max_flights is None:
        parser.error("give a limit: --funds-target, --science-target and/or --max-flights")
    if args.apoapsis <= 70_000:
        parser.error("--apoapsis must be above 70,000 m, where space begins at Kerbin")
    if args.orbit <= 70_000:
        parser.error("--orbit must be above 70,000 m, where space begins at Kerbin")
    if not 0 < args.deorbit_periapsis < 70_000:
        parser.error("--deorbit-periapsis must be inside the atmosphere (0 to 70,000 m)")
    if not 0 < args.turn_shape <= 2:
        parser.error("--turn-shape must be between 0 and 2")
    if args.suborbital_only and args.orbit_test:
        parser.error("--orbit-test and --suborbital-only exclude each other")
    if not 0.0 <= args.min_throttle <= 1.0:
        parser.error("--min-throttle must be between 0 and 1")
    if args.seats is not None and args.seats < 1:
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
    waiting = f", {summary.waiting} waiting for a later flight" if summary.waiting else ""
    orbit = ""
    if summary.mission is Mission.ORBITAL:
        where = "in orbit" if report.reached_orbit else "above the atmosphere"
        left = ("" if report.delta_v_orbit is None
                else f", {report.delta_v_orbit:,.0f} m/s left {where}")
        orbit = f" (orbit {'reached' if report.reached_orbit else 'NOT reached'}{left})"
    return (
        f"Flight {summary.number} ({summary.pilot}, {summary.mission.label}): "
        f"{report.situation} after {report.max_altitude:,.0f} m{orbit}; hottest part "
        f"{report.ascent_heat:.0%} climbing, {report.reentry_heat:.0%} coming down; "
        f"{summary.delivered}/{summary.tourists} tourist(s) "
        f"delivered, {summary.completed}/{len(summary.contracts)} contract(s) completed"
        f"{waiting}. Launch -{summary.launch_cost:,.0f}, contracts "
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
        use_rcs=not args.no_rcs,
        max_heat_ratio=args.max_heat / 100.0,
        orbit_delta_v=args.orbit_delta_v,
        orbit_apoapsis=args.orbit,
        orbit_turn_shape=args.turn_shape,
        turn_after_first_stage=args.turn_after_first_stage,
        deorbit_periapsis=args.deorbit_periapsis,
    )
    director = TourismFlightDirector(game, profile)
    options = CampaignOptions(
        craft=args.craft,
        target_funds=args.funds_target if args.funds_target is not None else math.inf,
        target_science=args.science_target,
        seats=args.seats,
        pilots=tuple(args.pilot),
        max_flights=args.max_flights,
        max_mission=Mission.SUBORBITAL if args.suborbital_only else Mission.ORBITAL,
        orbit_test=args.orbit_test,
        decline_types=(PART_TEST_TYPE,) if args.decline_part_tests else (),
        offer_wait=args.offer_wait,
        max_declines=args.max_declines,
        vessel_prefix=None if args.keep_craft_name else args.vessel_prefix,
        alarm_margin=args.alarm_margin * 60.0 if args.alarm_margin > 0 else None,
    )
    campaign = TourismCampaign(game, director.fly, recovery, options, person_name=person_names(),
                               designs=JsonVesselDesigns(args.designs))
    try:
        if args.dry_run:
            return _dry_run(game, campaign, options)
        def stop_after_this_flight() -> None:
            logger.warning("Ctrl+C: no more contracts; stopping once the flight under way (if "
                           "any) has landed and been recovered. Press Ctrl+C again to stop now "
                           "and leave the vessel as it is.")
            campaign.request_stop()

        with TwoStepInterrupt(stop_after_this_flight):
            result = campaign.run(on_flight=lambda s: logger.info(format_summary(s)))
        if result.alarm is not None:
            logger.warning("Game alarm \"%s\" %s: not launching another flight",
                           result.alarm.title, _when(result.alarm.seconds_away))
        # %-style logging has no thousands separator: format it here.
        logger.info("Stopped: %s. %d flight(s), funds %s -> %s (%s), science %s -> %s (%s)",
                    result.reason.value, len(result.flights),
                    f"{result.start_funds:,.0f}", f"{result.end_funds:,.0f}",
                    f"{result.end_funds - result.start_funds:+,.0f}",
                    f"{result.start_science:,.1f}", f"{result.end_science:,.1f}",
                    f"{result.end_science - result.start_science:+,.1f}")
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


def _when(seconds_away: float) -> str:
    """Say when an alarm goes off, in game time.

    Args:
        seconds_away: Game seconds until it goes off (negative: gone off).

    Returns:
        Text such as ``in 23 min`` or ``went off 5 min ago``.
    """
    minutes = abs(seconds_away) / 60.0
    if seconds_away < 0:
        return f"went off {minutes:,.0f} min ago and is still in the alarm clock"
    return f"goes off in {minutes:,.0f} min (game time)"


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
    target = (f"target {options.target_funds:,.0f}" if math.isfinite(options.target_funds)
              else "no target")
    print(f"Funds: {funds:,.0f} ({target})")
    science = game.science()
    target = (f"target {options.target_science:,.1f}" if options.target_science is not None
              else "no target")
    print(f"Science: {science:,.1f} ({target})")
    contracts = game.read_tourism_contracts()
    print(f"Tourism contracts: {len(contracts)}")
    for contract in contracts:
        tourists = ", ".join(t.kerbal or f"? ({t.itinerary})" for t in contract.tourists)
        mission = contract_mission(contract, options.body)
        kind = mission.label if mission is not None else "cannot fly"
        print(f"  [{contract.status.value}, {kind}] {contract.title} - {tourists} - "
              f"{contract.reward:,.0f} funds")
        for tourist in contract.tourists:
            done = " (delivered)" if tourist.completed else ""
            print(f"      {tourist.itinerary}{done}: {'; '.join(tourist.destinations)}")
    alarm = campaign.alarm_in_the_way()
    if alarm is not None:
        print(f"Game alarm \"{alarm.title}\" {_when(alarm.seconds_away)}: the campaign would "
              "stop before launching")
    plan = campaign.next_plan()
    if plan.is_empty:
        print("Next flight: nothing to fly.")
        return 0
    pilot = choose_pilot(game.roster(), 0, options.pilots)
    seats_note = "" if options.seats is not None else " (assumed: give --seats if it differs)"
    mission = Mission.ORBITAL if options.orbit_test else plan.mission
    print(f"Next flight ({mission.label}{', orbit test' if options.orbit_test else ''}): "
          f"pilot {pilot or '(none available!)'}, "
          f"{plan.seats_used}/{campaign.seats} seat(s){seats_note}, {plan.reward:,.0f} funds")
    if plan.seats_used < campaign.seats and options.decline_types:
        unwanted = offers_to_decline(game.read_offers(), options.decline_types)
        if unwanted:
            print(f"  seats left empty: up to {len(unwanted)} unwanted offer(s) would be "
                  "declined, one at a time, to make room for tourists")
    for label, boardings in (("already accepted", plan.carry), ("to accept", plan.accept)):
        for boarding in boardings:
            names = ", ".join(t.kerbal or t.itinerary for t in boarding.tourists)
            later = f" ({boarding.waiting} more on later flights)" if boarding.waiting else ""
            print(f"  {label + ':':<18}{names}{later}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
