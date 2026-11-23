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

## Architecture

```
src/ksp_control/
├── domain/              Pure model and rules (no I/O)
│   ├── model.py         Snapshots, contracts, resources, ledger events
│   ├── ledger.py        Snapshot diff -> events; ledger summary
│   └── recovery.py      Recovery value estimate and recovery decisions
├── application/         Use cases + ports
│   ├── ports.py         GameTelemetryPort, EconomyRepositoryPort, FleetPort
│   ├── economy_monitor.py
│   └── vessel_recovery.py
├── adapters/
│   ├── game/            kRPC telemetry and fleet, simulated telemetry
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
