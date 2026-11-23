"""Adapters: concrete implementations of the application ports.

* ``game``        - driven adapters that read the game (kRPC, simulator).
* ``persistence`` - driven adapters that store history (SQLite).
* ``web``         - driving adapter: HTTP API and dashboard.
* ``polling``     - driving adapter: background loop that triggers polls.
"""
