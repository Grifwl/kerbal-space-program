# KSP Control

Python tools to monitor and control **Kerbal Space Program** through the
[kRPC](https://github.com/krpc/krpc) mod, built with a hexagonal
(ports and adapters) architecture.

## Career economy monitor

A live web dashboard that tracks your career **funds, science and
reputation** and answers one question: *do contract rewards pay for the
vessels you launch, or are you losing money?*

- Real-time stat tiles and history charts (by real time or Kerbin game time).
- **Mission balance**: contract income minus vessel launch costs, plus
  recoveries and reverts.
- Funds breakdown by cause, cost per vessel, and a ledger of every detected
  event.
- History stored in SQLite, so it survives restarts.

### How events are detected

kRPC exposes balances but not KSP's internal transaction reasons, so the
monitor infers them by comparing consecutive readings:

| Evidence | Event |
|---|---|
| A contract appears among the active ones | Contract accepted (advance) |
| An objective of an active contract is completed (e.g. a tourist reaches a destination) | Contract objective |
| An active contract moves to completed / failed | Contract completed (plus any objectives not paid yet) / failed |
| An active contract disappears | Contract cancelled (penalty) |
| Funds drop when entering flight, or while the vessel is still on the pad | Vessel launch |
| Funds rise when leaving flight for the editor | Launch reverted (refund) |
| Funds rise when leaving flight for the space center / tracking station | Vessel recovery |
| Anything else | Other (milestones, facilities, R&D, strategies…) |

Many contracts (tourism, rescues…) pay per objective rather than on
completion, so the objectives are tracked too and counted as contract
income.

While KSP loads a scene, kRPC can briefly return empty or partial contract
lists. The monitor only trusts contract lists that are identical in two
consecutive readings, and ignores a completely empty list.

Contract amounts are exact. Vessel and "other" amounts are whatever change
the contracts do not explain, so the ledger always adds up to the real
change in your balances. Reputation from contracts is nominal: KSP scales it
non-linearly, and the difference shows up as "Other".

### Requirements

- KSP 1.12.5 with the [kRPC mod 0.8.0](https://github.com/krpc/krpc/releases/tag/v0.8.0)
  installed and its server started.
- Python 3.10+.
- The Python `krpc` client **must match the kRPC mod version**: 0.8.0 changed
  the wire protocol, so clients and servers of different release lines
  cannot talk to each other. It is pinned in `pyproject.toml`; change it
  together with the mod.

### Installation

```powershell
python -m venv .venv
.venv\Scripts\python -m pip install -e ".[dev]"
```

### Usage

1. Start KSP, load your career save and start the kRPC TCP/IP server
   (default port 50000).
2. Run the monitor, with one database per save game:

   ```powershell
   .venv\Scripts\ksp-economy-monitor --db data\my-career.sqlite
   ```

3. Open <http://127.0.0.1:8080>.

The monitor can be started before KSP: it keeps retrying and connects when
the server is available.

To try the dashboard without the game, run a simulated career:

```powershell
.venv\Scripts\ksp-economy-monitor --demo
```

Options: `--krpc-address`, `--krpc-port`, `--interval`
(seconds between readings), `--host` (`0.0.0.0` to view it from another
device on your network), `--port`, `-v`.

### Limitations

- Events are detected between two readings (every 2 s by default). If two
  things happen in the same interval, the contract amounts are still exact,
  but the rest is merged into a single event.
- Quickloads and "revert to launch" undo money. The monitor records those as
  reverted contracts or "Other", so the totals stay correct.
- Use a separate `--db` file per career. Mixing saves in one database
  produces large, meaningless jumps.

## Vessel recovery

`ksp-recover` recovers every vessel that is worth recovering: stages and
debris that landed or splashed down on Kerbin and, optionally, the vessel
you are flying.

```powershell
.venv\Scripts\ksp-recover --dry-run          # show the plan only
.venv\Scripts\ksp-recover --include-active   # end of mission: recover everything
```

In stock KSP recovering never costs funds: the game pays the value of the
parts and the resources left on board, times a factor that goes from 100% on
KSC grounds to 98% next to it, and down to 10% on the opposite side of
Kerbin. The script estimates that payout with the same formula and recovers a
vessel when:

- KSP says it is recoverable (landed or splashed on Kerbin);
- its estimated value reaches `--min-value` (default 1 fund);
- it is not of a protected type (`base`, `station`, `relay`,
  `space_object`, which are often contract targets; see `--allow-type`);
- it is not the active vessel, unless `--include-active` is given.

Vessels outside physics range (about 2.5 km from the active vessel) cannot
be inspected by kRPC, so their value is unknown. They are recovered anyway,
since recovering cannot lose money, unless `--skip-unknown` is given. The
real payout of each recovery is measured and printed.

Run it from the flight at the end of a mission: the debris is recovered
first, without leaving the flight, and the active vessel last, which ends
the flight and shows KSP's mission summary. It also works from the space
center, for pieces left behind. The script stops if the game changes scene
part way (with kRPC 0.5.x every recovery did).

The economy monitor recognizes these recoveries. Debris (`"X Debris"`,
`"Escombros de X"`) counts towards the vessel it came from.

## Part test assistant

`ksp-part-tests` helps with the "test a part" contracts ("Test X flying
over Kerbin at 2-8 km and 140-270 m/s"). Start it once per game session and
leave it running in its own terminal: it waits for the game, follows every
flight and reconnects if KSP or the kRPC server restarts.

```powershell
.venv\Scripts\ksp-part-tests                            # full assistance
.venv\Scripts\ksp-part-tests --no-throttle --no-run-test # messages only
```

While you design a craft in the VAB or the SPH, it warns (small, red, on
the side) about every pending test whose part is not on the craft: when you
enter the editor, whenever the list changes and every 30 seconds. Adding the
part is confirmed in green.

During a flight it:

- **plans the pending tests** and shows the plan on the side of the screen
  when the flight starts. Tests are ordered along the flight profile
  (launch pad, climb by altitude window, space, landing). Tests whose
  windows overlap are done at the same moment. The plan marks what needs
  another flight: a different body, a second touchdown, or a second
  staging test of the same part when only one copy is on board (staging
  uses it up). A missing part gets a small red warning on the side, only
  while on the launch pad: once flying, contracts for parts the vessel
  does not carry are left for other flights and stay silent;
- **holds the speed window with the throttle** while climbing towards a
  test with a speed range, aiming at 40% of the range (`--speed-aim`). It
  never touches the throttle when descending. Moving the throttle by hand
  gives control back to you for that test. Solid boosters cannot be
  throttled; you are warned if they push the speed over the limit;
- **announces the test altitude** with a banner when the vessel enters the
  altitude window. The banner is yellow when you will have to act;
- **acts when every condition holds**, according to the game itself (the
  contract's sub-objectives):
  - "Run Test" from the part's menu: the assistant presses it and reports
    the completion in green;
  - activate through the staging sequence: a yellow **STAGE NOW** banner
    repeats while you are in the window, and you press space;
  - tests with only a place and no altitude or speed window (on the launch
    pad, landed, splashed down, in orbit...) work the same way: the banner
    says where you are ("STAGE NOW: ... - on the launch pad");
  - "have the part" (haul) objectives: nothing to do, the completion is
    reported in green.

Only tests at Kerbin are assisted. Tests at other bodies usually need
transfers or particular orbits first, so the assistant leaves them to you
entirely, even when you are there: no throttle, no Run Test, no messages
(the editor still warns about their missing parts).

Contract objectives are only available as text, so the assistant reads the
conditions from the objective titles (`"Alt: 2,000m a 8,000m"`,
`"Vel: 140.0m/s a 270.0m/s"`, `"Volando"`...). The formats come from KSP's
code and localization and were checked on a Spanish game; English titles
are recognized too. The speed is the surface speed, as KSP uses it for
these windows in the atmosphere.

## Tourism (suborbital and orbital)

`ksp-tourism` earns funds with the tourism contracts of Kerbin (a
suborbital flight, an orbit, or both), in a loop, until the funds reach a
target or no such contract is left:

```powershell
.venv\Scripts\ksp-tourism --craft "Passatgers v1.2" --target 500000 --dry-run
.venv\Scripts\ksp-tourism --craft "Passatgers v1.2" --target 500000 --orbit-test --max-flights 1
.venv\Scripts\ksp-tourism --craft "Passatgers v1.2" --target 500000
```

Each flight:

1. **Chooses the tourists.** Only tourism contracts whose every tourist
   wants nothing but a suborbital flight or an orbit of Kerbin (a flyby of
   the Mun, say, rules the contract out). Tourists of contracts
   already accepted board first (contracts with fewer tourists left
   first, so they are completed sooner); the free seats are filled with
   the offers that seat
   the most tourists and earn the most on this flight. Several contracts
   fly together. If KSP refuses an offer (the Mission Control limit of
   active contracts), the flight goes with what was accepted.

   **Contracts bigger than the craft** are accepted too: KSP completes
   and pays each tourist's itinerary on its own, when that tourist is
   recovered, so a contract is flown over several flights. The tourists
   left behind board first on the next flight, and the contract is
   completed with its last tourist. Splitting an offer is chosen when it
   fills more seats or pays more per seat (five tourists at 19,500 each
   beat two pairs at 13,000); between equally paying choices, the one that
   fits exactly wins. `--dry-run` shows who flies now and how many wait.

   **Seats**: on the launch pad the campaign reads how many kerbals the
   craft holds and plans the next flights with that number, minus the
   pilot's seat; a bigger craft fills its new cabins from the second
   flight on. The first flight assumes 4 passenger seats, or `--seats`.
   Tourists that do not fit (a smaller craft than assumed) stay at the
   space center and fly next.

   **Suborbital and orbital tourists fly together**: an orbital flight
   leaves the atmosphere on its way up, which completes the suborbital
   destinations too. The flight is orbital as soon as one tourist on
   board wants an orbit, and suborbital otherwise; the choice of tourists
   is never bent to keep a flight suborbital. `--suborbital-only` leaves
   the orbital tourists alone, for a craft that cannot reach orbit.
2. **Launches the craft** from the launch pad with a pilot and the
   tourists. Pilots take turns (every hired pilot, or the `--pilot` ones in
   that order). The pilot is boarded first, so they sit in the first seat
   of the craft file, normally the command pod.
3. **Flies the hop**: full throttle with the autopilot, straight up for
   the first kilometre and then turning gradually to the east, down to
   `--pitch` (default 80 degrees) at 10 km (`--heading` for another
   direction), so the capsule comes down in the sea instead of on the
   space center: a near-vertical hop falls back almost where it started.
   A lower pitch lands further away. A stage
   is activated when every engine it drops has burnt out (boosters, side
   engines) or when nothing is pushing; the parachute stage is never
   activated on the way up. The engines are cut once the apoapsis passes
   `--apoapsis` (default 75 km) and relit if drag pulls it more than
   500 m below.
   **Air limit**: while the dynamic pressure (thick air and high speed
   together, what makes the air "boil" around the rocket) is above
   `--max-q` (default 20 kPa, like MechJeb's "Limit Q"), the throttle is
   lowered smoothly, down to `--min-throttle` (default 10%), and raised
   again as the air thins. The drag is also kept below the weight
   (`--max-drag`, default 1, i.e. no faster than terminal velocity), but a
   heavy rocket's terminal velocity is about 500 m/s, so that one rarely
   acts. Solid boosters cannot be throttled, so while they burn the limit
   may be passed. `--max-q 0 --max-drag 0` disables both. **Heat
   limit**: higher up the air is too thin for the dynamic pressure limit
   (20 kPa at 25 km is about 1,900 m/s), but a rocket flying fast there
   still heats up; while the hottest part is above `--max-heat` (default
   80%) of the temperature it withstands, the throttle is lowered the
   same way (`--max-heat 0` disables it).
4. **Above 70 km** the vessel is suborbital and the tourists' destination
   is done. The stages above the parachutes are jettisoned, the capsule
   turns its heat shield forward and time warp skips to the reentry.

   **An orbital flight** goes on instead (steps 3 and 4 change):
   - *Launch pad check*: the orbit is only attempted when KSP's vacuum
     delta-v reaches `--orbit-delta-v` (default 3,600 m/s: about 3,400 to
     reach a low orbit, plus the deorbit and a reserve). With less, an
     orbital ascent would spend its fuel sideways and might not even reach
     space; the vessel flies a suborbital hop instead, so the tourists
     reach space, and the campaign stops.
   - *Ascent*: vertical for the first kilometre, then a gravity turn to
     the east driven by the apoapsis: the pitch drops from 90 degrees in
     proportion to how far the apoapsis has risen, from where it was at
     the end of the vertical climb, and the rocket is horizontal when the
     apoapsis reaches `--orbit` (default 80 km), so the turn adapts to the
     rocket's thrust. `--turn-shape` below 1 (the default) tilts sooner:
     circularizing then costs less, but the rocket spends longer flying
     fast in thick, hot air. Same air and heat limits and staging as the
     hop. The engines are cut at the target apoapsis and relit if the air
     pulls it more than 500 m below before leaving the atmosphere.
   - *Circularization*: a maneuver node at the apoapsis raises the
     periapsis to the same altitude; the burn is centred on the node and
     eases off at the end. Before it, KSP's own vacuum delta-v must cover
     the circularization, the deorbit burn and 100 m/s of reserve:
     otherwise the vessel is not put into orbit (it could be stranded
     there), comes down as a suborbital flight and the campaign stops.
   - *Orbit*: the vessel stays in orbit until the game marks the tourists'
     orbit destinations as reached (after 2 minutes without it, the
     campaign stops and leaves the vessel in orbit for you).
   - *Deorbit*: a retrograde node at the apoapsis lowers the periapsis to
     `--deorbit-periapsis` (default 35 km); the stages above the
     parachutes are only jettisoned after it, and the landing is as in a
     suborbital flight. The capsule may come down far from the space
     center, which lowers the vessel's recovery value, not the tourists'
     rewards.

   The log tells how much the circularization costs and the delta-v left;
   if circularizing costs too much, try a lower `--turn-shape`.

   **Check a craft first** with `--orbit-test`: every flight goes to
   orbit, but only suborbital tourists board. Tourists recovered without
   reaching their destination do not fail their contract (seen in game):
   they stay at the space center for another flight.
5. **Lands**: the parachute stage is activated when KSP rates a parachute
   safe to open (or below 2 km anyway); the game then opens them when it
   is safe. The autopilot keeps the heat shield forward until a canopy
   really opens, then lets go, and physics warp runs under the canopies
   (`--physics-warp`, `--no-warp`).
6. **Recovers** the vessel and the pieces of it that landed (same rules as
   `ksp-recover`, but only this craft and its debris; other landed vessels
   are left alone). Recovering the tourists completes their itineraries,
   and their contracts once no tourist of them is left waiting.

The campaign stops, and asks you to look, when something does not go to
plan: a tourist whose itinerary is not completed after the recovery, a
flight that did not reach space (or orbit, when it had to), no pilot
available, or a vessel lost.
Ctrl+C stops it at any time; a vessel in flight is then yours.

You may go on EVA or look at another vessel during a flight: while the
rocket is not the active vessel the script waits and commands nothing
(every control acts on the active vessel, so it would be steering the
kerbal). It carries on when you switch back, so come back in time for
the reentry and the parachutes. A first EVA also pays a milestone; the
flight summary shows it under "recovery and other".

The craft must have a probe core or a crewed command pod, parachutes in
the last stage, and every decoupler in the stages above it. Run
`--max-flights 1` the first time to check a new craft. Do not run
`ksp-part-tests` with throttle control at the same time: both would
command the throttle.

## Architecture

```
src/ksp_control/
├── domain/              Pure model and rules (no I/O)
│   ├── model.py         Snapshots, contracts, resources, ledger events
│   ├── ledger.py        Snapshot diff -> events; ledger summary
│   ├── recovery.py      Recovery value estimate and recovery decisions
│   ├── part_tests.py    Part test requirements, flight plan, speed governor
│   └── tourism.py       Tourism contract choice and missions, pilot turns,
│                        ascent rules, orbital mechanics
├── application/         Use cases + ports
│   ├── ports.py         GameTelemetryPort, EconomyRepositoryPort, FleetPort,
│   │                    PartTestGamePort, TourismGamePort
│   ├── economy_monitor.py
│   ├── vessel_recovery.py
│   ├── part_test_assistant.py
│   ├── tourism_flight.py      Flies a suborbital hop or an orbit, and lands
│   └── tourism_campaign.py    Contracts -> launch -> flight -> recovery loop
├── adapters/
│   ├── game/            kRPC telemetry, fleet, part tests and tourism; contract text
│   │                    parsing; simulated telemetry
│   ├── persistence/     sqlite_repository.py
│   ├── polling.py       Background loop (driving adapter)
│   └── web/             Flask API + static dashboard (driving adapter)
└── entrypoints/         Composition roots (runnable scripts)
```

Only `adapters/game/krpc_connection.py` imports `krpc`. New scripts (for
example, ones that fly a vessel) add their own use cases and ports, and reuse
the same adapters where possible.

## Tests

```powershell
.venv\Scripts\python -m pytest
```
