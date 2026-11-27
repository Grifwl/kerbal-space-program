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
device on your network), `--port`, `--designs`, `-v`.

The cost per vessel is grouped by **craft design**. KSP forgets which
craft a vessel was built from once it is renamed, so the scripts that
rename what they launch (`ksp-tourism` names each flight "Cruise John
Doe") record it in `data/vessel-designs.json` (`--designs`), e.g.
`{"Cruise John Doe": "Passatgers v1.3"}`. The monitor reads it whenever it
builds the table, so every cruise and its debris add up under
"Passatgers v1.3", including flights recorded before the file had them.
Vessels missing from the file are grouped by their own name.

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
suborbital flight, an orbit, or both), in a loop, until a limit is
reached or no such contract is left:

```powershell
.venv\Scripts\ksp-tourism --craft "Passatgers v1.2" --funds-target 500000 --dry-run
.venv\Scripts\ksp-tourism --craft "Passatgers v1.2" --orbit-test --max-flights 1
.venv\Scripts\ksp-tourism --craft "Passatgers v1.2" --funds-target 500000
.venv\Scripts\ksp-tourism --craft "Passatgers v1.2" --science-target 160
```

The limits, checked before each flight (at least one is needed; the
first one reached stops the campaign):

- `--funds-target`: the funds reach this amount.
- `--science-target`: the science points reach this amount, e.g. the cost
  of the next technology: stop, research it, and run the campaign again.
  Tourism contracts pay no science themselves; it comes indirectly from
  whatever turns their reputation into science (an Administration
  strategy, for instance). In one career that made 13-17 points per
  orbital flight.
- `--max-flights`: this many flights have been flown.

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

   **Making room for tourists**: with `--decline-part-tests`, while the
   next flight would leave seats empty, the "test a part" offers are
   declined one at a time: after each one the campaign waits up to
   `--offer-wait` seconds (default 60) for KSP to offer something new and
   plans again. It stops declining when the seats are full, when no
   unwanted offer is left (new ones included), or after `--max-declines`
   (default 10). It then flies if at least one tourist boards, and stops
   otherwise. Declining costs reputation, which the extra tourists more
   than pay back; nothing is declined while the seats can be filled.

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
     space. If a tourist on board still has a suborbital destination to
     reach, the vessel flies a suborbital hop instead; otherwise (all of
     them already went suborbital and only miss the orbit) it does not
     take off: it is recovered from the pad, which refunds the launch. In
     both cases the campaign stops.
   - *Ascent*: vertical for the first kilometre, then a gravity turn to
     the east driven by the apoapsis: the pitch drops from 90 degrees in
     proportion to how far the apoapsis has risen, from where it was at
     the end of the vertical climb, and the rocket is horizontal when the
     apoapsis reaches `--orbit` (default 80 km), so the turn adapts to the
     rocket's thrust. `--turn-shape` below 1 (the default) tilts sooner:
     circularizing then costs less, but the rocket spends longer flying
     fast in thick, hot air. `--turn-after-first-stage` keeps the rocket
     vertical until the first stage (the boosters) has been dropped, for a
     long rocket that loses control when they separate while it is
     tilted; climbing straight up for longer costs some delta-v. Same air
     and heat limits and staging as the
     hop. The engines are cut at the target apoapsis and relit if the air
     pulls it more than 500 m below before leaving the atmosphere.
   - *Circularization*: a maneuver node at the apoapsis raises the
     periapsis to the same altitude; the burn is centred on the node and
     eases off at the end. Before it, KSP's own vacuum delta-v must cover
     the circularization, the deorbit burn and 100 m/s of reserve:
     otherwise the vessel is not put into orbit (it could be stranded
     there), comes down as a suborbital flight and the campaign stops.
     If the periapsis is still in the atmosphere after the burn (a long
     burn loses some push past the apoapsis), a prograde top-up with the
     delta-v beyond the deorbit burn and the reserve lifts it out. If no
     orbit is reached and the periapsis only grazes the atmosphere, it is
     lowered to the deorbit periapsis before the engines are jettisoned,
     so the capsule does not skim the atmosphere for orbits on end.
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
   **RCS**: the RCS is switched on from the launch until the parachutes
   open, so the autopilot can also steer with RCS thrusters (a vessel
   launched by kRPC starts with it off); `--no-rcs` leaves it off. Keep
   an eye on the monopropellant: the autopilot fires the thrusters on
   every correction.

   The flight summary shows how hot the hottest part got while climbing
   and while coming down, as a percentage of the temperature it
   withstands: the margin left before `--max-heat` acts (on the way up)
   or before a part could burn up (on the way down).
