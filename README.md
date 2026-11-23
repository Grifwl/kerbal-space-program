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
| An active contract moves to completed / failed | Contract completed / failed |
| An active contract disappears | Contract cancelled (penalty) |
| Funds drop when entering flight from the editor or space center | Vessel launch |
| Funds rise when leaving flight for the editor | Launch reverted (refund) |
| Funds rise when leaving flight for the space center / tracking station | Vessel recovery |
| Anything else | Other (milestones, facilities, R&D, strategies…) |

Contract amounts are exact. Vessel and "other" amounts are whatever change
the contracts do not explain, so the ledger always adds up to the real
change in your balances. Reputation from contracts is nominal: KSP scales it
non-linearly, and the difference shows up as "Other".

### Requirements

- KSP 1.12.x with the kRPC mod installed and its server started.
- Python 3.10+.
- The Python `krpc` client **must match the kRPC mod version**. It is pinned
  to `0.5.2` in `pyproject.toml`. If you update the mod (the latest release is
  0.8.0, which needs KSP 1.12.5 and changed the wire protocol), change the pin
  to the same version.

### Installation

```powershell
python -m venv .venv
.venv\Scripts\python -m pip install -e ".[dev]"
```

### Usage

1. Start KSP, load your career save and start the kRPC server
   (default ports 50000/50001).
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

Options: `--krpc-address`, `--rpc-port`, `--stream-port`, `--interval`
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

## Architecture

```
src/ksp_control/
├── domain/              Pure model and rules (no I/O)
│   ├── model.py         Snapshots, contracts, resources, ledger events
│   └── ledger.py        Snapshot diff -> events; ledger summary
├── application/         Use cases + ports
│   ├── ports.py         GameTelemetryPort, EconomyRepositoryPort
│   └── economy_monitor.py
├── adapters/
│   ├── game/            krpc_telemetry.py (real game), simulated_telemetry.py
│   ├── persistence/     sqlite_repository.py
│   ├── polling.py       Background loop (driving adapter)
│   └── web/             Flask API + static dashboard (driving adapter)
└── entrypoints/         Composition roots (runnable scripts)
```

Only `adapters/game/krpc_telemetry.py` imports `krpc`. New scripts (for
example, ones that fly a vessel) add their own use cases and ports, and reuse
the same adapters where possible.

## Tests

```powershell
.venv\Scripts\python -m pytest
```
