"""Tools to monitor and control Kerbal Space Program through kRPC.

The package follows a hexagonal (ports and adapters) architecture:

* ``domain``      - pure business model and rules, no I/O at all.
* ``application`` - use cases plus the *ports* (abstract interfaces) they need.
* ``adapters``    - concrete implementations of the ports (kRPC, SQLite,
  a simulator) and the driving adapters that trigger use cases (web UI,
  polling loop).
* ``entrypoints`` - composition roots that wire adapters into use cases and
  expose runnable scripts.
"""

__version__ = "0.1.0"