5. **Lands**: the parachute stage is activated when KSP rates a parachute
   safe to open (or below 2 km anyway); the game then opens them when it
   is safe. The autopilot keeps the heat shield forward until a canopy
   really opens, then lets go, and physics warp runs under the canopies
   (`--physics-warp`, `--no-warp`). Landing legs and retractable wheels,
   if the craft has any, are lowered under the canopies once slower than
   100 m/s: away from the reentry heat and long before the ground.
6. **Recovers** the vessel and the pieces of it that landed (same rules as
   `ksp-recover`, but only this craft and its debris; other landed vessels
   are left alone). Recovering the tourists completes their itineraries,
   and their contracts once no tourist of them is left waiting.

The campaign stops, and asks you to look, when something does not go to
plan: a tourist whose itinerary is not completed after the recovery, a
flight that did not reach space (or orbit, when it had to), no pilot
available, or a vessel lost.

**Ctrl+C** asks the campaign to stop without dropping the vessel: the
flight under way, if any, goes on until it has landed, been recovered and
its tourists paid; then the campaign stops, with no new contract accepted
and nothing launched (pressed while offers are being declined, it stops
before accepting anything). A **second Ctrl+C** stops it at once: the
autopilot lets go, the throttle goes to zero and the vessel is yours, as
it is. A Ctrl+C pressed during a time warp takes effect when the warp
ends.

It never launches while the active vessel is flying. A vessel waiting on the launch
pad with its crew (a flight reverted to launch) is recovered before the
next launch, which refunds it and brings the crew back: launching on top
of it lets KSP recover the crew only half way (it cannot remove the active
vessel), and a pilot ended up missing that way.

You may go on EVA or look at another vessel during a flight: while the
rocket is not the active vessel the script waits and commands nothing
(every control acts on the active vessel, so it would be steering the
kerbal). It carries on when you switch back, so come back in time for
the reentry and the parachutes. A first EVA also pays a milestone; the
flight summary shows it under "recovery and other".

**Game alarms**: before each flight the campaign reads the game's alarm
clock and stops, without accepting or launching anything, when an alarm
goes off within `--alarm-margin` minutes of game time (default 60; `0`
ignores alarms). An orbital flight takes about 45 minutes of game time
from launch to recovery (16 real minutes), a suborbital hop about 8, and
during a flight nobody would be at the controls to answer the alarm.
Alarms that have gone off and are still in the alarm clock stop it too:
delete them once dealt with. This includes the alarms of
`ksp-constellation`, so the campaign makes way for a relay probe that
has to brake. `--dry-run` shows the alarm that would stop it.

Every launched vessel is renamed on the pad `Cruise <invented name>`
("Cruise John Doe"), so its debris carry its name too; `--vessel-prefix`
changes the first word and `--keep-craft-name` keeps the craft's name.
The craft of each renamed vessel is recorded in `--designs` (default
`data/vessel-designs.json`), so the economy monitor still adds up every
flight of the same craft (see "Career economy monitor").

The craft must have a probe core or a crewed command pod, parachutes in
the last stage, and every decoupler in the stages above it. Run
`--max-flights 1` the first time to check a new craft. Do not run
`ksp-part-tests` with throttle control at the same time: both would
command the throttle.

## Relay constellation

`ksp-constellation` spreads relay probes evenly around a body (any planet
or moon), all on the same circular orbit, so that a vessel behind the body
keeps its link with Kerbin. You design the carrier with the probes and fly
it into any orbit around the body; the script does the rest:

```powershell
.venv\Scripts\ksp-constellation --body Mun --body-label Muna --dry-run
.venv\Scripts\ksp-constellation --body Mun --body-label Muna
```

