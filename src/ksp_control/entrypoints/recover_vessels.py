"""Recover every vessel that is worth recovering.

Run it at the end of a mission, from the flight (to also recover the active
vessel and get the mission summary) or from the space center, to cash in the
stages and pieces that landed or splashed down on Kerbin.

Examples:
    See what would be recovered, without touching the game::

        ksp-recover --dry-run

    Recover everything worth at least 500 funds, the active vessel included::

        ksp-recover --min-value 500 --include-active
"""

from __future__ import annotations

import argparse
import logging
import sys

from ksp_control.adapters.game.krpc_connection import KrpcEndpoint
from ksp_control.adapters.game.krpc_fleet import KrpcFleet
from ksp_control.application.ports import GameUnavailableError
from ksp_control.application.vessel_recovery import RecoveryOutcome, VesselRecoveryService
from ksp_control.domain.recovery import (
    DEFAULT_PROTECTED_TYPES,
    RecoveryDecision,
    RecoveryPolicy,
)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    """Parse the command line.

    Args:
        argv: Arguments without the program name; ``None`` uses ``sys.argv``.

    Returns:
        The parsed options.
    """
    parser = argparse.ArgumentParser(
        description="Recover the vessels whose recovery pays at least a minimum amount."
    )
    parser.add_argument("--dry-run", action="store_true",
                        help="Only show the plan; recover nothing.")
    parser.add_argument("--min-value", type=float, default=1.0,
                        help="Minimum estimated payout, in funds (default: 1).")
    parser.add_argument("--include-active", action="store_true",
                        help="Also recover the vessel being flown (ends the flight).")
    parser.add_argument("--skip-unknown", action="store_true",
                        help="Do not recover vessels whose value cannot be estimated "
                             "(out of physics range).")
    parser.add_argument("--allow-type", action="append", default=[], metavar="TYPE",
                        help="Allow a protected vessel type "
                             f"({', '.join(sorted(DEFAULT_PROTECTED_TYPES))}). Repeatable.")
    parser.add_argument("--krpc-address", default="127.0.0.1", help="kRPC server address.")
    parser.add_argument("--krpc-port", type=int, default=50000,
                        help="Port of the kRPC TCP/IP server.")
    parser.add_argument("-v", "--verbose", action="store_true", help="Debug logging.")
    return parser.parse_args(argv)


def format_decision(decision: RecoveryDecision) -> str:
    """Render one line of the plan.

    Args:
        decision: The decision.

    Returns:
        A fixed-width text line.
    """
    v = decision.vessel
    value = "?" if decision.estimated_value is None else f"{decision.estimated_value:,.0f}"
    distance = "" if decision.distance_km is None else f"{decision.distance_km:,.1f} km"
    mark = "RECOVER" if decision.recover else "skip"
    active = " (active)" if v.is_active else ""
    return (
        f"  {mark:<8}{v.name + active:<52.52} {v.vessel_type:<8} {v.situation:<12}"
        f" {distance:>10} {value:>10}  {decision.reason}"
    )


def format_outcome(outcome: RecoveryOutcome) -> str:
    """Render the result of one recovery.

    Args:
        outcome: The result.

    Returns:
        A text line.
    """
    payout = "" if outcome.payout is None else f" (+{outcome.payout:,.0f} funds)"
    return f"  {outcome.decision.vessel.name}: {outcome.message}{payout}"


def main(argv: list[str] | None = None) -> int:
    """Plan and perform the recoveries.

    Args:
        argv: Arguments without the program name; ``None`` uses ``sys.argv``.

    Returns:
        Process exit code (0 on success).
    """
    args = parse_args(argv)
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.WARNING,
                        format="%(levelname)s %(name)s: %(message)s")
    policy = RecoveryPolicy(
        min_value=args.min_value,
        include_active=args.include_active,
        recover_unknown=not args.skip_unknown,
        protected_types=DEFAULT_PROTECTED_TYPES - {t.lower() for t in args.allow_type},
    )
    endpoint = KrpcEndpoint(args.krpc_address, args.krpc_port)
    service = VesselRecoveryService(KrpcFleet(endpoint))
    try:
        decisions = service.plan(policy)
        print(f"  {'':<8}{'Vessel':<52} {'Type':<8} {'Situation':<12}"
              f" {'To KSC':>10} {'Est. value':>10}  Decision")
        for decision in decisions:
            print(format_decision(decision))

        chosen = [d for d in decisions if d.recover]
        estimate = sum(d.estimated_value or 0.0 for d in chosen)
        unknown = sum(1 for d in chosen if d.estimated_value is None)
        extra = f" plus {unknown} of unknown value" if unknown else ""
        print(f"\n{len(chosen)} vessel(s) to recover, estimated {estimate:,.0f} funds{extra}.")
        if args.dry_run or not chosen:
            return 0

        print("Recovering...")
        outcomes = service.execute(decisions, on_outcome=lambda o: print(format_outcome(o)))
        received = sum(o.payout or 0.0 for o in outcomes if o.recovered)
        done = sum(1 for o in outcomes if o.recovered)
        print(f"\nRecovered {done} of {len(chosen)} vessel(s), received {received:,.0f} funds.")
        return 0 if done == len(chosen) else 1
    except GameUnavailableError as error:
        print(f"Cannot recover vessels: {error}", file=sys.stderr)
        return 2
    finally:
        service.close()


if __name__ == "__main__":
    sys.exit(main())