It uses a *resonant orbit*, as in
[Eric Meyer's resonant orbit calculator](https://meyerweb.com/eric/ksp/resonant-orbits/):
the carrier flies an orbit whose periapsis is at the altitude of the
constellation and whose period is `(N + 1) / N` times the final one. A
probe that brakes into the circular orbit at the periapsis completes a
lap while the others come back to the periapsis `1 / N` of a lap later,
so the next one lands one slot behind it, and so on.

**The plan.** The number of probes `N` is read from the carrier. The
altitude is the lowest one where each probe sees its two neighbours over
the horizon (`r cos(pi / N) = R`), rounded up to the next 10 km
(`--round`), and never below the atmosphere or the highest mountain. For
6 probes at the Mun: lowest 30.9 km, so 40 km; resonant orbit 40 x 92 km;
24.8 m/s of braking per probe. `--altitude` chooses another one; the plan
warns when the margin before neighbours lose sight is small (6 probes at
Minmus at 10 km keep only 2 degrees: 20 km is safer). The constellation
stays in the plane the carrier arrives in (no plane change).

**The steps.**

1. **Insertion**: at most two burns at the apsides put the carrier on the
   resonant orbit; its period is then trimmed to a tenth of a second.
2. **Release** near the apoapsis, pointing along the orbit normal, one
   probe after another. Each probe gets its probe core as control point,
   the relay type, its antennas and solar panels deployed, and a name:
   `CA-40-Muna-01 John Doe` (Communication Array, altitude in km, body,
   position on the ring, an invented person's name). The carrier is
   renamed `CA-40-Muna Carrier`. Keep the decouplers' ejection force low.
3. **Line-up**: each probe trims its period a little below the resonant
   one (more for higher numbers, `--spacing` metres per lap), which also
   corrects the push of the decouplers: the group stretches forward, ahead
   of the carrier.
4. **Carrier disposal**: the carrier brakes until its periapsis is under
   the surface (or deep in the atmosphere), 30 degrees off retrograde
   towards the side with fewer probes. A vessel that brakes falls back
   first; the sideways component takes it out of the plane of the group.
5. **One probe per periapsis passage**: the *rearmost* probe of the group
   brakes into the circular orbit, so no other probe is behind it to catch
   up (braking makes a vessel fall back). Its period is trimmed to the
   final one within `--period-tolerance` (0.05 s): probes with different
   periods drift apart forever. It is then renamed with its position on
   the ring (it may swap numbers with a waiting probe). After the first
   one, each waiting probe gets a period within seconds of the resonant
   one, chosen so it reaches the periapsis exactly when its slot comes
   round.

Before each braking the script predicts where the probe would land: a
missed periapsis leaves its slot empty, the next passages fill the next
slots, and the last probe waits the laps it takes for the empty one.

**Waiting.** When the next maneuver is less than `--alarm-after` minutes
of game time away (default 60) and you are on one of the constellation's
vessels, time warp gets there. Otherwise a game alarm is set
`--alarm-lead` minutes before (default 5) and the script waits while you
play. When the time comes:

- on a vessel of the constellation: it goes ahead;
- in the space center or the tracking station: it takes command of the
  vessel it needs (loading the flight scene);
- on another vessel, in a building or an editor: it warns in red, on
  screen and in the log, every 30 s, and commands nothing.

With `--no-wait` it stops once the alarm is set. Every decision is taken
from the orbits in the game, so it can be stopped at any time (Ctrl+C,
closing the game) and run again another day with the same command: it
finds the deployment in progress by the probe names.

At the end it reports each probe's orbit, period error, gap to the next
one and distance from its ideal slot, how long the ring stays in sight
with those period errors, and warns about gaps out of sight or antennas
too weak for the distance between neighbours. `--report` measures an
existing ring.

**The craft.** Each probe needs a probe core (its only one), an engine
pointing the same way as the core, fuel for its braking plus a few m/s,
a relay antenna, and power (batteries and solar panels: the burns may
happen in the shadow). Hold each probe with a decoupler or a docking port;
stacks work (the outer probe goes first). The carrier's root part should
be its own command part, and it needs fuel for the insertion and for
coming down. Tested in a simulated game (6 probes at the Mun: gaps
60 ± 0.4 degrees, periods within 0.015 s); not yet flown in KSP.

## Architecture

```
src/ksp_control/
├── domain/              Pure model and rules (no I/O)
│   ├── model.py         Snapshots, contracts, resources, ledger events
│   ├── ledger.py        Snapshot diff -> events; ledger summary
│   ├── recovery.py      Recovery value estimate and recovery decisions
│   ├── part_tests.py    Part test requirements, flight plan, speed governor
│   ├── tourism.py       Tourism contract choice and missions, pilot turns,
│   │                    ascent rules, orbital mechanics
│   └── constellation.py Relay ring plan, resonant orbit, transfers, Kepler
│                        propagation, slots and probe names
├── application/         Use cases + ports
│   ├── ports.py         GameTelemetryPort, EconomyRepositoryPort, FleetPort,
│   │                    PartTestGamePort, TourismGamePort, ConstellationGamePort,
│   │                    VesselDesignsPort
│   ├── economy_monitor.py
│   ├── vessel_recovery.py
│   ├── part_test_assistant.py
│   ├── tourism_flight.py      Flies a suborbital hop or an orbit, and lands
│   ├── tourism_campaign.py    Contracts -> launch -> flight -> recovery loop
│   └── relay_constellation.py Carrier -> resonant orbit -> release -> one probe per lap
├── adapters/
│   ├── game/            kRPC telemetry, fleet, part tests, tourism and constellation;
│   │                    contract text parsing; simulated telemetry
│   ├── names.py         Invented people's names (Faker) for vessels
│   ├── persistence/     sqlite_repository.py, json_vessel_designs.py
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
